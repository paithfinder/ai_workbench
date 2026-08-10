import { afterEach, describe, expect, it, vi } from "vitest";
import {
  completeUpload,
  createSource,
  getJob,
  getSource,
  getSourceDetails,
  importPastedTextSource,
  importWebSource,
  listSourceSections,
  listSources,
  reparseSourceVersion,
  retryJob,
  reserveUpload,
} from "./sources";

const ids = {
  space: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
  source: "6ee8885f-e0ca-4e04-bf02-47d63b6c5881",
  version: "77881bd1-0c4a-4cdb-b0d8-7ed52d25273f",
  job: "7e8ee491-cd97-435c-97d4-82cc1951300c",
};
const now = "2026-07-31T12:00:00Z";
const source = {
  id: ids.source,
  space_id: ids.space,
  kind: "markdown",
  title: "真实笔记",
  status: "pending",
  created_at: now,
  updated_at: now,
};
const version = {
  id: ids.version,
  source_id: ids.source,
  version_number: 1,
  acquisition_type: "upload",
  source_uri: null,
  acquisition_metadata: {},
  original_filename: "真实笔记.md",
  media_type: "text/markdown",
  size_bytes: 8,
  content_sha256: null,
  processing_status: "pending",
  parse_status: "not_started",
  current_parse_artifact_id: null,
  upload_expires_at: "2026-07-31T12:15:00Z",
  completed_at: null,
  created_at: now,
};
const job = {
  id: ids.job,
  space_id: ids.space,
  source_version_id: ids.version,
  kind: "source_ingest",
  status: "queued",
  progress: 0,
  attempt_count: 0,
  retryable: false,
  error_code: null,
  error_message: null,
  started_at: null,
  finished_at: null,
  created_at: now,
  updated_at: now,
};

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status });
}

describe("source API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("lists and creates sources using the current space contract", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ items: [source] }))
      .mockResolvedValueOnce(json(source, 201));
    vi.stubGlobal("fetch", fetchMock);

    await expect(listSources(ids.space)).resolves.toEqual([source]);
    await expect(createSource(ids.space, "markdown", "真实笔记", "create-key")).resolves.toEqual(source);

    const [createUrl, createInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(createUrl).toBe(`http://localhost:8000/api/v1/knowledge-spaces/${ids.space}/sources`);
    expect(createInit.method).toBe("POST");
    expect(JSON.parse(String(createInit.body))).toEqual({ kind: "markdown", title: "真实笔记" });
    expect(new Headers(createInit.headers).get("Content-Type")).toBe("application/json");
    expect(new Headers(createInit.headers).get("Idempotency-Key")).toBe("create-key");
  });

  it("reserves and completes an upload with exact declarations and independent keys", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({
        version,
        upload_url: "http://storage.test/reserved",
        upload_headers: {},
        upload_fields: { key: "uploads/example", policy: "signed-policy" },
        expires_at: version.upload_expires_at,
      }, 201))
      .mockResolvedValueOnce(json({
        source: { ...source, status: "active" },
        version: { ...version, content_sha256: "a".repeat(64), completed_at: now },
        job: { id: ids.job, status: "queued" },
      }));
    vi.stubGlobal("fetch", fetchMock);

    await reserveUpload(ids.space, ids.source, {
      originalFilename: "真实笔记.md",
      mediaType: "text/markdown",
      sizeBytes: 8,
      contentSha256: "a".repeat(64),
    }, "reserve-key");
    await completeUpload(ids.space, ids.source, ids.version, "complete-key");

    const [, reserveInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(reserveInit.headers).get("Idempotency-Key")).toBe("reserve-key");
    expect(JSON.parse(String(reserveInit.body))).toEqual({
      original_filename: "真实笔记.md",
      media_type: "text/markdown",
      size_bytes: 8,
      content_sha256: "a".repeat(64),
    });

    const [completeUrl, completeInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(completeUrl).toContain(`/sources/${ids.source}/versions/${ids.version}/complete`);
    expect(completeInit.body).toBeUndefined();
    expect(new Headers(completeInit.headers).get("Idempotency-Key")).toBe("complete-key");
  });

  it("gets and retries a known job without inventing a request body", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json(job))
      .mockResolvedValueOnce(json(job));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getJob(ids.space, ids.job)).resolves.toEqual(job);
    await expect(retryJob(ids.space, ids.job, "retry-key")).resolves.toEqual(job);

    const [retryUrl, retryInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(retryUrl).toContain(`/jobs/${ids.job}/retry`);
    expect(retryInit).toMatchObject({ method: "POST" });
    expect(retryInit.body).toBeUndefined();
    expect(new Headers(retryInit.headers).get("Idempotency-Key")).toBe("retry-key");
  });

  it("accepts extraction and index job kinds returned by the backend", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ ...job, kind: "source_extract" }))
      .mockResolvedValueOnce(json({ ...job, kind: "source_index" }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getJob(ids.space, ids.job)).resolves.toMatchObject({ kind: "source_extract" });
    await expect(getJob(ids.space, ids.job)).resolves.toMatchObject({ kind: "source_index" });
  });

  it("accepts every persisted source kind while file creation stays limited", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({
      items: [
        { ...source, kind: "web", id: "b4e2ab8f-0808-40c1-8398-97ab424e5565" },
        { ...source, kind: "pasted_text", id: "38a79919-6376-421d-977c-66ac5a54f5e2" },
      ],
    })));

    await expect(listSources(ids.space)).resolves.toEqual([
      expect.objectContaining({ kind: "web" }),
      expect.objectContaining({ kind: "pasted_text" }),
    ]);
  });

  it("gets a source for reconciliation", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(source));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getSource(ids.space, ids.source)).resolves.toEqual(source);
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain(`/sources/${ids.source}`);
  });

  it("imports web and pasted text with exact endpoint bodies and stable operation keys", async () => {
    const imported = {
      source,
      version: { ...version, acquisition_type: "web_fetch", source_uri: "https://example.com/note", original_filename: null, upload_expires_at: null },
      job: { id: ids.job, status: "queued" },
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json(imported, 202))
      .mockResolvedValueOnce(json({
        ...imported,
        source: { ...source, kind: "pasted_text" },
        version: { ...imported.version, acquisition_type: "pasted_text", source_uri: null },
      }, 202));
    vi.stubGlobal("fetch", fetchMock);

    await importWebSource(ids.space, "网页笔记", "https://example.com/note", "web-key");
    await importPastedTextSource(ids.space, "粘贴笔记", "真实正文", "text-key");

    const [webUrl, webInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    const [textUrl, textInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(webUrl).toContain("/sources/web");
    expect(JSON.parse(String(webInit.body))).toEqual({ title: "网页笔记", url: "https://example.com/note" });
    expect(new Headers(webInit.headers).get("Idempotency-Key")).toBe("web-key");
    expect(textUrl).toContain("/sources/pasted-text");
    expect(JSON.parse(String(textInit.body))).toEqual({ title: "粘贴笔记", text: "真实正文" });
    expect(new Headers(textInit.headers).get("Idempotency-Key")).toBe("text-key");
  });

  it("gets details, paginated sections, and reparses without inventing a body", async () => {
    const artifact = {
      id: "a9a6e120-59b4-4ebc-a9b3-b4b81c7ee206",
      revision: 2,
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
    const parseJob = { ...job, kind: "source_parse", status: "succeeded", progress: 100 };
    const details = { source, versions: [{ version: { ...version, parse_status: "ready", current_parse_artifact_id: artifact.id }, parse_job: parseJob, current_parse_artifact: artifact, section_count: 1 }] };
    const section = {
      id: "c90d99d3-4080-4918-aae6-5d7dc59289c9",
      artifact_id: artifact.id,
      artifact_revision: 2,
      ordinal: 0,
      block_id: "paragraph-1",
      parent_block_id: null,
      block_type: "paragraph",
      title: null,
      text: "可信正文",
      heading_path: ["第一章"],
      page_number: 1,
      paragraph_index: 0,
      bbox: null,
      locator: { locatorType: "page_heading_paragraph" },
      quote_hash: "b".repeat(64),
      content_hash: "c".repeat(64),
      provenance: {},
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json(details))
      .mockResolvedValueOnce(json({ items: [section], next_cursor: "next/cursor", artifact }))
      .mockResolvedValueOnce(json({ job: parseJob, artifact }, 202));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getSourceDetails(ids.space, ids.source)).resolves.toEqual(details);
    await expect(listSourceSections(ids.space, ids.source, ids.version, { limit: 10, cursor: "cursor/一" })).resolves.toEqual({ items: [section], next_cursor: "next/cursor", artifact });
    await expect(reparseSourceVersion(ids.space, ids.source, ids.version, "reparse-key")).resolves.toEqual({ job: parseJob, artifact });

    const [sectionsUrl] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(sectionsUrl).toContain("limit=10&cursor=cursor%2F%E4%B8%80");
    const [reparseUrl, reparseInit] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(reparseUrl).toContain(`/versions/${ids.version}/reparse`);
    expect(reparseInit.body).toBeUndefined();
    expect(new Headers(reparseInit.headers).get("Idempotency-Key")).toBe("reparse-key");
  });

  it("passes AbortSignal through source requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ items: [] }));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await listSources(ids.space, { signal: controller.signal });

    expect((fetchMock.mock.calls[0]?.[1] as RequestInit).signal).toBe(controller.signal);
  });
});
