import type { components } from "@ai-workbench/api-contract";
import { z } from "zod";

type Schemas = components["schemas"];

export type HealthData = Schemas["HealthData"];
export type Repository = Schemas["RepositorySummaryResponse"];
export type RepositoryList = Schemas["RepositoryListResponse"];
export type AuthorizationPreview = Schemas["AuthorizationPreviewResponse"];
export type AuthorizationResult = Schemas["AuthorizationResponse"];
export type ScanResult = Schemas["ScanResponse"];
export type RevocationResult = Schemas["RevocationResponse"];

type PreviewRequest = Schemas["LocalAuthorizationPreviewRequest"];
type AuthorizationRequest = Schemas["LocalAuthorizationRequest"];
type ScanRequest = Schemas["ScanRequestBody"];
type RevocationRequest = Schemas["RevocationRequestBody"];

const errorDetailSchema = z.object({
  code: z.string(),
  message: z.string(),
  details: z.unknown().nullish(),
}).strict();

const healthDataSchema: z.ZodType<HealthData> = z.object({
  app: z.string(),
  database: z.enum(["connected", "unavailable"]),
  model_configured: z.boolean(),
  status: z.enum(["ok", "degraded"]),
  version: z.string(),
}).strict().refine(
  (data) => (data.status === "ok" && data.database === "connected")
    || (data.status === "degraded" && data.database === "unavailable"),
  { message: "Health status and database readiness are inconsistent" },
);

const repositoryStatsSchema = z.object({
  eligible_bytes: z.number().int().nonnegative(),
  eligible_files: z.number().int().nonnegative(),
}).strict();

export const repositorySchema: z.ZodType<Repository> = z.object({
  authorization_epoch: z.number().int().nonnegative(),
  authorization_status: z.enum(["pending", "authorized", "revoked"]),
  authorized_at: z.string().datetime({ offset: true }).nullable(),
  canonical_path: z.string(),
  id: z.string().uuid(),
  indexing_state: z.enum(["not_queued", "pending", "running", "succeeded", "failed", "cancelled"]),
  manifest_hash: z.string().nullable(),
  name: z.string(),
  revoked_at: z.string().datetime({ offset: true }).nullable(),
  scan_state: z.enum(["not_scanned", "manifest_ready"]),
  stats: repositoryStatsSchema,
}).strict();

const repositoryListSchema: z.ZodType<RepositoryList> = z.object({
  repositories: z.array(repositorySchema),
  space: z.object({
    id: z.string().uuid(),
    name: z.string(),
    slug: z.literal("personal-development"),
  }).strict(),
}).strict();

const authorizationPreviewSchema: z.ZodType<AuthorizationPreview> = z.object({
  canonical_path: z.string(),
  display_name: z.string(),
  expires_at: z.string().datetime({ offset: true }),
  policy_version: z.string(),
  preview_token: z.string(),
  security_notice: z.string(),
  state: z.literal("ready"),
}).strict();

const authorizationResultSchema: z.ZodType<AuthorizationResult> = z.object({
  outcome: z.enum(["authorized", "already_authorized", "reauthorized"]),
  repository: repositorySchema,
}).strict();

const scanResultSchema: z.ZodType<ScanResult> = z.object({
  index_job_id: z.string().uuid().nullable(),
  indexing_state: z.enum(["not_queued", "pending"]),
  manifest_hash: z.string(),
  outcome: z.enum(["created", "unchanged"]),
  source_version_id: z.string().uuid(),
  stats: z.object({
    directories_visited: z.number().int().nonnegative(),
    eligible_bytes: z.number().int().nonnegative(),
    eligible_files: z.number().int().nonnegative(),
    files_seen: z.number().int().nonnegative(),
    skipped: z.record(z.string(), z.number().int().nonnegative()),
  }).strict(),
}).strict();

const revocationResultSchema: z.ZodType<RevocationResult> = z.object({
  outcome: z.enum(["revoked", "already_revoked"]),
  repository: repositorySchema,
}).strict();

export class ApiClientError extends Error {
  readonly code: string;
  readonly requestId?: string;
  readonly status?: number;

  constructor(message: string, options: { code: string; requestId?: string; status?: number }) {
    super(message);
    this.name = "ApiClientError";
    this.code = options.code;
    this.requestId = options.requestId;
    this.status = options.status;
  }
}

function normalizeBaseUrl(baseUrl: string) {
  return baseUrl.replace(/\/+$/, "");
}

function errorFromPayload(payload: unknown, status: number): ApiClientError {
  const envelope = z.object({
    data: z.unknown().nullish(),
    error: errorDetailSchema.nullish(),
    request_id: z.string(),
  }).strict().safeParse(payload);

  if (envelope.success && envelope.data.error) {
    return new ApiClientError(envelope.data.error.message, {
      code: envelope.data.error.code,
      requestId: envelope.data.request_id,
      status,
    });
  }

  return new ApiClientError(`API 请求失败（HTTP ${status}）`, {
    code: "http_error",
    requestId: envelope.success ? envelope.data.request_id : undefined,
    status,
  });
}

const defaultTimeoutMs = 8_000;
// The API accepts a configured repository scan limit up to 600 seconds.
const defaultScanTimeoutMs = 605_000;

function requestFailure(
  timeoutSignal: AbortSignal,
  callerSignal: AbortSignal | null | undefined,
): ApiClientError {
  const timedOut = timeoutSignal.aborted && !callerSignal?.aborted;
  return new ApiClientError(
    timedOut ? "API 请求超时" : callerSignal?.aborted ? "API 请求已取消" : "无法连接本地 API",
    { code: timedOut ? "timeout" : callerSignal?.aborted ? "aborted" : "network_error" },
  );
}

async function requestEnvelope<T>(
  baseUrl: string,
  path: string,
  schema: z.ZodType<T>,
  init?: RequestInit,
  timeoutMs = defaultTimeoutMs,
): Promise<T> {
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  const signal = init?.signal
    ? AbortSignal.any([init.signal, timeoutSignal])
    : timeoutSignal;
  let response: Response;
  try {
    response = await fetch(`${normalizeBaseUrl(baseUrl)}${path}`, {
      ...init,
      signal,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
  } catch {
    throw requestFailure(timeoutSignal, init?.signal);
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    if (signal.aborted) throw requestFailure(timeoutSignal, init?.signal);
    throw new ApiClientError("API 返回了无法解析的响应", {
      code: "invalid_json",
      status: response.status,
    });
  }

  if (!response.ok) throw errorFromPayload(payload, response.status);

  const envelope = z.object({
    data: schema.nullish(),
    error: errorDetailSchema.nullish(),
    request_id: z.string(),
  }).strict().safeParse(payload);

  if (!envelope.success || !envelope.data.data || envelope.data.error) {
    throw new ApiClientError("API 响应不符合已生成的契约", {
      code: "invalid_contract",
      requestId: envelope.success ? envelope.data.request_id : undefined,
      status: response.status,
    });
  }

  return envelope.data.data;
}

function postJson<T>(baseUrl: string, path: string, body: unknown, schema: z.ZodType<T>, timeoutMs: number) {
  return requestEnvelope(baseUrl, path, schema, {
    method: "POST",
    body: JSON.stringify(body),
  }, timeoutMs);
}

export function createApiClient(
  baseUrl: string,
  timeoutMs = defaultTimeoutMs,
  scanTimeoutMs = Math.max(defaultScanTimeoutMs, timeoutMs),
) {
  return {
    health(signal?: AbortSignal) {
      return requestEnvelope(baseUrl, "/health", healthDataSchema, { signal }, timeoutMs);
    },
    listRepositories(signal?: AbortSignal) {
      return requestEnvelope(baseUrl, "/repositories", repositoryListSchema, { signal }, timeoutMs);
    },
    previewAuthorization(body: PreviewRequest) {
      return postJson(baseUrl, "/repositories/local/authorization-previews", body, authorizationPreviewSchema, timeoutMs);
    },
    authorizeRepository(body: AuthorizationRequest) {
      return postJson(baseUrl, "/repositories/local/authorizations", body, authorizationResultSchema, timeoutMs);
    },
    scanRepository(repositoryId: string, body: ScanRequest) {
      return postJson(
        baseUrl,
        `/repositories/${encodeURIComponent(repositoryId)}/scans`,
        body,
        scanResultSchema,
        scanTimeoutMs,
      );
    },
    revokeRepository(repositoryId: string, body: RevocationRequest) {
      return postJson(
        baseUrl,
        `/repositories/${encodeURIComponent(repositoryId)}/revocation`,
        body,
        revocationResultSchema,
        timeoutMs,
      );
    },
  };
}

export type ApiClient = ReturnType<typeof createApiClient>;
