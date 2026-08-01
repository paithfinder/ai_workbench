import { z } from "zod";
import { requestJson } from "./api";

const uuid = z.string().uuid();
const dateTime = z.string().datetime({ offset: true });

export const fileSourceKindSchema = z.enum(["pdf", "markdown", "text"]);
export type FileSourceKind = z.infer<typeof fileSourceKindSchema>;
export const persistedSourceKindSchema = z.enum([
  "pdf",
  "markdown",
  "text",
  "web",
  "pasted_text",
]);
export type PersistedSourceKind = z.infer<typeof persistedSourceKindSchema>;

export const sourceStatusSchema = z.enum(["pending", "active", "failed", "deleted"]);
export type SourceStatus = z.infer<typeof sourceStatusSchema>;

export const sourceSchema = z.object({
  id: uuid,
  space_id: uuid,
  kind: persistedSourceKindSchema,
  title: z.string().min(1),
  status: sourceStatusSchema,
  created_at: dateTime,
  updated_at: dateTime,
});
export type Source = z.infer<typeof sourceSchema>;

export const sourceVersionSchema = z.object({
  id: uuid,
  source_id: uuid,
  version_number: z.number().int().positive(),
  original_filename: z.string().min(1),
  media_type: z.string().min(1),
  size_bytes: z.number().int().positive(),
  content_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  processing_status: z.enum(["pending", "ready", "failed"]),
  parse_status: z.enum(["not_started", "queued", "parsing", "ready", "failed"]),
  upload_expires_at: dateTime,
  completed_at: dateTime.nullable(),
  created_at: dateTime,
});
export type SourceVersion = z.infer<typeof sourceVersionSchema>;

export const jobStatusSchema = z.enum([
  "queued",
  "running",
  "succeeded",
  "failed",
  "cancelled",
]);
export type JobStatus = z.infer<typeof jobStatusSchema>;

export const jobSchema = z.object({
  id: uuid,
  space_id: uuid,
  source_version_id: uuid.nullable(),
  kind: z.literal("source_ingest"),
  status: jobStatusSchema,
  progress: z.number().int().min(0).max(100),
  attempt_count: z.number().int().nonnegative(),
  retryable: z.boolean(),
  error_code: z.string().nullable(),
  error_message: z.string().nullable(),
  started_at: dateTime.nullable(),
  finished_at: dateTime.nullable(),
  created_at: dateTime,
  updated_at: dateTime,
});
export type Job = z.infer<typeof jobSchema>;

const sourcesListSchema = z.object({ items: z.array(sourceSchema) });

const uploadReservationSchema = z.object({
  version: sourceVersionSchema,
  upload_url: z.string().url(),
  upload_headers: z.record(z.string(), z.string()),
  upload_fields: z.record(z.string(), z.string()),
  expires_at: dateTime,
});
export type UploadReservation = z.infer<typeof uploadReservationSchema>;

const completeUploadSchema = z.object({
  source: sourceSchema,
  version: sourceVersionSchema,
  job: z.object({ id: uuid, status: jobStatusSchema }),
});
export type CompleteUpload = z.infer<typeof completeUploadSchema>;

type ReservationDeclaration = {
  originalFilename: string;
  mediaType: string;
  sizeBytes: number;
  contentSha256: string;
};

type RequestOptions = { signal?: AbortSignal };

function spacePath(spaceId: string) {
  return `/api/v1/knowledge-spaces/${encodeURIComponent(spaceId)}`;
}

export async function listSources(
  spaceId: string,
  options: RequestOptions = {},
): Promise<Source[]> {
  const result = await requestJson(`${spacePath(spaceId)}/sources`, sourcesListSchema, {
    signal: options.signal,
  });
  return result.items;
}

export function getSource(
  spaceId: string,
  sourceId: string,
  options: RequestOptions = {},
) {
  return requestJson(
    `${spacePath(spaceId)}/sources/${encodeURIComponent(sourceId)}`,
    sourceSchema,
    { signal: options.signal },
  );
}

export function createSource(
  spaceId: string,
  kind: FileSourceKind,
  title: string,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(`${spacePath(spaceId)}/sources`, sourceSchema, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ kind, title }),
    signal: options.signal,
  });
}

export function reserveUpload(
  spaceId: string,
  sourceId: string,
  declaration: ReservationDeclaration,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(
    `${spacePath(spaceId)}/sources/${encodeURIComponent(sourceId)}/upload-reservations`,
    uploadReservationSchema,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        original_filename: declaration.originalFilename,
        media_type: declaration.mediaType,
        size_bytes: declaration.sizeBytes,
        content_sha256: declaration.contentSha256,
      }),
      signal: options.signal,
    },
  );
}

export function completeUpload(
  spaceId: string,
  sourceId: string,
  versionId: string,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(
    `${spacePath(spaceId)}/sources/${encodeURIComponent(sourceId)}/versions/${encodeURIComponent(versionId)}/complete`,
    completeUploadSchema,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      signal: options.signal,
    },
  );
}

export function getJob(spaceId: string, jobId: string, options: RequestOptions = {}) {
  return requestJson(
    `${spacePath(spaceId)}/jobs/${encodeURIComponent(jobId)}`,
    jobSchema,
    { signal: options.signal },
  );
}

export function retryJob(
  spaceId: string,
  jobId: string,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(
    `${spacePath(spaceId)}/jobs/${encodeURIComponent(jobId)}/retry`,
    jobSchema,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      signal: options.signal,
    },
  );
}
