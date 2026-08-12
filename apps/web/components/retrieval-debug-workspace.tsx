"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type CSSProperties, type FormEvent } from "react";
import { ApiError, createIdempotencyKey } from "@/lib/api";
import { fetchBootstrap } from "@/lib/bootstrap";
import { getKnowledgeTree, type KnowledgeNode } from "@/lib/knowledge";
import {
  debugRetrievalSearch,
  getRetrievalIndexStatus,
  previewRetrievalScope,
  rebuildRetrievalIndex,
  type RetrievalHit,
  type RetrievalScopeSummary,
} from "@/lib/retrieval";
import { PageHeader } from "./page-states";

function errorMessage(error: unknown) {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "请求失败，请稍后重试";
}

function nodeLabel(node: KnowledgeNode) {
  return node.title ?? ({ root: "知识库", folder: "未命名目录", document: "未命名文档", point: "未命名知识点", source: "未命名来源" } as const)[node.kind];
}

function scopeBreadcrumb(nodes: KnowledgeNode[], nodeId: string) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const labels: string[] = [];
  let current = byId.get(nodeId);
  const seen = new Set<string>();
  while (current && !seen.has(current.id)) {
    seen.add(current.id);
    labels.unshift(nodeLabel(current));
    current = current.parent_id ? byId.get(current.parent_id) : undefined;
  }
  return labels.join(" / ");
}

function ScopeTree({ nodes, selectedId, onSelect }: { nodes: KnowledgeNode[]; selectedId: string; onSelect: (nodeId: string) => void }) {
  const children = useMemo(() => {
    const result = new Map<string | null, KnowledgeNode[]>();
    for (const node of nodes) result.set(node.parent_id, [...(result.get(node.parent_id) ?? []), node]);
    for (const siblings of result.values()) siblings.sort((left, right) => left.sort_order - right.sort_order);
    return result;
  }, [nodes]);

  const renderLevel = (parentId: string | null, level: number) => {
    const items = children.get(parentId) ?? [];
    if (!items.length) return null;
    return <ul role={parentId === null ? "tree" : "group"} aria-label={parentId === null ? "检索范围" : undefined}>{items.map((node) => <li key={node.id} role="none">
      <button
        aria-selected={selectedId === node.id}
        className="scope-tree-item"
        onClick={() => onSelect(node.id)}
        role="treeitem"
        style={{ "--scope-depth": level } as CSSProperties}
        type="button"
      >
        <span aria-hidden="true" className={`scope-node-mark scope-${node.kind}`}>{node.kind === "root" ? "根" : node.kind === "folder" ? "目" : node.kind === "document" ? "文" : node.kind === "point" ? "点" : "源"}</span>
        <span><strong>{nodeLabel(node)}</strong><small>{node.kind.toUpperCase()}</small></span>
        <i aria-hidden="true">{selectedId === node.id ? "●" : "○"}</i>
      </button>
      {renderLevel(node.id, level + 1)}
    </li>)}</ul>;
  };

  return renderLevel(null, 0);
}

function ScopeSummary({ summary, breadcrumb, includeDescendants, pending }: {
  summary?: RetrievalScopeSummary;
  breadcrumb: string;
  includeDescendants: boolean;
  pending: boolean;
}) {
  const counts = summary ? [
    ["knowledge", summary.knowledge_count],
    ["sources", summary.source_count],
    ["versions", summary.source_version_count],
    ["chunks", summary.chunk_count],
  ] as const : [];
  return <section aria-labelledby="scope-summary-title" className="scope-summary panel-card">
    <div className="scope-summary-copy">
      <p className="state-kicker">SCOPE SUMMARY · {summary?.index_status ?? "loading"}</p>
      <h2 id="scope-summary-title">当前检索边界</h2>
      <p>{summary?.scope_path ?? (breadcrumb || "正在确认知识路径…")}</p>
      <span>{includeDescendants ? "包含所选节点及全部下级" : "仅检索所选节点"}{summary?.index_config_version ? ` · ${summary.index_config_version}` : ""}</span>
    </div>
    <dl aria-busy={pending}>{pending ? <div><dt>范围</dt><dd>…</dd></div> : counts.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
  </section>;
}

function IndexPanel({ spaceId }: { spaceId: string }) {
  const queryClient = useQueryClient();
  const operationKey = useRef<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const statusQuery = useQuery({ queryKey: ["retrieval-index-status", spaceId], queryFn: ({ signal }) => getRetrievalIndexStatus(spaceId, { signal }) });
  const rebuild = useMutation({
    mutationFn: async () => {
      operationKey.current ??= createIdempotencyKey();
      return rebuildRetrievalIndex(spaceId, operationKey.current);
    },
    onSuccess: async (result) => {
      operationKey.current = null;
      setFeedback(result.message);
      await queryClient.invalidateQueries({ queryKey: ["retrieval-index-status", spaceId] });
    },
    onError: (error) => setFeedback(errorMessage(error)),
  });
  const status = statusQuery.data;
  return <section aria-labelledby="index-title" className="index-card panel-card">
    <div className="index-heading"><div><p className="state-kicker">INDEX STATUS</p><h2 id="index-title">检索索引</h2></div><span className={`index-state state-${status?.status ?? "loading"}`}>{status?.status ?? (statusQuery.isError ? "unavailable" : "loading")}</span></div>
    {status ? <dl><div><dt>索引版本</dt><dd>{status.index_version ?? "尚未建立"}</dd></div><div><dt>已索引 / 待处理</dt><dd>{status.indexed_chunks} / {status.pending_chunks}</dd></div><div><dt>FTS</dt><dd>{status.keyword.status} · {status.keyword.indexed_chunks}</dd></div><div><dt>Vector</dt><dd>{status.vector.status} · {status.vector.indexed_chunks}</dd></div></dl> : null}
    {statusQuery.isError ? <p className="inline-error" role="alert">{errorMessage(statusQuery.error)}</p> : null}
    <button className="button" disabled={rebuild.isPending} onClick={() => { setFeedback(null); rebuild.mutate(); }} type="button">{rebuild.isPending ? "正在请求重建…" : "重建检索索引"}</button>
    {feedback ? <p aria-live="polite" className={rebuild.isError ? "index-feedback is-error" : "index-feedback"}>{feedback}</p> : null}
  </section>;
}

function ResultLane({ channel, error, hits, timing }: { channel: "FTS" | "Vector"; error?: string | null; hits: RetrievalHit[]; timing?: number }) {
  return <section aria-labelledby={`lane-${channel}`} className={`result-lane lane-${channel.toLowerCase()}`}>
    <header><div><p>{channel === "FTS" ? "KEYWORD TRACK" : "SEMANTIC TRACK"}</p><h3 id={`lane-${channel}`}>{channel} 召回</h3></div><span>{hits.length} hits{timing === undefined ? "" : ` · ${timing.toFixed(1)} ms`}</span></header>
    {error ? <p className="inline-error" role="alert">{error}</p> : null}
    {!error && hits.length === 0 ? <p className="lane-empty">当前范围没有返回结果。</p> : null}
    <ol>{hits.map((hit) => <li key={`${channel}-${hit.chunk_id}`}>
      <div className="result-rank"><span>RANK</span><strong>{String(hit.rank).padStart(2, "0")}</strong></div>
      <article>
        <div className="result-score"><span>{hit.score_semantics}</span><strong>{hit.raw_score.toPrecision(5)}</strong></div>
        <p>{hit.snippet || "该片段没有可显示的摘要。"}</p>
        <dl><div><dt>path</dt><dd>{hit.knowledge_path.join(" / ") || "未关联知识路径"}</dd></div><div><dt>source</dt><dd>{hit.source_title ?? hit.source_id ?? "无来源记录"}</dd></div><div><dt>identity</dt><dd>{hit.content_identity}</dd></div></dl>
        {hit.deep_link ? <Link className="result-link" href={hit.deep_link}>在来源中查看 <span aria-hidden="true">↗</span></Link> : <span className="result-link is-disabled">无来源深链</span>}
      </article>
    </li>)}</ol>
  </section>;
}

export function RetrievalDebugWorkspace() {
  const searchController = useRef<AbortController | null>(null);
  const [searchResult, setSearchResult] = useState<Awaited<ReturnType<typeof debugRetrievalSearch>> | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [includeDescendants, setIncludeDescendants] = useState(true);
  const [query, setQuery] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);
  const bootstrapQuery = useQuery({ queryKey: ["bootstrap"], queryFn: fetchBootstrap });
  const spaceId = bootstrapQuery.data?.space.id;
  const treeQuery = useQuery({ queryKey: ["knowledge-tree", spaceId], queryFn: ({ signal }) => getKnowledgeTree(spaceId!, { signal }), enabled: Boolean(spaceId) });
  const nodes = treeQuery.data ?? [];
  const selectedNodeId = selectedId || nodes.find((node) => node.kind === "root")?.id || nodes[0]?.id || "";

  const scopeQuery = useQuery({
    queryKey: ["retrieval-scope", spaceId, selectedNodeId, includeDescendants],
    queryFn: ({ signal }) => previewRetrievalScope(spaceId!, { scopeNodeId: selectedNodeId, includeDescendants }, { signal }),
    enabled: Boolean(spaceId && selectedNodeId && bootstrapQuery.data?.capabilities.retrieval_debug),
  });
  const search = useMutation({
    mutationFn: async (searchQuery: string) => {
      searchController.current?.abort();
      const controller = new AbortController();
      searchController.current = controller;
      return debugRetrievalSearch(spaceId!, { query: searchQuery, scopeNodeId: selectedNodeId, includeDescendants, topK: 5 }, { signal: controller.signal });
    },
    onSuccess: (response) => {
      searchController.current = null;
      if (response.scope_summary.scope_node_id === selectedNodeId && response.scope_summary.include_descendants === includeDescendants) setSearchResult(response);
    },
    onError: (error) => { if (!(error instanceof DOMException && error.name === "AbortError")) searchController.current = null; },
  });
  useEffect(() => () => searchController.current?.abort(), []);
  const resetSearch = () => { searchController.current?.abort(); searchController.current = null; setSearchResult(null); search.reset(); };
  const breadcrumb = selectedNodeId ? scopeBreadcrumb(nodes, selectedNodeId) : "";

  function submit(event: FormEvent) {
    event.preventDefault();
    const normalized = query.trim();
    if (!normalized) { setValidationError("请输入要验证的检索语句"); return; }
    setValidationError(null);
    search.mutate(normalized);
  }

  const pageHeader = (description: string) => <PageHeader eyebrow="RETRIEVAL LAB · D7" headingId="retrieval-title" title="范围检索工作台" description={description} />;
  if (bootstrapQuery.isPending) return <section aria-live="polite">{pageHeader("正在连接默认知识空间…")}<div className="panel-card loading-panel">正在读取检索能力…</div></section>;
  if (bootstrapQuery.isError || !spaceId) return <section>{pageHeader("先连接默认知识空间，再检查真实召回。")}<div className="state-card error-state" role="alert"><div><h2>无法连接检索工作台</h2><p>{errorMessage(bootstrapQuery.error)}</p></div><button className="button primary" onClick={() => bootstrapQuery.refetch()} type="button">重新连接</button></div></section>;
  if (!bootstrapQuery.data.capabilities.retrieval_debug) return <section aria-labelledby="retrieval-title">{pageHeader("当前知识空间尚未开放检索调试能力。")}<div className="state-card"><div><p className="state-kicker">CAPABILITY · RETRIEVAL DEBUG OFF</p><h2>范围检索尚未开放</h2><p>页面不会发送调试查询或展示模拟结果。</p></div></div></section>;
  if (treeQuery.isPending) return <section aria-live="polite">{pageHeader("正在载入可选知识范围…")}<div className="panel-card loading-panel">正在读取知识树…</div></section>;
  if (treeQuery.isError || !nodes.length) return <section>{pageHeader("使用知识树定义每次检索的边界。")}<div className="state-card error-state" role="alert"><div><h2>{treeQuery.isError ? "无法读取知识树" : "知识树为空"}</h2><p>{treeQuery.isError ? errorMessage(treeQuery.error) : "先建立知识节点，再验证范围召回。"}</p></div>{treeQuery.isError ? <button className="button primary" onClick={() => treeQuery.refetch()} type="button">重新加载</button> : null}</div></section>;

  const result = searchResult;
  return <section aria-labelledby="retrieval-title">
    {pageHeader(`在「${bootstrapQuery.data.space.name}」中限定知识范围，对照 FTS 与 Vector 的原始召回；D7 只验证检索，不生成回答。`)}
    <div className="retrieval-boundary" role="note"><strong>D7 · RETRIEVAL ONLY</strong><span>这里不会生成、续写或总结答案。所有输出都是索引中的原始召回诊断。</span></div>
    <div className="retrieval-grid">
      <aside aria-labelledby="scope-tree-title" className="scope-panel panel-card"><div className="retrieval-panel-head"><p className="state-kicker">BOUNDARY</p><h2 id="scope-tree-title">选择知识范围</h2><p>选择树节点决定允许进入召回池的内容。</p></div><div className="scope-tree"><ScopeTree nodes={nodes} onSelect={(nodeId) => { setSelectedId(nodeId); resetSearch(); }} selectedId={selectedNodeId} /></div><label className="descendant-toggle"><input checked={includeDescendants} onChange={(event) => { setIncludeDescendants(event.target.checked); resetSearch(); }} type="checkbox" /><span><strong>包含全部下级</strong><small>关闭后仅检索所选节点</small></span></label></aside>
      <main className="retrieval-main">
        <ScopeSummary breadcrumb={breadcrumb} includeDescendants={includeDescendants} pending={scopeQuery.isPending} summary={scopeQuery.data?.scope_summary} />
        {scopeQuery.isError ? <p className="inline-error" role="alert">范围预览失败：{errorMessage(scopeQuery.error)}</p> : null}
        <form className="retrieval-composer panel-card" onSubmit={submit}><label htmlFor="retrieval-query"><span>检索语句</span><strong>同时检查关键词与语义召回</strong></label><div><input aria-describedby="retrieval-guidance" aria-invalid={Boolean(validationError)} id="retrieval-query" onChange={(event) => { setQuery(event.target.value); setValidationError(null); }} placeholder="例如：如何保存可追溯的来源引用？" type="search" value={query} /><button className="button primary" disabled={search.isPending || scopeQuery.isError} type="submit">{search.isPending ? "正在检索…" : "运行双路检索"}</button></div><p id="retrieval-guidance">固定返回前 5 条原始结果，不融合、不重排、不生成回答。</p>{validationError ? <p className="field-error" role="alert">{validationError}</p> : null}</form>
        {search.isError ? <p className="inline-error retrieval-error" role="alert">{errorMessage(search.error)}</p> : null}
        {result ? <div aria-live="polite" className="retrieval-results"><div className="results-heading"><div><p className="state-kicker">RAW RECALL · “{result.query}”</p><h2>双路召回对照</h2></div>{result.embedding ? <span>{result.embedding.provider} / {result.embedding.model} / {result.embedding.dimensions}d</span> : <span>Vector embedding unavailable</span>}</div><div className="result-lanes"><ResultLane channel="FTS" error={result.channel_errors.keyword} hits={result.keyword_hits} timing={result.timings_ms.keyword} /><ResultLane channel="Vector" error={result.channel_errors.vector} hits={result.vector_hits} timing={result.timings_ms.vector} /></div></div> : null}
      </main>
      <aside className="retrieval-status"><IndexPanel spaceId={spaceId} /></aside>
    </div>
  </section>;
}
