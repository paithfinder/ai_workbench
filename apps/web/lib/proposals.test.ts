import { afterEach, describe, expect, it, vi } from "vitest";
import {
  applyProposal,
  approveProposal,
  createProposal,
  getProposal,
  getProposalRequest,
  listProposals,
  rejectProposal,
} from "./proposals";

const ids = {
  space: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
  proposal: "6ee8885f-e0ca-4e04-bf02-47d63b6c5881",
  result: "77881bd1-0c4a-4cdb-b0d8-7ed52d25273f",
  evidence: "7e8ee491-cd97-435c-97d4-82cc1951300c",
  source: "a9a6e120-59b4-4ebc-a9b3-b4b81c7ee206",
  version: "c90d99d3-4080-4918-aae6-5d7dc59289c9",
  artifact: "247197b5-4e02-4442-a0a6-280151242198",
  section: "3f27ab12-8593-44b8-afc0-b5d3a1f76ab4",
};
const now = "2026-08-17T12:00:00Z";

const item = {
  id: ids.proposal,
  space_id: ids.space,
  research_run_id: null,
  target_node_id: null,
  target_revision_id: null,
  target_node_version: null,
  action: "create",
  create_kind: "document",
  status: "pending_review",
  version: 2,
  suggested_title: "审核标题",
  suggested_body: "审核正文",
  suggested_tags: ["测试"],
  conditions: [],
  exceptions: [],
  comparison_summary: null,
  confidence: 0.9,
  uncertainty_reason: null,
  superseded_by_proposal_id: null,
  submitted_at: now,
  reviewed_at: null,
  applied_at: null,
  superseded_at: null,
  created_at: now,
  updated_at: now,
};
const evidence = {
  id: ids.evidence,
  role: "new_support",
  source_id: ids.source,
  source_version_id: ids.version,
  parse_artifact_id: ids.artifact,
  section_id: ids.section,
  knowledge_revision_id: null,
  frozen_quote: "冻结证据",
  quote_hash: "a".repeat(64),
  content_hash: "b".repeat(64),
  locator: { page: 1 },
  ordinal: 0,
};
const detail = { proposal: item, evidence: [evidence], transitions: [] };
const result = {
  result_id: ids.result,
  proposal_id: ids.proposal,
  proposal_version: 3,
  proposal_status: "approved",
  snapshot: {
    proposal: { status: "approved", version: 3 },
    apply: {
      action: "create",
      target_node_id: ids.proposal,
      old_revision_id: null,
      new_revision_id: ids.result,
      node_version: 4,
      evidence_ids: [ids.evidence],
      index_job_id: null,
      index_run_id: null,
    },
  },
};

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("proposal API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("lists proposals with contract filters and validates create_kind", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ items: [item] }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(listProposals(ids.space, {
      status: "pending_review",
      action: "create",
      targetNodeId: ids.proposal,
      limit: 20,
      offset: 10,
    })).resolves.toEqual([item]);

    expect(String(fetchMock.mock.calls[0]?.[0])).toContain(
      `/proposals?status=pending_review&action=create&target_node_id=${ids.proposal}&limit=20&offset=10`,
    );
  });

  it("gets proposal detail and request recovery records with encoded keys", async () => {
    const request = { idempotency_key: "approve/key", action: "approve", result };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json(detail))
      .mockResolvedValueOnce(json(request));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getProposal(ids.space, ids.proposal)).resolves.toEqual(detail);
    await expect(getProposalRequest(ids.space, "approve/key")).resolves.toEqual(request);
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain(`/proposals/${ids.proposal}`);
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain("proposal-requests/approve%2Fkey");
  });

  it("sends create, approve, reject, and apply with idempotency keys and AbortSignals", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json(result))
      .mockResolvedValueOnce(json(result))
      .mockResolvedValueOnce(json(result))
      .mockResolvedValueOnce(json(result));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    const create = {
      action: "create" as const,
      create_kind: "document" as const,
      suggested_title: "审核标题",
      evidence: [{ role: "new_support" as const, section_id: ids.section }],
    };
    const approve = { expected_version: 2, reason: "同意" };
    const reject = { expected_version: 2, reason: "证据不足" };
    const apply = { expected_version: 2, reason: null };

    await expect(createProposal(ids.space, create, "create-key", { signal: controller.signal })).resolves.toEqual(result);
    await expect(approveProposal(ids.space, ids.proposal, approve, "approve-key", { signal: controller.signal })).resolves.toEqual(result);
    await expect(rejectProposal(ids.space, ids.proposal, reject, "reject-key", { signal: controller.signal })).resolves.toEqual(result);
    await expect(applyProposal(ids.space, ids.proposal, apply, "apply-key", { signal: controller.signal })).resolves.toEqual(result);

    const calls = fetchMock.mock.calls as Array<[string, RequestInit]>;
    expect(calls.map(([url]) => url)).toEqual([
      `http://localhost:8000/api/v1/knowledge-spaces/${ids.space}/proposals`,
      `http://localhost:8000/api/v1/knowledge-spaces/${ids.space}/proposals/${ids.proposal}/approve`,
      `http://localhost:8000/api/v1/knowledge-spaces/${ids.space}/proposals/${ids.proposal}/reject`,
      `http://localhost:8000/api/v1/knowledge-spaces/${ids.space}/proposals/${ids.proposal}/apply`,
    ]);
    for (const [index, payload] of [create, approve, reject, apply].entries()) {
      const init = calls[index][1];
      expect(init.method).toBe("POST");
      expect(init.signal).toBe(controller.signal);
      expect(new Headers(init.headers).get("Idempotency-Key")).toBe(["create-key", "approve-key", "reject-key", "apply-key"][index]);
      expect(JSON.parse(String(init.body))).toEqual(payload);
    }
  });

  it("reconciles an unknown mutation result by the same idempotency key", async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError("connection reset"))
      .mockResolvedValueOnce(json({ idempotency_key: "stable/key", action: "apply", result }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(applyProposal(
      ids.space,
      ids.proposal,
      { expected_version: 2 },
      "stable/key",
    )).resolves.toEqual(result);
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain("proposal-requests/stable%2Fkey");
  });

  it("preserves the unknown failure when recovery has no persisted request", async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError("connection reset"))
      .mockResolvedValueOnce(json({
        error: { code: "proposal_request_not_found", message: "请求不存在", details: [], request_id: "request-404" },
      }, 404));
    vi.stubGlobal("fetch", fetchMock);

    await expect(applyProposal(
      ids.space,
      ids.proposal,
      { expected_version: 2 },
      "stable-key",
    )).rejects.toMatchObject({ status: 0, code: "network_error" });
  });

  it("preserves structured conflict errors", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({
      error: {
        code: "proposal_target_stale",
        message: "目标版本已变化",
        details: [{ expected_version: 2, actual_version: 3 }],
        request_id: "request-409",
      },
    }, 409));
    vi.stubGlobal("fetch", fetchMock);

    await expect(approveProposal(
      ids.space,
      ids.proposal,
      { expected_version: 2 },
      "approve-key",
    )).rejects.toMatchObject({
      status: 409,
      code: "proposal_target_stale",
      details: [{ expected_version: 2, actual_version: 3 }],
      requestId: "request-409",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
