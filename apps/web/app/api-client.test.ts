import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "./api-client";

const health = {
  app: "ai-workbench",
  database: "connected" as const,
  model_configured: true,
  status: "ok" as const,
  version: "0.1.0",
};

function response(data: unknown) {
  return new Response(JSON.stringify({ data, error: null, request_id: "request-1" }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("runtime validated API client", () => {
  it("accepts a valid typed health envelope", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(health)));
    await expect(createApiClient("http://api.test/api/v1/").health()).resolves.toEqual(health);
    expect(fetch).toHaveBeenCalledWith("http://api.test/api/v1/health", expect.objectContaining({ cache: "no-store" }));
  });

  it("fails closed when a successful health response violates the contract", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ ...health, database: "maybe" })));
    await expect(createApiClient("http://api.test/api/v1").health()).rejects.toMatchObject({
      code: "invalid_contract",
    });
  });

  it("fails closed when health status and database readiness are inconsistent", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response({ ...health, database: "unavailable" }))
      .mockResolvedValueOnce(response({ ...health, status: "degraded" })));
    const client = createApiClient("http://api.test/api/v1");
    await expect(client.health()).rejects.toMatchObject({ code: "invalid_contract" });
    await expect(client.health()).rejects.toMatchObject({ code: "invalid_contract" });
  });

  it("classifies caller cancellation without exposing low-level details", async () => {
    const controller = new AbortController();
    vi.stubGlobal("fetch", vi.fn((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("secret URL", "AbortError")));
    })));

    const request = createApiClient("http://api.test/api/v1").health(controller.signal);
    controller.abort();
    await expect(request).rejects.toMatchObject({ code: "aborted", message: "API 请求已取消" });
  });

  it("times out bounded requests with a stable error", async () => {
    vi.stubGlobal("fetch", vi.fn((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("internal endpoint", "TimeoutError")));
    })));

    const request = createApiClient("http://api.test/api/v1", 5).health();
    await expect(request).rejects.toMatchObject({ code: "timeout", message: "API 请求超时" });
  });

  it("rejects invalid repository date-time and scan UUID formats", async () => {
    const repository = {
      authorization_epoch: 1,
      authorization_status: "authorized",
      authorized_at: "not-a-date",
      canonical_path: "D:\\workbench\\project",
      id: "11111111-1111-4111-8111-111111111111",
      indexing_state: "not_queued",
      manifest_hash: null,
      name: "project",
      revoked_at: null,
      scan_state: "not_scanned",
      stats: { eligible_bytes: 0, eligible_files: 0 },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      repositories: [repository],
      space: {
        id: "22222222-2222-4222-8222-222222222222",
        name: "Personal Development",
        slug: "personal-development",
      },
    })));
    await expect(createApiClient("http://api.test/api/v1").listRepositories()).rejects.toMatchObject({
      code: "invalid_contract",
    });

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      index_job_id: "not-a-uuid",
      indexing_state: "pending",
      manifest_hash: "manifest",
      outcome: "created",
      source_version_id: "44444444-4444-4444-8444-444444444444",
      stats: { directories_visited: 1, eligible_bytes: 1, eligible_files: 1, files_seen: 1, skipped: {} },
    })));
    await expect(createApiClient("http://api.test/api/v1").scanRepository(
      "11111111-1111-4111-8111-111111111111",
      { expected_authorization_epoch: 1 },
    )).rejects.toMatchObject({ code: "invalid_contract" });
  });

  it("classifies body-read cancellation instead of reporting invalid JSON", async () => {
    const controller = new AbortController();
    const body = new ReadableStream({
      start(streamController) {
        controller.signal.addEventListener("abort", () => streamController.error(new DOMException("cancelled", "AbortError")));
      },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));

    const request = createApiClient("http://api.test/api/v1").health(controller.signal);
    controller.abort();
    await expect(request).rejects.toMatchObject({ code: "aborted", message: "API 请求已取消" });
  });

  it("keeps structured API errors and request ids", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      data: null,
      error: { code: "stale_authorization_epoch", message: "Authorization changed", details: null },
      request_id: "request-stale",
    }), { status: 409 })));

    await expect(createApiClient("http://api.test/api/v1").scanRepository(
      "11111111-1111-4111-8111-111111111111",
      { expected_authorization_epoch: 1 },
    )).rejects.toMatchObject({
      code: "stale_authorization_epoch",
      requestId: "request-stale",
      status: 409,
    });
  });
});
