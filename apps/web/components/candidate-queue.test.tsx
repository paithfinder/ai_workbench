import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CandidateQueue } from "./candidate-queue";

const ids = {
  space: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
  version: "77881bd1-0c4a-4cdb-b0d8-7ed52d25273f",
  job: "7e8ee491-cd97-435c-97d4-82cc1951300c",
  extraction: "a9a6e120-59b4-4ebc-a9b3-b4b81c7ee206",
  artifact: "c90d99d3-4080-4918-aae6-5d7dc59289c9",
  candidate: "247197b5-4e02-4442-a0a6-280151242198",
  secondCandidate: "3f27ab12-8593-44b8-afc0-b5d3a1f76ab4",
  section: "c3e1d289-31c1-444d-bda0-b5a4ea165142",
  destination: "f21b429d-c60d-4f0b-85ba-d2ac82a2eb70",
  node: "e44c926f-399b-4087-a727-e9e5c2a6b112",
  revision: "0cc7c2c8-d03b-472c-b11e-e2e721700efa",
  card: "152ed6f1-ecf2-4aec-aa85-ad33c85944f7",
  evidence: "190b4f7f-7dfd-482b-89b8-ef4349767531",
  activity: "16fbfbf0-7f6d-47cd-8d8a-4ef917070405",
};
const now = "2026-08-05T12:00:00Z";
const bootstrap = {
  space: { id: ids.space, slug: "mine", name: "我的知识库" },
  capabilities: { source_import: true, extraction_review: true, knowledge_tree: true, trusted_qa: false, spaced_review: false, evidence_agent: false },
  statistics: { sources: 1, queued_jobs: 0, activity_events: 0 }, foundation_status: "ready",
};
const job = { id: ids.job, space_id: ids.space, source_version_id: ids.version, kind: "source_extract", status: "succeeded", progress: 100, attempt_count: 1, retryable: false, error_code: null, error_message: null, started_at: now, finished_at: now, created_at: now, updated_at: now };
const extraction = { id: ids.extraction, job_id: ids.job, source_version_id: ids.version, parse_artifact_id: ids.artifact, status: "ready", provider: "anthropic", model: "claude-opus-5", prompt_version: "v1", input_tokens: 10, output_tokens: 20, latency_ms: 30, provider_request_id: null, error_code: null, error_message: null, started_at: now, completed_at: now, created_at: now };
const baseCandidate = {
  id: ids.candidate, extraction_job_id: ids.extraction, source_version_id: ids.version, source_title: "真实来源", title: "候选标题", body: "候选正文", tags: ["原始标签"], suggested_destination_id: null,
  atomicity: "atomic", confidence: .82, status: "pending_review", version: 1, reviewed_at: null, verification_reason: null, rejection_reason: null, conditions: [], exceptions: [], model: "claude-opus-5", prompt_version: "v1", created_at: now,
  evidence: [{ section_id: ids.section, title: "证据标题", text: "整段可信证据，不包含 HTML。", heading_path: ["第一章"], page_number: 1, locator: {}, quote_hash: "a".repeat(64) }],
};

const reviewOutcome = {
  result_id: ids.activity,
  candidate_review_id: ids.evidence,
  candidate_id: ids.candidate,
  candidate_version: 2,
  candidate_status: "accepted",
  knowledge_node_id: ids.node,
  knowledge_revision_id: ids.revision,
  review_card_id: ids.card,
  evidence_ids: [ids.evidence],
};

function json(payload: unknown, status = 200) { return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } }); }
function renderQueue() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  return render(<QueryClientProvider client={client}><CandidateQueue /></QueryClientProvider>);
}
function baseFetch(candidates = [baseCandidate]) {
  return vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/v1/bootstrap")) return Promise.resolve(json(bootstrap));
    if (url.endsWith("/extractions")) return Promise.resolve(json({ items: [{ job, extraction, source_title: "真实来源" }] }));
    if (url.endsWith("/knowledge-destinations")) return Promise.resolve(json({ items: [{ id: ids.destination, name: "研究", path: ["主题"] }] }));
    if (url.endsWith("/candidates") && !init?.method) return Promise.resolve(json({ items: candidates }));
    throw new Error(`Unexpected request ${url}`);
  });
}

describe("CandidateQueue", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal("crypto", { randomUUID: vi.fn(() => "4d7bc6ee-2025-4b18-8d64-000000000001") });
    Element.prototype.scrollIntoView = vi.fn();
  });

  it("renders source evidence separately from the editable review draft", async () => {
    vi.stubGlobal("fetch", baseFetch());
    renderQueue();

    expect(await screen.findByRole("heading", { name: "候选队列" })).toBeInTheDocument();
    expect(screen.getByLabelText("只读来源证据")).toHaveTextContent("整段可信证据，不包含 HTML。");
    expect(screen.getByRole("complementary", { name: "AI 候选编辑与决策" })).toBeInTheDocument();
    expect(screen.getByLabelText("最终标题 *")).toHaveValue("候选标题");
    expect(screen.getByRole("option", { name: "主题 / 研究" })).toBeInTheDocument();
  });

  it("guards a dirty draft when switching candidates", async () => {
    const second = { ...baseCandidate, id: ids.secondCandidate, title: "第二候选" };
    vi.stubGlobal("fetch", baseFetch([baseCandidate, second]));
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    renderQueue();

    const title = await screen.findByLabelText("最终标题 *");
    await userEvent.clear(title);
    await userEvent.type(title, "未保存标题");
    await userEvent.click(screen.getByRole("button", { name: /第二候选/ }));
    expect(confirm).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("最终标题 *")).toHaveValue("未保存标题");
  });

  it("sends the final draft, destination, expected version, and idempotency key when accepting", async () => {
    const fetchMock = baseFetch().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith(`/candidates/${ids.candidate}/accept`) && init?.method === "POST") return Promise.resolve(json(reviewOutcome));
      if (url.endsWith("/api/v1/bootstrap")) return Promise.resolve(json(bootstrap));
      if (url.endsWith("/extractions")) return Promise.resolve(json({ items: [{ job, extraction, source_title: "真实来源" }] }));
      if (url.endsWith("/knowledge-destinations")) return Promise.resolve(json({ items: [{ id: ids.destination, name: "研究", path: ["主题"] }] }));
      if (url.endsWith("/candidates") && !init?.method) return Promise.resolve(json({ items: [baseCandidate] }));
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderQueue();

    const title = await screen.findByLabelText("最终标题 *");
    await userEvent.clear(title); await userEvent.type(title, "最终标题");
    const body = screen.getByLabelText("最终正文 *");
    await userEvent.clear(body); await userEvent.type(body, "最终正文");
    const tags = screen.getByLabelText("标签");
    await userEvent.clear(tags); await userEvent.type(tags, "定稿");
    await userEvent.selectOptions(screen.getByLabelText("知识树目标位置"), ids.destination);
    await userEvent.click(screen.getByRole("button", { name: "加入知识树" }));

    expect(await screen.findByText(/已加入知识树，并创建 1 条证据关系/)).toBeInTheDocument();
    const acceptCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/accept"));
    const init = acceptCall?.[1] as RequestInit;
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("4d7bc6ee-2025-4b18-8d64-000000000001");
    expect(JSON.parse(String(init.body))).toEqual({ title: "最终标题", body: "最终正文", tags: ["定稿"], suggested_destination_id: ids.destination, conditions: [], exceptions: [], expected_version: 1 });
    expect(screen.queryByRole("button", { name: "加入知识树" })).not.toBeInTheDocument();
  });

  it("preserves the draft on a 409 conflict", async () => {
    const fetchMock = baseFetch().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith(`/candidates/${ids.candidate}`) && init?.method === "PATCH") return Promise.resolve(json({ error: { code: "version_conflict", message: "版本冲突", details: [] } }, 409));
      if (url.endsWith("/api/v1/bootstrap")) return Promise.resolve(json(bootstrap));
      if (url.endsWith("/extractions")) return Promise.resolve(json({ items: [{ job, extraction, source_title: "真实来源" }] }));
      if (url.endsWith("/knowledge-destinations")) return Promise.resolve(json({ items: [] }));
      if (url.endsWith("/candidates") && !init?.method) return Promise.resolve(json({ items: [baseCandidate] }));
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderQueue();

    const title = await screen.findByLabelText("最终标题 *");
    await userEvent.clear(title); await userEvent.type(title, "保留我的草稿");
    await userEvent.click(screen.getByRole("button", { name: "保存编辑" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("你的草稿仍保留");
    expect(title).toHaveValue("保留我的草稿");
  });

  it("reconciles an uncertain accept and uses one stable operation key", async () => {
    let acceptCalls = 0;
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/bootstrap")) return Promise.resolve(json(bootstrap));
      if (url.endsWith("/extractions")) return Promise.resolve(json({ items: [{ job, extraction, source_title: "真实来源" }] }));
      if (url.endsWith("/knowledge-destinations")) return Promise.resolve(json({ items: [] }));
      if (url.endsWith("/candidates") && !init?.method) return Promise.resolve(json({ items: [baseCandidate] }));
      if (url.endsWith(`/candidates/${ids.candidate}/accept`)) { acceptCalls += 1; return Promise.reject(new TypeError("response lost")); }
      if (url.includes("/review-requests/")) return Promise.resolve(json({ idempotency_key: "4d7bc6ee-2025-4b18-8d64-000000000001", action: "accept", status: "succeeded", result: reviewOutcome, error_code: null, error_message: null }));
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderQueue();

    await userEvent.click(await screen.findByRole("button", { name: "加入知识树" }));
    expect(await screen.findByText(/服务端已完成操作/)).toBeInTheDocument();
    expect(acceptCalls).toBe(1);
    const requestCalls = fetchMock.mock.calls.filter(([url]) => String(url).includes(`/candidates/${ids.candidate}`));
    const writeKey = new Headers((requestCalls[0]?.[1] as RequestInit).headers).get("Idempotency-Key");
    expect(writeKey).toBe("4d7bc6ee-2025-4b18-8d64-000000000001");
    await waitFor(() => expect(screen.queryByRole("button", { name: "加入知识树" })).not.toBeInTheDocument());
  });
});
