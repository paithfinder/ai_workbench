import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ImportWorkspace } from "./import-workspace";

const { calculateFileSha256, uploadFileWithProgress } = vi.hoisted(() => ({
  calculateFileSha256: vi.fn().mockResolvedValue("a".repeat(64)),
  uploadFileWithProgress: vi.fn().mockImplementation(
    ({ onProgress }: { onProgress: (value: number) => void }) => {
      onProgress(100);
      return Promise.resolve();
    },
  ),
}));

vi.mock("@/lib/file-import", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/file-import")>();
  return { ...actual, calculateFileSha256, uploadFileWithProgress };
});

const ids = {
  space: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
  source: "6ee8885f-e0ca-4e04-bf02-47d63b6c5881",
  version: "77881bd1-0c4a-4cdb-b0d8-7ed52d25273f",
  job: "7e8ee491-cd97-435c-97d4-82cc1951300c",
};
const now = "2026-07-31T12:00:00Z";

const bootstrap = {
  space: { id: ids.space, slug: "my-knowledge-base", name: "我的知识库" },
  capabilities: {
    source_import: true,
    extraction_review: false,
    knowledge_tree: false,
    retrieval_debug: false,
    trusted_qa: false,
    spaced_review: false,
    evidence_agent: false,
  },
  statistics: { sources: 0, queued_jobs: 0, activity_events: 0 },
  foundation_status: "ready",
};
const source = {
  id: ids.source,
  space_id: ids.space,
  kind: "markdown",
  title: "真实笔记",
  status: "active",
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
  content_sha256: "a".repeat(64),
  processing_status: "pending",
  parse_status: "not_started",
  current_parse_artifact_id: null,
  upload_expires_at: "2099-07-31T12:15:00Z",
  completed_at: now,
  created_at: now,
};

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderWorkspace() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ImportWorkspace />
    </QueryClientProvider>,
  );
}

function successfulFetch() {
  return vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url.endsWith("/api/v1/bootstrap")) return Promise.resolve(json(bootstrap));
    if (url.endsWith(`/knowledge-spaces/${ids.space}/sources`) && method === "GET") {
      return Promise.resolve(json({ items: [] }));
    }
    if (url.endsWith(`/knowledge-spaces/${ids.space}/sources`) && method === "POST") {
      return Promise.resolve(json({ ...source, status: "pending" }, 201));
    }
    if (url.endsWith(`/sources/${ids.source}/upload-reservations`)) {
      return Promise.resolve(json({
        version: { ...version, content_sha256: null, completed_at: null },
        upload_url: "http://storage.test/reserved",
        upload_headers: {},
        upload_fields: {
          key: "uploads/example",
          policy: "signed-policy",
          "Content-Type": "text/markdown",
        },
        expires_at: "2099-07-31T12:15:00Z",
      }, 201));
    }
    if (url.endsWith(`/sources/${ids.source}/versions/${ids.version}/complete`)) {
      return Promise.resolve(json({ source, version, job: { id: ids.job, status: "queued" } }));
    }
    if (url.endsWith(`/jobs/${ids.job}`)) {
      return Promise.resolve(json({
        id: ids.job,
        space_id: ids.space,
        source_version_id: ids.version,
        kind: "source_ingest",
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
      }));
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  });
}

describe("ImportWorkspace", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    calculateFileSha256.mockClear();
    uploadFileWithProgress.mockClear();
    let keySequence = 0;
    vi.stubGlobal("crypto", { randomUUID: vi.fn(() => `4d7bc6ee-2025-4b18-8d64-${String(++keySequence).padStart(12, "0")}`) });
  });

  it("uses bootstrap space, imports a real file declaration, and states the D3 boundary", async () => {
    const fetchMock = successfulFetch();
    vi.stubGlobal("fetch", fetchMock);
    renderWorkspace();

    const input = await screen.findByLabelText("选择文件或拖放到这里");
    const file = new File(["# note\n"], "真实笔记.md", { type: "text/markdown" });
    await userEvent.upload(input, file);

    expect(screen.getByText("text/markdown")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "校验并导入" }));

    expect(await screen.findByRole("heading", { name: "来源任务已完成" })).toBeInTheDocument();
    expect(screen.getByLabelText("本页当前导入")).toHaveTextContent("真实笔记");
    expect(screen.getByLabelText("本页当前导入")).toHaveTextContent(ids.job);
    expect(screen.getByText(/进入来源详情查看独立的版本解析状态/)).toBeInTheDocument();
    expect(calculateFileSha256).toHaveBeenCalledWith(file, expect.any(AbortSignal));
    expect(uploadFileWithProgress).toHaveBeenCalledWith(expect.objectContaining({
      url: "http://storage.test/reserved",
      headers: {},
      fields: expect.objectContaining({ key: "uploads/example", "Content-Type": "text/markdown" }),
      mediaType: "text/markdown",
      file,
    }));

    const calls = fetchMock.mock.calls.map(([url, init]) => ({
      url: String(url),
      method: (init as RequestInit | undefined)?.method ?? "GET",
      headers: new Headers((init as RequestInit | undefined)?.headers),
    }));
    expect(calls.some((call) => call.url.endsWith(`/knowledge-spaces/${ids.space}/sources`) && call.method === "POST")).toBe(true);
    expect(calls.filter((call) => call.headers.has("Idempotency-Key"))).toHaveLength(3);
  });

  it("resumes a failed multipart POST with the same source, version, and idempotency keys", async () => {
    const fetchMock = successfulFetch();
    vi.stubGlobal("fetch", fetchMock);
    uploadFileWithProgress
      .mockRejectedValueOnce(new Error("temporary storage failure"))
      .mockImplementationOnce(({ onProgress }: { onProgress: (value: number) => void }) => {
        onProgress(100);
        return Promise.resolve();
      });
    renderWorkspace();

    const input = await screen.findByLabelText("选择文件或拖放到这里");
    await userEvent.upload(input, new File(["resume"], "resume.txt", { type: "text/plain" }));
    await userEvent.click(screen.getByRole("button", { name: "校验并导入" }));

    expect(await screen.findByRole("button", { name: "继续导入" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "继续导入" }));
    expect(await screen.findByRole("heading", { name: "来源任务已完成" })).toBeInTheDocument();

    const createCalls = fetchMock.mock.calls.filter(([url, init]) =>
      String(url).endsWith(`/knowledge-spaces/${ids.space}/sources`) && init?.method === "POST",
    );
    const reserveCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith(`/sources/${ids.source}/upload-reservations`));
    const completeCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith(`/sources/${ids.source}/versions/${ids.version}/complete`));
    expect(createCalls).toHaveLength(1);
    expect(reserveCalls).toHaveLength(1);
    expect(completeCalls).toHaveLength(1);
    const keys = [createCalls[0], reserveCalls[0], completeCalls[0]].map((call) =>
      new Headers((call?.[1] as RequestInit).headers).get("Idempotency-Key"),
    );
    expect(keys.every(Boolean)).toBe(true);
    expect(uploadFileWithProgress).toHaveBeenCalledTimes(2);
  });

  it("creates a fresh reservation key after the previous reservation expires", async () => {
    const fetchMock = successfulFetch();
    const expiredReservation = {
      version: { ...version, content_sha256: null, completed_at: null, upload_expires_at: "2020-07-31T12:15:00Z" },
      upload_url: "http://storage.test/expired",
      upload_headers: {},
      upload_fields: { key: "uploads/expired", policy: "expired-policy", "Content-Type": "text/plain" },
      expires_at: "2020-07-31T12:15:00Z",
    };
    const freshReservation = {
      version: { ...version, content_sha256: null, completed_at: null },
      upload_url: "http://storage.test/fresh",
      upload_headers: {},
      upload_fields: { key: "uploads/fresh", policy: "fresh-policy", "Content-Type": "text/plain" },
      expires_at: "2099-07-31T12:15:00Z",
    };
    let reservationReads = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith(`/sources/${ids.source}/upload-reservations`)) {
        reservationReads += 1;
        return Promise.resolve(json(reservationReads === 1 ? expiredReservation : freshReservation, 201));
      }
      return successfulFetch()(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    uploadFileWithProgress
      .mockRejectedValueOnce(new Error("expired upload address"))
      .mockImplementationOnce(({ onProgress }: { onProgress: (value: number) => void }) => {
        onProgress(100);
        return Promise.resolve();
      });
    renderWorkspace();

    const input = await screen.findByLabelText("选择文件或拖放到这里");
    await userEvent.upload(input, new File(["resume"], "resume.txt", { type: "text/plain" }));
    await userEvent.click(screen.getByRole("button", { name: "校验并导入" }));
    expect(await screen.findByRole("button", { name: "继续导入" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "继续导入" }));
    expect(await screen.findByRole("heading", { name: "来源任务已完成" })).toBeInTheDocument();

    const reserveCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith(`/sources/${ids.source}/upload-reservations`));
    expect(reserveCalls).toHaveLength(2);
    const reserveKeys = reserveCalls.map((call) => new Headers((call[1] as RequestInit).headers).get("Idempotency-Key"));
    expect(reserveKeys[0]).not.toBe(reserveKeys[1]);
    expect(uploadFileWithProgress).toHaveBeenLastCalledWith(expect.objectContaining({ url: "http://storage.test/fresh" }));
  });

  it("shows client validation errors before any source is created", async () => {
    const fetchMock = successfulFetch();
    vi.stubGlobal("fetch", fetchMock);
    renderWorkspace();

    const input = await screen.findByLabelText("选择文件或拖放到这里");
    const unsafeFile = new File(["doc"], "unsafe.docx", { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
    Object.defineProperty(input, "files", { configurable: true, value: [unsafeFile] });
    input.dispatchEvent(new Event("change", { bubbles: true }));

    expect(await screen.findByRole("alert")).toHaveTextContent("仅支持扩展名为 .pdf、.md 或 .txt 的文件");
    expect(screen.getByRole("button", { name: "校验并导入" })).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("retries a retryable failed job with a fresh idempotency key", async () => {
    const baseFetch = successfulFetch();
    const failedJob = {
      id: ids.job,
      space_id: ids.space,
      source_version_id: ids.version,
      kind: "source_ingest",
      status: "failed",
      progress: 0,
      attempt_count: 1,
      retryable: true,
      error_code: "source_ingest_failed",
      error_message: "对象入库暂时失败",
      started_at: now,
      finished_at: now,
      created_at: now,
      updated_at: now,
    };
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith(`/jobs/${ids.job}`) && (init?.method ?? "GET") === "GET") {
        return Promise.resolve(json(failedJob));
      }
      if (url.endsWith(`/jobs/${ids.job}/retry`) && init?.method === "POST") {
        return Promise.resolve(json({
          ...failedJob,
          status: "queued",
          retryable: false,
          error_code: null,
          error_message: null,
          started_at: null,
          finished_at: null,
          updated_at: now,
        }));
      }
      return baseFetch(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWorkspace();

    const input = await screen.findByLabelText("选择文件或拖放到这里");
    await userEvent.upload(input, new File(["retry me"], "retry.txt", { type: "text/plain" }));
    await userEvent.click(screen.getByRole("button", { name: "校验并导入" }));

    expect(await screen.findByRole("heading", { name: "入库任务失败" })).toBeInTheDocument();
    expect(screen.getByText("对象入库暂时失败")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重试失败任务" }));

    expect(await screen.findByRole("heading", { name: "任务正在排队" })).toBeInTheDocument();
    const retryCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith(`/jobs/${ids.job}/retry`));
    expect(new Headers((retryCall?.[1] as RequestInit).headers).has("Idempotency-Key")).toBe(true);
  });

  it("uses a stable retry key and reconciles an uncertain retry response", async () => {
    const baseFetch = successfulFetch();
    const failedJob = {
      id: ids.job,
      space_id: ids.space,
      source_version_id: ids.version,
      kind: "source_ingest",
      status: "failed",
      progress: 0,
      attempt_count: 1,
      retryable: true,
      error_code: "source_ingest_failed",
      error_message: "对象入库暂时失败",
      started_at: now,
      finished_at: now,
      created_at: now,
      updated_at: now,
    };
    let jobReads = 0;
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith(`/jobs/${ids.job}/retry`)) return Promise.reject(new TypeError("response lost"));
      if (url.endsWith(`/jobs/${ids.job}`) && (init?.method ?? "GET") === "GET") {
        jobReads += 1;
        return Promise.resolve(json(jobReads === 1 ? failedJob : {
          ...failedJob,
          status: "queued",
          retryable: false,
          error_code: null,
          error_message: null,
          started_at: null,
          finished_at: null,
        }));
      }
      return baseFetch(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWorkspace();

    const input = await screen.findByLabelText("选择文件或拖放到这里");
    await userEvent.upload(input, new File(["retry me"], "retry.txt", { type: "text/plain" }));
    await userEvent.click(screen.getByRole("button", { name: "校验并导入" }));
    expect(await screen.findByRole("heading", { name: "入库任务失败" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重试失败任务" }));

    expect(await screen.findByRole("heading", { name: "任务正在排队" })).toBeInTheDocument();
    expect(jobReads).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("无法连接知识工作台 API")).not.toBeInTheDocument();
  });

  it("imports web and pasted text from accessible tabs using exact API fields", async () => {
    const fetchMock = successfulFetch();
    const webVersion = { ...version, acquisition_type: "web_fetch", source_uri: "https://example.com/note", original_filename: null, upload_expires_at: null };
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith(`/knowledge-spaces/${ids.space}/sources/web`)) return Promise.resolve(json({ source: { ...source, kind: "web", title: "网页笔记" }, version: webVersion, job: { id: ids.job, status: "queued" } }, 202));
      if (url.endsWith(`/knowledge-spaces/${ids.space}/sources/pasted-text`)) return Promise.resolve(json({ source: { ...source, kind: "pasted_text", title: "文本笔记" }, version: { ...webVersion, acquisition_type: "pasted_text", source_uri: null }, job: { id: ids.job, status: "queued" } }, 202));
      return successfulFetch()(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWorkspace();

    const tablist = await screen.findByRole("tablist", { name: "来源类型" });
    expect(tablist).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "网页" }));
    await userEvent.type(screen.getByLabelText("来源标题"), "网页笔记");
    await userEvent.type(screen.getByLabelText("网页地址"), "https://example.com/note");
    await userEvent.click(screen.getByRole("button", { name: "导入网页" }));
    expect(await screen.findByRole("link", { name: "查看来源详情" })).toHaveAttribute("href", `/sources/${ids.source}`);

    const webCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/sources/web"));
    expect(JSON.parse(String((webCall?.[1] as RequestInit).body))).toEqual({ title: "网页笔记", url: "https://example.com/note" });

    await userEvent.click(screen.getByRole("tab", { name: "粘贴文本" }));
    await userEvent.type(screen.getByLabelText("来源标题"), "文本笔记");
    await userEvent.type(screen.getByLabelText("正文"), "真实正文");
    await userEvent.click(screen.getByRole("button", { name: "导入文本" }));
    const textCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/sources/pasted-text"));
    expect(JSON.parse(String((textCall?.[1] as RequestInit).body))).toEqual({ title: "文本笔记", text: "真实正文" });
  });

  it("renders recent sources returned by the current default space", async () => {
    const fetchMock = successfulFetch();
    fetchMock.mockImplementationOnce(() => Promise.resolve(json(bootstrap)));
    fetchMock.mockImplementationOnce(() => Promise.resolve(json({ items: [source] })));
    vi.stubGlobal("fetch", fetchMock);
    renderWorkspace();

    expect(await screen.findByText("真实笔记")).toBeInTheDocument();
    expect(screen.getByText("来源可用")).toBeInTheDocument();
    expect(screen.getByText(/打开来源详情可查看真实版本/)).toBeInTheDocument();
    expect(screen.queryByLabelText("本页当前导入")).not.toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("does not offer file import when the bootstrap capability is off", async () => {
    const disabledBootstrap = {
      ...bootstrap,
      capabilities: { ...bootstrap.capabilities, source_import: false },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(disabledBootstrap)));
    renderWorkspace();

    expect(await screen.findByRole("heading", { name: "来源导入尚未开放" })).toBeInTheDocument();
    expect(screen.queryByLabelText("选择文件或拖放到这里")).not.toBeInTheDocument();
  });

  it("shows a bootstrap error and only loads sources after the retry succeeds", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({
        error: {
          code: "internal_error",
          message: "暂时不可用",
          details: [],
          request_id: "request-bootstrap",
        },
      }, 503))
      .mockResolvedValueOnce(json(bootstrap))
      .mockResolvedValueOnce(json({ items: [] }));
    vi.stubGlobal("fetch", fetchMock);
    renderWorkspace();

    expect(await screen.findByRole("heading", { name: "无法取得默认知识空间" })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "重试连接" }));

    expect(await screen.findByRole("heading", { name: "选择来源类型" })).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });
});
