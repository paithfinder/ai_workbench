import { z } from "zod";
import { ApiError, requestJson } from "./api";

const uuid = z.string().uuid();

export const proposalActionSchema = z.enum([
  "create",
  "revise",
  "supersede",
  "merge_suggestion",
  "mark_review_recommended",
]);
export type ProposalAction = z.infer<typeof proposalActionSchema>;

export const proposalCreateKindSchema = z.enum(["folder", "document"]);
export type ProposalCreateKind = z.infer<typeof proposalCreateKindSchema>;

export const proposalEvidenceRoleSchema = z.enum([
  "new_support",
  "existing_support",
  "conflict",
  "outdated",
  "contextual",
]);
export type ProposalEvidenceRole = z.infer<typeof proposalEvidenceRoleSchema>;

export const proposalEvidenceInputSchema = z.object({
  role: proposalEvidenceRoleSchema,
  section_id: uuid,
  knowledge_revision_id: uuid.nullable().optional(),
});
export type ProposalEvidenceInput = z.infer<typeof proposalEvidenceInputSchema>;

export const proposalCreateSchema = z.object({
  action: proposalActionSchema,
  research_run_id: uuid.nullable().optional(),
  target_node_id: uuid.nullable().optional(),
  target_revision_id: uuid.nullable().optional(),
  create_kind: proposalCreateKindSchema.nullable().optional(),
  suggested_title: z.string().min(1).max(500).nullable().optional(),
  suggested_body: z.string().min(1).max(20_000).nullable().optional(),
  suggested_tags: z.array(z.string()).max(20).optional(),
  conditions: z.array(z.string()).max(12).optional(),
  exceptions: z.array(z.string()).max(12).optional(),
  comparison_summary: z.string().max(20_000).nullable().optional(),
  confidence: z.number().min(0).max(1).nullable().optional(),
  uncertainty_reason: z.string().max(2_000).nullable().optional(),
  evidence: z.array(proposalEvidenceInputSchema).min(1).max(24),
});
export type ProposalCreate = z.infer<typeof proposalCreateSchema>;

export const proposalDecisionSchema = z.object({
  expected_version: z.number().int().positive(),
  reason: z.string().max(1_000).nullable().optional(),
});
export type ProposalDecision = z.infer<typeof proposalDecisionSchema>;

export const proposalApplySnapshotSchema = z.object({
  action: z.string().min(1),
  target_node_id: uuid.nullable(),
  old_revision_id: uuid.nullable(),
  new_revision_id: uuid.nullable(),
  node_version: z.number().int().nullable(),
  evidence_ids: z.array(uuid),
  index_job_id: uuid.nullable(),
  index_run_id: uuid.nullable(),
});
export type ProposalApplySnapshot = z.infer<typeof proposalApplySnapshotSchema>;

export const proposalSnapshotSchema = z.object({
  apply: proposalApplySnapshotSchema.optional(),
}).catchall(z.unknown());
export type ProposalSnapshot = z.infer<typeof proposalSnapshotSchema>;

export const proposalResultSchema = z.object({
  result_id: uuid,
  proposal_id: uuid,
  proposal_version: z.number().int(),
  proposal_status: z.string().min(1),
  snapshot: proposalSnapshotSchema,
});
export type ProposalResult = z.infer<typeof proposalResultSchema>;

export const proposalItemSchema = z.object({
  id: uuid,
  space_id: uuid,
  research_run_id: uuid.nullable(),
  target_node_id: uuid.nullable(),
  target_revision_id: uuid.nullable(),
  target_node_version: z.number().int().nullable(),
  action: z.string().min(1),
  create_kind: z.string().nullable(),
  status: z.string().min(1),
  version: z.number().int(),
  suggested_title: z.string().nullable(),
  suggested_body: z.string().nullable(),
  suggested_tags: z.array(z.string()),
  conditions: z.array(z.string()),
  exceptions: z.array(z.string()),
  comparison_summary: z.string().nullable(),
  confidence: z.number().nullable(),
  uncertainty_reason: z.string().nullable(),
  superseded_by_proposal_id: uuid.nullable(),
  submitted_at: z.unknown(),
  reviewed_at: z.unknown(),
  applied_at: z.unknown(),
  superseded_at: z.unknown(),
  created_at: z.unknown(),
  updated_at: z.unknown(),
});
export type ProposalItem = z.infer<typeof proposalItemSchema>;

export const proposalEvidenceSchema = z.object({
  id: uuid,
  role: z.string().min(1),
  source_id: uuid,
  source_version_id: uuid,
  parse_artifact_id: uuid,
  section_id: uuid,
  knowledge_revision_id: uuid.nullable(),
  frozen_quote: z.string(),
  quote_hash: z.string(),
  content_hash: z.string(),
  locator: z.record(z.string(), z.unknown()),
  ordinal: z.number().int(),
});
export type ProposalEvidence = z.infer<typeof proposalEvidenceSchema>;

export const proposalTransitionSchema = z.object({
  id: uuid,
  request_id: uuid,
  operation: z.string().min(1),
  actor: z.string().min(1),
  reason: z.string().nullable(),
  before_snapshot: z.record(z.string(), z.unknown()),
  after_snapshot: z.record(z.string(), z.unknown()),
  from_status: z.string().min(1),
  to_status: z.string().min(1),
  from_version: z.number().int(),
  to_version: z.number().int(),
  created_at: z.unknown(),
});
export type ProposalTransition = z.infer<typeof proposalTransitionSchema>;

export const proposalDetailSchema = z.object({
  proposal: proposalItemSchema,
  evidence: z.array(proposalEvidenceSchema),
  transitions: z.array(proposalTransitionSchema),
});
export type ProposalDetail = z.infer<typeof proposalDetailSchema>;

export const proposalRequestSchema = z.object({
  idempotency_key: z.string().min(1),
  action: z.string().min(1),
  result: proposalResultSchema,
});
export type ProposalRequest = z.infer<typeof proposalRequestSchema>;

const proposalListSchema = z.object({ items: z.array(proposalItemSchema) });

type RequestOptions = { signal?: AbortSignal };

export type ProposalListOptions = RequestOptions & {
  status?: string;
  action?: string;
  targetNodeId?: string;
  limit?: number;
  offset?: number;
};

function spacePath(spaceId: string) {
  return `/api/v1/knowledge-spaces/${encodeURIComponent(spaceId)}`;
}

function proposalPath(spaceId: string, proposalId: string) {
  return `${spacePath(spaceId)}/proposals/${encodeURIComponent(proposalId)}`;
}

export async function listProposals(
  spaceId: string,
  options: ProposalListOptions = {},
): Promise<ProposalItem[]> {
  const params = new URLSearchParams();
  if (options.status) params.set("status", options.status);
  if (options.action) params.set("action", options.action);
  if (options.targetNodeId) params.set("target_node_id", options.targetNodeId);
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined) params.set("offset", String(options.offset));
  const query = params.size ? `?${params}` : "";
  const response = await requestJson(`${spacePath(spaceId)}/proposals${query}`, proposalListSchema, {
    signal: options.signal,
  });
  return response.items;
}

export function getProposal(spaceId: string, proposalId: string, options: RequestOptions = {}) {
  return requestJson(proposalPath(spaceId, proposalId), proposalDetailSchema, {
    signal: options.signal,
  });
}

export const getProposalDetail = getProposal;

export function getProposalRequest(
  spaceId: string,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(
    `${spacePath(spaceId)}/proposal-requests/${encodeURIComponent(idempotencyKey)}`,
    proposalRequestSchema,
    { signal: options.signal },
  );
}

async function requestMutation(
  spaceId: string,
  idempotencyKey: string,
  path: string,
  payload: unknown,
  signal?: AbortSignal,
): Promise<ProposalResult> {
  try {
    return await requestJson(path, proposalResultSchema, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(payload),
      signal,
    });
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 0 || error.code !== "network_error") {
      throw error;
    }
    try {
      return (await getProposalRequest(spaceId, idempotencyKey, { signal })).result;
    } catch (recoveryError) {
      if (recoveryError instanceof ApiError && recoveryError.status === 404) throw error;
      if (recoveryError instanceof ApiError && recoveryError.code === "network_error") throw error;
      throw recoveryError;
    }
  }
}

export function createProposal(
  spaceId: string,
  payload: ProposalCreate,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestMutation(spaceId, idempotencyKey, `${spacePath(spaceId)}/proposals`, payload, options.signal);
}

export function approveProposal(
  spaceId: string,
  proposalId: string,
  payload: ProposalDecision,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestMutation(spaceId, idempotencyKey, `${proposalPath(spaceId, proposalId)}/approve`, payload, options.signal);
}

export function rejectProposal(
  spaceId: string,
  proposalId: string,
  payload: ProposalDecision,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestMutation(spaceId, idempotencyKey, `${proposalPath(spaceId, proposalId)}/reject`, payload, options.signal);
}

export function applyProposal(
  spaceId: string,
  proposalId: string,
  payload: ProposalDecision,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestMutation(spaceId, idempotencyKey, `${proposalPath(spaceId, proposalId)}/apply`, payload, options.signal);
}
