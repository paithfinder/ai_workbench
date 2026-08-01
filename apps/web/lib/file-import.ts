import type { FileSourceKind } from "./sources";

export const MAX_IMPORT_SIZE_BYTES = 25 * 1024 * 1024;

const supportedFiles = {
  pdf: {
    extension: ".pdf",
    mediaType: "application/pdf",
    kind: "pdf",
  },
  md: {
    extension: ".md",
    mediaType: "text/markdown",
    kind: "markdown",
  },
  txt: {
    extension: ".txt",
    mediaType: "text/plain",
    kind: "text",
  },
} as const satisfies Record<
  string,
  { extension: string; mediaType: string; kind: FileSourceKind }
>;

export type ValidatedFile = {
  file: File;
  kind: FileSourceKind;
  mediaType: string;
};

export class FileValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "FileValidationError";
  }
}

function extensionOf(filename: string) {
  const index = filename.lastIndexOf(".");
  return index < 0 ? "" : filename.slice(index + 1).toLowerCase();
}

function normalizedMediaType(type: string) {
  return type.split(";", 1)[0]?.trim().toLowerCase() ?? "";
}

function validateFilename(filename: string) {
  const trimmed = filename.trim();
  const hasControlCharacter = Array.from(trimmed).some((character) => character.charCodeAt(0) < 32);
  if (
    !trimmed ||
    trimmed.includes("/") ||
    trimmed.includes("\\") ||
    hasControlCharacter
  ) {
    throw new FileValidationError("文件名无效，不能包含路径或控制字符");
  }
  if (trimmed.length > 500) {
    throw new FileValidationError("文件名不能超过 500 个字符");
  }
}

export function validateImportFile(
  file: File,
  maxSizeBytes = MAX_IMPORT_SIZE_BYTES,
): ValidatedFile {
  validateFilename(file.name);
  const definition = supportedFiles[extensionOf(file.name) as keyof typeof supportedFiles];
  if (!definition) {
    throw new FileValidationError("仅支持扩展名为 .pdf、.md 或 .txt 的文件");
  }
  if (file.size === 0) {
    throw new FileValidationError("不能导入空文件");
  }
  if (file.size > maxSizeBytes) {
    throw new FileValidationError(`文件不能超过 ${formatFileSize(maxSizeBytes)}`);
  }

  const actualMediaType = normalizedMediaType(file.type);
  if (actualMediaType && actualMediaType !== definition.mediaType) {
    throw new FileValidationError(
      `文件类型与扩展名不一致，应为 ${definition.mediaType}`,
    );
  }

  return {
    file,
    kind: definition.kind,
    mediaType: definition.mediaType,
  };
}

export async function calculateFileSha256(file: File, signal?: AbortSignal) {
  signal?.throwIfAborted();
  if (!globalThis.crypto?.subtle) {
    throw new Error("当前浏览器不支持文件完整性校验");
  }

  const bytes = await file.arrayBuffer();
  signal?.throwIfAborted();
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  signal?.throwIfAborted();
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function formatFileSize(sizeBytes: number) {
  if (sizeBytes < 1024) return `${sizeBytes} B`;
  if (sizeBytes < 1024 * 1024) return `${(sizeBytes / 1024).toFixed(1)} KiB`;
  const mebibytes = sizeBytes / (1024 * 1024);
  return `${Number.isInteger(mebibytes) ? mebibytes : mebibytes.toFixed(1)} MiB`;
}

export function sourceTitleFromFilename(filename: string) {
  const lastDot = filename.lastIndexOf(".");
  return (lastDot > 0 ? filename.slice(0, lastDot) : filename).trim();
}

type UploadOptions = {
  url: string;
  headers: Record<string, string>;
  fields: Record<string, string>;
  mediaType: string;
  file: File;
  onProgress: (percent: number) => void;
  signal?: AbortSignal;
  timeoutMs?: number;
};

export function uploadFileWithProgress({
  url,
  headers,
  fields,
  mediaType,
  file,
  onProgress,
  signal,
  timeoutMs,
}: UploadOptions): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let settled = false;

    const finish = (callback: () => void) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", abortUpload);
      callback();
    };

    const abortUpload = () => xhr.abort();

    xhr.open("POST", url);
    if (timeoutMs !== undefined) xhr.timeout = Math.max(1, timeoutMs);
    Object.entries(headers).forEach(([name, value]) => {
      if (name.toLowerCase() !== "content-type") xhr.setRequestHeader(name, value);
    });

    const formData = new FormData();
    Object.entries(fields).forEach(([name, value]) => formData.append(name, value));
    if (!("Content-Type" in fields)) formData.append("Content-Type", mediaType);
    formData.append("file", file);

    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.min(100, Math.round((event.loaded / event.total) * 100)));
      }
    });
    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        finish(() => {
          onProgress(100);
          resolve();
        });
      } else {
        finish(() => reject(new Error(`文件上传失败（HTTP ${xhr.status}）`)));
      }
    });
    xhr.addEventListener("error", () =>
      finish(() => reject(new Error("文件上传失败，请检查对象存储连接或跨域配置"))),
    );
    xhr.addEventListener("timeout", () =>
      finish(() => reject(new Error("文件上传超时，预留地址可能即将或已经过期"))),
    );
    xhr.addEventListener("abort", () =>
      finish(() => reject(new DOMException("文件上传已取消", "AbortError"))),
    );

    if (signal?.aborted) {
      abortUpload();
      return;
    }
    signal?.addEventListener("abort", abortUpload, { once: true });
    xhr.send(formData);
  });
}
