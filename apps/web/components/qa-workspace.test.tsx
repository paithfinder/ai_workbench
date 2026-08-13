import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QaWorkspace } from "./qa-workspace";

const ids = { space: "102ee035-406f-41a6-b46b-d6c1d4e80d11", root: "77881bd1-0c4a-4cdb-b0d8-7ed52d25273f", folder: "a9a6e120-59b4-4ebc-a9b3-b4b81c7ee206", turn: "c90d99d3-4080-4918-aae6-5d7dc59289c9" };
const now = "2026-08-10T08:00:00+00:00";
const bootstrap = { space: { id: ids.space, slug: "mine", name: "我的知识库" }, capabilities: { source_import: true, extraction_review: true, knowledge_tree: true, retrieval_debug: true, trusted_qa: true, spaced_review: false, evidence_agent: false }, statistics: { sources: 1, queued_jobs: 0, activity_events: 0 }, foundation_status: "ready" };
const nodes = [
  { id: ids.root, space_id: ids.space, parent_id: null, kind: "root", path: "root", version: 1, sort_order: 0, origin_candidate_id: null, current_revision_id: null, source_id: null, source_version_id: null, title: "我的知识库", created_at: now, updated_at: now, deleted_at: null },
  { id: ids.folder, space_id: ids.space, parent_id: ids.root, kind: "folder", path: "root.folder", version: 1, sort_order: 0, origin_candidate_id: null, current_revision_id: null, source_id: null, source_version_id: null, title: "研究", created_at: now, updated_at: now, deleted_at: null },
];
function json(payload: unknown) { return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } }); }
function renderWorkspace() { const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } }); return render(<QueryClientProvider client={client}><QaWorkspace /></QueryClientProvider>); }

describe("QaWorkspace", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal("crypto", { randomUUID: vi.fn(() => "a6b06b89-319e-4f82-920f-aa1c2f971eae") });
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/bootstrap")) return Promise.resolve(json(bootstrap));
      if (url.endsWith("/knowledge-tree")) return Promise.resolve(json({ items: nodes }));
      if (url.endsWith("/scope-preview")) { const body = JSON.parse(String(init?.body)); return Promise.resolve(json({ scope_summary: { scope_node_id: body.scope_node_id, include_descendants: body.include_descendants, scope_snapshot_hash: "a".repeat(64), scope_path: body.scope_node_id === ids.folder ? "我的知识库 / 研究" : "我的知识库", node_kind: "folder", node_title: "研究", knowledge_count: 1, source_count: 1, source_version_count: 1, chunk_count: 2, index_status: "ready", index_config_version: "d7-v1" } })); }
      if (url.endsWith("/qa/turns")) return Promise.resolve(json({ id: ids.turn, status: "answered", question: "审核流程", scope_snapshot: {}, index_config_version: "d8-v1", ai_provider: "fake", ai_model: "fake", answer: "由领域负责人审核。", abstain_code: null, error_code: null, error_message: null, warnings: [], claims: [{ claim_id: "C1", claim_text: "由领域负责人审核", evidence_ids: ["E1"] }], citations: [{ claim_id: "C1", claim_text: "由领域负责人审核", evidence_id: "E1", content_identity: "a".repeat(64), section_id: "b7abaf61-044e-4a17-a377-94e7953b8301", corpus_kind: "source_evidence", frozen_quote: "新知识条目由直属领域负责人审核。", deep_link: "/sources/source-1" }] }));
      throw new Error(`Unexpected request ${url}`);
    }));
  });

  it("renders a verified answer and frozen citation", async () => {
    renderWorkspace();
    await userEvent.type(await screen.findByRole("textbox", { name: /问题/ }), "审核流程");
    await userEvent.click(screen.getByRole("button", { name: "生成可信回答" }));

    expect(await screen.findByText("由领域负责人审核。")).toBeInTheDocument();
    expect(screen.getByText("新知识条目由直属领域负责人审核。")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /查看冻结锚点/ })).toHaveAttribute("href", "/sources/source-1");
    const call = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith("/qa/turns"));
    expect(new Headers((call?.[1] as RequestInit).headers).get("Idempotency-Key")).toBe("a6b06b89-319e-4f82-920f-aa1c2f971eae");
  }, 15_000);
});
