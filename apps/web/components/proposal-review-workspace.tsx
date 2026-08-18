"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, createIdempotencyKey } from "@/lib/api";
import { fetchBootstrap } from "@/lib/bootstrap";
import {
  applyProposal,
  approveProposal,
  getProposal,
  listProposals,
  rejectProposal,
  type ProposalDetail,
  type ProposalItem,
} from "@/lib/proposals";
import { PageHeader } from "./page-states";
import {
  ProposalReviewDialog,
  type ProposalReviewSubmission,
} from "./proposal-review-dialog";

type ReviewAction = "approve" | "reject" | "apply";
type Notice = { kind: "success" | "error"; message: string } | null;

const statusLabels: Record<string, string> = {
  submitted: "待审",
  pending_review: "待审",
  approved: "已批准",
  rejected: "已拒绝",
  applied: "已应用",
  superseded: "已替代",
};

const actionLabels: Record<string, string> = {
  create: "创建",
  revise: "修订",
  supersede: "替代",
  merge_suggestion: "合并建议",
  mark_review_recommended: "标记复查",
};

function errorMessage(error: unknown) {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "提案审查请求失败";
}

function statusLabel(status: string) {
  return statusLabels[status] ?? status;
}

function actionLabel(action: string) {
  return actionLabels[action] ?? action;
}

function timestamp(value: unknown) {
  if (typeof value !== "string") return "未记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function ProposalListItem({ proposal, selected, onSelect }: {
  proposal: ProposalItem;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <button
      aria-pressed={selected}
      className={selected ? "is-selected" : ""}
      onClick={() => onSelect(proposal.id)}
      type="button"
    >
      <strong>{proposal.suggested_title ?? "未命名提案"}</strong>
      <span>{actionLabel(proposal.action)} · {statusLabel(proposal.status)}</span>
      <small>v{proposal.version} · 目标 {proposal.target_node_id ? "已有节点" : "新节点"}</small>
    </button>
  );
}

function ProposalDraft({ proposal }: { proposal: ProposalItem }) {
  const listFields = [
    ["建议标签", proposal.suggested_tags],
    ["适用条件", proposal.conditions],
    ["例外情况", proposal.exceptions],
  ] as const;

  return (
    <section aria-labelledby="proposal-draft-title">
      <p className="state-kicker">STRUCTURED DRAFT · 只读</p>
      <h2 id="proposal-draft-title">结构化草稿</h2>
      <dl>
        <div><dt>建议动作</dt><dd>{actionLabel(proposal.action)}</dd></div>
        <div><dt>创建类型</dt><dd>{proposal.create_kind ?? "不适用"}</dd></div>
        <div><dt>置信度</dt><dd>{proposal.confidence === null ? "未提供" : `${Math.round(proposal.confidence * 100)}%`}</dd></div>
        <div><dt>不确定性</dt><dd>{proposal.uncertainty_reason ?? "未说明"}</dd></div>
      </dl>
      <h3>{proposal.suggested_title ?? "未命名提案"}</h3>
      <p>{proposal.suggested_body ?? "该提案没有提供建议正文。"}</p>
      {listFields.map(([label, values]) => (
        <div key={label}>
          <h3>{label}</h3>
          {values.length ? <ul>{values.map((value) => <li key={value}>{value}</li>)}</ul> : <p>未提供</p>}
        </div>
      ))}
      <div>
        <h3>比较摘要</h3>
        <p>{proposal.comparison_summary ?? "未提供比较摘要。"}</p>
      </div>
    </section>
  );
}

function TargetRevision({ proposal }: { proposal: ProposalItem }) {
  return (
    <section aria-labelledby="proposal-target-title">
      <p className="state-kicker">TARGET REVISION</p>
      <h2 id="proposal-target-title">目标当前修订</h2>
      <dl>
        <div><dt>目标节点</dt><dd>{proposal.target_node_id ?? "本次创建不指定目标节点"}</dd></div>
        <div><dt>目标修订</dt><dd>{proposal.target_revision_id ?? "尚无目标修订"}</dd></div>
        <div><dt>节点版本</dt><dd>{proposal.target_node_version === null ? "服务端未提供" : `v${proposal.target_node_version}`}</dd></div>
      </dl>
      <p className="panel-caption" role="note">当前 detail API 仅返回 Proposal、Evidence 与 transitions，未包含目标节点正文；此处不能展示或比较目标当前正文。</p>
    </section>
  );
}

function FrozenEvidence({ detail }: { detail: ProposalDetail }) {
  return (
    <section aria-labelledby="proposal-evidence-title">
      <p className="state-kicker">FROZEN EVIDENCE · 只读</p>
      <h2 id="proposal-evidence-title">冻结证据</h2>
      {!detail.evidence.length ? <p>服务端没有返回冻结证据。</p> : <ol>
        {detail.evidence.map((evidence) => (
          <li key={evidence.id}>
            <strong>{evidence.role}</strong>
            <blockquote>{evidence.frozen_quote || "该证据没有可显示的摘录。"}</blockquote>
            <dl>
              <div><dt>来源版本</dt><dd>{evidence.source_version_id}</dd></div>
              <div><dt>解析产物</dt><dd>{evidence.parse_artifact_id}</dd></div>
              <div><dt>分段</dt><dd>#{evidence.ordinal} · {evidence.section_id}</dd></div>
              <div><dt>引用哈希</dt><dd>{evidence.quote_hash}</dd></div>
            </dl>
          </li>
        ))}
      </ol>}
    </section>
  );
}

function TransitionAudit({ detail }: { detail: ProposalDetail }) {
  return (
    <section aria-labelledby="proposal-audit-title">
      <p className="state-kicker">TRANSITION AUDIT</p>
      <h2 id="proposal-audit-title">状态转换审计</h2>
      {!detail.transitions.length ? <p>尚无状态转换记录。</p> : <ol>
        {detail.transitions.map((transition) => (
          <li key={transition.id}>
            <strong>{statusLabel(transition.from_status)} → {statusLabel(transition.to_status)}</strong>
            <span>{transition.operation} · {transition.actor} · {timestamp(transition.created_at)}</span>
            <small>v{transition.from_version} → v{transition.to_version} · {transition.reason ?? "未填写原因"}</small>
          </li>
        ))}
      </ol>}
    </section>
  );
}

function actionEligibility(status: string): ReviewAction[] {
  if (status === "submitted" || status === "pending_review") return ["approve", "reject"];
  if (status === "approved") return ["apply"];
  return [];
}

export function ProposalReviewWorkspace() {
  const queryClient = useQueryClient();
  const busyRef = useRef(false);
  const [status, setStatus] = useState("pending_review");
  const [action, setAction] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<ReviewAction | null>(null);
  const [dialogAction, setDialogAction] = useState<ReviewAction | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const bootstrapQuery = useQuery({ queryKey: ["bootstrap"], queryFn: fetchBootstrap });
  const spaceId = bootstrapQuery.data?.space.id;
  const proposalsQuery = useQuery({
    queryKey: ["proposals", spaceId, status, action],
    queryFn: ({ signal }) => listProposals(spaceId!, { status: status || undefined, action: action || undefined, signal }),
    enabled: Boolean(spaceId),
  });
  const proposals = proposalsQuery.data ?? [];
  const selected = proposals.find((proposal) => proposal.id === selectedId) ?? proposals[0];
  const detailQuery = useQuery({
    queryKey: ["proposal", spaceId, selected?.id],
    queryFn: ({ signal }) => getProposal(spaceId!, selected!.id, { signal }),
    enabled: Boolean(spaceId && selected),
  });

  useEffect(() => {
    if (selected && selected.id !== selectedId) setSelectedId(selected.id);
  }, [selected, selectedId]);

  async function refreshFromServer(proposalId: string) {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["proposals", spaceId] }),
      queryClient.invalidateQueries({ queryKey: ["proposal", spaceId, proposalId] }),
    ]);
  }

  async function review({ action: nextAction, reason }: ProposalReviewSubmission) {
    const proposal = detailQuery.data?.proposal;
    if (!spaceId || !proposal || busyRef.current) return;

    busyRef.current = true;
    setBusyAction(nextAction);
    setNotice(null);
    const idempotencyKey = createIdempotencyKey();
    const payload = { expected_version: proposal.version, reason: reason.trim() || null };
    const submit = nextAction === "approve" ? approveProposal : nextAction === "reject" ? rejectProposal : applyProposal;
    try {
      await submit(spaceId, proposal.id, payload, idempotencyKey);
      await refreshFromServer(proposal.id);
      setNotice({ kind: "success", message: `${nextAction === "approve" ? "批准" : nextAction === "reject" ? "拒绝" : "应用"}请求已由服务端确认，审查记录已刷新。` });
      setDialogAction(null);
    } catch (error) {
      setNotice({ kind: "error", message: errorMessage(error) });
    } finally {
      busyRef.current = false;
      setBusyAction(null);
    }
  }

  const actions = useMemo(() => detailQuery.data ? actionEligibility(detailQuery.data.proposal.status) : [], [detailQuery.data]);
  const header = (description: string) => <PageHeader eyebrow="KNOWLEDGE PROPOSALS · REVIEW" headingId="proposal-review-title" title="提案审查" description={description} />;

  if (bootstrapQuery.isPending || (spaceId && proposalsQuery.isPending)) return <section aria-live="polite">{header("正在读取提案审查队列…")}<div className="panel-card loading-panel">正在加载待审提案…</div></section>;
  if (bootstrapQuery.isError || !spaceId) return <section>{header("需要先连接默认知识空间。")}<div className="state-card error-state" role="alert"><h2>无法取得默认知识空间</h2><p>{errorMessage(bootstrapQuery.error)}</p><button className="button primary" onClick={() => bootstrapQuery.refetch()} type="button">重试连接</button></div></section>;
  if (proposalsQuery.isError) return <section>{header("页面不会以本地样例替代失败的服务端请求。")}<div className="state-card error-state" role="alert"><h2>提案队列不可用</h2><p>{errorMessage(proposalsQuery.error)}</p><button className="button primary" onClick={() => proposalsQuery.refetch()} type="button">重新加载</button></div></section>;

  return (
    <section aria-labelledby="proposal-review-title">
      {header(`在「${bootstrapQuery.data.space.name}」中核对冻结证据、目标修订标识及全部状态转换。`)}
      <div className="proposal-review-workspace panel-card">
        <aside aria-labelledby="proposal-queue-title">
          <p className="state-kicker">REVIEW QUEUE</p>
          <h2 id="proposal-queue-title">待审列表</h2>
          <label>状态<select aria-label="按状态筛选" disabled={Boolean(busyAction)} onChange={(event) => { setStatus(event.target.value); setSelectedId(null); }} value={status}><option value="">全部状态</option><option value="pending_review">待审</option><option value="approved">已批准</option><option value="rejected">已拒绝</option><option value="applied">已应用</option></select></label>
          <label>动作<select aria-label="按动作筛选" disabled={Boolean(busyAction)} onChange={(event) => { setAction(event.target.value); setSelectedId(null); }} value={action}><option value="">全部动作</option>{Object.entries(actionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          {!proposals.length ? <p>当前筛选没有提案。</p> : <div aria-label="提案列表">{proposals.map((proposal) => <ProposalListItem key={proposal.id} onSelect={setSelectedId} proposal={proposal} selected={proposal.id === selected?.id} />)}</div>}
        </aside>
        <article aria-busy={detailQuery.isFetching} aria-live="polite">
          {!selected ? <p>从列表选择一条提案以开始审查。</p> : detailQuery.isPending ? <p>正在读取提案详情…</p> : detailQuery.isError ? <div className="inline-error" role="alert">无法读取提案详情：{errorMessage(detailQuery.error)}<button className="button" onClick={() => detailQuery.refetch()} type="button">重试详情</button></div> : detailQuery.data ? <>
            <header><p className="state-kicker">PROPOSAL · {statusLabel(detailQuery.data.proposal.status)}</p><h2>{detailQuery.data.proposal.suggested_title ?? "未命名提案"}</h2><p>提案 {detailQuery.data.proposal.id} · 当前版本 v{detailQuery.data.proposal.version}</p></header>
            <ProposalDraft proposal={detailQuery.data.proposal} />
            <TargetRevision proposal={detailQuery.data.proposal} />
            <FrozenEvidence detail={detailQuery.data} />
            <TransitionAudit detail={detailQuery.data} />
            <section aria-labelledby="proposal-actions-title">
              <p className="state-kicker">SERVER-SIDE DECISION</p><h2 id="proposal-actions-title">审查操作</h2>
              {!actions.length ? <p>该提案当前状态没有可执行的审查操作。</p> : <div className="review-actions">{actions.map((availableAction) => <button className={`button ${availableAction === "apply" ? "primary" : availableAction === "reject" ? "reject" : ""}`} disabled={Boolean(busyAction)} key={availableAction} onClick={() => setDialogAction(availableAction)} type="button">{busyAction === availableAction ? `正在${availableAction === "approve" ? "批准" : availableAction === "reject" ? "拒绝" : "应用"}…` : availableAction === "approve" ? "批准提案" : availableAction === "reject" ? "拒绝提案" : "应用提案"}</button>)}</div>}
              <ProposalReviewDialog
                actions={dialogAction ? [dialogAction] : []}
                description="该决定将写入服务端审计记录，并要求使用当前 Proposal 版本。"
                loading={Boolean(busyAction)}
                onClose={() => setDialogAction(null)}
                onSubmit={(submission) => void review(submission)}
                open={dialogAction !== null}
              />
              <p className="panel-caption">提交期间工作区会锁定；每次成功提交都会重新读取服务端列表和详情。</p>
            </section>
          </> : null}
          <p aria-live="assertive" className={notice?.kind === "error" ? "inline-error" : ""} role={notice?.kind === "error" ? "alert" : "status"}>{notice?.message}</p>
        </article>
      </div>
    </section>
  );
}
