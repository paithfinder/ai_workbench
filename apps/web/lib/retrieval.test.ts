import { afterEach, describe, expect, it, vi } from "vitest";
import {
  debugRetrievalSearch,
  getRetrievalIndexStatus,
  previewRetrievalScope,
  rebuildRetrievalIndex,
} from "./retrieval";

const ids = {
  space: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
  node: "77881bd1-0c4a-4cdb-b0d8-7ed52d25273f",
  chunk: "a9a6e120-59b4-4ebc-a9b3-b4b81c7ee206",
  source: "c90d99d3-4080-4918-aae6-5d7dc59289c9",
  version: "247197b5-4e02-4442-a0a6-280151242198",
  artifact: "3f27ab12-8593-44b8-afc0-b5d3a1f76ab4",
  section: "c3e1d289-31c1-444d-bda0-b5a4ea165142",
  rebuild: "f21b429d-c60d-4f0b-85ba-d2ac82a2eb70",
};
const scope = {
  scope_node_id: ids.node,
  include_descendants: true,
  scope_snapshot_hash: "a".repeat(64),
  scope_path: "我的知识库 / 研究",
  node_kind: "folder",
  node_title: "研究",
  knowledge_count: 2,
  source_count: 1,
  source_version_count: 1,
  chunk_count: 3,
  index_status: "ready",
  index_config_version: "d7-v1",
};
const hit = {
  chunk_id: ids.chunk,
  content_identity: "section:5:chunk:0",
  rank: 1,
  raw_score: 0.82,
  score_semantics: "cosine_similarity",
  corpus_kind: "source_evidence",
  knowledge_node_id: null,
  knowledge_revision_id: null,
  knowledge_path: ["我的知识库", "研究"],
  source_id: ids.source,
  source_title: "真实来源",
  source_version_id: ids.version,
  source_version_number: 2,
  parse_artifact_id: ids.artifact,
  section_id: ids.section,
  snippet: "真实的检索片段。",
  deep_link: `/sources/${ids.source}?sectionId=${ids.section}`,
};
function json(payload: unknown) { return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } }); }

afterEach(() => vi.unstubAllGlobals());

describe("retrieval client", () => {
  it("sends the frozen scope and parses flat dual-channel hits", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ scope_summary: scope }))
      .mockResolvedValueOnce(json({ query: "可追溯引用", scope_summary: scope, embedding: { provider: "bge", model: "m3", dimensions: 1024 }, keyword_hits: [hit], vector_hits: [{ ...hit, deep_link: null }], channel_errors: { keyword: null, vector: null }, timings_ms: { keyword: 4.3, vector: 9.8 } }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(previewRetrievalScope(ids.space, { scopeNodeId: ids.node, includeDescendants: true })).resolves.toMatchObject({ scope_summary: scope });
    await expect(debugRetrievalSearch(ids.space, { query: " 可追溯引用 ", scopeNodeId: ids.node, includeDescendants: true })).resolves.toMatchObject({ keyword_hits: [hit], vector_hits: [{ deep_link: null }] });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(`http://localhost:8000/api/v1/knowledge-spaces/${ids.space}/retrieval/scope-preview`);
    expect(JSON.parse(String((fetchMock.mock.calls[0]?.[1] as RequestInit).body))).toEqual({ scope_node_id: ids.node, include_descendants: true });
    expect(fetchMock.mock.calls[1]?.[0]).toBe(`http://localhost:8000/api/v1/knowledge-spaces/${ids.space}/retrieval/debug-search`);
    expect(JSON.parse(String((fetchMock.mock.calls[1]?.[1] as RequestInit).body))).toEqual({ query: "可追溯引用", scope: { scope_node_id: ids.node, include_descendants: true }, top_k: 5, channels: ["keyword", "vector"] });
  });

  it("reads status and sends an idempotent rebuild request", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ status: "ready", index_version: "d7-v1", indexed_chunks: 3, pending_chunks: 0, keyword: { status: "ready", indexed_chunks: 3 }, vector: { status: "ready", indexed_chunks: 3, provider: "bge", model: "m3", dimensions: 1024 }, updated_at: "2026-08-10T08:00:00+00:00" }))
      .mockResolvedValueOnce(json({ rebuild_id: ids.rebuild, status: "queued", message: "重建已排队" }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getRetrievalIndexStatus(ids.space)).resolves.toMatchObject({ status: "ready", indexed_chunks: 3 });
    await expect(rebuildRetrievalIndex(ids.space, "debug-rebuild-key")).resolves.toMatchObject({ status: "queued" });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(`http://localhost:8000/api/v1/knowledge-spaces/${ids.space}/retrieval/index-status`);
    expect(fetchMock.mock.calls[1]?.[0]).toBe(`http://localhost:8000/api/v1/knowledge-spaces/${ids.space}/retrieval/index-rebuilds`);
    expect(new Headers((fetchMock.mock.calls[1]?.[1] as RequestInit).headers).get("Idempotency-Key")).toBe("debug-rebuild-key");
  });
});
