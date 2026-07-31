import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchBootstrap } from "./bootstrap";

const bootstrapPayload = {
  space: {
    id: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
    slug: "my-knowledge-base",
    name: "我的知识库",
  },
  capabilities: {
    source_import: false,
    extraction_review: false,
    knowledge_tree: false,
    trusted_qa: false,
    spaced_review: false,
    evidence_agent: false,
  },
  statistics: {
    sources: 0,
    queued_jobs: 0,
    activity_events: 0,
  },
  foundation_status: "ready",
} as const;

describe("fetchBootstrap", () => {
  afterEach(() => {
    delete process.env.NEXT_PUBLIC_API_BASE_URL;
    vi.unstubAllGlobals();
  });

  it("uses the local API by default and parses the frozen contract", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(bootstrapPayload)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchBootstrap()).resolves.toEqual(bootstrapPayload);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/bootstrap",
      { headers: { Accept: "application/json" } },
    );
  });

  it("uses NEXT_PUBLIC_API_BASE_URL without duplicating a trailing slash", async () => {
    process.env.NEXT_PUBLIC_API_BASE_URL = "https://knowledge.example.test/";
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(bootstrapPayload)));
    vi.stubGlobal("fetch", fetchMock);

    await fetchBootstrap();

    expect(fetchMock).toHaveBeenCalledWith(
      "https://knowledge.example.test/api/v1/bootstrap",
      { headers: { Accept: "application/json" } },
    );
  });

  it("rejects missing capabilities and non-ready foundations", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            ...bootstrapPayload,
            capabilities: { source_import: true },
            foundation_status: "starting",
          }),
        ),
      ),
    );

    await expect(fetchBootstrap()).rejects.toThrow(
      "首页数据格式与当前应用不兼容",
    );
  });
});
