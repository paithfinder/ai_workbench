import { z } from "zod";
import { requestJson } from "./api";

const uuid = z.string().uuid();
const nonNegativeInteger = z.number().int().nonnegative();
const indexStateSchema = z.enum(["ready", "building", "degraded", "failed", "empty"]);

export const retrievalScopeSchema = z.object({
  scope_node_id: uuid,
  include_descendants: z.boolean(),
  scope_snapshot_hash: z.string().min(1),
  scope_path: z.string().min(1),
  node_kind: z.string().min(1),
  node_title: z.string().min(1),
  knowledge_count: nonNegativeInteger,
  source_count: nonNegativeInteger,
  source_version_count: nonNegativeInteger,
  chunk_count: nonNegativeInteger,
  index_status: indexStateSchema,
  index_config_version: z.string().min(1).nullable(),
});
export type RetrievalScopeSummary = z.infer<typeof retrievalScopeSchema>;

const retrievalHitBaseSchema = z.object({
  chunk_id: uuid,
  content_identity: z.string().min(1),
  rank: z.number().int().positive(),
  raw_score: z.number().finite(),
  score_semantics: z.string().min(1),
  knowledge_path: z.array(z.string()),
  snippet: z.string(),
  deep_link: z.string().startsWith("/sources/").nullable(),
});

export const retrievalHitSchema = z.discriminatedUnion("corpus_kind", [
  retrievalHitBaseSchema.extend({
    corpus_kind: z.literal("source_evidence"),
    knowledge_node_id: z.null(),
    knowledge_revision_id: z.null(),
    source_id: uuid,
    source_title: z.string().min(1),
    source_version_id: uuid,
    source_version_number: z.number().int().positive(),
    parse_artifact_id: uuid,
    section_id: uuid,
  }),
  retrievalHitBaseSchema.extend({
    corpus_kind: z.literal("confirmed_knowledge"),
    knowledge_node_id: uuid,
    knowledge_revision_id: uuid,
    source_id: z.null(),
    source_title: z.null(),
    source_version_id: z.null(),
    source_version_number: z.null(),
    parse_artifact_id: z.null(),
    section_id: z.null(),
  }),
]);
export type RetrievalHit = z.infer<typeof retrievalHitSchema>;

export const scopePreviewResponseSchema = z.object({ scope_summary: retrievalScopeSchema });
export type ScopePreviewResponse = z.infer<typeof scopePreviewResponseSchema>;

export const debugSearchResponseSchema = z.object({
  query: z.string(),
  scope_summary: retrievalScopeSchema,
  embedding: z.object({
    provider: z.string().min(1),
    model: z.string().min(1),
    dimensions: z.number().int().positive(),
  }).nullable(),
  keyword_hits: z.array(retrievalHitSchema),
  vector_hits: z.array(retrievalHitSchema),
  channel_errors: z.record(z.string(), z.string().nullable()),
  timings_ms: z.record(z.string(), z.number().nonnegative()),
});
export type DebugSearchResponse = z.infer<typeof debugSearchResponseSchema>;

const channelStatusSchema = z.object({
  status: indexStateSchema,
  indexed_chunks: nonNegativeInteger,
});

export const indexStatusSchema = z.object({
  status: indexStateSchema,
  index_version: z.string().min(1).nullable(),
  indexed_chunks: nonNegativeInteger,
  pending_chunks: nonNegativeInteger,
  keyword: channelStatusSchema,
  vector: channelStatusSchema.extend({
    provider: z.string().min(1).nullable(),
    model: z.string().min(1).nullable(),
    dimensions: z.number().int().positive().nullable(),
  }),
  updated_at: z.string().datetime({ offset: true }).nullable(),
});
export type IndexStatus = z.infer<typeof indexStatusSchema>;

export const indexRebuildSchema = z.object({
  rebuild_id: uuid,
  status: z.enum(["queued", "running", "succeeded", "failed"]),
  message: z.string().min(1),
});
export type IndexRebuild = z.infer<typeof indexRebuildSchema>;

type RequestOptions = { signal?: AbortSignal };
export type ScopeSelection = { scopeNodeId: string; includeDescendants: boolean };

function retrievalPath(spaceId: string, resource: string) {
  return `/api/v1/knowledge-spaces/${encodeURIComponent(spaceId)}/retrieval/${resource}`;
}

function scopePayload(scope: ScopeSelection) {
  return { scope_node_id: scope.scopeNodeId, include_descendants: scope.includeDescendants };
}

export function previewRetrievalScope(spaceId: string, scope: ScopeSelection, options: RequestOptions = {}) {
  return requestJson(retrievalPath(spaceId, "scope-preview"), scopePreviewResponseSchema, {
    method: "POST",
    body: JSON.stringify(scopePayload(scope)),
    signal: options.signal,
  });
}

export function debugRetrievalSearch(
  spaceId: string,
  input: ScopeSelection & { query: string; topK?: number },
  options: RequestOptions = {},
) {
  return requestJson(retrievalPath(spaceId, "debug-search"), debugSearchResponseSchema, {
    method: "POST",
    body: JSON.stringify({
      query: input.query.trim(),
      scope: scopePayload(input),
      top_k: input.topK ?? 5,
      channels: ["keyword", "vector"],
    }),
    signal: options.signal,
  });
}

export function getRetrievalIndexStatus(spaceId: string, options: RequestOptions = {}) {
  return requestJson(retrievalPath(spaceId, "index-status"), indexStatusSchema, { signal: options.signal });
}

export function rebuildRetrievalIndex(spaceId: string, idempotencyKey: string, options: RequestOptions = {}) {
  return requestJson(retrievalPath(spaceId, "index-rebuilds"), indexRebuildSchema, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    signal: options.signal,
  });
}
