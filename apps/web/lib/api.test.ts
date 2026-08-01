import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  createIdempotencyKey,
  getApiUrl,
  parseApiError,
  requestJson,
} from "./api";
import { z } from "zod";

const schema = z.object({ value: z.string() });

describe("API client", () => {
  afterEach(() => {
    delete process.env.NEXT_PUBLIC_API_BASE_URL;
    vi.unstubAllGlobals();
  });

  it("builds API URLs and parses successful JSON without relying on response headers", async () => {
    process.env.NEXT_PUBLIC_API_BASE_URL = "https://knowledge.example.test/";
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ value: "ready" })),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(requestJson("/api/v1/example", schema)).resolves.toEqual({ value: "ready" });
    expect(getApiUrl("api/v1/example")).toBe("https://knowledge.example.test/api/v1/example");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Accept")).toBe("application/json");
  });

  it("preserves the backend error envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              code: "upload_too_large",
              message: "上传文件过大",
              details: [{ max_bytes: 26_214_400 }],
              request_id: "request-7",
            },
          }),
          { status: 413 },
        ),
      ),
    );

    const error = await requestJson("/api/v1/example", schema).catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 413,
      code: "upload_too_large",
      message: "上传文件过大",
      requestId: "request-7",
    });
  });

  it("falls back to the response request ID for an unrecognized error body", () => {
    const error = parseApiError(
      new Response("not json", {
        status: 502,
        headers: { "X-Request-ID": "gateway-request-9" },
      }),
      null,
    );

    expect(error).toMatchObject({
      status: 502,
      code: "unexpected_api_error",
      requestId: "gateway-request-9",
    });
  });

  it("rejects invalid success payloads and normalizes network failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response("{}")));
    await expect(requestJson("/invalid", schema)).rejects.toMatchObject({
      code: "invalid_api_response",
    });

    vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new TypeError("offline")));
    await expect(requestJson("/offline", schema)).rejects.toMatchObject({
      status: 0,
      code: "network_error",
    });
  });

  it("generates idempotency keys through Web Crypto and fails explicitly without it", () => {
    const randomUUID = vi.fn(() => "4d7bc6ee-2025-4b18-8d64-22d8c53337df");
    vi.stubGlobal("crypto", { randomUUID });
    expect(createIdempotencyKey()).toBe("4d7bc6ee-2025-4b18-8d64-22d8c53337df");

    vi.stubGlobal("crypto", {});
    expect(() => createIdempotencyKey()).toThrow("当前浏览器无法生成安全的请求标识");
  });
});
