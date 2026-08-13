import { afterEach, describe, expect, it, vi } from "vitest";
import { createQaTurn, getQaTurnByIdempotencyKey, waitForQaTurn } from "./qa";

const ids = {
  space: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
  node: "77881bd1-0c4a-4cdb-b0d8-7ed52d25273f",
  turn: "a9a6e120-59b4-4ebc-a9b3-b4b81c7ee206",
};
const turn = {
  id: ids.turn,
  status: "abstained",
  question: "问题",
  scope_snapshot: { scope_node_id: ids.node },
  index_config_version: "d8-v1",
  ai_provider: "fake",
  ai_model: "fake",
  answer: null,
  abstain_code: "no_evidence",
  error_code: null,
  error_message: null,
  warnings: [],
  claims: [],
  citations: [],
};
function json(payload: unknown) { return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } }); }

afterEach(() => vi.unstubAllGlobals());

describe("qa client", () => {
  it("creates and reconciles a turn with a stable idempotency key", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(json(turn)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(createQaTurn(ids.space, { question: "问题", scopeNodeId: ids.node, includeDescendants: true }, "qa-key")).resolves.toEqual(turn);
    await expect(getQaTurnByIdempotencyKey(ids.space, "qa-key")).resolves.toEqual(turn);

    const createInit = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(createInit.headers).get("Idempotency-Key")).toBe("qa-key");
    expect(JSON.parse(String(createInit.body))).toEqual({ question: "问题", scope: { scope_node_id: ids.node, include_descendants: true } });
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain("by-idempotency-key/qa-key");
  });

  it("polls processing turns until the durable result is available", async () => {
    const processing = { ...turn, status: "processing", abstain_code: null };
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => Promise.resolve(json(processing)))
      .mockImplementationOnce(() => Promise.resolve(json(turn)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(waitForQaTurn(ids.space, "qa-key", { intervalMs: 0 })).resolves.toEqual(turn);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
