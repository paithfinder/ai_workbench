import { z } from "zod";
import { createIdempotencyKey, requestJson } from "./api";
import type { Bootstrap } from "./bootstrap";

const uuid = z.string().uuid();

export const knowledgeImportItemSchema = z.object({
  ordinal: z.number().int().nonnegative(),
  relative_path: z.string().min(1),
  kind: z.enum(["folder", "document"]),
  node_id: uuid,
  revision_id: uuid,
  content_hash: z.string().regex(/^[0-9a-f]{64}$/),
});

export const knowledgeImportResponseSchema = z.object({
  request_id: uuid,
  idempotency_key: z.string().min(1),
  status: z.literal("succeeded"),
  root_node_id: uuid,
  summary: z.object({
    entry_count: z.number().int().positive(),
    folder_count: z.number().int().positive(),
    document_count: z.number().int().positive(),
    total_body_utf8_bytes: z.number().int().nonnegative(),
  }),
  items: z.array(knowledgeImportItemSchema).min(2),
});

export type KnowledgeImportResponse = z.infer<typeof knowledgeImportResponseSchema>;
export type KnowledgeImportLimits = Bootstrap["limits"]["knowledge_import"];
export type KnowledgeImportDocument = { relative_path: string; body: string };
export type KnowledgeImportManifest = {
  rootName: string;
  documents: KnowledgeImportDocument[];
  folderCount: number;
  entryCount: number;
  totalBodyUtf8Bytes: number;
  idempotencyKey: string;
};

export class KnowledgeImportValidationError extends Error {
  readonly issues: string[];

  constructor(issues: string[]) {
    super(issues[0] ?? "文件夹无法导入");
    this.name = "KnowledgeImportValidationError";
    this.issues = issues;
  }
}

type DirectoryFile = File & { webkitRelativePath?: string };

function normalizeSegment(segment: string, label: string, issues: string[]) {
  const normalized = segment.normalize("NFC");
  if (!normalized || normalized === "." || normalized === "..") {
    issues.push(`${label} 包含空目录或保留路径`);
  } else if (normalized !== normalized.trim()) {
    issues.push(`${label} 的目录或文件名不能以空格开头或结尾`);
  } else if (normalized.length > 500) {
    issues.push(`${label} 的目录或文件名超过 500 个字符`);
  } else if ([...normalized].some((character) => /\p{Cc}/u.test(character))) {
    issues.push(`${label} 包含控制字符`);
  }
  return normalized;
}

function unicodeCharacters(value: string) {
  return [...value].length;
}

function readFileBytes(file: File) {
  if (typeof file.arrayBuffer === "function") return file.arrayBuffer();
  return new Promise<ArrayBuffer>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("无法读取文件"));
    reader.onload = () => {
      if (reader.result instanceof ArrayBuffer) resolve(reader.result);
      else reject(new Error("无法读取文件"));
    };
    reader.readAsArrayBuffer(file);
  });
}

async function decodeUtf8(file: File) {
  const bytes = new Uint8Array(await readFileBytes(file));
  let body: string;
  try {
    body = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new Error("不是有效的 UTF-8 文本");
  }
  return { body: body.startsWith("﻿") ? body.slice(1) : body, bytes: bytes.byteLength };
}

export async function prepareKnowledgeImport(
  files: FileList | File[],
  limits: KnowledgeImportLimits,
  options: { mode?: "folder" | "files" } = {},
): Promise<KnowledgeImportManifest> {
  const selected = Array.from(files) as DirectoryFile[];
  const issues: string[] = [];
  if (!selected.length) throw new KnowledgeImportValidationError(["没有选择文件"]);
  const mode = options.mode ?? "folder";
  const standaloneRootName = mode === "files"
    ? (selected.length === 1
      ? selected[0].name.replace(/\.[^.]+$/, "") || "导入的笔记"
      : "导入的笔记")
    : null;

  const roots = new Set<string>();
  const normalizedEntries: Array<{ file: DirectoryFile; parts: string[] }> = [];
  for (const file of selected) {
    const rawPath = mode === "files"
      ? `${standaloneRootName}/${file.name}`
      : file.webkitRelativePath || file.name;
    if (!rawPath || rawPath.startsWith("/") || rawPath.includes("\\")) {
      issues.push(`${file.name} 没有安全的相对路径`);
      continue;
    }
    const rawParts = rawPath.split("/");
    if (rawParts.length < 2 || rawParts.some((part) => !part)) {
      issues.push(`${rawPath} 不包含可用的文件夹路径`);
      continue;
    }
    const parts = rawParts.map((part) => normalizeSegment(part, rawPath, issues));
    roots.add(parts[0].toLocaleLowerCase());
    normalizedEntries.push({ file, parts });
  }
  if (roots.size !== 1 || !normalizedEntries.length) {
    issues.push("一次只能导入一个顶层文件夹");
  }
  const rootName = normalizedEntries[0]?.parts[0] ?? "";
  const seen = new Map<string, string>();
  const folders = new Set<string>();
  const documents: KnowledgeImportDocument[] = [];
  let totalBodyUtf8Bytes = 0;

  for (const { file, parts } of normalizedEntries) {
    const relativeParts = parts.slice(1);
    const relativePath = relativeParts.join("/");
    if (relativePath.length > limits.max_relative_path_characters) {
      issues.push(`${relativePath} 的相对路径过长`);
      continue;
    }
    if (parts.length > limits.max_depth) {
      issues.push(`${relativePath} 超过 ${limits.max_depth} 层目录限制`);
      continue;
    }
    const extension = file.name.slice(file.name.lastIndexOf(".")).toLocaleLowerCase();
    if (!limits.allowed_extensions.includes(extension)) {
      issues.push(`${relativePath} 不是支持的 Markdown 或 TXT 文件`);
      continue;
    }
    const key = relativePath.normalize("NFC").toLocaleLowerCase();
    const conflict = seen.get(key);
    if (conflict) {
      issues.push(`${relativePath} 与 ${conflict} 路径冲突`);
      continue;
    }
    seen.set(key, relativePath);
    relativeParts.slice(0, -1).forEach((_, index) => {
      folders.add(relativeParts.slice(0, index + 1).join("/").toLocaleLowerCase());
    });
    try {
      const decoded = await decodeUtf8(file);
      if (unicodeCharacters(decoded.body) > limits.max_document_characters) {
        issues.push(`${relativePath} 超过 ${limits.max_document_characters} 字符限制`);
        continue;
      }
      totalBodyUtf8Bytes += new TextEncoder().encode(decoded.body).byteLength;
      documents.push({ relative_path: relativePath, body: decoded.body });
    } catch (error) {
      issues.push(`${relativePath}：${error instanceof Error ? error.message : "无法读取"}`);
    }
  }

  const folderCount = folders.size + 1;
  const entryCount = folderCount + documents.length;
  if (entryCount > limits.max_entries) issues.push(`总节点数超过 ${limits.max_entries} 个限制`);
  if (folderCount > limits.max_folders) issues.push(`文件夹数超过 ${limits.max_folders} 个限制`);
  if (totalBodyUtf8Bytes > limits.max_total_body_utf8_bytes) {
    issues.push("所有文档正文总量超过导入限制");
  }
  if (!documents.length) issues.push("所选文件夹中没有可导入的 Markdown 或 TXT 文件");
  if (issues.length) throw new KnowledgeImportValidationError([...new Set(issues)]);

  documents.sort((left, right) => {
    const leftKey = left.relative_path.normalize("NFC").toLocaleLowerCase();
    const rightKey = right.relative_path.normalize("NFC").toLocaleLowerCase();
    if (leftKey < rightKey) return -1;
    if (leftKey > rightKey) return 1;
    return left.relative_path < right.relative_path ? -1 : left.relative_path > right.relative_path ? 1 : 0;
  });
  return {
    rootName,
    documents,
    folderCount,
    entryCount,
    totalBodyUtf8Bytes,
    idempotencyKey: createIdempotencyKey(),
  };
}

function spacePath(spaceId: string) {
  return `/api/v1/knowledge-spaces/${encodeURIComponent(spaceId)}`;
}

export function createKnowledgeImport(
  spaceId: string,
  input: {
    parentId: string | null;
    expectedParentVersion: number;
    manifest: KnowledgeImportManifest;
  },
  options: { signal?: AbortSignal } = {},
) {
  return requestJson(
    `${spacePath(spaceId)}/knowledge-imports`,
    knowledgeImportResponseSchema,
    {
      method: "POST",
      headers: { "Idempotency-Key": input.manifest.idempotencyKey },
      body: JSON.stringify({
        parent_id: input.parentId,
        expected_parent_version: input.expectedParentVersion,
        root_name: input.manifest.rootName,
        documents: input.manifest.documents,
      }),
      signal: options.signal,
    },
  );
}

export function getKnowledgeImportRequest(
  spaceId: string,
  idempotencyKey: string,
  options: { signal?: AbortSignal } = {},
) {
  return requestJson(
    `${spacePath(spaceId)}/knowledge-import-requests/${encodeURIComponent(idempotencyKey)}`,
    knowledgeImportResponseSchema,
    { signal: options.signal },
  );
}
