import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SourceDetail } from "./source-detail";

const ids = {
  space: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
  source: "6ee8885f-e0ca-4e04-bf02-47d63b6c5881",
  version: "77881bd1-0c4a-4cdb-b0d8-7ed52d25273f",
  job: "7e8ee491-cd97-435c-97d4-82cc1951300c",
  artifact: "a9a6e120-59b4-4ebc-a9b3-b4b81c7ee206",
  section: "c90d99d3-4080-4918-aae6-5d7dc59289c9",
};
const now = "2026-07-31T12:00:00Z";
const bootstrap = {
  space: { id: ids.space, slug: "my-knowledge-base", name: "我的知识库" },
  capabilities: { source_import: true, extraction_review: false, knowledge_tree: false, trusted_qa: false, spaced_review: false, evidence_agent: false },
  statistics: { sources: 1, queued_jobs: 0, activity_events: 0 },
  foundation_status: "ready",
};
const source = { id: ids.source, space_id: ids.space, kind: "markdown", title: "真实笔记", status: "active", created_at: now, updated_at: now };
const version = {
  id: ids.version,
  source_id: ids.source,
  version_number: 1,
  acquisition_type: "upload",
  source_uri: null,
  acquisition_metadata: {},
  original_filename: "真实笔记.md",
  media_type: "text/markdown",
  size_bytes: 20,
  content_sha256: "a".repeat(64),
  processing_status: "ready",
  parse_status: "ready",
  current_parse_artifact_id: ids.artifact,
  upload_expires_at: null,
  completed_at: now,
  created_at: now,
};
const artifact = {
  id: ids.artifact,
  revision: 1,
  status: "ready",
  parser_name: "docling",
  parser_version: "1.0",
  parser_config: {},
  page_count: 1,
  warnings: [],
  error_code: null,
  error_message: null,
  started_at: now,
  completed_at: now,
};
const parseJob = {
  id: ids.job,
  space_id: ids.space,
  source_version_id: ids.version,
  kind: "source_parse",
  status: "succeeded",
  progress: 100,
  attempt_count: 1,
  retryable: false,
  error_code: null,
  error_message: null,
  started_at: now,
  finished_at: now,
  created_at: now,
  updated_at: now,
};
const details = { source, versions: [{ version, parse_job: parseJob, current_parse_artifact: artifact, section_count: 2 }] };
const section = {
  id: ids.section,
  artifact_id: ids.artifact,
  artifact_revision: 1,
  ordinal: 0,
  block_id: "paragraph-1",
  parent_block_id: null,
  block_type: "paragraph",
  title: null,
  text: "第一段可信正文",
  heading_path: ["第一章"],
  page_number: 1,
  paragraph_index: 0,
  bbox: null,
  locator: { locatorType: "page_heading_paragraph" },
  quote_hash: "b".repeat(64),
  content_hash: "c".repeat(64),
  provenance: {},
};

function json(payload: unknown, status = 200) { return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } }); }
function renderDetail() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  return render(<QueryClientProvider client={client}><SourceDetail sourceId={ids.source} /></QueryClientProvider>);
}

describe("SourceDetail", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal("crypto", { randomUUID: vi.fn(() => "4d7bc6ee-2025-4b18-8d64-000000000001") });
  });

  it("renders real parse metadata, locators, and cursor pagination", async () => {
    let sectionReads = 0;
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/bootstrap")) return Promise.resolve(json(bootstrap));
      if (url.endsWith(`/sources/${ids.source}/details`)) return Promise.resolve(json(details));
      if (url.includes(`/versions/${ids.version}/sections`)) {
        sectionReads += 1;
        return Promise.resolve(json({ items: [{ ...section, id: sectionReads === 1 ? ids.section : "247197b5-4e02-4442-a0a6-280151242198", ordinal: sectionReads - 1, text: sectionReads === 1 ? section.text : "第二段可信正文" }], next_cursor: sectionReads === 1 ? "next/cursor" : null, artifact }));
      }
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDetail();

    expect(await screen.findByRole("heading", { name: "真实笔记" })).toBeInTheDocument();
    expect(await screen.findByText("第一段可信正文")).toBeInTheDocument();
    await userEvent.click(screen.getByText("第 1 页 · 第一章 · 段落 1"));
    expect(screen.getByText(ids.section)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "加载更多分段" }));
    expect(await screen.findByText("第二段可信正文")).toBeInTheDocument();
    const sectionUrls = fetchMock.mock.calls.map(([url]) => String(url)).filter((url) => url.includes("/sections?"));
    expect(sectionUrls[1]).toContain("cursor=next%2Fcursor");
  });

  it("reconciles an uncertain reparse response and keeps the same operation key", async () => {
    let detailReads = 0;
    const queuedArtifact = { ...artifact, id: "1c24fab5-62ec-4844-afb9-6f35fd466730", revision: 2, status: "queued", completed_at: null };
    const queuedJob = { ...parseJob, id: "f5eb215d-ab89-47dc-943b-f51e8f199700", status: "queued", progress: 0, finished_at: null };
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/bootstrap")) return Promise.resolve(json(bootstrap));
      if (url.endsWith(`/versions/${ids.version}/sections`)) return Promise.resolve(json({ items: [], next_cursor: null, artifact }));
      if (url.endsWith(`/versions/${ids.version}/reparse`) && init?.method === "POST") return Promise.reject(new TypeError("response lost"));
      if (url.endsWith(`/sources/${ids.source}/details`)) {
        detailReads += 1;
        return Promise.resolve(json(detailReads === 1 ? details : { source, versions: [{ version: { ...version, parse_status: "queued", current_parse_artifact_id: queuedArtifact.id }, parse_job: queuedJob, current_parse_artifact: queuedArtifact, section_count: 0 }] }));
      }
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDetail();

    await userEvent.click(await screen.findByRole("button", { name: "重新解析此版本" }));
    await waitFor(() => expect(screen.getAllByText("等待解析")).toHaveLength(2));
    expect(screen.queryByText("无法连接知识工作台 API")).not.toBeInTheDocument();
    const reparseCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/reparse"));
    expect(new Headers((reparseCall?.[1] as RequestInit).headers).get("Idempotency-Key")).toBe("4d7bc6ee-2025-4b18-8d64-000000000001");
    expect(detailReads).toBeGreaterThanOrEqual(2);
  });
});
