"use client";

import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type DragEvent } from "react";
import { PageHeader } from "./page-states";
import { ApiError, createIdempotencyKey } from "@/lib/api";
import { fetchBootstrap } from "@/lib/bootstrap";
import {
  MAX_IMPORT_SIZE_BYTES,
  calculateFileSha256,
  formatFileSize,
  sourceTitleFromFilename,
  uploadFileWithProgress,
  validateImportFile,
  type ValidatedFile,
} from "@/lib/file-import";
import {
  completeUpload,
  createSource,
  getJob,
  importPastedTextSource,
  importWebSource,
  listSources,
  reserveUpload,
  retryJob,
  type FileSourceKind,
  type ImportedSource,
  type Job,
  type Source,
  type UploadReservation,
} from "@/lib/sources";

type ImportMode = "file" | "web" | "pasted-text";
type CurrentImport = ImportedSource & { detail: string };
type ImportSession = {
  file: ValidatedFile;
  contentSha256?: string;
  source?: Source;
  createAttempted: boolean;
  createKey: string;
  reservation?: UploadReservation;
  reserveKey: string;
  completeKey: string;
};
type ImportPhase = "idle" | "hashing" | "creating" | "reserving" | "uploading" | "completing" | "processing" | "submitting";

const kindLabels: Record<Source["kind"], string> = { pdf: "PDF", markdown: "Markdown", text: "纯文本", web: "网页", pasted_text: "粘贴文本" };
const fileKindLabels: Record<FileSourceKind, string> = { pdf: "PDF", markdown: "Markdown", text: "纯文本" };
const sourceStatusLabels: Record<Source["status"], string> = { pending: "来源待处理", active: "来源可用", failed: "来源不可用", deleted: "来源已删除" };
const phaseLabels: Record<Exclude<ImportPhase, "idle">, string> = {
  hashing: "正在计算 SHA-256",
  creating: "正在创建来源记录",
  reserving: "正在预留上传位置",
  uploading: "正在上传原始文件",
  completing: "正在校验并保存原件",
  processing: "来源已保存，正在确认任务状态",
  submitting: "正在保存来源并创建解析任务",
};
const tabs: Array<{ id: ImportMode; label: string }> = [
  { id: "file", label: "文件" },
  { id: "web", label: "网页" },
  { id: "pasted-text", label: "粘贴文本" },
];

function errorMessage(error: unknown) {
  if (error instanceof DOMException && error.name === "AbortError") return "导入已取消";
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "导入失败，请稍后重试";
}
function isPollingJob(job: Job | undefined) { return job?.status === "queued" || job?.status === "running"; }
function isUncertainRequestFailure(error: unknown) { return error instanceof ApiError && (error.status === 0 || error.status >= 500); }
function uploadTimeoutMs(expiresAt: string) { return Math.max(1, new Date(expiresAt).getTime() - Date.now() - 1_000); }

function RecentImports({ sources, isPending, isError, onRetry }: { sources: Source[] | undefined; isPending: boolean; isError: boolean; onRetry: () => void }) {
  return (
    <section aria-labelledby="recent-imports-title" className="recent-imports panel-card">
      <div className="panel-heading"><div><p className="state-kicker">SOURCE REGISTER</p><h2 id="recent-imports-title">最近来源记录</h2><p className="panel-caption">打开来源详情可查看真实版本、解析状态、解析产物与可引用分段。</p></div>{isError ? <button className="button" onClick={onRetry} type="button">重新加载</button> : null}</div>
      {isPending ? <p aria-live="polite" className="muted-message">正在读取来源记录…</p> : null}
      {isError ? <p className="inline-error" role="alert">暂时无法读取最近来源。</p> : null}
      {sources?.length === 0 ? <div className="source-empty"><strong>还没有来源</strong><p>导入文件、网页或文本后，来源记录会出现在这里。</p></div> : null}
      {sources?.length ? <ol className="source-list">{sources.slice(0, 8).map((source) => <li key={source.id}><Link className="source-item source-link" href={`/sources/${source.id}`}><span aria-hidden="true" className={`file-mark kind-${source.kind}`}>{source.kind === "markdown" ? "MD" : source.kind === "pasted_text" ? "TXT" : source.kind.toUpperCase()}</span><span className="source-copy"><strong>{source.title}</strong><small>{kindLabels[source.kind]} · {new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(source.created_at))}</small></span><span className={`source-status status-${source.status}`}>{sourceStatusLabels[source.status]}</span></Link></li>)}</ol> : null}
    </section>
  );
}

export function ImportWorkspace() {
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const flowControllerRef = useRef<AbortController | null>(null);
  const sessionRef = useRef<ImportSession | null>(null);
  const retryKeyRef = useRef<{ jobId: string; key: string } | null>(null);
  const nonFileKeyRef = useRef<{ fingerprint: string; key: string } | null>(null);
  const terminalJobRef = useRef<string | null>(null);
  const [mode, setMode] = useState<ImportMode>("file");
  const [selected, setSelected] = useState<ValidatedFile | null>(null);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [flowError, setFlowError] = useState<string | null>(null);
  const [phase, setPhase] = useState<ImportPhase>("idle");
  const [uploadProgress, setUploadProgress] = useState(0);
  const [jobReference, setJobReference] = useState<{ id: string; status: Job["status"] } | null>(null);
  const [currentImport, setCurrentImport] = useState<CurrentImport | null>(null);
  const [canResume, setCanResume] = useState(false);
  const [isRetrying, setIsRetrying] = useState(false);
  const [webTitle, setWebTitle] = useState("");
  const [webUrl, setWebUrl] = useState("");
  const [textTitle, setTextTitle] = useState("");
  const [pastedText, setPastedText] = useState("");

  const bootstrapQuery = useQuery({ queryKey: ["bootstrap"], queryFn: fetchBootstrap });
  const spaceId = bootstrapQuery.data?.space.id;
  const maxUploadSize = bootstrapQuery.data?.max_upload_size_bytes ?? MAX_IMPORT_SIZE_BYTES;
  const sourcesQuery = useQuery({ queryKey: ["sources", spaceId], queryFn: ({ signal }) => listSources(spaceId!, { signal }), enabled: Boolean(spaceId) });
  const jobQuery = useQuery({ queryKey: ["job", spaceId, jobReference?.id], queryFn: ({ signal }) => getJob(spaceId!, jobReference!.id, { signal }), enabled: Boolean(spaceId && jobReference), refetchInterval: (query) => (isPollingJob(query.state.data) ? 1_000 : false) });
  const currentJob = jobQuery.data;
  const isBusy = phase !== "idle" && (!currentJob || isPollingJob(currentJob));

  useEffect(() => () => flowControllerRef.current?.abort(), []);
  useEffect(() => {
    if (!currentJob || isPollingJob(currentJob) || terminalJobRef.current === currentJob.id) return;
    terminalJobRef.current = currentJob.id;
    retryKeyRef.current = null;
    setPhase("idle");
    void queryClient.invalidateQueries({ queryKey: ["sources", spaceId] });
    void queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
  }, [currentJob, queryClient, spaceId]);

  function resetResult() { setFlowError(null); setJobReference(null); setCurrentImport(null); terminalJobRef.current = null; retryKeyRef.current = null; }
  function clearImportSession() { sessionRef.current = null; flowControllerRef.current?.abort(); flowControllerRef.current = null; }
  function selectFile(file: File | undefined) {
    clearImportSession(); setSelectionError(null); resetResult(); setUploadProgress(0); setCanResume(false);
    if (!file) { setSelected(null); return; }
    try { setSelected(validateImportFile(file, maxUploadSize)); }
    catch (error) { setSelected(null); setSelectionError(errorMessage(error)); if (inputRef.current) inputRef.current.value = ""; }
  }
  function handleDrop(event: DragEvent<HTMLDivElement>) { event.preventDefault(); if (!isBusy) selectFile(event.dataTransfer.files.item(0) ?? undefined); }
  function getOrCreateSession(file: ValidatedFile) {
    const existing = sessionRef.current;
    if (existing?.file.file === file.file) return existing;
    const created: ImportSession = { file, createAttempted: false, createKey: createIdempotencyKey(), reserveKey: createIdempotencyKey(), completeKey: createIdempotencyKey() };
    sessionRef.current = created; return created;
  }
  async function invalidateWorkspace() { await Promise.all([queryClient.invalidateQueries({ queryKey: ["sources", spaceId] }), queryClient.invalidateQueries({ queryKey: ["bootstrap"] })]); }

  async function startFileImport() {
    if (!selected || !spaceId || isBusy) return;
    resetResult(); setUploadProgress(0);
    const controller = new AbortController(); flowControllerRef.current = controller;
    const session = getOrCreateSession(selected);
    try {
      if (!session.contentSha256) { setPhase("hashing"); session.contentSha256 = await calculateFileSha256(selected.file, controller.signal); }
      if (!session.source) { setPhase("creating"); session.createAttempted = true; session.source = await createSource(spaceId, selected.kind, sourceTitleFromFilename(selected.file.name), session.createKey, { signal: controller.signal }); }
      if (session.reservation && new Date(session.reservation.expires_at).getTime() <= Date.now() + 1_000) { session.reservation = undefined; session.reserveKey = createIdempotencyKey(); session.completeKey = createIdempotencyKey(); }
      if (!session.reservation) { setPhase("reserving"); session.reservation = await reserveUpload(spaceId, session.source.id, { originalFilename: selected.file.name, mediaType: selected.mediaType, sizeBytes: selected.file.size, contentSha256: session.contentSha256 }, session.reserveKey, { signal: controller.signal }); }
      setPhase("uploading");
      await uploadFileWithProgress({ url: session.reservation.upload_url, headers: session.reservation.upload_headers, fields: session.reservation.upload_fields, mediaType: selected.mediaType, file: selected.file, onProgress: setUploadProgress, signal: controller.signal, timeoutMs: uploadTimeoutMs(session.reservation.expires_at) });
      setPhase("completing");
      const completed = await completeUpload(spaceId, session.source.id, session.reservation.version.id, session.completeKey, { signal: controller.signal });
      sessionRef.current = null; setCanResume(false); flowControllerRef.current = null; setPhase("processing");
      setCurrentImport({ ...completed, detail: completed.version.original_filename ?? selected.file.name }); setJobReference(completed.job); await invalidateWorkspace();
    } catch (error) { flowControllerRef.current = null; setPhase("idle"); setCanResume(Boolean(sessionRef.current?.createAttempted)); setFlowError(errorMessage(error)); void queryClient.invalidateQueries({ queryKey: ["sources", spaceId] }); }
  }

  function validateNonFile() {
    const title = (mode === "web" ? webTitle : textTitle).trim();
    if (!title) throw new Error("请输入来源标题");
    if (title.length > 500) throw new Error("来源标题不能超过 500 个字符");
    if (mode === "web") {
      const url = webUrl.trim();
      let parsed: URL;
      try { parsed = new URL(url); } catch { throw new Error("请输入完整有效的网页地址"); }
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error("网页地址仅支持 HTTP 或 HTTPS");
      return { title, value: url, fingerprint: `web\n${title}\n${url}` };
    }
    if (!pastedText.trim()) throw new Error("请输入需要保存的正文");
    return { title, value: pastedText, fingerprint: `pasted-text\n${title}\n${pastedText}` };
  }

  async function startNonFileImport() {
    if (!spaceId || isBusy || mode === "file") return;
    resetResult();
    try {
      const request = validateNonFile(); setPhase("submitting");
      const keyState = nonFileKeyRef.current?.fingerprint === request.fingerprint ? nonFileKeyRef.current : { fingerprint: request.fingerprint, key: createIdempotencyKey() };
      nonFileKeyRef.current = keyState;
      const imported = mode === "web" ? await importWebSource(spaceId, request.title, request.value, keyState.key) : await importPastedTextSource(spaceId, request.title, request.value, keyState.key);
      nonFileKeyRef.current = null; setCurrentImport({ ...imported, detail: mode === "web" ? request.value : `${new TextEncoder().encode(request.value).length} bytes 文本` }); setJobReference(imported.job); setPhase("processing"); await invalidateWorkspace();
    } catch (error) { setPhase("idle"); setFlowError(errorMessage(error)); if (!isUncertainRequestFailure(error)) nonFileKeyRef.current = null; void queryClient.invalidateQueries({ queryKey: ["sources", spaceId] }); }
  }

  async function handleRetryJob() {
    if (!spaceId || !currentJob?.retryable || isRetrying) return;
    setIsRetrying(true); setFlowError(null); terminalJobRef.current = null;
    const retryState = retryKeyRef.current?.jobId === currentJob.id ? retryKeyRef.current : { jobId: currentJob.id, key: createIdempotencyKey() }; retryKeyRef.current = retryState;
    try { const retried = await retryJob(spaceId, currentJob.id, retryState.key); retryKeyRef.current = null; queryClient.setQueryData(["job", spaceId, currentJob.id], retried); setJobReference({ id: retried.id, status: retried.status }); setPhase("processing"); }
    catch (error) { if (isUncertainRequestFailure(error)) { const reconciled = await jobQuery.refetch(); if (reconciled.data && (reconciled.data.status !== "failed" || !reconciled.data.retryable)) { retryKeyRef.current = null; setPhase(isPollingJob(reconciled.data) ? "processing" : "idle"); return; } } setFlowError(errorMessage(error)); }
    finally { setIsRetrying(false); }
  }

  function resetSelection() { clearImportSession(); setSelected(null); setSelectionError(null); resetResult(); setUploadProgress(0); setCanResume(false); if (inputRef.current) inputRef.current.value = ""; }
  function switchMode(nextMode: ImportMode) { if (isBusy) return; setMode(nextMode); setSelectionError(null); setFlowError(null); setCurrentImport(null); setJobReference(null); terminalJobRef.current = null; }

  if (bootstrapQuery.isPending) return <section aria-live="polite"><PageHeader eyebrow="INTAKE · D3" title="导入知识" description="正在连接默认知识空间…" /><div className="panel-card loading-panel">正在读取工作台配置…</div></section>;
  if (bootstrapQuery.isError || !spaceId) return <section><PageHeader eyebrow="INTAKE · D3" title="导入知识" description="先连接默认个人知识空间，再保存原始资料。" /><div className="state-card error-state" role="alert"><p className="state-kicker">CONNECTION · 导入暂不可用</p><h2>无法取得默认知识空间</h2><p>页面不会把内容发送到未知空间。请确认 API 正常后重试。</p><button className="button primary" onClick={() => bootstrapQuery.refetch()} type="button">重试连接</button></div></section>;
  if (!bootstrapQuery.data.capabilities.source_import) return <section aria-labelledby="import-title"><PageHeader eyebrow="INTAKE · D3" headingId="import-title" title="导入知识" description={`「${bootstrapQuery.data.space.name}」当前没有开放来源导入能力。`} /><div className="state-card" role="status"><p className="state-kicker">CAPABILITY · SOURCE IMPORT OFF</p><h2>来源导入尚未开放</h2><p>服务端 capability 明确关闭了来源导入，因此不会选择或发送内容。</p></div></section>;

  return (
    <section aria-labelledby="import-title">
      <PageHeader eyebrow="INTAKE · D3 解析入库" headingId="import-title" title="导入知识" description={`把文件、网页或文本保存到「${bootstrapQuery.data.space.name}」，并追踪服务端返回的真实入库与解析任务。`} />
      <div className="import-layout">
        <section aria-labelledby="upload-title" className="upload-panel panel-card">
          <div className="panel-heading"><div><p className="state-kicker">01 · SELECT SOURCE</p><h2 id="upload-title">选择来源类型</h2></div><span className="limit-note">真实保存 · 真实解析状态</span></div>
          <div aria-label="来源类型" className="import-tabs" role="tablist">{tabs.map((tab) => <button aria-controls={`panel-${tab.id}`} aria-selected={mode === tab.id} disabled={isBusy} id={`tab-${tab.id}`} key={tab.id} onClick={() => switchMode(tab.id)} role="tab" tabIndex={mode === tab.id ? 0 : -1} type="button">{tab.label}</button>)}</div>
          <div aria-labelledby={`tab-${mode}`} className="import-tab-panel" id={`panel-${mode}`} role="tabpanel">
            {mode === "file" ? <><div className={`file-drop ${selected ? "has-file" : ""} ${selectionError ? "has-error" : ""}`} onDragOver={(event) => event.preventDefault()} onDrop={handleDrop}><input accept=".pdf,.md,.txt,application/pdf,text/markdown,text/plain" aria-label="选择文件或拖放到这里" aria-describedby={selectionError ? "file-error file-guidance" : "file-guidance"} disabled={isBusy} id="source-file" onChange={(event) => selectFile(event.target.files?.[0])} ref={inputRef} type="file" /><label htmlFor="source-file"><span aria-hidden="true" className="upload-glyph">⇧</span><strong>{selected ? selected.file.name : "选择文件或拖放到这里"}</strong><span id="file-guidance">{selected ? `${fileKindLabels[selected.kind]} · ${formatFileSize(selected.file.size)}` : `PDF / MD / TXT，最大 ${formatFileSize(maxUploadSize)}；空文件不会上传。`}</span></label></div>{selectionError ? <p className="inline-error" id="file-error" role="alert">{selectionError}</p> : null}{selected ? <div aria-label="待导入文件信息" className="file-declaration"><div><span>来源标题</span><strong>{sourceTitleFromFilename(selected.file.name)}</strong></div><div><span>声明类型</span><strong>{selected.mediaType}</strong></div><div><span>文件大小</span><strong>{formatFileSize(selected.file.size)}</strong></div></div> : null}</> : null}
            {mode === "web" ? <div className="source-form"><label htmlFor="web-title"><span>来源标题</span><input disabled={isBusy} id="web-title" maxLength={500} onChange={(event) => { setWebTitle(event.target.value); nonFileKeyRef.current = null; }} placeholder="例如：项目规范" value={webTitle} /></label><label htmlFor="web-url"><span>网页地址</span><input disabled={isBusy} id="web-url" inputMode="url" onChange={(event) => { setWebUrl(event.target.value); nonFileKeyRef.current = null; }} placeholder="https://example.com/article" type="url" value={webUrl} /></label><p className="field-hint">服务端会安全获取网页快照；页面不会预先伪造标题、摘要或正文。</p></div> : null}
            {mode === "pasted-text" ? <div className="source-form"><label htmlFor="text-title"><span>来源标题</span><input disabled={isBusy} id="text-title" maxLength={500} onChange={(event) => { setTextTitle(event.target.value); nonFileKeyRef.current = null; }} placeholder="例如：会议记录" value={textTitle} /></label><label htmlFor="pasted-text"><span>正文</span><textarea disabled={isBusy} id="pasted-text" onChange={(event) => { setPastedText(event.target.value); nonFileKeyRef.current = null; }} placeholder="粘贴要保存并解析的原始文本" rows={10} value={pastedText} /></label><p className="field-hint">当前输入 {new TextEncoder().encode(pastedText).length} bytes；最终大小限制和内容校验以服务端响应为准。</p></div> : null}
          </div>
          {phase !== "idle" ? <div aria-live="polite" className="import-progress"><div className="progress-copy"><strong>{phaseLabels[phase]}</strong><span>{phase === "uploading" ? `${uploadProgress}%` : "请勿重复提交"}</span></div>{phase === "uploading" ? <progress aria-label="文件上传进度" max="100" value={uploadProgress}>{uploadProgress}%</progress> : <div aria-hidden="true" className="indeterminate-track"><span /></div>}{mode === "file" && phase !== "processing" ? <button className="text-button cancel-import" onClick={() => flowControllerRef.current?.abort()} type="button">取消导入</button> : null}</div> : null}
          {flowError ? <p className="inline-error flow-error" role="alert">{flowError}</p> : null}
          {jobQuery.isError ? <p className="inline-error flow-error" role="alert">无法刷新任务状态。已保存的来源不会被页面改写。<button className="text-button" onClick={() => jobQuery.refetch()} type="button">重试查询</button></p> : null}
          {currentImport ? <div aria-label="本页当前导入" className="current-upload"><p className="state-kicker">CURRENT SOURCE · 本页记录</p><div><span>来源</span><strong>{currentImport.source.title}</strong></div><div><span>原始内容</span><strong>{currentImport.detail}</strong></div><div><span>任务</span><strong>{currentJob?.status ?? currentImport.job.status}</strong></div><small>版本 {currentImport.version.id} · 任务 {currentImport.job.id}</small><Link className="button" href={`/sources/${currentImport.source.id}`}>查看来源详情</Link></div> : null}
          {currentJob ? <div aria-live="polite" className={`job-state job-${currentJob.status}`}><p className="state-kicker">JOB · {currentJob.kind.toUpperCase()} · {currentJob.status.toUpperCase()}</p>{currentJob.status === "succeeded" ? <><h3>来源任务已完成</h3><p>服务端已完成当前任务。进入来源详情查看独立的版本解析状态和真实分段。</p></> : null}{currentJob.status === "failed" ? <><h3>{currentJob.kind === "source_parse" ? "解析任务失败" : "入库任务失败"}</h3><p>{currentJob.error_message ?? "任务未能完成，原始失败信息未提供。"}</p>{currentJob.retryable ? <button className="button" disabled={isRetrying} onClick={handleRetryJob} type="button">{isRetrying ? "正在重试…" : "重试失败任务"}</button> : null}</> : null}{currentJob.status === "cancelled" ? <><h3>任务已取消</h3><p>服务端未继续处理这个任务。</p></> : null}{isPollingJob(currentJob) ? <><h3>{currentJob.status === "queued" ? "任务正在排队" : "任务正在处理"}</h3><progress aria-label="来源任务进度" max="100" value={currentJob.progress}>{currentJob.progress}%</progress></> : null}</div> : null}
          <div className="form-actions">{mode === "file" ? <><button className="button primary" disabled={!selected || isBusy} onClick={startFileImport} type="button">{isBusy ? "正在导入…" : canResume ? "继续导入" : "校验并导入"}</button>{selected && !isBusy ? <button className="button" onClick={resetSelection} type="button">选择其他文件</button> : null}</> : <button className="button primary" disabled={isBusy} onClick={startNonFileImport} type="button">{isBusy ? "正在导入…" : mode === "web" ? "导入网页" : "导入文本"}</button>}</div>
        </section>
        <aside aria-label="D3 导入说明" className="intake-notes"><div className="note-block active-note"><span>当前能力</span><strong>入库与解析追踪</strong><p>三类来源均通过真实 API 保存；页面只展示服务端返回的来源、版本、任务与解析状态。</p></div><div className="note-block future-note"><span>可信定位</span><strong>版本化分段</strong><p>来源详情按当前解析产物分页读取 section，并显示页码、标题路径、段落与冻结 locator。</p></div></aside>
      </div>
      <RecentImports isError={sourcesQuery.isError} isPending={sourcesQuery.isPending} onRetry={() => void sourcesQuery.refetch()} sources={sourcesQuery.data} />
    </section>
  );
}
