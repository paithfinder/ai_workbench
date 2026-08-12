import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchBootstrap } from "./bootstrap";

const bootstrapPayload = {
  space: {
    id: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
    slug: "my-knowledge-base",
    name: "我的知识库",
  },
  capabilities: {
    source_import: true,
    extraction_review: false,
    knowledge_tree: false,
    knowledge_folder_import: true,
    retrieval_debug: false,
    trusted_qa: false,
    spaced_review: false,
    evidence_agent: false,
  },
  limits: {
    knowledge_import: {
      max_entries: 500,
      max_folders: 250,
      max_depth: 32,
      max_total_body_utf8_bytes: 5 * 1024 * 1024,
      max_document_characters: 20_000,
      max_relative_path_characters: 4_000,
      allowed_extensions: [".md", ".txt"],
    },
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
      { headers: new Headers({ Accept: "application/json" }) },
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
      { headers: new Headers({ Accept: "application/json" }) },
    );
  });

  it("accepts an optional dynamic upload limit", async () => {
    const payload = { ...bootstrapPayload, max_upload_size_bytes: 10 * 1024 * 1024 };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload))));

    await expect(fetchBootstrap()).resolves.toEqual(payload);
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
      "API 返回了当前应用无法识别的数据",
    );
  });
});
