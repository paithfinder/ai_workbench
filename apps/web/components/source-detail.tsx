"use client";

import Link from "next/link";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { ApiError, createIdempotencyKey } from "@/lib/api";
import { getExtraction, scheduleExtraction } from "@/lib/extraction";
import { fetchBootstrap } from "@/lib/bootstrap";
import {
  getJob,
  getSourceDetails,
  listSourceSections,
  reparseSourceVersion,
  retryJob,
  type ParseArtifact,
  type SourceSection,
  type SourceVersionDetail,
} from "@/lib/sources";
import { PageHeader } from "./page-states";

const sourceKindLabels = { pdf: "PDF", markdown: "Markdown", text: "纯文本", web: "网页", pasted_text: "粘贴文本" } as const;
const parseStatusLabels = { not_started: "尚未解析", queued: "等待解析", parsing: "正在解析", ready: "解析完成", failed: "解析失败" } as const;
const acquisitionLabels = { upload: "文件上传", pasted_text: "粘贴文本", web_fetch: "网页快照" } as const;

function errorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "请求失败，请稍后重试";
}
function isUncertainRequestFailure(error: unknown) { return error instanceof ApiError && (error.status === 0 || error.status >= 500); }
function shouldPoll(versions: SourceVersionDetail[] | undefined) { return versions?.some(({ version }) => version.parse_status === "queued" || version.parse_status === "parsing") ?? false; }
function formatDate(value: string | null) { return value ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "—"; }

function Locator({ section }: { section: SourceSection }) {
  const parts = [
    section.page_number ? `第 ${section.page_number} 页` : null,
    section.heading_path.length ? section.heading_path.join(" / ") : null,
    section.paragraph_index !== null ? `段落 ${section.paragraph_index + 1}` : null,
  ].filter(Boolean);
  return (
    <details className="locator-details">
      <summary>{parts.length ? parts.join(" · ") : "结构定位"}</summary>
      <dl><div><dt>Locator type</dt><dd>{String(section.locator.locatorType ?? "未提供")}</dd></div><div><dt>Artifact revision</dt><dd>{section.artifact_revision}</dd></div><div><dt>Section ID</dt><dd>{section.id}</dd></div><div><dt>Quote hash</dt><dd>{section.quote_hash}</dd></div>{section.bbox ? <div><dt>Bounding box</dt><dd><code>{JSON.stringify(section.bbox)}</code></dd></div> : null}</dl>
    </details>
  );
}

function ArtifactSummary({ artifact }: { artifact: ParseArtifact }) {
  return <div className="artifact-summary"><span>解析器 <strong>{artifact.parser_name} {artifact.parser_version}</strong></span><span>Revision <strong>{artifact.revision}</strong></span><span>页数 <strong>{artifact.page_count ?? "未提供"}</strong></span>{artifact.warnings.length ? <span className="artifact-warning">警告 <strong>{artifact.warnings.length}</strong></span> : null}</div>;
}

function Sections({ spaceId, sourceId, version }: { spaceId: string; sourceId: string; version: SourceVersionDetail }) {
  const sectionsQuery = useInfiniteQuery({
    queryKey: ["source-sections", spaceId, sourceId, version.version.id, version.current_parse_artifact?.id],
    queryFn: ({ pageParam, signal }) => listSourceSections(spaceId, sourceId, version.version.id, { cursor: pageParam, signal }),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: version.version.parse_status === "ready" && Boolean(version.current_parse_artifact),
  });
  const sections = sectionsQuery.data?.pages.flatMap((page) => page.items) ?? [];
  if (version.version.parse_status !== "ready") return null;
  if (!version.current_parse_artifact) return <p className="inline-error" role="alert">版本标记为解析完成，但接口没有返回当前解析产物。页面不会猜测分段内容。</p>;
  if (sectionsQuery.isPending) return <p aria-live="polite" className="muted-message">正在读取可引用分段…</p>;
  if (sectionsQuery.isError) return <div className="inline-error" role="alert">无法读取当前解析产物的分段。<button className="text-button" onClick={() => sectionsQuery.refetch()} type="button">重新加载</button></div>;
  return <section aria-labelledby={`sections-${version.version.id}`} className="sections-panel"><div className="panel-heading"><div><p className="state-kicker">CANONICAL SECTIONS</p><h3 id={`sections-${version.version.id}`}>可引用分段</h3><p className="panel-caption">已读取 {sections.length} / {version.section_count} 个 section；每条引用冻结到当前 artifact revision。</p></div></div><ArtifactSummary artifact={sectionsQuery.data?.pages[0]?.artifact ?? version.current_parse_artifact} />{sections.length === 0 ? <div className="source-empty"><strong>解析产物没有分段</strong><p>这是服务端返回的真实空结果。</p></div> : <ol className="section-list">{sections.map((section) => <li className="section-card" key={section.id}><div className="section-heading"><span>{String(section.ordinal + 1).padStart(2, "0")}</span><div><small>{section.block_type}</small><h4>{section.title ?? section.heading_path.at(-1) ?? "正文"}</h4></div></div><p className="section-text">{section.text}</p><Locator section={section} /></li>)}</ol>}{sectionsQuery.hasNextPage ? <button className="button load-more" disabled={sectionsQuery.isFetchingNextPage} onClick={() => sectionsQuery.fetchNextPage()} type="button">{sectionsQuery.isFetchingNextPage ? "正在加载…" : "加载更多分段"}</button> : sections.length ? <p className="pagination-end">已显示当前解析产物的全部分段</p> : null}</section>;
}

function VersionCard({ spaceId, sourceId, detail, latest, extractionEnabled }: { spaceId: string; sourceId: string; detail: SourceVersionDetail; latest: boolean; extractionEnabled: boolean }) {
  const queryClient = useQueryClient();
  const reparseKeyRef = useRef<string | null>(null);
  const extractionKeyRef = useRef<string | null>(null);
  const extractionRetryKeyRef = useRef<{ jobId: string; key: string } | null>(null);
  const [isReparsing, setIsReparsing] = useState(false);
  const [isExtracting, setIsExtracting] = useState(false);
  const [extractionJobId, setExtractionJobId] = useState<string | null>(null);
  const [reparseError, setReparseError] = useState<string | null>(null);
  const [extractionError, setExtractionError] = useState<string | null>(null);
  const beforeRef = useRef<{ artifactId: string | null; jobId: string | null } | null>(null);
  const version = detail.version;
  const parseStatus = version.parse_status;
  const extractionQuery = useQuery({
    queryKey: ["source-extraction", spaceId, sourceId, version.id],
    queryFn: ({ signal }) => getExtraction(spaceId, sourceId, version.id, { signal }),
    enabled: extractionEnabled && parseStatus === "ready" && detail.section_count > 0,
    retry: (count, error) => error instanceof ApiError && error.status === 404 ? false : count < 2,
    refetchInterval: (query) => {
      const status = query.state.data?.job.status;
      return status === "queued" || status === "running" ? 1_000 : false;
    },
  });
  const extractionJobQuery = useQuery({
    queryKey: ["job", spaceId, extractionJobId],
    queryFn: ({ signal }) => getJob(spaceId, extractionJobId!, { signal }),
    enabled: Boolean(extractionJobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "running" ? 1_000 : false;
    },
  });
  const extractionRun = extractionJobQuery.data ?? extractionQuery.data?.job;

  async function reparse() {
    if (isReparsing) return;
    setIsReparsing(true); setReparseError(null);
    const key = reparseKeyRef.current ?? createIdempotencyKey(); reparseKeyRef.current = key;
    beforeRef.current ??= { artifactId: detail.current_parse_artifact?.id ?? null, jobId: detail.parse_job?.id ?? null };
    try {
      await reparseSourceVersion(spaceId, sourceId, version.id, key);
      reparseKeyRef.current = null; beforeRef.current = null;
      await queryClient.invalidateQueries({ queryKey: ["source-details", spaceId, sourceId] });
      await queryClient.invalidateQueries({ queryKey: ["source-sections", spaceId, sourceId, version.id] });
    } catch (error) {
      if (isUncertainRequestFailure(error)) {
        const reconciled = await queryClient.fetchQuery({ queryKey: ["source-details", spaceId, sourceId], queryFn: ({ signal }) => getSourceDetails(spaceId, sourceId, { signal }), staleTime: 0 });
        const current = reconciled.versions.find((item) => item.version.id === version.id);
        const before = beforeRef.current;
        if (current && before && ((current.current_parse_artifact?.id ?? null) !== before.artifactId || (current.parse_job?.id ?? null) !== before.jobId || current.version.parse_status === "queued" || current.version.parse_status === "parsing")) {
          reparseKeyRef.current = null; beforeRef.current = null;
          await queryClient.invalidateQueries({ queryKey: ["source-sections", spaceId, sourceId, version.id] });
          return;
        }
      }
      setReparseError(errorMessage(error));
    } finally { setIsReparsing(false); }
  }

  async function extract() {
    if (isExtracting || parseStatus !== "ready" || detail.section_count === 0) return;
    setIsExtracting(true); setExtractionError(null);
    const key = extractionKeyRef.current ?? createIdempotencyKey(); extractionKeyRef.current = key;
    try {
      const scheduled = await scheduleExtraction(spaceId, sourceId, version.id, key);
      extractionKeyRef.current = null; setExtractionJobId(scheduled.job.id);
      queryClient.setQueryData(
        ["source-extraction", spaceId, sourceId, version.id],
        scheduled,
      );
      await queryClient.invalidateQueries({ queryKey: ["extractions", spaceId] });
      await queryClient.invalidateQueries({ queryKey: ["candidates", spaceId] });
    } catch (error) {
      if (isUncertainRequestFailure(error)) {
        try {
          const reconciled = await queryClient.fetchQuery({
            queryKey: ["source-extraction", spaceId, sourceId, version.id],
            queryFn: ({ signal }) => getExtraction(spaceId, sourceId, version.id, { signal }),
            staleTime: 0,
          });
          extractionKeyRef.current = null;
          setExtractionJobId(reconciled.job.id);
          return;
        } catch {
          // Keep the operation key so a user retry remains idempotent.
        }
      }
      setExtractionError(errorMessage(error));
    }
    finally { setIsExtracting(false); }
  }

  async function retryExtraction() {
    if (!extractionRun?.retryable || isExtracting) return;
    setIsExtracting(true); setExtractionError(null);
    const operation = extractionRetryKeyRef.current?.jobId === extractionRun.id
      ? extractionRetryKeyRef.current
      : { jobId: extractionRun.id, key: createIdempotencyKey() };
    extractionRetryKeyRef.current = operation;
    try {
      const retried = await retryJob(
        spaceId,
        extractionRun.id,
        operation.key,
      );
      extractionRetryKeyRef.current = null;
      setExtractionJobId(retried.id);
      queryClient.setQueryData(["job", spaceId, retried.id], retried);
      await queryClient.invalidateQueries({
        queryKey: ["source-extraction", spaceId, sourceId, version.id],
      });
    } catch (error) {
      if (isUncertainRequestFailure(error) || (error instanceof ApiError && error.status === 409)) {
        try {
          const reconciled = await queryClient.fetchQuery({
            queryKey: ["job", spaceId, extractionRun.id],
            queryFn: ({ signal }) => getJob(spaceId, extractionRun.id, { signal }),
            staleTime: 0,
          });
          if (reconciled.status !== "failed" || !reconciled.retryable) {
            extractionRetryKeyRef.current = null;
            setExtractionJobId(reconciled.id);
            await queryClient.invalidateQueries({
              queryKey: ["source-extraction", spaceId, sourceId, version.id],
            });
            return;
          }
        } catch {
          // Keep the operation key so a user retry remains idempotent.
        }
      } else {
        extractionRetryKeyRef.current = null;
      }
      setExtractionError(errorMessage(error));
    } finally {
      setIsExtracting(false);
    }
  }

  return <article aria-labelledby={`version-${version.id}`} className={`version-card parse-${parseStatus}`}><div className="version-heading"><div><p className="state-kicker">VERSION {version.version_number}{latest ? " · LATEST" : ""}</p><h2 id={`version-${version.id}`}>{version.original_filename ?? version.source_uri ?? acquisitionLabels[version.acquisition_type]}</h2></div><span className={`parse-badge parse-${parseStatus}`}>{parseStatusLabels[parseStatus]}</span></div><dl className="version-facts"><div><dt>获取方式</dt><dd>{acquisitionLabels[version.acquisition_type]}</dd></div><div><dt>内容类型</dt><dd>{version.media_type ?? "未提供"}</dd></div><div><dt>原件大小</dt><dd>{version.size_bytes === null ? "未提供" : `${version.size_bytes.toLocaleString("zh-CN")} bytes`}</dd></div><div><dt>完成时间</dt><dd>{formatDate(version.completed_at)}</dd></div><div><dt>分段数量</dt><dd>{detail.section_count}</dd></div><div><dt>解析任务</dt><dd>{detail.parse_job ? `${detail.parse_job.status} · ${detail.parse_job.id}` : "暂无"}</dd></div></dl>{parseStatus === "queued" || parseStatus === "parsing" ? <div aria-live="polite" className="parse-state"><div><strong>{parseStatusLabels[parseStatus]}</strong><span>{detail.parse_job ? `任务进度 ${detail.parse_job.progress}%` : "正在等待服务端状态更新"}</span></div><progress aria-label="解析任务进度" max="100" value={detail.parse_job?.progress ?? 0}>{detail.parse_job?.progress ?? 0}%</progress></div> : null}{parseStatus === "failed" ? <div className="inline-error parse-error" role="alert"><strong>解析未完成</strong><p>{detail.current_parse_artifact?.error_message ?? detail.parse_job?.error_message ?? "服务端没有提供失败说明。"}</p></div> : null}{detail.current_parse_artifact ? <ArtifactSummary artifact={detail.current_parse_artifact} /> : null}<div className="version-actions"><button className="button" disabled={isReparsing || parseStatus === "queued" || parseStatus === "parsing"} onClick={reparse} type="button">{isReparsing ? "正在确认重新解析…" : "重新解析此版本"}</button>{extractionEnabled && parseStatus === "ready" && detail.section_count > 0 && !extractionRun ? <button className="button primary" disabled={isExtracting} onClick={extract} type="button">{isExtracting ? "正在创建提炼任务…" : "启动 AI 提炼"}</button> : null}<small>提炼任务只读取当前可信 parse revision；候选完成后进入 D4 队列，不会直接写入知识树。</small></div>{extractionRun ? <div className="job-state" role="status"><strong>{extractionRun.status === "succeeded" ? "AI 提炼已完成" : extractionRun.status === "failed" ? "AI 提炼失败" : "AI 提炼进行中"}</strong><p>Job {extractionRun.id} · {extractionRun.status} · {extractionRun.progress}%</p>{extractionRun.status === "queued" || extractionRun.status === "running" ? <progress aria-label="AI 提炼任务进度" max="100" value={extractionRun.progress}>{extractionRun.progress}%</progress> : null}{extractionRun.error_message ? <p>{extractionRun.error_message}</p> : null}{extractionRun.retryable ? <button className="button" disabled={isExtracting} onClick={retryExtraction} type="button">{isExtracting ? "正在重试…" : "重试 AI 提炼"}</button> : null}{extractionRun.status === "succeeded" ? <Link className="button" href="/extraction">打开候选队列</Link> : null}</div> : null}{extractionError ? <p className="inline-error" role="alert">{extractionError}</p> : null}{reparseError ? <p className="inline-error" role="alert">{reparseError}</p> : null}<Sections sourceId={sourceId} spaceId={spaceId} version={detail} /></article>;
}

export function SourceDetail({ sourceId }: { sourceId: string }) {
  const bootstrapQuery = useQuery({ queryKey: ["bootstrap"], queryFn: fetchBootstrap });
  const spaceId = bootstrapQuery.data?.space.id;
  const detailsQuery = useQuery({ queryKey: ["source-details", spaceId, sourceId], queryFn: ({ signal }) => getSourceDetails(spaceId!, sourceId, { signal }), enabled: Boolean(spaceId), refetchInterval: (query) => shouldPoll(query.state.data?.versions) ? 1_000 : false });
  if (bootstrapQuery.isPending || (spaceId && detailsQuery.isPending)) return <section aria-live="polite"><PageHeader eyebrow="SOURCE · D3" title="来源详情" description="正在读取真实版本与解析状态…" /><div className="panel-card loading-panel">正在加载来源详情…</div></section>;
  if (bootstrapQuery.isError || !spaceId) return <section><PageHeader eyebrow="SOURCE · D3" title="来源详情" description="需要先连接默认个人知识空间。" /><div className="state-card error-state" role="alert"><h2>无法取得默认知识空间</h2><button className="button primary" onClick={() => bootstrapQuery.refetch()} type="button">重试连接</button></div></section>;
  if (detailsQuery.isError || !detailsQuery.data) return <section><PageHeader eyebrow="SOURCE · D3" title="来源详情" description="页面不会用缓存或示例数据替代失败的详情请求。" /><div className="state-card error-state" role="alert"><div><p className="state-kicker">SOURCE DETAILS · UNAVAILABLE</p><h2>无法读取来源详情</h2><p>{errorMessage(detailsQuery.error)}</p></div><div className="form-actions"><button className="button primary" onClick={() => detailsQuery.refetch()} type="button">重新加载</button><Link className="button" href="/import">返回导入</Link></div></div></section>;
  const { source, versions } = detailsQuery.data;
  return <section aria-labelledby="source-title"><Link className="back-link" href="/import">← 返回来源列表</Link><PageHeader eyebrow={`SOURCE · ${sourceKindLabels[source.kind]} · ${source.status.toUpperCase()}`} headingId="source-title" title={source.title} description={`共 ${versions.length} 个不可变版本。解析状态、产物与 section 均来自来源详情 API。`} />{versions.length === 0 ? <div className="source-empty panel-card"><strong>这个来源还没有版本</strong><p>页面不会为尚未创建的版本生成占位内容。</p></div> : <div className="version-list">{versions.map((version, index) => <VersionCard detail={version} extractionEnabled={bootstrapQuery.data.capabilities.extraction_review} key={version.version.id} latest={index === 0} sourceId={sourceId} spaceId={spaceId} />)}</div>}</section>;
}
