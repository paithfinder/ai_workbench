import { afterEach, describe, expect, it, vi } from "vitest";
import {
  FileValidationError,
  MAX_IMPORT_SIZE_BYTES,
  calculateFileSha256,
  sourceTitleFromFilename,
  uploadFileWithProgress,
  validateImportFile,
} from "./file-import";

class MockXMLHttpRequest extends EventTarget {
  static latest: MockXMLHttpRequest;
  readonly upload = new EventTarget();
  readonly headers = new Map<string, string>();
  method = "";
  url = "";
  status = 0;
  timeout = 0;
  body: FormData | null = null;

  constructor() {
    super();
    MockXMLHttpRequest.latest = this;
  }

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(name: string, value: string) {
    this.headers.set(name, value);
  }

  send(body: FormData) {
    this.body = body;
  }

  abort() {
    this.dispatchEvent(new Event("abort"));
  }
}

describe("file import helpers", () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each([
    ["paper.PDF", "application/pdf", "pdf", "application/pdf"],
    ["notes.md", "", "markdown", "text/markdown"],
    ["plain.txt", "text/plain; charset=utf-8", "text", "text/plain"],
  ])("validates %s", (name, type, kind, mediaType) => {
    const file = new File(["content"], name, { type });
    expect(validateImportFile(file)).toMatchObject({ kind, mediaType, file });
  });

  it.each([
    [new File(["content"], "notes.docx"), "仅支持扩展名"],
    [new File([], "empty.txt", { type: "text/plain" }), "不能导入空文件"],
    [new File(["content"], "fake.pdf", { type: "text/plain" }), "文件类型与扩展名不一致"],
  ])("rejects an invalid declaration", (file, message) => {
    expect(() => validateImportFile(file)).toThrowError(FileValidationError);
    expect(() => validateImportFile(file)).toThrow(message);
  });

  it("rejects files over 25 MiB without allocating a large body", () => {
    const file = new File(["x"], "large.txt", { type: "text/plain" });
    Object.defineProperty(file, "size", { value: MAX_IMPORT_SIZE_BYTES + 1 });
    expect(() => validateImportFile(file)).toThrow("文件不能超过 25 MiB");
  });

  it("matches backend filename rules and accepts a supplied upload limit", () => {
    expect(() => validateImportFile(new File(["x"], "dir\\notes.md", { type: "text/markdown" }))).toThrow("文件名无效");
    expect(() => validateImportFile(new File(["x"], "notes.md", { type: "text/markdown" }))).toThrow("文件名无效");
    const file = new File(["xx"], "notes.md", { type: "text/markdown" });
    expect(() => validateImportFile(file, 1)).toThrow("文件不能超过 1 B");
  });

  it("calculates a lowercase SHA-256 and derives the source title", async () => {
    const file = new File(["abc"], "reading.notes.md", { type: "text/markdown" });
    Object.defineProperty(file, "arrayBuffer", {
      value: vi.fn().mockResolvedValue(new TextEncoder().encode("abc").buffer),
    });
    await expect(calculateFileSha256(file)).resolves.toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
    expect(sourceTitleFromFilename(file.name)).toBe("reading.notes");
  });

  it("posts policy fields and the file as multipart data while reporting progress", async () => {
    vi.stubGlobal("XMLHttpRequest", MockXMLHttpRequest);
    const progress = vi.fn();
    const file = new File(["content"], "notes.md", { type: "text/markdown" });
    const upload = uploadFileWithProgress({
      url: "http://storage.test/upload",
      headers: { "Content-Type": "multipart/form-data; stale-boundary=bad", "X-Signed": "yes" },
      fields: { key: "uploads/example", policy: "signed-policy" },
      mediaType: "text/markdown",
      file,
      onProgress: progress,
      timeoutMs: 12_000,
    });
    const xhr = MockXMLHttpRequest.latest;

    xhr.upload.dispatchEvent(
      new ProgressEvent("progress", { lengthComputable: true, loaded: 2, total: 4 }),
    );
    xhr.status = 200;
    xhr.dispatchEvent(new Event("load"));

    await expect(upload).resolves.toBeUndefined();
    expect(xhr).toMatchObject({ method: "POST", url: "http://storage.test/upload", timeout: 12_000 });
    expect(xhr.body).toBeInstanceOf(FormData);
    expect(Array.from(xhr.body!.entries()).map(([name, value]) => [name, value instanceof File ? value.name : value])).toEqual([
      ["key", "uploads/example"],
      ["policy", "signed-policy"],
      ["Content-Type", "text/markdown"],
      ["file", "notes.md"],
    ]);
    expect(xhr.headers.get("Content-Type")).toBeUndefined();
    expect(xhr.headers.get("X-Signed")).toBe("yes");
    expect(progress).toHaveBeenNthCalledWith(1, 50);
    expect(progress).toHaveBeenLastCalledWith(100);
  });

  it("rejects non-success upload responses", async () => {
    vi.stubGlobal("XMLHttpRequest", MockXMLHttpRequest);
    const upload = uploadFileWithProgress({
      url: "http://storage.test/upload",
      headers: {},
      fields: { "Content-Type": "text/plain" },
      mediaType: "text/plain",
      file: new File(["content"], "notes.txt"),
      onProgress: vi.fn(),
    });
    MockXMLHttpRequest.latest.status = 403;
    MockXMLHttpRequest.latest.dispatchEvent(new Event("load"));
    await expect(upload).rejects.toThrow("文件上传失败（HTTP 403）");
  });

  it("normalizes storage timeout and network failures and supports aborting the POST", async () => {
    vi.stubGlobal("XMLHttpRequest", MockXMLHttpRequest);
    const timedOutUpload = uploadFileWithProgress({
      url: "http://storage.test/upload",
      headers: {},
      fields: { "Content-Type": "text/plain" },
      mediaType: "text/plain",
      file: new File(["content"], "notes.txt"),
      onProgress: vi.fn(),
      timeoutMs: 1,
    });
    MockXMLHttpRequest.latest.dispatchEvent(new Event("timeout"));
    await expect(timedOutUpload).rejects.toThrow("文件上传超时");

    const failedUpload = uploadFileWithProgress({
      url: "http://storage.test/upload",
      headers: {},
      fields: { "Content-Type": "text/plain" },
      mediaType: "text/plain",
      file: new File(["content"], "notes.txt"),
      onProgress: vi.fn(),
    });
    MockXMLHttpRequest.latest.dispatchEvent(new Event("error"));
    await expect(failedUpload).rejects.toThrow("对象存储连接或跨域配置");

    const controller = new AbortController();
    const abortedUpload = uploadFileWithProgress({
      url: "http://storage.test/upload",
      headers: {},
      fields: { "Content-Type": "text/plain" },
      mediaType: "text/plain",
      file: new File(["content"], "notes.txt"),
      onProgress: vi.fn(),
      signal: controller.signal,
    });
    controller.abort();
    await expect(abortedUpload).rejects.toMatchObject({ name: "AbortError" });
  });
});
