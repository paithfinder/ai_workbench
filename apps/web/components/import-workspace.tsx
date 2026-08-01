"use client";

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
  listSources,
  reserveUpload,
  retryJob,
  type FileSourceKind,
  type Job,
  type Source,
  type UploadReservation,
} from "@/lib/sources";

type CurrentUpload = {
  source: Source;
  versionId: string;
  filename: string;
  job: { id: string; status: Job["status"] };
};

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

type ImportPhase =
  | "idle"
  | "hashing"
  | "creating"
  | "reserving"
  | "uploading"
  | "completing"
  | "processing";

const kindLabels: Record<Source["kind"], string> = {
  pdf: "PDF",
  markdown: "Markdown",
  text: "纯文本",
  web: "网页",
  pasted_text: "粘贴文本",
};

const fileKindLabels: Record<FileSourceKind, string> = {
  pdf: "PDF",
  markdown: "Markdown",
  text: "纯文本",
};

const sourceStatusLabels: Record<Source["status"], string> = {
  pending: "来源待处理",
  active: "来源可用",
  failed: "来源不可用",
  deleted: "来源已删除",
};

const phaseLabels: Record<Exclude<ImportPhase, "idle">, string> = {
  hashing: "正在计算 SHA-256",
  creating: "正在创建来源记录",
  reserving: "正在预留上传位置",
  uploading: "正在上传原始文件",
  completing: "正在校验并保存原件",
  processing: "原件已保存，正在确认任务状态",
};

function errorMessage(error: unknown) {
  if (error instanceof DOMException && error.name === "AbortError") return "导入已取消";
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "导入失败，请稍后重试";
}

function isPollingJob(job: Job | undefined) {
  return job?.status === "queued" || job?.status === "running";
}

function isUncertainRequestFailure(error: unknown) {
  return error instanceof ApiError && (error.status === 0 || error.status >= 500);
}

function uploadTimeoutMs(expiresAt: string) {
  return Math.max(1, new Date(expiresAt).getTime() - Date.now() - 1_000);
}

function RecentImports({
  sources,
  isPending,
  isError,
  onRetry,
}: {
  sources: Source[] | undefined;
  isPending: boolean;
  isError: boolean;
  onRetry: () => void;
}) {
  return (
    <section aria-labelledby="recent-imports-title" className="recent-imports panel-card">
      <div className="panel-heading">
        <div>
          <p className="state-kicker">SOURCE REGISTER</p>
          <h2 id="recent-imports-title">最近来源记录</h2>
          <p className="panel-caption">
            来自当前 sources 列表；接口未返回最新版本或任务，因此这里只展示来源字段。
          </p>
        </div>
        {isError ? <button className="button" onClick={onRetry} type="button">重新加载</button> : null}
      </div>
      {isPending ? <p aria-live="polite" className="muted-message">正在读取来源记录…</p> : null}
      {isError ? <p className="inline-error" role="alert">暂时无法读取最近来源。</p> : null}
      {sources?.length === 0 ? (
        <div className="source-empty"><strong>还没有来源</strong><p>选择一个真实文件，创建后的来源记录会出现在这里。</p></div>
      ) : null}
      {sources?.length ? (
        <ol className="source-list">
          {sources.slice(0, 8).map((source) => (
            <li className="source-item" key={source.id}>
              <span aria-hidden="true" className={`file-mark kind-${source.kind}`}>
                {source.kind === "markdown" ? "MD" : source.kind === "pasted_text" ? "TXT" : source.kind.toUpperCase()}
              </span>
              <div className="source-copy">
                <strong>{source.title}</strong>
                <small>{kindLabels[source.kind]} · {new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(source.created_at))}</small>
              </div>
              <span className={`source-status status-${source.status}`}>{sourceStatusLabels[source.status]}</span>
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}

export function ImportWorkspace() {
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const flowControllerRef = useRef<AbortController | null>(null);
  const sessionRef = useRef<ImportSession | null>(null);
  const retryKeyRef = useRef<{ jobId: string; key: string } | null>(null);
  const terminalJobRef = useRef<string | null>(null);
  const [selected, setSelected] = useState<ValidatedFile | null>(null);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [flowError, setFlowError] = useState<string | null>(null);
  const [phase, setPhase] = useState<ImportPhase>("idle");
  const [uploadProgress, setUploadProgress] = useState(0);
  const [jobReference, setJobReference] = useState<{ id: string; status: Job["status"] } | null>(null);
  const [currentUpload, setCurrentUpload] = useState<CurrentUpload | null>(null);
  const [canResume, setCanResume] = useState(false);
  const [isRetrying, setIsRetrying] = useState(false);

  const bootstrapQuery = useQuery({ queryKey: ["bootstrap"], queryFn: fetchBootstrap });
  const spaceId = bootstrapQuery.data?.space.id;
  const maxUploadSize = bootstrapQuery.data?.max_upload_size_bytes ?? MAX_IMPORT_SIZE_BYTES;
  const sourcesQuery = useQuery({
    queryKey: ["sources", spaceId],
    queryFn: ({ signal }) => listSources(spaceId!, { signal }),
    enabled: Boolean(spaceId),
  });
  const jobQuery = useQuery({
    queryKey: ["job", spaceId, jobReference?.id],
    queryFn: ({ signal }) => getJob(spaceId!, jobReference!.id, { signal }),
    enabled: Boolean(spaceId && jobReference),
    refetchInterval: (query) => (isPollingJob(query.state.data) ? 1_000 : false),
  });

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

  function clearImportSession() {
    sessionRef.current = null;
    flowControllerRef.current?.abort();
    flowControllerRef.current = null;
  }

  function selectFile(file: File | undefined) {
    clearImportSession();
    setSelectionError(null);
    setFlowError(null);
    setJobReference(null);
    setCurrentUpload(null);
    terminalJobRef.current = null;
    retryKeyRef.current = null;
    setUploadProgress(0);
    setCanResume(false);
    if (!file) { setSelected(null); return; }
    try {
      setSelected(validateImportFile(file, maxUploadSize));
    } catch (error) {
      setSelected(null);
      setSelectionError(errorMessage(error));
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    if (!isBusy) selectFile(event.dataTransfer.files.item(0) ?? undefined);
  }

  function getOrCreateSession(file: ValidatedFile) {
    const existing = sessionRef.current;
    if (existing?.file.file === file.file) return existing;
    const created: ImportSession = {
      file,
      createAttempted: false,
      createKey: createIdempotencyKey(),
      reserveKey: createIdempotencyKey(),
      completeKey: createIdempotencyKey(),
    };
    sessionRef.current = created;
    return created;
  }

  async function startImport() {
    if (!selected || !spaceId || isBusy) return;
    setFlowError(null);
    setJobReference(null);
    setCurrentUpload(null);
    terminalJobRef.current = null;
    setUploadProgress(0);
    const controller = new AbortController();
    flowControllerRef.current = controller;
    const session = getOrCreateSession(selected);

    try {
      if (!session.contentSha256) {
        setPhase("hashing");
        session.contentSha256 = await calculateFileSha256(selected.file, controller.signal);
      }
      if (!session.source) {
        setPhase("creating");
        session.createAttempted = true;
        session.source = await createSource(
          spaceId,
          selected.kind,
          sourceTitleFromFilename(selected.file.name),
          session.createKey,
          { signal: controller.signal },
        );
      }
      if (session.reservation && new Date(session.reservation.expires_at).getTime() <= Date.now() + 1_000) {
        session.reservation = undefined;
        session.reserveKey = createIdempotencyKey();
        session.completeKey = createIdempotencyKey();
      }
      if (!session.reservation) {
        setPhase("reserving");
        session.reservation = await reserveUpload(spaceId, session.source.id, {
          originalFilename: selected.file.name,
          mediaType: selected.mediaType,
          sizeBytes: selected.file.size,
          contentSha256: session.contentSha256,
        }, session.reserveKey, { signal: controller.signal });
      }

      setPhase("uploading");
      await uploadFileWithProgress({
        url: session.reservation.upload_url,
        headers: session.reservation.upload_headers,
        fields: session.reservation.upload_fields,
        mediaType: selected.mediaType,
        file: selected.file,
        onProgress: setUploadProgress,
        signal: controller.signal,
        timeoutMs: uploadTimeoutMs(session.reservation.expires_at),
      });

      setPhase("completing");
      const completed = await completeUpload(spaceId, session.source.id, session.reservation.version.id, session.completeKey, { signal: controller.signal });
      sessionRef.current = null;
      setCanResume(false);
      flowControllerRef.current = null;
      setPhase("processing");
      setCurrentUpload({ source: completed.source, versionId: completed.version.id, filename: completed.version.original_filename, job: completed.job });
      setJobReference(completed.job);
      await queryClient.invalidateQueries({ queryKey: ["sources", spaceId] });
      await queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
    } catch (error) {
      flowControllerRef.current = null;
      setPhase("idle");
      setCanResume(Boolean(sessionRef.current?.createAttempted));
      setFlowError(errorMessage(error));
      void queryClient.invalidateQueries({ queryKey: ["sources", spaceId] });
    }
  }

  function cancelImport() {
    flowControllerRef.current?.abort();
  }

  async function handleRetryJob() {
    if (!spaceId || !currentJob?.retryable || isRetrying) return;
    setIsRetrying(true);
    setFlowError(null);
    terminalJobRef.current = null;
    const retryState = retryKeyRef.current?.jobId === currentJob.id
      ? retryKeyRef.current
      : { jobId: currentJob.id, key: createIdempotencyKey() };
    retryKeyRef.current = retryState;
    try {
      const retried = await retryJob(spaceId, currentJob.id, retryState.key);
      retryKeyRef.current = null;
      queryClient.setQueryData(["job", spaceId, currentJob.id], retried);
      setJobReference({ id: retried.id, status: retried.status });
      setPhase("processing");
    } catch (error) {
      if (isUncertainRequestFailure(error)) {
        const reconciled = await jobQuery.refetch();
        if (reconciled.data && (reconciled.data.status !== "failed" || !reconciled.data.retryable)) {
          retryKeyRef.current = null;
          setPhase(isPollingJob(reconciled.data) ? "processing" : "idle");
          return;
        }
      }
      setFlowError(errorMessage(error));
    } finally {
      setIsRetrying(false);
    }
  }

  function resetSelection() {
    clearImportSession();
    setSelected(null);
    setSelectionError(null);
    setFlowError(null);
    setJobReference(null);
    setCurrentUpload(null);
    setUploadProgress(0);
    setCanResume(false);
    terminalJobRef.current = null;
    retryKeyRef.current = null;
    if (inputRef.current) inputRef.current.value = "";
  }

  if (bootstrapQuery.isPending) return <section aria-live="polite"><PageHeader eyebrow="INTAKE · D2" title="导入知识" description="正在连接默认知识空间…" /><div className="panel-card loading-panel">正在读取工作台配置…</div></section>;
  if (bootstrapQuery.isError || !spaceId) return <section><PageHeader eyebrow="INTAKE · D2" title="导入知识" description="先连接默认个人知识空间，再保存原始资料。" /><div className="state-card error-state" role="alert"><p className="state-kicker">CONNECTION · 导入暂不可用</p><h2>无法取得默认知识空间</h2><p>页面不会把文件发送到未知空间。请确认 API 正常后重试。</p><button className="button primary" onClick={() => bootstrapQuery.refetch()} type="button">重试连接</button></div></section>;
  if (!bootstrapQuery.data.capabilities.source_import) return <section aria-labelledby="import-title"><PageHeader eyebrow="INTAKE · D2" headingId="import-title" title="导入知识" description={`「${bootstrapQuery.data.space.name}」当前没有开放来源导入能力。`} /><div className="state-card" role="status"><p className="state-kicker">CAPABILITY · SOURCE IMPORT OFF</p><h2>来源导入尚未开放</h2><p>页面已连接默认知识空间，但服务端 capability 明确关闭了来源导入，因此不会选择或发送文件。</p></div></section>;

  return (
    <section aria-labelledby="import-title">
      <PageHeader eyebrow="INTAKE · D2 原文入库" headingId="import-title" title="导入知识" description={`保存 PDF、Markdown 或纯文本原件到「${bootstrapQuery.data.space.name}」。D2 只负责可信保存与任务状态；内容解析将在 D3 开放。`} />
      <div className="import-layout">
        <section aria-labelledby="upload-title" className="upload-panel panel-card">
          <div className="panel-heading"><div><p className="state-kicker">01 · SELECT SOURCE</p><h2 id="upload-title">选择原始文件</h2></div><span className="limit-note">PDF / MD / TXT · 最大 {formatFileSize(maxUploadSize)}</span></div>
          <div className={`file-drop ${selected ? "has-file" : ""} ${selectionError ? "has-error" : ""}`} onDragOver={(event) => event.preventDefault()} onDrop={handleDrop}>
            <input accept=".pdf,.md,.txt,application/pdf,text/markdown,text/plain" aria-label="选择文件或拖放到这里" aria-describedby={selectionError ? "file-error file-guidance" : "file-guidance"} disabled={isBusy} id="source-file" onChange={(event) => selectFile(event.target.files?.[0])} ref={inputRef} type="file" />
            <label htmlFor="source-file"><span aria-hidden="true" className="upload-glyph">⇧</span><strong>{selected ? selected.file.name : "选择文件或拖放到这里"}</strong><span id="file-guidance">{selected ? `${fileKindLabels[selected.kind]} · ${formatFileSize(selected.file.size)}` : "扩展名和浏览器报告的 MIME 必须一致；浏览器 MIME 为空时按扩展名申报，空文件不会上传。"}</span></label>
          </div>
          {selectionError ? <p className="inline-error" id="file-error" role="alert">{selectionError}</p> : null}
          {selected ? <div className="file-declaration" aria-label="待导入文件信息"><div><span>来源标题</span><strong>{sourceTitleFromFilename(selected.file.name)}</strong></div><div><span>声明类型</span><strong>{selected.mediaType}</strong></div><div><span>文件大小</span><strong>{formatFileSize(selected.file.size)}</strong></div></div> : null}
          {phase !== "idle" ? <div aria-live="polite" className="import-progress"><div className="progress-copy"><strong>{phaseLabels[phase]}</strong><span>{phase === "uploading" ? `${uploadProgress}%` : "请勿关闭页面"}</span></div>{phase === "uploading" ? <progress aria-label="文件上传进度" max="100" value={uploadProgress}>{uploadProgress}%</progress> : <div aria-hidden="true" className="indeterminate-track"><span /></div>}<button className="text-button cancel-import" onClick={cancelImport} type="button">取消导入</button></div> : null}
          {flowError ? <p className="inline-error flow-error" role="alert">{flowError}</p> : null}
          {jobQuery.isError ? <p className="inline-error flow-error" role="alert">无法刷新任务状态。系统会保留已保存的原件；请重试查询。<button className="text-button" onClick={() => jobQuery.refetch()} type="button">重试查询</button></p> : null}
          {currentUpload ? <div className="current-upload" aria-label="本页当前上传"><p className="state-kicker">CURRENT UPLOAD · 本页记录</p><div><span>来源</span><strong>{currentUpload.source.title}</strong></div><div><span>原始文件</span><strong>{currentUpload.filename}</strong></div><div><span>任务</span><strong>{currentJob?.status ?? currentUpload.job.status}</strong></div><small>版本 {currentUpload.versionId} · 任务 {currentUpload.job.id}</small></div> : null}
          {currentJob ? <div className={`job-state job-${currentJob.status}`} aria-live="polite"><p className="state-kicker">JOB · {currentJob.status.toUpperCase()}</p>{currentJob.status === "succeeded" ? <><h3>原件已保存，等待 D3 解析</h3><p>文件完整性校验和入库任务已完成。本页面不会展示伪造的解析结果。</p></> : null}{currentJob.status === "failed" ? <><h3>入库任务失败</h3><p>{currentJob.error_message ?? "任务未能完成，原始失败信息未提供。"}</p>{currentJob.retryable ? <button className="button" disabled={isRetrying} onClick={handleRetryJob} type="button">{isRetrying ? "正在重试…" : "重试失败任务"}</button> : null}</> : null}{currentJob.status === "cancelled" ? <><h3>任务已取消</h3><p>服务端未继续处理这个已知任务；来源字段请查看下方最近来源记录。</p></> : null}{isPollingJob(currentJob) ? <><h3>{currentJob.status === "queued" ? "原件已保存，任务正在排队" : "正在确认原件入库"}</h3><progress aria-label="入库任务进度" max="100" value={currentJob.progress}>{currentJob.progress}%</progress></> : null}</div> : null}
          <div className="form-actions"><button className="button primary" disabled={!selected || isBusy} onClick={startImport} type="button">{isBusy ? "正在导入…" : canResume ? "继续导入" : "校验并导入"}</button>{selected && !isBusy ? <button className="button" onClick={resetSelection} type="button">选择其他文件</button> : null}</div>
        </section>
        <aside className="intake-notes" aria-label="D2 导入说明"><div className="note-block active-note"><span>当前边界</span><strong>原件保存与任务追踪</strong><p>浏览器先校验文件并计算 SHA-256，再直传到预留地址，服务端会复核大小、类型、签名与摘要。</p></div><div className="note-block future-note"><span>D3 继续</span><strong>解析与内容提取</strong><p>本阶段不生成页数、段落、摘要或知识点，也不会用占位数据冒充解析结果。</p></div></aside>
      </div>
      <RecentImports isError={sourcesQuery.isError} isPending={sourcesQuery.isPending} onRetry={() => void sourcesQuery.refetch()} sources={sourcesQuery.data} />
    </section>
  );
}
