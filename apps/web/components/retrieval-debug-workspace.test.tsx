import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RetrievalDebugWorkspace } from "./retrieval-debug-workspace";

const ids = { space: "102ee035-406f-41a6-b46b-d6c1d4e80d11", root: "77881bd1-0c4a-4cdb-b0d8-7ed52d25273f", folder: "a9a6e120-59b4-4ebc-a9b3-b4b81c7ee206", chunk: "c90d99d3-4080-4918-aae6-5d7dc59289c9", source: "247197b5-4e02-4442-a0a6-280151242198", version: "3f27ab12-8593-44b8-afc0-b5d3a1f76ab4", artifact: "c3e1d289-31c1-444d-bda0-b5a4ea165142", section: "f21b429d-c60d-4f0b-85ba-d2ac82a2eb70", rebuild: "e44c926f-399b-4087-a727-e9e5c2a6b112" };
const now = "2026-08-10T08:00:00+00:00";
const bootstrap = { space: { id: ids.space, slug: "mine", name: "我的知识库" }, capabilities: { source_import: true, extraction_review: true, knowledge_tree: true, retrieval_debug: true, trusted_qa: false, spaced_review: false, evidence_agent: false }, statistics: { sources: 1, queued_jobs: 0, activity_events: 0 }, foundation_status: "ready" };
const nodes = [
  { id: ids.root, space_id: ids.space, parent_id: null, kind: "root", path: "root", version: 1, sort_order: 0, origin_candidate_id: null, current_revision_id: null, source_id: null, source_version_id: null, title: "我的知识库", created_at: now, updated_at: now, deleted_at: null },
  { id: ids.folder, space_id: ids.space, parent_id: ids.root, kind: "folder", path: "root.folder", version: 1, sort_order: 0, origin_candidate_id: null, current_revision_id: null, source_id: null, source_version_id: null, title: "研究", created_at: now, updated_at: now, deleted_at: null },
];
function scope(nodeId = ids.root, includeDescendants = true) {
  return { scope_node_id: nodeId, include_descendants: includeDescendants, scope_snapshot_hash: "a".repeat(64), scope_path: nodeId === ids.folder ? "我的知识库 / 研究" : "我的知识库", node_kind: nodeId === ids.folder ? "folder" : "root", node_title: nodeId === ids.folder ? "研究" : "我的知识库", knowledge_count: nodeId === ids.folder ? 1 : 2, source_count: 1, source_version_count: 1, chunk_count: 3, index_status: "ready", index_config_version: "d7-v1" };
}
const hit = { chunk_id: ids.chunk, content_identity: "section:1:chunk:0", rank: 1, raw_score: 0.82, score_semantics: "cosine similarity", corpus_kind: "source_evidence", knowledge_node_id: null, knowledge_revision_id: null, knowledge_path: ["我的知识库", "研究"], source_id: ids.source, source_title: "真实来源", source_version_id: ids.version, source_version_number: 1, parse_artifact_id: ids.artifact, section_id: ids.section, deep_link: `/sources/${ids.source}?sectionId=${ids.section}`, snippet: "用于验证范围内原始召回的真实片段。" };
const confirmedHit = { ...hit, chunk_id: ids.version, content_identity: "revision:1:chunk:0", corpus_kind: "confirmed_knowledge", knowledge_node_id: ids.folder, knowledge_revision_id: ids.artifact, source_id: null, source_title: null, source_version_id: null, source_version_number: null, parse_artifact_id: null, section_id: null, deep_link: null, snippet: "已确认知识的检索片段。" };
function json(payload: unknown) { return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } }); }
function renderWorkspace() { const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } }); return render(<QueryClientProvider client={client}><RetrievalDebugWorkspace /></QueryClientProvider>); }

function requestScope(init?: RequestInit) {
  const body = JSON.parse(String(init?.body)) as { scope_node_id: string; include_descendants: boolean };
  return scope(body.scope_node_id, body.include_descendants);
}

describe("RetrievalDebugWorkspace", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal("crypto", { randomUUID: vi.fn(() => "a6b06b89-319e-4f82-920f-aa1c2f971eae") });
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/bootstrap")) return Promise.resolve(json(bootstrap));
      if (url.endsWith("/knowledge-tree")) return Promise.resolve(json({ items: nodes }));
      if (url.endsWith("/scope-preview")) return Promise.resolve(json({ scope_summary: requestScope(init) }));
      if (url.endsWith("/index-status")) return Promise.resolve(json({ status: "ready", index_version: "d7-v1", indexed_chunks: 3, pending_chunks: 0, keyword: { status: "ready", indexed_chunks: 3 }, vector: { status: "ready", indexed_chunks: 3, provider: "bge", model: "m3", dimensions: 1024 }, updated_at: now }));
      if (url.endsWith("/debug-search")) { const body = JSON.parse(String(init?.body)); const summary = scope(body.scope.scope_node_id, body.scope.include_descendants); return Promise.resolve(json({ query: body.query, scope_summary: summary, embedding: { provider: "bge", model: "m3", dimensions: 1024 }, keyword_hits: [hit], vector_hits: [confirmedHit], channel_errors: { keyword: null, vector: null }, timings_ms: { keyword: 2.1, vector: 6.5 } })); }
      if (url.endsWith("/index-rebuilds") && init?.method === "POST") return Promise.resolve(json({ rebuild_id: ids.rebuild, status: "queued", message: "重建已排队" }));
      throw new Error(`Unexpected request ${url}`);
    }));
  });

  it("shows scope summary, raw dual-channel results, nullable deep links, and no answer UI", async () => {
    renderWorkspace();
    expect(await screen.findByRole("tree", { name: "检索范围" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "当前检索边界" })).toBeInTheDocument();
    expect(screen.getByText("D7 · RETRIEVAL ONLY")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("不会生成、续写或总结答案");

    await userEvent.type(screen.getByRole("searchbox"), "可追溯引用");
    await userEvent.click(screen.getByRole("button", { name: "运行双路检索" }));
    expect(await screen.findByRole("heading", { name: "FTS 召回" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Vector 召回" })).toBeInTheDocument();
    expect(screen.getByText("用于验证范围内原始召回的真实片段。")).toBeInTheDocument();
    expect(screen.getByText("已确认知识的检索片段。")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /在来源中查看/ })).toHaveAttribute("href", hit.deep_link);
    expect(screen.getByText("无来源深链")).toBeInTheDocument();
  }, 15_000);

  it("discards an old search response after scope changes and requests the selected scope", async () => {
    let resolveSearch: ((response: Response) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/bootstrap")) return Promise.resolve(json(bootstrap));
      if (url.endsWith("/knowledge-tree")) return Promise.resolve(json({ items: nodes }));
      if (url.endsWith("/scope-preview")) return Promise.resolve(json({ scope_summary: requestScope(init) }));
      if (url.endsWith("/index-status")) return Promise.resolve(json({ status: "ready", index_version: "d7-v1", indexed_chunks: 3, pending_chunks: 0, keyword: { status: "ready", indexed_chunks: 3 }, vector: { status: "ready", indexed_chunks: 3, provider: "bge", model: "m3", dimensions: 1024 }, updated_at: now }));
      if (url.endsWith("/debug-search")) return new Promise<Response>((resolve) => { resolveSearch = resolve; });
      throw new Error(`Unexpected request ${url}`);
    }));
    renderWorkspace();
    await userEvent.type(await screen.findByRole("searchbox"), "旧范围");
    await userEvent.click(screen.getByRole("button", { name: "运行双路检索" }));
    await userEvent.click(screen.getByRole("treeitem", { name: /研究/ }));
    expect((vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith("/debug-search"))?.[1] as RequestInit).signal?.aborted).toBe(true);
    resolveSearch?.(json({ query: "旧范围", scope_summary: scope(ids.root), embedding: null, keyword_hits: [hit], vector_hits: [], channel_errors: { keyword: null, vector: null }, timings_ms: {} }));
    await waitFor(() => expect(screen.queryByText("旧范围")).not.toBeInTheDocument());
    expect(vi.mocked(fetch).mock.calls.some(([url, init]) => String(url).endsWith("/scope-preview") && JSON.parse(String((init as RequestInit).body)).scope_node_id === ids.folder)).toBe(true);
  }, 15_000);

  it("gives idempotent rebuild feedback", async () => {
    renderWorkspace();
    await userEvent.click(await screen.findByRole("button", { name: "重建检索索引" }));
    expect(await screen.findByText("重建已排队")).toBeInTheDocument();
    const rebuildCall = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith("/index-rebuilds"));
    expect(new Headers((rebuildCall?.[1] as RequestInit).headers).get("Idempotency-Key")).toBe("a6b06b89-319e-4f82-920f-aa1c2f971eae");
  });
});
