import { z } from "zod";
import { requestJson } from "./api";
import { jobSchema } from "./sources";

const uuid = z.string().uuid();
const dateTime = z.string().datetime({ offset: true });

export const candidateStatusSchema = z.enum([
  "pending_review",
  "needs_verification",
  "accepted",
  "rejected",
]);
export type CandidateStatus = z.infer<typeof candidateStatusSchema>;

export const extractionJobSchema = z.object({
  id: uuid,
  job_id: uuid,
  source_version_id: uuid,
  parse_artifact_id: uuid,
  status: z.enum(["queued", "running", "ready", "failed"]),
  provider: z.string().nullable(),
  model: z.string().min(1),
  prompt_version: z.string().min(1),
  input_tokens: z.number().int().nonnegative(),
  output_tokens: z.number().int().nonnegative(),
  latency_ms: z.number().int().nonnegative(),
  provider_request_id: z.string().nullable(),
  error_code: z.string().nullable(),
  error_message: z.string().nullable(),
  started_at: dateTime.nullable(),
  completed_at: dateTime.nullable(),
  created_at: dateTime,
});
export type ExtractionJob = z.infer<typeof extractionJobSchema>;

const evidenceSchema = z.object({
  section_id: uuid,
  title: z.string().nullable(),
  text: z.string(),
  heading_path: z.array(z.string()),
  page_number: z.number().int().positive().nullable(),
  locator: z.record(z.string(), z.unknown()),
  quote_hash: z.string().regex(/^[0-9a-f]{64}$/),
});

export const candidateSchema = z.object({
  id: uuid,
  extraction_job_id: uuid,
  source_version_id: uuid,
  source_title: z.string().min(1),
  title: z.string().min(1),
  body: z.string().min(1),
  tags: z.array(z.string()),
  suggested_destination_id: uuid.nullable(),
  atomicity: z.enum(["atomic", "needs_split"]),
  confidence: z.number().min(0).max(1),
  status: candidateStatusSchema,
  version: z.number().int().positive(),
  reviewed_at: dateTime.nullable(),
  verification_reason: z.string().nullable(),
  rejection_reason: z.string().nullable(),
  conditions: z.array(z.string()),
  exceptions: z.array(z.string()),
  model: z.string().min(1),
  prompt_version: z.string().min(1),
  created_at: dateTime,
  evidence: z.array(evidenceSchema).min(1),
});
export type ExtractionCandidate = z.infer<typeof candidateSchema>;

export const knowledgeDestinationSchema = z.object({
  id: uuid,
  name: z.string().min(1),
  path: z.array(z.string()).default([]),
});
export type KnowledgeDestination = z.infer<typeof knowledgeDestinationSchema>;

export const candidateDraftSchema = z.object({
  title: z.string().trim().min(1).max(500),
  body: z.string().trim().min(1).max(20_000),
  tags: z.array(z.string().trim().min(1).max(64)).max(20),
  suggested_destination_id: uuid.nullable(),
  conditions: z.array(z.string()).max(12),
  exceptions: z.array(z.string()).max(12),
});
export type CandidateDraft = z.infer<typeof candidateDraftSchema>;

type CandidateWrite = CandidateDraft & { expected_version: number };
type CandidateVerificationWrite = CandidateWrite & { reason?: string };
type CandidateRejectionWrite = CandidateWrite & { reason?: string | null };

const DEFAULT_VERIFICATION_REASON = "需要人工核验来源证据";

export const candidateReviewResponseSchema = z.object({
  result_id: uuid,
  candidate_review_id: uuid,
  candidate_id: uuid,
  candidate_version: z.number().int().positive(),
  candidate_status: candidateStatusSchema,
  knowledge_node_id: uuid.nullable(),
  knowledge_revision_id: uuid.nullable(),
  review_card_id: uuid.nullable(),
  evidence_ids: z.array(uuid),
});
export type CandidateReviewResponse = z.infer<typeof candidateReviewResponseSchema>;

export const reviewRequestSchema = z.object({
  idempotency_key: z.string().min(1),
  action: z.string().min(1),
  status: z.literal("succeeded"),
  result: candidateReviewResponseSchema,
  error_code: z.string().nullable(),
  error_message: z.string().nullable(),
});
export type CandidateReviewRequest = z.infer<typeof reviewRequestSchema>;

const candidateListSchema = z.object({ items: z.array(candidateSchema) });
const destinationListSchema = z.object({ items: z.array(knowledgeDestinationSchema) });
const scheduleSchema = z.object({ job: jobSchema, extraction: extractionJobSchema });
export type ScheduledExtraction = z.infer<typeof scheduleSchema>;

const extractionRunSchema = scheduleSchema.extend({ source_title: z.string().min(1) });
const extractionRunListSchema = z.object({ items: z.array(extractionRunSchema) });
export type ExtractionRun = z.infer<typeof extractionRunSchema>;

type RequestOptions = { signal?: AbortSignal };

function spacePath(spaceId: string) {
  return `/api/v1/knowledge-spaces/${encodeURIComponent(spaceId)}`;
}

function candidatePath(spaceId: string, candidateId: string) {
  return `${spacePath(spaceId)}/candidates/${encodeURIComponent(candidateId)}`;
}

function writeInit(
  method: "PATCH" | "POST",
  payload: CandidateWrite & { reason?: string | null },
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return {
    method,
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(payload),
    signal,
  } satisfies RequestInit;
}

export async function listCandidates(spaceId: string, status?: CandidateStatus, options: RequestOptions = {}) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  const query = params.size ? `?${params}` : "";
  const response = await requestJson(`${spacePath(spaceId)}/candidates${query}`, candidateListSchema, {
    signal: options.signal,
  });
  return response.items;
}

export async function listKnowledgeDestinations(spaceId: string, options: RequestOptions = {}) {
  const response = await requestJson(
    `${spacePath(spaceId)}/knowledge-destinations`,
    destinationListSchema,
    { signal: options.signal },
  );
  return response.items;
}

export function updateCandidate(
  spaceId: string,
  candidateId: string,
  payload: CandidateWrite,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(
    candidatePath(spaceId, candidateId),
    candidateReviewResponseSchema,
    writeInit("PATCH", payload, idempotencyKey, options.signal),
  );
}

export function acceptCandidate(
  spaceId: string,
  candidateId: string,
  payload: CandidateWrite,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(
    `${candidatePath(spaceId, candidateId)}/accept`,
    candidateReviewResponseSchema,
    writeInit("POST", payload, idempotencyKey, options.signal),
  );
}

export function markCandidateNeedsVerification(
  spaceId: string,
  candidateId: string,
  payload: CandidateVerificationWrite,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(
    `${candidatePath(spaceId, candidateId)}/mark-needs-verification`,
    candidateReviewResponseSchema,
    writeInit(
      "POST",
      { ...payload, reason: payload.reason ?? DEFAULT_VERIFICATION_REASON },
      idempotencyKey,
      options.signal,
    ),
  );
}

export function rejectCandidate(
  spaceId: string,
  candidateId: string,
  payload: CandidateRejectionWrite,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(
    `${candidatePath(spaceId, candidateId)}/reject`,
    candidateReviewResponseSchema,
    writeInit("POST", payload, idempotencyKey, options.signal),
  );
}

export function getCandidateReviewRequest(
  spaceId: string,
  candidateId: string,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(
    `${candidatePath(spaceId, candidateId)}/review-requests/${encodeURIComponent(idempotencyKey)}`,
    reviewRequestSchema,
    { signal: options.signal },
  );
}

export function getExtraction(spaceId: string, sourceId: string, versionId: string, options: RequestOptions = {}) {
  return requestJson(
    `${spacePath(spaceId)}/sources/${encodeURIComponent(sourceId)}/versions/${encodeURIComponent(versionId)}/extraction`,
    scheduleSchema,
    { signal: options.signal },
  );
}

export async function listExtractions(spaceId: string, options: RequestOptions = {}) {
  const response = await requestJson(`${spacePath(spaceId)}/extractions`, extractionRunListSchema, {
    signal: options.signal,
  });
  return response.items;
}

export function scheduleExtraction(
  spaceId: string,
  sourceId: string,
  versionId: string,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(
    `${spacePath(spaceId)}/sources/${encodeURIComponent(sourceId)}/versions/${encodeURIComponent(versionId)}/extractions`,
    scheduleSchema,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      signal: options.signal,
    },
  );
}
