import { z } from "zod";
import { requestJson } from "./api";

const uuid = z.string().uuid();
const dateTime = z.string().datetime({ offset: true });
const sha256 = z.string().regex(/^[0-9a-f]{64}$/);

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

export const acquisitionTypeSchema = z.enum(["upload", "pasted_text", "web_fetch"]);
export const parseStatusSchema = z.enum(["not_started", "queued", "parsing", "ready", "failed"]);

export const sourceVersionSchema = z.object({
  id: uuid,
  source_id: uuid,
  version_number: z.number().int().positive(),
  acquisition_type: acquisitionTypeSchema,
  source_uri: z.string().nullable(),
  acquisition_metadata: z.record(z.string(), z.unknown()),
  original_filename: z.string().min(1).nullable(),
  media_type: z.string().min(1).nullable(),
  size_bytes: z.number().int().positive().nullable(),
  content_sha256: sha256.nullable(),
  processing_status: z.enum(["pending", "ready", "failed"]),
  parse_status: parseStatusSchema,
  current_parse_artifact_id: uuid.nullable(),
  upload_expires_at: dateTime.nullable(),
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

export const jobKindSchema = z.enum([
  "source_ingest",
  "source_parse",
  "source_extract",
  "source_index",
]);
export type JobKind = z.infer<typeof jobKindSchema>;

export const jobSchema = z.object({
  id: uuid,
  space_id: uuid,
  source_version_id: uuid.nullable(),
  kind: jobKindSchema,
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

export const parseArtifactSchema = z.object({
  id: uuid,
  revision: z.number().int().positive(),
  status: z.enum(["queued", "parsing", "ready", "failed"]),
  parser_name: z.string().min(1),
  parser_version: z.string().min(1),
  parser_config: z.record(z.string(), z.unknown()),
  page_count: z.number().int().nonnegative().nullable(),
  warnings: z.array(z.unknown()),
  error_code: z.string().nullable(),
  error_message: z.string().nullable(),
  started_at: dateTime.nullable(),
  completed_at: dateTime.nullable(),
});
export type ParseArtifact = z.infer<typeof parseArtifactSchema>;

export const sourceVersionDetailSchema = z.object({
  version: sourceVersionSchema,
  parse_job: jobSchema.nullable(),
  current_parse_artifact: parseArtifactSchema.nullable(),
  section_count: z.number().int().nonnegative(),
});
export type SourceVersionDetail = z.infer<typeof sourceVersionDetailSchema>;

export const sourceDetailsSchema = z.object({
  source: sourceSchema,
  versions: z.array(sourceVersionDetailSchema),
});
export type SourceDetails = z.infer<typeof sourceDetailsSchema>;

export const sectionSchema = z.object({
  id: uuid,
  artifact_id: uuid,
  artifact_revision: z.number().int().positive(),
  ordinal: z.number().int().nonnegative(),
  block_id: z.string().min(1),
  parent_block_id: z.string().nullable(),
  block_type: z.string().min(1),
  title: z.string().nullable(),
  text: z.string(),
  heading_path: z.array(z.string()),
  page_number: z.number().int().positive().nullable(),
  paragraph_index: z.number().int().nonnegative().nullable(),
  bbox: z.record(z.string(), z.unknown()).nullable(),
  locator: z.record(z.string(), z.unknown()),
  quote_hash: sha256,
  content_hash: sha256,
  provenance: z.record(z.string(), z.unknown()),
});
export type SourceSection = z.infer<typeof sectionSchema>;

export const sectionsPageSchema = z.object({
  items: z.array(sectionSchema),
  previous_cursor: z.string().min(1).nullable(),
  next_cursor: z.string().min(1).nullable(),
  artifact: parseArtifactSchema,
});
export type SectionsPage = z.infer<typeof sectionsPageSchema>;

const sourcesListSchema = z.object({ items: z.array(sourceSchema) });

const uploadReservationSchema = z.object({
  version: sourceVersionSchema,
  upload_url: z.string().url(),
  upload_headers: z.record(z.string(), z.string()),
  upload_fields: z.record(z.string(), z.string()),
  expires_at: dateTime,
});
export type UploadReservation = z.infer<typeof uploadReservationSchema>;

const jobReferenceSchema = z.object({ id: uuid, status: jobStatusSchema });
const importSourceSchema = z.object({
  source: sourceSchema,
  version: sourceVersionSchema,
  job: jobReferenceSchema,
});
export type ImportedSource = z.infer<typeof importSourceSchema>;
export type CompleteUpload = ImportedSource;

const reparseResponseSchema = z.object({
  job: jobSchema,
  artifact: parseArtifactSchema,
});
export type ReparseResponse = z.infer<typeof reparseResponseSchema>;

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

function sourcePath(spaceId: string, sourceId: string) {
  return `${spacePath(spaceId)}/sources/${encodeURIComponent(sourceId)}`;
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
  return requestJson(sourcePath(spaceId, sourceId), sourceSchema, {
    signal: options.signal,
  });
}

export function getSourceDetails(
  spaceId: string,
  sourceId: string,
  options: RequestOptions = {},
) {
  return requestJson(`${sourcePath(spaceId, sourceId)}/details`, sourceDetailsSchema, {
    signal: options.signal,
  });
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

function importNonFileSource(
  spaceId: string,
  endpoint: "web" | "pasted-text",
  body: { title: string; url: string } | { title: string; text: string },
  idempotencyKey: string,
  options: RequestOptions,
) {
  return requestJson(`${spacePath(spaceId)}/sources/${endpoint}`, importSourceSchema, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(body),
    signal: options.signal,
  });
}

export function importWebSource(
  spaceId: string,
  title: string,
  url: string,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return importNonFileSource(spaceId, "web", { title, url }, idempotencyKey, options);
}

export function importPastedTextSource(
  spaceId: string,
  title: string,
  text: string,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return importNonFileSource(spaceId, "pasted-text", { title, text }, idempotencyKey, options);
}

export function reserveUpload(
  spaceId: string,
  sourceId: string,
  declaration: ReservationDeclaration,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(`${sourcePath(spaceId, sourceId)}/upload-reservations`, uploadReservationSchema, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({
      original_filename: declaration.originalFilename,
      media_type: declaration.mediaType,
      size_bytes: declaration.sizeBytes,
      content_sha256: declaration.contentSha256,
    }),
    signal: options.signal,
  });
}

export function completeUpload(
  spaceId: string,
  sourceId: string,
  versionId: string,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(`${sourcePath(spaceId, sourceId)}/versions/${encodeURIComponent(versionId)}/complete`, importSourceSchema, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    signal: options.signal,
  });
}

export function listSourceSections(
  spaceId: string,
  sourceId: string,
  versionId: string,
  { cursor, limit = 20, artifactId, sectionId, signal }: RequestOptions & { cursor?: string | null; limit?: number; artifactId?: string; sectionId?: string } = {},
) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) params.set("cursor", cursor);
  if (artifactId) params.set("artifact_id", artifactId);
  if (sectionId) params.set("anchor_section_id", sectionId);
  return requestJson(
    `${sourcePath(spaceId, sourceId)}/versions/${encodeURIComponent(versionId)}/sections?${params}`,
    sectionsPageSchema,
    { signal },
  );
}

export function reparseSourceVersion(
  spaceId: string,
  sourceId: string,
  versionId: string,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(
    `${sourcePath(spaceId, sourceId)}/versions/${encodeURIComponent(versionId)}/reparse`,
    reparseResponseSchema,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      signal: options.signal,
    },
  );
}

export function getJob(spaceId: string, jobId: string, options: RequestOptions = {}) {
  return requestJson(`${spacePath(spaceId)}/jobs/${encodeURIComponent(jobId)}`, jobSchema, {
    signal: options.signal,
  });
}

export function retryJob(
  spaceId: string,
  jobId: string,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(`${spacePath(spaceId)}/jobs/${encodeURIComponent(jobId)}/retry`, jobSchema, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    signal: options.signal,
  });
}
