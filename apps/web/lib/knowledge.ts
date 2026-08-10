import { z } from "zod";
import { requestJson } from "./api";

const uuid = z.string().uuid();
const dateTime = z.string().datetime({ offset: true });
const requestOptionsSchema = z.object({}).strict();
void requestOptionsSchema;

export const knowledgeNodeKindSchema = z.enum(["root", "folder", "document", "point", "source"]);
export type KnowledgeNodeKind = z.infer<typeof knowledgeNodeKindSchema>;
export type MutableKnowledgeNodeKind = Extract<KnowledgeNodeKind, "folder" | "document" | "point">;

export const knowledgeNodeSchema = z.object({
  id: uuid,
  space_id: uuid,
  parent_id: uuid.nullable(),
  kind: knowledgeNodeKindSchema,
  path: z.string().min(1),
  version: z.number().int().positive(),
  sort_order: z.number().int().nonnegative(),
  origin_candidate_id: uuid.nullable(),
  current_revision_id: uuid.nullable(),
  source_id: uuid.nullable(),
  source_version_id: uuid.nullable(),
  title: z.string().nullable(),
  created_at: dateTime,
  updated_at: dateTime,
  deleted_at: dateTime.nullable(),
});
export type KnowledgeNode = z.infer<typeof knowledgeNodeSchema>;

export const knowledgeRevisionSchema = z.object({
  id: uuid,
  node_id: uuid,
  revision_number: z.number().int().positive(),
  title: z.string().min(1),
  body: z.string(),
  tags: z.array(z.string()),
  conditions: z.array(z.string()),
  exceptions: z.array(z.string()),
  content_hash: z.string().regex(/^[0-9a-f]{64}$/),
  actor: z.string().min(1),
  edit_reason: z.string().nullable(),
  created_at: dateTime,
});
export type KnowledgeRevision = z.infer<typeof knowledgeRevisionSchema>;

export const knowledgeEvidenceSchema = z.object({
  id: uuid,
  node_id: uuid,
  revision_id: uuid,
  revision_number: z.number().int().positive(),
  source_id: uuid,
  source_title: z.string().min(1),
  source_version_id: uuid,
  source_version_number: z.number().int().positive(),
  source_content_hash: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  parse_artifact_id: uuid,
  artifact_revision: z.number().int().positive(),
  section_id: uuid,
  section_ordinal: z.number().int().nonnegative(),
  quote_hash: z.string().regex(/^[0-9a-f]{64}$/),
  content_hash: z.string().regex(/^[0-9a-f]{64}$/),
  locator: z.record(z.string(), z.unknown()),
  frozen_quote: z.string(),
  deep_link: z.string().startsWith("/sources/"),
  anchor_status: z.literal("exact"),
  created_at: dateTime,
});
export type KnowledgeEvidence = z.infer<typeof knowledgeEvidenceSchema>;

export const knowledgeTreeSchema = z.object({ items: z.array(knowledgeNodeSchema) });
export const knowledgeSearchItemSchema = z.object({
  node: knowledgeNodeSchema,
  breadcrumb: z.string().min(1),
  ancestor_ids: z.array(uuid),
  match_fields: z.array(z.string()),
});
export type KnowledgeSearchItem = z.infer<typeof knowledgeSearchItemSchema>;
const knowledgeSearchSchema = z.object({ items: z.array(knowledgeSearchItemSchema) });

export const knowledgeNodeDetailSchema = z.object({
  node: knowledgeNodeSchema,
  revision: knowledgeRevisionSchema.nullable(),
  evidence: z.array(knowledgeEvidenceSchema),
});
export type KnowledgeNodeDetail = z.infer<typeof knowledgeNodeDetailSchema>;

export const knowledgeWriteResponseSchema = z.object({
  result_id: uuid,
  request_id: uuid,
  node_id: uuid,
  node_version: z.number().int().positive(),
  snapshot: z.record(z.string(), z.unknown()),
});
export type KnowledgeWriteResponse = z.infer<typeof knowledgeWriteResponseSchema>;

export const knowledgeWriteRequestSchema = z.object({
  idempotency_key: z.string().min(1),
  operation: z.enum(["create", "edit", "move", "delete"]),
  status: z.literal("succeeded"),
  result: knowledgeWriteResponseSchema,
});
export type KnowledgeWriteRequest = z.infer<typeof knowledgeWriteRequestSchema>;

type RequestOptions = { signal?: AbortSignal };
type RevisionFields = {
  title?: string;
  body?: string;
  tags?: string[];
  conditions?: string[];
  exceptions?: string[];
};

function spacePath(spaceId: string) {
  return `/api/v1/knowledge-spaces/${encodeURIComponent(spaceId)}`;
}

function nodePath(spaceId: string, nodeId: string) {
  return `${spacePath(spaceId)}/knowledge-nodes/${encodeURIComponent(nodeId)}`;
}

function writeInit(method: "POST" | "PATCH" | "DELETE", idempotencyKey: string, body: unknown, signal?: AbortSignal) {
  return {
    method,
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(body),
    signal,
  } satisfies RequestInit;
}

export async function getKnowledgeTree(spaceId: string, options: RequestOptions = {}) {
  const result = await requestJson(`${spacePath(spaceId)}/knowledge-tree`, knowledgeTreeSchema, { signal: options.signal });
  return result.items;
}

export async function searchKnowledge(spaceId: string, query: string, options: RequestOptions & { limit?: number } = {}) {
  const params = new URLSearchParams({ q: query.trim() });
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  const result = await requestJson(`${spacePath(spaceId)}/knowledge-search?${params}`, knowledgeSearchSchema, { signal: options.signal });
  return result.items;
}

export function getKnowledgeNode(spaceId: string, nodeId: string, options: RequestOptions = {}) {
  return requestJson(nodePath(spaceId, nodeId), knowledgeNodeDetailSchema, { signal: options.signal });
}

export function createKnowledgeNode(
  spaceId: string,
  input: RevisionFields & {
    parentId: string | null;
    kind: MutableKnowledgeNodeKind;
    expectedVersion: number;
    title: string;
  },
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(`${spacePath(spaceId)}/knowledge-nodes`, knowledgeWriteResponseSchema, writeInit("POST", idempotencyKey, {
    parent_id: input.parentId,
    kind: input.kind,
    expected_version: input.expectedVersion,
    title: input.title,
    body: input.body ?? "",
    tags: input.tags ?? [],
    conditions: input.conditions ?? [],
    exceptions: input.exceptions ?? [],
  }, options.signal));
}

export function editKnowledgeNode(
  spaceId: string,
  nodeId: string,
  input: RevisionFields & { expectedVersion: number; expectedRevisionId: string; reason?: string },
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  const body: Record<string, unknown> = {
    expected_version: input.expectedVersion,
    expected_revision_id: input.expectedRevisionId,
  };
  if (input.title !== undefined) body.title = input.title;
  if (input.body !== undefined) body.body = input.body;
  if (input.tags !== undefined) body.tags = input.tags;
  if (input.conditions !== undefined) body.conditions = input.conditions;
  if (input.exceptions !== undefined) body.exceptions = input.exceptions;
  if (input.reason !== undefined) body.reason = input.reason;
  return requestJson(nodePath(spaceId, nodeId), knowledgeWriteResponseSchema, writeInit("PATCH", idempotencyKey, body, options.signal));
}

export function moveKnowledgeNode(
  spaceId: string,
  nodeId: string,
  input: { parentId: string | null; expectedVersion: number },
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(`${nodePath(spaceId, nodeId)}/move`, knowledgeWriteResponseSchema, writeInit("POST", idempotencyKey, {
    parent_id: input.parentId,
    expected_version: input.expectedVersion,
  }, options.signal));
}

export function deleteKnowledgeNode(
  spaceId: string,
  nodeId: string,
  expectedVersion: number,
  idempotencyKey: string,
  options: RequestOptions = {},
) {
  return requestJson(nodePath(spaceId, nodeId), knowledgeWriteResponseSchema, writeInit("DELETE", idempotencyKey, {
    expected_version: expectedVersion,
  }, options.signal));
}

export function getKnowledgeWriteRequest(spaceId: string, idempotencyKey: string, options: RequestOptions = {}) {
  return requestJson(
    `${spacePath(spaceId)}/knowledge-write-requests/${encodeURIComponent(idempotencyKey)}`,
    knowledgeWriteRequestSchema,
    { signal: options.signal },
  );
}

export function getKnowledgeEvidence(spaceId: string, evidenceId: string, options: RequestOptions = {}) {
  return requestJson(
    `${spacePath(spaceId)}/knowledge-evidence/${encodeURIComponent(evidenceId)}`,
    knowledgeEvidenceSchema,
    { signal: options.signal },
  );
}
