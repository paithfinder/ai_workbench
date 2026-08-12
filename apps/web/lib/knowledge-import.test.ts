import { describe, expect, it, vi } from "vitest";
import {
  createKnowledgeImport,
  getKnowledgeImportRequest,
  KnowledgeImportValidationError,
  prepareKnowledgeImport,
  type KnowledgeImportLimits,
} from "./knowledge-import";

const limits: KnowledgeImportLimits = {
  max_entries: 500,
  max_folders: 250,
  max_depth: 32,
  max_total_body_utf8_bytes: 5 * 1024 * 1024,
  max_document_characters: 20_000,
  max_relative_path_characters: 4_000,
  allowed_extensions: [".md", ".txt"],
};
const ids = {
  space: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
  root: "77881bd1-0c4a-4cdb-b0d8-7ed52d25273f",
  request: "7e8ee491-cd97-435c-97d4-82cc1951300c",
  revision: "a9a6e120-59b4-4ebc-a9b3-b4b81c7ee206",
};

function directoryFile(path: string, body: string | Uint8Array) {
  const file = new File([body], path.split("/").at(-1)!, { type: "text/plain" });
  Object.defineProperty(file, "webkitRelativePath", { value: path });
  return file;
}

function response() {
  return {
    request_id: ids.request,
    idempotency_key: "import-key",
    status: "succeeded",
    root_node_id: ids.root,
    summary: { entry_count: 2, folder_count: 1, document_count: 1, total_body_utf8_bytes: 6 },
    items: [
      { ordinal: 0, relative_path: ".", kind: "folder", node_id: ids.root, revision_id: ids.revision, content_hash: "a".repeat(64) },
      { ordinal: 1, relative_path: "说明.md", kind: "document", node_id: ids.request, revision_id: ids.revision, content_hash: "b".repeat(64) },
    ],
  } as const;
}

describe("knowledge folder import", () => {
  it("preserves nested relative paths and strict UTF-8 text", async () => {
    vi.stubGlobal("crypto", { randomUUID: () => "import-key" });
    const manifest = await prepareKnowledgeImport([
      directoryFile("知识库/编程/Python/异步.md", "﻿# 异步\r\n正文"),
      directoryFile("知识库/产品/需求.txt", "需求"),
    ], limits);

    expect(manifest.rootName).toBe("知识库");
    expect(manifest.folderCount).toBe(4);
    expect(manifest.entryCount).toBe(6);
    expect(manifest.documents).toEqual([
      { relative_path: "产品/需求.txt", body: "需求" },
      { relative_path: "编程/Python/异步.md", body: "# 异步\r\n正文" },
    ]);
  });

  it("rejects unsupported, invalid UTF-8, duplicate, and oversized files", async () => {
    await expect(prepareKnowledgeImport([
      directoryFile("知识库/note.pdf", "pdf"),
      directoryFile("知识库/bad.md", new Uint8Array([0xff])),
      directoryFile("知识库/A.md", "one"),
      directoryFile("知识库/a.md", "two"),
      directoryFile("知识库/long.txt", "x".repeat(20_001)),
    ], limits)).rejects.toSatisfy((error: unknown) => {
      expect(error).toBeInstanceOf(KnowledgeImportValidationError);
      const issues = (error as KnowledgeImportValidationError).issues.join("\n");
      expect(issues).toContain("不是支持的");
      expect(issues).toContain("不是有效的 UTF-8");
      expect(issues).toContain("路径冲突");
      expect(issues).toContain("超过 20000 字符");
      return true;
    });
  });

  it("posts one atomic manifest and reconciles by encoded key", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify(response()), {
        headers: { "Content-Type": "application/json" },
      }),
    ));
    vi.stubGlobal("fetch", fetchMock);
    const manifest = {
      rootName: "知识库",
      documents: [{ relative_path: "说明.md", body: "正文" }],
      folderCount: 1,
      entryCount: 2,
      totalBodyUtf8Bytes: 6,
      idempotencyKey: "stable/key",
    };

    await createKnowledgeImport(ids.space, { parentId: null, expectedParentVersion: 1, manifest });
    await getKnowledgeImportRequest(ids.space, "stable/key");

    const calls = fetchMock.mock.calls as Array<[string, RequestInit]>;
    expect(calls[0][0]).toContain("/knowledge-imports");
    expect(new Headers(calls[0][1].headers).get("Idempotency-Key")).toBe("stable/key");
    expect(JSON.parse(String(calls[0][1].body))).toEqual({
      parent_id: null,
      expected_parent_version: 1,
      root_name: "知识库",
      documents: [{ relative_path: "说明.md", body: "正文" }],
    });
    expect(calls[1][0]).toContain("knowledge-import-requests/stable%2Fkey");
  });
});
