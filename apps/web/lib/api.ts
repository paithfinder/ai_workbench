import { z, type ZodType } from "zod";

const DEFAULT_API_URL = "http://localhost:8000";

const apiErrorEnvelopeSchema = z.object({
  error: z.object({
    code: z.string().min(1),
    message: z.string().min(1),
    details: z.array(z.record(z.string(), z.unknown())).default([]),
    request_id: z.string().min(1).optional(),
  }),
});

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Array<Record<string, unknown>>;
  readonly requestId?: string;

  constructor({
    status,
    code,
    message,
    details = [],
    requestId,
  }: {
    status: number;
    code: string;
    message: string;
    details?: Array<Record<string, unknown>>;
    requestId?: string;
  }) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

export function getApiUrl(path: string) {
  const baseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? DEFAULT_API_URL;
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${baseUrl.replace(/\/$/, "")}${normalizedPath}`;
}

async function parseResponseBody(response: Response): Promise<unknown> {
  if (response.status === 204) return null;

  try {
    return await response.json();
  } catch {
    return null;
  }
}

export function parseApiError(response: Response, payload: unknown) {
  const parsed = apiErrorEnvelopeSchema.safeParse(payload);
  if (parsed.success) {
    return new ApiError({
      status: response.status,
      code: parsed.data.error.code,
      message: parsed.data.error.message,
      details: parsed.data.error.details,
      requestId: parsed.data.error.request_id ?? response.headers.get("X-Request-ID") ?? undefined,
    });
  }

  return new ApiError({
    status: response.status,
    code: "unexpected_api_error",
    message: `请求失败（HTTP ${response.status}）`,
    requestId: response.headers.get("X-Request-ID") ?? undefined,
  });
}

export async function requestJson<T>(
  path: string,
  schema: ZodType<T>,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body !== undefined && init.body !== null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(getApiUrl(path), { ...init, headers });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError({
      status: 0,
      code: "network_error",
      message: "无法连接知识工作台 API",
      details: error instanceof Error ? [{ cause: error.message }] : [],
    });
  }

  const payload = await parseResponseBody(response);
  if (!response.ok) throw parseApiError(response, payload);

  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    throw new ApiError({
      status: response.status,
      code: "invalid_api_response",
      message: "API 返回了当前应用无法识别的数据",
      details: [{ issues: parsed.error.issues }],
    });
  }

  return parsed.data;
}

export function createIdempotencyKey() {
  if (!globalThis.crypto?.randomUUID) {
    throw new Error("当前浏览器无法生成安全的请求标识");
  }
  return globalThis.crypto.randomUUID();
}
