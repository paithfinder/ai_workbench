import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createKnowledgeNode,
  deleteKnowledgeNode,
  editKnowledgeNode,
  getKnowledgeEvidence,
  getKnowledgeNode,
  getKnowledgeTree,
  getKnowledgeWriteRequest,
  moveKnowledgeNode,
  searchKnowledge,
} from "./knowledge";

const ids = {
  space: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
  root: "77881bd1-0c4a-4cdb-b0d8-7ed52d25273f",
  node: "7e8ee491-cd97-435c-97d4-82cc1951300c",
  revision: "a9a6e120-59b4-4ebc-a9b3-b4b81c7ee206",
  request: "c90d99d3-4080-4918-aae6-5d7dc59289c9",
  result: "247197b5-4e02-4442-a0a6-280151242198",
  evidence: "3f27ab12-8593-44b8-afc0-b5d3a1f76ab4",
  sourceVersion: "c3e1d289-31c1-444d-bda0-b5a4ea165142",
  artifact: "f21b429d-c60d-4f0b-85ba-d2ac82a2eb70",
  section: "e44c926f-399b-4087-a727-e9e5c2a6b112",
};
const now = "2026-08-05T12:00:00Z";
const node = { id: ids.node, space_id: ids.space, parent_id: ids.root, kind: "document", path: "nroot.nnode", version: 2, sort_order: 0, origin_candidate_id: null, current_revision_id: ids.revision, source_id: null, source_version_id: null, title: "文档", created_at: now, updated_at: now, deleted_at: null };
const revision = { id: ids.revision, node_id: ids.node, revision_number: 2, title: "文档", body: "正文", tags: [], conditions: [], exceptions: [], content_hash: "a".repeat(64), actor: "local", edit_reason: null, created_at: now };
const evidence = { id: ids.evidence, node_id: ids.node, revision_id: ids.revision, revision_number: 2, source_id: ids.root, source_title: "真实来源", source_version_id: ids.sourceVersion, source_version_number: 3, source_content_hash: "d".repeat(64), parse_artifact_id: ids.artifact, artifact_revision: 2, section_id: ids.section, section_ordinal: 4, quote_hash: "b".repeat(64), content_hash: "c".repeat(64), locator: { page: 1 }, frozen_quote: "冻结原文", deep_link: `/sources/${ids.root}?versionId=${ids.sourceVersion}&artifactId=${ids.artifact}&sectionId=${ids.section}`, anchor_status: "exact", created_at: now };
const write = { result_id: ids.result, request_id: ids.request, node_id: ids.node, node_version: 3, snapshot: { node } };
function json(payload: unknown, status = 200) { return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } }); }

describe("knowledge API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("reads tree, detail, server search, evidence, and write requests", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ items: [node] }))
      .mockResolvedValueOnce(json({ node, revision, evidence: [evidence] }))
      .mockResolvedValueOnce(json({ items: [{ node, breadcrumb: "知识库 / 文档", ancestor_ids: [ids.root], match_fields: ["body"] }] }))
      .mockResolvedValueOnce(json(evidence))
      .mockResolvedValueOnce(json({ idempotency_key: "stable/key", operation: "edit", status: "succeeded", result: write }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getKnowledgeTree(ids.space)).resolves.toEqual([node]);
    await expect(getKnowledgeNode(ids.space, ids.node)).resolves.toEqual({ node, revision, evidence: [evidence] });
    await expect(searchKnowledge(ids.space, "  冻结/引用  ", { limit: 12 })).resolves.toEqual([{ node, breadcrumb: "知识库 / 文档", ancestor_ids: [ids.root], match_fields: ["body"] }]);
    await expect(getKnowledgeEvidence(ids.space, ids.evidence)).resolves.toEqual(evidence);
    await expect(getKnowledgeWriteRequest(ids.space, "stable/key")).resolves.toMatchObject({ operation: "edit", result: write });

    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/knowledge-tree");
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain(`/knowledge-nodes/${ids.node}`);
    expect(String(fetchMock.mock.calls[2]?.[0])).toContain("/knowledge-search?q=%E5%86%BB%E7%BB%93%2F%E5%BC%95%E7%94%A8&limit=12");
    expect(String(fetchMock.mock.calls[3]?.[0])).toContain(`/knowledge-evidence/${ids.evidence}`);
    expect(String(fetchMock.mock.calls[4]?.[0])).toContain("/knowledge-write-requests/stable%2Fkey");
  });

  it("sends create, edit, move, and delete optimistic concurrency fields", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(json(write)));
    vi.stubGlobal("fetch", fetchMock);

    await createKnowledgeNode(ids.space, { parentId: ids.root, kind: "document", expectedVersion: 4, title: "新文档", body: "正文" }, "create-key");
    await editKnowledgeNode(ids.space, ids.node, { expectedVersion: 2, expectedRevisionId: ids.revision, title: "新标题", body: "新正文", reason: "整理" }, "edit-key");
    await moveKnowledgeNode(ids.space, ids.node, { parentId: ids.root, expectedVersion: 3 }, "move-key");
    await deleteKnowledgeNode(ids.space, ids.node, 4, "delete-key");

    const calls = fetchMock.mock.calls as Array<[string, RequestInit]>;
    expect(calls.map(([, init]) => init.method)).toEqual(["POST", "PATCH", "POST", "DELETE"]);
    expect(calls.map(([, init]) => new Headers(init.headers).get("Idempotency-Key"))).toEqual(["create-key", "edit-key", "move-key", "delete-key"]);
    expect(JSON.parse(String(calls[0][1].body))).toEqual({ parent_id: ids.root, kind: "document", expected_version: 4, title: "新文档", body: "正文", tags: [], conditions: [], exceptions: [] });
    expect(JSON.parse(String(calls[1][1].body))).toEqual({ expected_version: 2, expected_revision_id: ids.revision, title: "新标题", body: "新正文", reason: "整理" });
    expect(JSON.parse(String(calls[2][1].body))).toEqual({ parent_id: ids.root, expected_version: 3 });
    expect(JSON.parse(String(calls[3][1].body))).toEqual({ expected_version: 4 });
  });

  it("accepts all five node kinds and rejects malformed evidence hashes", async () => {
    const items = ["root", "folder", "document", "point", "source"].map((kind, index) => ({ ...node, id: `${index + 1}0281bd1-0c4a-4cdb-b0d8-7ed52d25273f`, kind }));
    const fetchMock = vi.fn().mockResolvedValueOnce(json({ items })).mockResolvedValueOnce(json({ ...evidence, quote_hash: "unsafe" }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getKnowledgeTree(ids.space)).resolves.toHaveLength(5);
    await expect(getKnowledgeEvidence(ids.space, ids.evidence)).rejects.toMatchObject({ code: "invalid_api_response" });
  });
});
