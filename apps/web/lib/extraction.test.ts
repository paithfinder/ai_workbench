import { afterEach, describe, expect, it, vi } from "vitest";
import {
  acceptCandidate,
  getCandidateReviewRequest,
  getExtraction,
  listCandidates,
  listExtractions,
  listKnowledgeDestinations,
  markCandidateNeedsVerification,
  rejectCandidate,
  scheduleExtraction,
  updateCandidate,
} from "./extraction";

const ids = {
  space: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
  source: "6ee8885f-e0ca-4e04-bf02-47d63b6c5881",
  version: "77881bd1-0c4a-4cdb-b0d8-7ed52d25273f",
  job: "7e8ee491-cd97-435c-97d4-82cc1951300c",
  extraction: "a9a6e120-59b4-4ebc-a9b3-b4b81c7ee206",
  artifact: "c90d99d3-4080-4918-aae6-5d7dc59289c9",
  candidate: "247197b5-4e02-4442-a0a6-280151242198",
  section: "3f27ab12-8593-44b8-afc0-b5d3a1f76ab4",
  destination: "c3e1d289-31c1-444d-bda0-b5a4ea165142",
  node: "f21b429d-c60d-4f0b-85ba-d2ac82a2eb70",
  revision: "e44c926f-399b-4087-a727-e9e5c2a6b112",
  card: "0cc7c2c8-d03b-472c-b11e-e2e721700efa",
  evidence: "152ed6f1-ecf2-4aec-aa85-ad33c85944f7",
  activity: "190b4f7f-7dfd-482b-89b8-ef4349767531",
};
const now = "2026-08-05T12:00:00Z";
const job = {
  id: ids.job, space_id: ids.space, source_version_id: ids.version, kind: "source_extract", status: "queued", progress: 0,
  attempt_count: 0, retryable: false, error_code: null, error_message: null, started_at: null, finished_at: null, created_at: now, updated_at: now,
};
const extraction = {
  id: ids.extraction, job_id: ids.job, source_version_id: ids.version, parse_artifact_id: ids.artifact, status: "queued",
  provider: null, model: "claude-opus-5", prompt_version: "knowledge-candidates-v1", input_tokens: 0, output_tokens: 0,
  latency_ms: 0, provider_request_id: null, error_code: null, error_message: null, started_at: null, completed_at: null, created_at: now,
};
const candidate = {
  id: ids.candidate, extraction_job_id: ids.extraction, source_version_id: ids.version, source_title: "真实笔记", title: "候选标题",
  body: "候选正文", tags: ["测试"], suggested_destination_id: ids.destination, atomicity: "atomic", confidence: 0.8,
  status: "needs_verification", version: 2, reviewed_at: null, verification_reason: "需要第二来源", rejection_reason: null, conditions: [], exceptions: [],
  model: "claude-opus-5", prompt_version: "knowledge-candidates-v1", created_at: now,
  evidence: [{ section_id: ids.section, title: null, text: "可信证据", heading_path: ["第一章"], page_number: 1,
    locator: { locatorType: "page_heading_paragraph" }, quote_hash: "a".repeat(64) }],
};
const draft = { title: "最终标题", body: "最终正文", tags: ["测试", "定稿"], suggested_destination_id: ids.destination, conditions: [], exceptions: [], expected_version: 2 };
const reviewOutcome = {
  result_id: ids.activity,
  candidate_review_id: ids.evidence,
  candidate_id: ids.candidate,
  candidate_version: 3,
  candidate_status: "accepted",
  knowledge_node_id: ids.node,
  knowledge_revision_id: ids.revision,
  review_card_id: ids.card,
  evidence_ids: [ids.evidence],
};

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}

describe("extraction API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("schedules and reads a source_extract job with an idempotency key", async () => {
    const response = { job, extraction };
    const fetchMock = vi.fn().mockResolvedValue(json(response, 202));
    vi.stubGlobal("fetch", fetchMock);

    await expect(scheduleExtraction(ids.space, ids.source, ids.version, "extract-key")).resolves.toEqual(response);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`/sources/${ids.source}/versions/${ids.version}/extractions`);
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("extract-key");
  });

  it("gets the current extraction and lists extraction runs", async () => {
    const response = { job, extraction };
    const run = { ...response, source_title: "真实笔记" };
    const fetchMock = vi.fn().mockResolvedValueOnce(json(response)).mockResolvedValueOnce(json({ items: [run] }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getExtraction(ids.space, ids.source, ids.version)).resolves.toEqual(response);
    await expect(listExtractions(ids.space)).resolves.toEqual([run]);
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain(`/versions/${ids.version}/extraction`);
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain(`/knowledge-spaces/${ids.space}/extractions`);
  });

  it("validates all candidate statuses and destination records", async () => {
    const accepted = { ...candidate, status: "accepted", reviewed_at: now };
    const destinations = [{ id: ids.destination, name: "研究方法", path: ["知识树", "方法论"] }];
    const fetchMock = vi.fn().mockResolvedValueOnce(json({ items: [accepted] })).mockResolvedValueOnce(json({ items: destinations }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(listCandidates(ids.space, "accepted")).resolves.toEqual([accepted]);
    await expect(listKnowledgeDestinations(ids.space)).resolves.toEqual(destinations);
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("status=accepted");
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain("/knowledge-destinations");
  });

  it.each([
    ["PATCH", "", updateCandidate, draft],
    [
      "POST",
      "/mark-needs-verification",
      markCandidateNeedsVerification,
      { ...draft, reason: "需要人工核验来源证据" },
    ],
    ["POST", "/reject", rejectCandidate, draft],
  ] as const)("sends %s candidate writes with the full draft and expected version", async (method, suffix, call, expectedBody) => {
    const fetchMock = vi.fn().mockResolvedValue(json(reviewOutcome));
    vi.stubGlobal("fetch", fetchMock);

    await call(ids.space, ids.candidate, draft, "stable-key");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`/candidates/${ids.candidate}${suffix}`);
    expect(init.method).toBe(method);
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("stable-key");
    expect(JSON.parse(String(init.body))).toEqual(expectedBody);
  });

  it("validates the complete accept transaction response", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(reviewOutcome));
    vi.stubGlobal("fetch", fetchMock);

    await expect(acceptCandidate(ids.space, ids.candidate, draft, "accept-key")).resolves.toEqual(reviewOutcome);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`/candidates/${ids.candidate}/accept`);
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("accept-key");
    expect(JSON.parse(String(init.body))).toEqual(draft);
  });

  it("reads an idempotent review request for reconciliation", async () => {
    const response = { idempotency_key: "stable/key", action: "accept", status: "succeeded", result: reviewOutcome, error_code: null, error_message: null };
    const fetchMock = vi.fn().mockResolvedValue(json(response));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getCandidateReviewRequest(ids.space, ids.candidate, "stable/key")).resolves.toEqual(response);
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/review-requests/stable%2Fkey");
  });
});
