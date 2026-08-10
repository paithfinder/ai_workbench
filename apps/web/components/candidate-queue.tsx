"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, createIdempotencyKey } from "@/lib/api";
import { fetchBootstrap } from "@/lib/bootstrap";
import {
  acceptCandidate,
  candidateDraftSchema,
  getCandidateReviewRequest,
  listCandidates,
  listExtractions,
  listKnowledgeDestinations,
  markCandidateNeedsVerification,
  rejectCandidate,
  updateCandidate,
  type CandidateDraft,
  type CandidateReviewResponse,
  type CandidateStatus,
  type ExtractionCandidate,
  type ExtractionRun,
  type KnowledgeDestination,
} from "@/lib/extraction";
import { retryJob } from "@/lib/sources";
import { PageHeader } from "./page-states";

const statusLabels: Record<CandidateStatus, string> = {
  pending_review: "等待审查",
  needs_verification: "待验证",
  accepted: "已加入知识树",
  rejected: "已拒绝",
};

type ReviewAction = "edit" | "accept" | "mark_needs_verification" | "reject";
type Operation = { candidateId: string; action: ReviewAction; fingerprint: string; key: string };
type FieldErrors = Partial<Record<keyof CandidateDraft | "tagsText", string>>;

function errorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "候选队列请求失败";
}

function isUncertainRequestFailure(error: unknown) {
  return error instanceof ApiError && (error.status === 0 || error.status >= 500);
}

function candidateToDraft(candidate: ExtractionCandidate): CandidateDraft {
  return {
    title: candidate.title,
    body: candidate.body,
    tags: candidate.tags,
    suggested_destination_id: candidate.suggested_destination_id,
    conditions: candidate.conditions,
    exceptions: candidate.exceptions,
  };
}

function draftFingerprint(action: ReviewAction, candidate: ExtractionCandidate, draft: CandidateDraft) {
  return JSON.stringify({ action, candidateId: candidate.id, expectedVersion: candidate.version, ...draft });
}

function destinationLabel(destination: KnowledgeDestination) {
  return [...destination.path, destination.name].filter(Boolean).join(" / ");
}

function applyReviewOutcome(candidate: ExtractionCandidate, outcome: CandidateReviewResponse, draft: CandidateDraft) {
  return {
    ...candidate,
    ...draft,
    status: outcome.candidate_status,
    version: outcome.candidate_version,
  };
}

function EvidenceDocument({ candidate }: { candidate: ExtractionCandidate }) {
  const [activeIndex, setActiveIndex] = useState(0);
  const evidenceRefs = useRef<Array<HTMLElement | null>>([]);

  function navigate(nextIndex: number) {
    const bounded = Math.max(0, Math.min(nextIndex, candidate.evidence.length - 1));
    setActiveIndex(bounded);
    evidenceRefs.current[bounded]?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  return (
    <article className="candidate-evidence" aria-label="只读来源证据">
      <header className="candidate-column-head source-identity">
        <p className="state-kicker">ORIGINAL SOURCE · 只读证据</p>
        <h2>{candidate.source_title}</h2>
        <p>整段高亮用于核对上下文；所有文字均按服务端证据原样呈现。</p>
        <div className="evidence-navigation" aria-label="证据段落导航">
          <button disabled={activeIndex === 0} onClick={() => navigate(activeIndex - 1)} type="button">上一条</button>
          <span aria-live="polite">{activeIndex + 1} / {candidate.evidence.length}</span>
          <button disabled={activeIndex === candidate.evidence.length - 1} onClick={() => navigate(activeIndex + 1)} type="button">下一条</button>
        </div>
      </header>
      <div className="evidence-paper">
        {candidate.evidence.map((evidence, index) => (
          <section
            aria-current={index === activeIndex ? "true" : undefined}
            className={`evidence-block ${index === activeIndex ? "is-active" : ""}`}
            key={evidence.section_id}
            onClick={() => setActiveIndex(index)}
            ref={(element) => { evidenceRefs.current[index] = element; }}
          >
            <button aria-label={`定位到证据 ${index + 1}`} className="evidence-index" onClick={() => navigate(index)} type="button">
              {String(index + 1).padStart(2, "0")}
            </button>
            <div>
              <p className="evidence-locator">
                {evidence.page_number ? `第 ${evidence.page_number} 页 · ` : ""}
                {evidence.heading_path.join(" / ") || evidence.title || "正文"}
              </p>
              <h3>{evidence.title ?? evidence.heading_path.at(-1) ?? "来源分段"}</h3>
              <p className="evidence-copy">{evidence.text}</p>
              <details className="candidate-trace">
                <summary>查看稳定证据标识</summary>
                <code>{evidence.section_id}</code>
                <code>sha256:{evidence.quote_hash}</code>
              </details>
            </div>
          </section>
        ))}
      </div>
    </article>
  );
}

function CandidateEditor({
  candidate,
  destinations,
  destinationsError,
  run,
  spaceId,
  onCandidateChange,
  onDirtyChange,
}: {
  candidate: ExtractionCandidate;
  destinations: KnowledgeDestination[];
  destinationsError: boolean;
  run?: ExtractionRun;
  spaceId: string;
  onCandidateChange: (candidate: ExtractionCandidate) => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const initialDraft = useMemo(() => candidateToDraft(candidate), [candidate]);
  const [draft, setDraft] = useState(initialDraft);
  const [tagsText, setTagsText] = useState(candidate.tags.join(", "));
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [notice, setNotice] = useState<{ kind: "error" | "success" | "uncertain"; message: string } | null>(null);
  const [busyAction, setBusyAction] = useState<ReviewAction | null>(null);
  const operationRef = useRef<Operation | null>(null);
  const isTerminal = candidate.status === "accepted" || candidate.status === "rejected";
  const isDirty = JSON.stringify(draft) !== JSON.stringify(initialDraft);

  useEffect(() => onDirtyChange(isDirty), [isDirty, onDirtyChange]);
  useEffect(() => {
    if (!isDirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [isDirty]);

  function applyCandidate(updated: ExtractionCandidate) {
    setDraft(candidateToDraft(updated));
    setTagsText(updated.tags.join(", "));
    setFieldErrors({});
    onCandidateChange(updated);
  }

  function update<K extends keyof CandidateDraft>(key: K, value: CandidateDraft[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
    setFieldErrors((current) => ({ ...current, [key]: undefined }));
    setNotice(null);
  }

  function updateTags(value: string) {
    setTagsText(value);
    const tags = value.split(/[,，\n]/).map((tag) => tag.trim()).filter(Boolean);
    update("tags", Array.from(new Set(tags)));
    setFieldErrors((current) => ({ ...current, tagsText: undefined }));
  }

  function validate() {
    const parsed = candidateDraftSchema.safeParse(draft);
    const errors: FieldErrors = {};
    if (!draft.title.trim()) errors.title = "标题不能为空";
    else if (draft.title.trim().length > 500) errors.title = "标题不能超过 500 个字符";
    if (!draft.body.trim()) errors.body = "正文不能为空";
    else if (draft.body.trim().length > 20_000) errors.body = "正文不能超过 20,000 个字符";
    if (draft.tags.length > 20) errors.tagsText = "标签不能超过 20 个";
    else if (draft.tags.some((tag) => tag.length > 64)) errors.tagsText = "每个标签不能超过 64 个字符";
    if (!parsed.success && Object.keys(errors).length === 0) errors.title = "请检查候选内容与目标位置";
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  }

  function getOperation(action: ReviewAction) {
    const fingerprint = draftFingerprint(action, candidate, draft);
    const existing = operationRef.current;
    if (existing?.candidateId === candidate.id && existing.action === action && existing.fingerprint === fingerprint) return existing;
    const operation = { candidateId: candidate.id, action, fingerprint, key: createIdempotencyKey() };
    operationRef.current = operation;
    return operation;
  }

  async function reconcile(operation: Operation) {
    try {
      const request = await getCandidateReviewRequest(spaceId, candidate.id, operation.key);
      operationRef.current = null;
      applyCandidate(applyReviewOutcome(candidate, request.result, draft));
      setNotice({ kind: "success", message: "服务端已完成操作，页面已核对并同步结果。" });
      return true;
    } catch {
      return false;
    }
  }

  async function submit(action: ReviewAction) {
    if (busyAction || isTerminal || !validate()) return;
    setBusyAction(action);
    setNotice(null);
    const operation = getOperation(action);
    const payload = { ...draft, title: draft.title.trim(), body: draft.body.trim(), expected_version: candidate.version };
    try {
      let outcome: CandidateReviewResponse;
      if (action === "edit") outcome = await updateCandidate(spaceId, candidate.id, payload, operation.key);
      else if (action === "accept") outcome = await acceptCandidate(spaceId, candidate.id, payload, operation.key);
      else if (action === "mark_needs_verification") outcome = await markCandidateNeedsVerification(spaceId, candidate.id, payload, operation.key);
      else outcome = await rejectCandidate(spaceId, candidate.id, payload, operation.key);
      operationRef.current = null;
      applyCandidate(applyReviewOutcome(candidate, outcome, draft));
      if (action === "accept") {
        setNotice({ kind: "success", message: `已加入知识树，并创建 ${outcome.evidence_ids.length} 条证据关系与复习卡。` });
      } else {
        const messages: Record<Exclude<ReviewAction, "accept">, string> = {
          edit: "草稿已保存。",
          mark_needs_verification: "候选已标记为待验证。",
          reject: "候选已拒绝。",
        };
        setNotice({ kind: "success", message: messages[action] });
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        operationRef.current = null;
        setNotice({ kind: "error", message: "此候选已被其他操作更新。你的草稿仍保留；请复制需要的内容，再重新加载队列取得最新版。" });
      } else if (isUncertainRequestFailure(error)) {
        const settled = await reconcile(operation);
        if (!settled) setNotice({ kind: "uncertain", message: "无法确认服务端是否已完成操作。请用同一按钮重试；请求标识会保持不变。" });
      } else {
        operationRef.current = null;
        setNotice({ kind: "error", message: errorMessage(error) });
      }
    } finally {
      setBusyAction(null);
    }
  }

  function resetDraft() {
    setDraft(initialDraft);
    setTagsText(initialDraft.tags.join(", "));
    setFieldErrors({});
    setNotice(null);
    operationRef.current = null;
  }

  return (
    <aside className="candidate-detail" aria-label="AI 候选编辑与决策">
      <header className="candidate-column-head ai-identity">
        <p className="state-kicker">REVIEW DRAFT · 人工定稿</p>
        <h2>编辑与决策</h2>
        <p>右侧为可编辑草稿；只有“加入知识树”会创建正式知识与证据关系。</p>
      </header>
      <div className="candidate-detail-body">
        <div className="candidate-status-row">
          <span className={`candidate-status status-${candidate.status}`}>{statusLabels[candidate.status]}</span>
          <span>{candidate.atomicity === "atomic" ? "原子候选" : "建议拆分"}</span>
          <span>置信度 {(candidate.confidence * 100).toFixed(0)}%</span>
          <span>版本 {candidate.version}</span>
          {isDirty ? <span className="draft-state">未保存草稿</span> : null}
        </div>

        <div className="candidate-form">
          <label htmlFor={`candidate-title-${candidate.id}`}>最终标题 <span aria-hidden="true">*</span></label>
          <input
            aria-describedby={fieldErrors.title ? `candidate-title-error-${candidate.id}` : undefined}
            aria-invalid={Boolean(fieldErrors.title)}
            disabled={isTerminal || Boolean(busyAction)}
            id={`candidate-title-${candidate.id}`}
            maxLength={500}
            onChange={(event) => update("title", event.target.value)}
            value={draft.title}
          />
          {fieldErrors.title ? <p className="field-error" id={`candidate-title-error-${candidate.id}`} role="alert">{fieldErrors.title}</p> : null}

          <label htmlFor={`candidate-body-${candidate.id}`}>最终正文 <span aria-hidden="true">*</span></label>
          <textarea
            aria-describedby={fieldErrors.body ? `candidate-body-error-${candidate.id}` : undefined}
            aria-invalid={Boolean(fieldErrors.body)}
            disabled={isTerminal || Boolean(busyAction)}
            id={`candidate-body-${candidate.id}`}
            maxLength={20_000}
            onChange={(event) => update("body", event.target.value)}
            rows={9}
            value={draft.body}
          />
          <div className="field-meta"><span>{fieldErrors.body ?? "保留一个可独立理解的知识单元"}</span><span>{draft.body.length} / 20,000</span></div>

          <label htmlFor={`candidate-tags-${candidate.id}`}>标签</label>
          <input
            aria-describedby={`candidate-tags-hint-${candidate.id}`}
            aria-invalid={Boolean(fieldErrors.tagsText)}
            disabled={isTerminal || Boolean(busyAction)}
            id={`candidate-tags-${candidate.id}`}
            onChange={(event) => updateTags(event.target.value)}
            value={tagsText}
          />
          <div className="field-meta" id={`candidate-tags-hint-${candidate.id}`}><span>{fieldErrors.tagsText ?? "用逗号或换行分隔，最多 20 个"}</span><span>{draft.tags.length} / 20</span></div>

          <label htmlFor={`candidate-destination-${candidate.id}`}>知识树目标位置</label>
          <select
            disabled={isTerminal || Boolean(busyAction) || destinationsError}
            id={`candidate-destination-${candidate.id}`}
            onChange={(event) => update("suggested_destination_id", event.target.value || null)}
            value={draft.suggested_destination_id ?? ""}
          >
            <option value="">暂不指定</option>
            {destinations.map((destination) => <option key={destination.id} value={destination.id}>{destinationLabel(destination)}</option>)}
          </select>
          <p className={destinationsError ? "field-error" : "field-hint"}>{destinationsError ? "目标位置暂时无法读取；仍可保存草稿，接受前请稍后重试。" : "选择正式知识节点将归属的位置。"}</p>
        </div>

        {candidate.conditions.length ? <section><small>适用条件（提炼参考）</small><ul>{candidate.conditions.map((item) => <li key={item}>{item}</li>)}</ul></section> : null}
        {candidate.exceptions.length ? <section><small>例外（提炼参考）</small><ul>{candidate.exceptions.map((item) => <li key={item}>{item}</li>)}</ul></section> : null}
        {candidate.verification_reason ? <div className="verification-note"><strong>验证说明</strong><p>{candidate.verification_reason}</p></div> : null}

        <dl className="model-trace">
          <div><dt>Model</dt><dd>{candidate.model}</dd></div>
          <div><dt>Prompt</dt><dd>{candidate.prompt_version}</dd></div>
          <div><dt>Evidence</dt><dd>{candidate.evidence.length} 条</dd></div>
          {run ? <><div><dt>Provider</dt><dd>{run.extraction.provider ?? "未提供"}</dd></div><div><dt>Tokens</dt><dd>{run.extraction.input_tokens} in / {run.extraction.output_tokens} out</dd></div><div><dt>Latency</dt><dd>{run.extraction.latency_ms} ms</dd></div></> : null}
        </dl>

        <div className="review-gate">
          <div className="review-gate-heading"><div><strong>D5 人工审查门</strong><p>{isTerminal ? `此候选已${candidate.status === "accepted" ? "加入知识树" : "拒绝"}，不可再次提交。` : "决定前可先保存编辑；提交期间所有操作都会锁定，避免重复请求。"}</p></div>{isDirty && !isTerminal ? <button className="text-button" disabled={Boolean(busyAction)} onClick={resetDraft} type="button">放弃修改</button> : null}</div>
          {!isTerminal ? <div className="review-actions">
            <button className="button" disabled={Boolean(busyAction) || !isDirty} onClick={() => submit("edit")} type="button">{busyAction === "edit" ? "正在保存…" : "保存编辑"}</button>
            <button className="button verification" disabled={Boolean(busyAction)} onClick={() => submit("mark_needs_verification")} type="button">{busyAction === "mark_needs_verification" ? "正在标记…" : "标记待验证"}</button>
            <button className="button reject" disabled={Boolean(busyAction)} onClick={() => submit("reject")} type="button">{busyAction === "reject" ? "正在拒绝…" : "拒绝候选"}</button>
            <button className="button primary accept" disabled={Boolean(busyAction) || destinationsError} onClick={() => submit("accept")} type="button">{busyAction === "accept" ? "正在加入…" : "加入知识树"}</button>
          </div> : null}
        </div>
        <div aria-live="assertive" aria-atomic="true" className={`review-notice ${notice ? `is-${notice.kind}` : ""}`} role={notice?.kind === "error" ? "alert" : "status"}>{notice?.message ?? ""}</div>
      </div>
    </aside>
  );
}

export function CandidateQueue() {
  const queryClient = useQueryClient();
  const bootstrapQuery = useQuery({ queryKey: ["bootstrap"], queryFn: fetchBootstrap });
  const spaceId = bootstrapQuery.data?.space.id;
  const extractionsQuery = useQuery({
    queryKey: ["extractions", spaceId],
    queryFn: ({ signal }) => listExtractions(spaceId!, { signal }),
    enabled: Boolean(spaceId),
    refetchInterval: (query) => query.state.data?.some(({ job }) => job.status === "queued" || job.status === "running") ? 2_000 : false,
  });
  const hasActiveExtraction = extractionsQuery.data?.some(({ job }) => job.status === "queued" || job.status === "running") ?? false;
  const candidatesQuery = useQuery({
    queryKey: ["candidates", spaceId],
    queryFn: ({ signal }) => listCandidates(spaceId!, undefined, { signal }),
    enabled: Boolean(spaceId),
    refetchInterval: hasActiveExtraction ? 2_000 : false,
  });
  const destinationsQuery = useQuery({
    queryKey: ["knowledge-destinations", spaceId],
    queryFn: ({ signal }) => listKnowledgeDestinations(spaceId!, { signal }),
    enabled: Boolean(spaceId),
  });
  const candidates = useMemo(() => candidatesQuery.data ?? [], [candidatesQuery.data]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hasDirtyDraft, setHasDirtyDraft] = useState(false);

  const selected = candidates.find((candidate) => candidate.id === selectedId) ?? candidates[0];
  const latestRun = extractionsQuery.data?.[0];

  function selectCandidate(candidateId: string) {
    if (candidateId === selectedId) return;
    if (hasDirtyDraft && !window.confirm("当前候选有未保存修改。放弃修改并切换候选吗？")) return;
    setHasDirtyDraft(false);
    setSelectedId(candidateId);
  }

  function updateCachedCandidate(candidate: ExtractionCandidate) {
    queryClient.setQueryData<ExtractionCandidate[]>(["candidates", spaceId], (current = []) => current.map((item) => item.id === candidate.id ? candidate : item));
  }

  async function retryLatest() {
    if (!spaceId || !latestRun?.job.retryable) return;
    await retryJob(spaceId, latestRun.job.id, createIdempotencyKey());
    await queryClient.invalidateQueries({ queryKey: ["extractions", spaceId] });
    await queryClient.invalidateQueries({ queryKey: ["candidates", spaceId] });
  }

  const pageHeader = (description: string) => <PageHeader eyebrow="AI EXTRACTION · D5 人工审查" title="提炼审查" description={description} />;

  if (bootstrapQuery.isPending || (spaceId && (candidatesQuery.isPending || extractionsQuery.isPending))) return <section aria-live="polite">{pageHeader("正在读取真实候选队列…")}<div className="panel-card loading-panel">正在加载候选、证据关系与审查状态…</div></section>;
  if (bootstrapQuery.isError || !spaceId) return <section>{pageHeader("需要先连接默认个人知识空间。")}<div className="state-card error-state" role="alert"><h2>无法取得默认知识空间</h2><button className="button primary" onClick={() => bootstrapQuery.refetch()} type="button">重试连接</button></div></section>;
  if (!bootstrapQuery.data.capabilities.extraction_review) return <section>{pageHeader("服务端未开放候选审查能力。")}<div className="state-card"><h2>提炼能力尚未开放</h2></div></section>;
  if (candidatesQuery.isError || extractionsQuery.isError) return <section>{pageHeader("页面不会使用 Mock 候选替代失败请求。")}<div className="state-card error-state" role="alert"><h2>候选队列不可用</h2><p>{errorMessage(candidatesQuery.error ?? extractionsQuery.error)}</p><button className="button primary" onClick={() => { candidatesQuery.refetch(); extractionsQuery.refetch(); }} type="button">重新加载</button></div></section>;

  return (
    <section aria-labelledby="candidate-page-title">
      <PageHeader eyebrow="AI EXTRACTION · D5 人工审查" headingId="candidate-page-title" title="提炼审查" description={`在「${bootstrapQuery.data.space.name}」中逐条核对来源证据、定稿并决定候选去向。`} />
      {!selected ? <div className="source-empty panel-card"><strong>{latestRun?.job.status === "queued" || latestRun?.job.status === "running" ? "AI 提炼进行中" : latestRun?.job.status === "failed" ? "最近一次 AI 提炼失败" : latestRun?.job.status === "succeeded" ? "提炼完成，但没有生成候选" : "还没有 AI 候选"}</strong><p>{latestRun ? `${latestRun.source_title} · Job ${latestRun.job.status} · ${latestRun.job.progress}%` : "打开一份解析完成的来源并启动提炼；任务完成后候选会出现在这里。"}</p>{latestRun?.job.status === "queued" || latestRun?.job.status === "running" ? <progress aria-label="AI 提炼任务进度" max="100" value={latestRun.job.progress}>{latestRun.job.progress}%</progress> : null}{latestRun?.job.error_message ? <p>{latestRun.job.error_message}</p> : null}{latestRun?.job.retryable ? <button className="button primary" onClick={retryLatest} type="button">重试 AI 提炼</button> : null}</div> : <div className="candidate-workbench panel-card">
        <aside className="candidate-queue">
          <header className="candidate-column-head"><p className="state-kicker">REVIEW QUEUE</p><h2>候选队列</h2><p>{candidates.length} 条候选 · 选择前会保护未保存草稿</p></header>
          <div className="candidate-queue-list">{candidates.map((candidate) => <button aria-pressed={candidate.id === selected.id} className={candidate.id === selected.id ? "is-selected" : ""} key={candidate.id} onClick={() => selectCandidate(candidate.id)} type="button"><strong>{candidate.title}</strong><span>{candidate.source_title}</span><small>{statusLabels[candidate.status]} · v{candidate.version} · {candidate.evidence.length} 条证据</small></button>)}</div>
        </aside>
        <EvidenceDocument candidate={selected} />
        <CandidateEditor candidate={selected} destinations={destinationsQuery.data ?? []} destinationsError={destinationsQuery.isError} key={selected.id} onCandidateChange={updateCachedCandidate} onDirtyChange={setHasDirtyDraft} run={extractionsQuery.data?.find((run) => run.extraction.id === selected.extraction_job_id)} spaceId={spaceId} />
      </div>}
    </section>
  );
}
