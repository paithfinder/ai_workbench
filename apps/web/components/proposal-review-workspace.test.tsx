import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProposalReviewWorkspace } from "./proposal-review-workspace";

const ids = {
  space: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
  proposal: "6ee8885f-e0ca-4e04-bf02-47d63b6c5881",
  target: "77881bd1-0c4a-4cdb-b0d8-7ed52d25273f",
  revision: "7e8ee491-cd97-435c-97d4-82cc1951300c",
  evidence: "a9a6e120-59b4-4ebc-a9b3-b4b81c7ee206",
  source: "c90d99d3-4080-4918-aae6-5d7dc59289c9",
  sourceVersion: "247197b5-4e02-4442-a0a6-280151242198",
  artifact: "190b4f7f-7dfd-482b-89b8-ef4349767531",
  section: "4d7bc6ee-2025-4b18-8d64-000000000001",
  transition: "a6b06b89-319e-4f82-920f-aa1c2f971eae",
  request: "b6b06b89-319e-4f82-920f-aa1c2f971eae",
  result: "c6b06b89-319e-4f82-920f-aa1c2f971eae",
};
const now = "2026-08-17T12:00:00Z";

const bootstrap = {
  space: { id: ids.space, slug: "my-knowledge-base", name: "我的知识库" },
  capabilities: {
    source_import: true,
    extraction_review: true,
    knowledge_tree: true,
    knowledge_folder_import: true,
    retrieval_debug: true,
    trusted_qa: false,
    spaced_review: false,
    evidence_agent: true,
  },
  statistics: { sources: 1, queued_jobs: 0, activity_events: 0 },
  foundation_status: "ready",
};

function proposal(status = "pending_review", version = 3) {
  return {
    id: ids.proposal,
    space_id: ids.space,
    research_run_id: null,
    target_node_id: ids.target,
    target_revision_id: ids.revision,
    target_node_version: 7,
    action: "revise",
    create_kind: null,
    status,
    version,
    suggested_title: "更新检索边界",
    suggested_body: "将来源版本固定为可追溯的证据集合。",
    suggested_tags: ["检索", "证据"],
    conditions: ["来源解析已完成"],
    exceptions: ["证据冲突时人工复查"],
    comparison_summary: "新证据补强原修订。",
    confidence: 0.86,
    uncertainty_reason: "尚需人工确认目标正文。",
    superseded_by_proposal_id: null,
    submitted_at: now,
    reviewed_at: null,
    applied_at: null,
    superseded_at: null,
    created_at: now,
    updated_at: now,
  };
}

function detail(status = "pending_review", version = 3) {
  return {
    proposal: proposal(status, version),
    evidence: [{
      id: ids.evidence,
      role: "new_support",
      source_id: ids.source,
      source_version_id: ids.sourceVersion,
      parse_artifact_id: ids.artifact,
      section_id: ids.section,
      knowledge_revision_id: ids.revision,
      frozen_quote: "冻结摘录：来源支持本次修订。",
      quote_hash: "a".repeat(64),
      content_hash: "b".repeat(64),
      locator: { page: 1 },
      ordinal: 4,
    }],
    transitions: [{
      id: ids.transition,
      request_id: ids.request,
      operation: "submit",
      actor: "system",
      reason: "生成后提交审查",
      before_snapshot: {},
      after_snapshot: {},
      from_status: "draft",
      to_status: status,
      from_version: 2,
      to_version: version,
      created_at: now,
    }],
  };
}

function mutationResult(status: string, version: number) {
  return {
    result_id: ids.result,
    proposal_id: ids.proposal,
    proposal_version: version,
    proposal_status: status,
    snapshot: {},
  };
}

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  return render(<QueryClientProvider client={client}><ProposalReviewWorkspace /></QueryClientProvider>);
}

function endpointFetch() {
  let proposalStatus = "pending_review";
  let proposalVersion = 3;
  return vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url.endsWith("/api/v1/bootstrap")) return Promise.resolve(json(bootstrap));
    if (url.includes(`/knowledge-spaces/${ids.space}/proposals?`) && method === "GET") {
      const requestedStatus = new URL(url).searchParams.get("status");
      return Promise.resolve(json({ items: !requestedStatus || requestedStatus === proposalStatus ? [proposal(proposalStatus, proposalVersion)] : [] }));
    }
    if (url.endsWith(`/proposals/${ids.proposal}`) && method === "GET") return Promise.resolve(json(detail(proposalStatus, proposalVersion)));
    if (url.endsWith(`/proposals/${ids.proposal}/approve`) && method === "POST") {
      proposalStatus = "approved";
      proposalVersion = 4;
      return Promise.resolve(json(mutationResult(proposalStatus, proposalVersion)));
    }
    if (url.endsWith(`/proposals/${ids.proposal}/reject`) && method === "POST") return Promise.resolve(json(mutationResult("rejected", 4)));
    if (url.endsWith(`/proposals/${ids.proposal}/apply`) && method === "POST") {
      proposalStatus = "applied";
      proposalVersion = 5;
      return Promise.resolve(json(mutationResult(proposalStatus, proposalVersion)));
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  });
}

describe("ProposalReviewWorkspace", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal("crypto", { randomUUID: vi.fn(() => "d6b06b89-319e-4f82-920f-aa1c2f971eae") });
  });

  it("filters the queue and renders frozen evidence, audit history, and the missing target body boundary", async () => {
    const fetchMock = endpointFetch();
    vi.stubGlobal("fetch", fetchMock);
    renderWorkspace();

    expect((await screen.findAllByRole("heading", { name: "更新检索边界" })).length).toBeGreaterThan(0);
    expect(screen.getByText("冻结摘录：来源支持本次修订。")).toBeInTheDocument();
    expect(screen.getByText("状态转换审计")).toBeInTheDocument();
    expect(screen.getByText(/未包含目标节点正文/)).toBeInTheDocument();
    expect(screen.getByText(ids.revision)).toBeInTheDocument();
    expect(screen.getByText("#4 · " + ids.section)).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("按动作筛选"), "revise");
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes("status=pending_review&action=revise"))).toBe(true));
  });

  it("locks competing actions and refreshes server data after approve then apply", async () => {
    const fetchMock = endpointFetch();
    vi.stubGlobal("fetch", fetchMock);
    renderWorkspace();

    const approve = await screen.findByRole("button", { name: "批准提案" });
    await userEvent.click(approve);
    await userEvent.type(screen.getByRole("textbox", { name: "审核原因" }), "证据充分");
    await userEvent.click(screen.getByRole("button", { name: "批准" }));

    expect(await screen.findByText("当前筛选没有提案。")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("按状态筛选"), "approved");
    expect(await screen.findByRole("button", { name: "应用提案" })).toBeInTheDocument();
    const approveCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/approve"));
    expect(JSON.parse(String((approveCall?.[1] as RequestInit).body))).toEqual({ expected_version: 3, reason: "证据充分" });
    expect(new Headers((approveCall?.[1] as RequestInit).headers).get("Idempotency-Key")).toBe("d6b06b89-319e-4f82-920f-aa1c2f971eae");
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith(`/proposals/${ids.proposal}`))).toHaveLength(3);

    await userEvent.click(screen.getByRole("button", { name: "应用提案" }));
    expect(await screen.findByText("该提案当前状态没有可执行的审查操作。")).toBeInTheDocument();
    const applyCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/apply"));
    expect(JSON.parse(String((applyCall?.[1] as RequestInit).body))).toEqual({ expected_version: 4, reason: null });
  });

  it("does not submit a second transition while the first mutation is pending", async () => {
    let resolveApprove: ((response: Response) => void) | undefined;
    const fetchMock = endpointFetch();
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith(`/proposals/${ids.proposal}/approve`)) {
        return new Promise<Response>((resolve) => { resolveApprove = resolve; });
      }
      if (url.endsWith(`/proposals/${ids.proposal}/reject`)) return Promise.resolve(json(mutationResult("rejected", 4)));
      if (url.endsWith("/api/v1/bootstrap")) return Promise.resolve(json(bootstrap));
      if (url.includes(`/knowledge-spaces/${ids.space}/proposals?`)) return Promise.resolve(json({ items: [proposal()] }));
      if (url.endsWith(`/proposals/${ids.proposal}`)) return Promise.resolve(json(detail()));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWorkspace();

    await userEvent.click(await screen.findByRole("button", { name: "批准提案" }));
    expect(screen.getByRole("button", { name: "正在批准…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "拒绝提案" })).toBeDisabled();
    expect(screen.getByLabelText("按状态筛选")).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "拒绝提案" }));
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/reject"))).toHaveLength(0);

    resolveApprove?.(json(mutationResult("approved", 4)));
    await screen.findByText("批准请求已由服务端确认，审查记录已刷新。");
  });
});
