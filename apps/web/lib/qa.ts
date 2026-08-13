import { z } from "zod";
import { requestJson } from "./api";

const qaClaimSchema = z.object({
  claim_id: z.string().min(1),
  claim_text: z.string().min(1),
  evidence_ids: z.array(z.string().min(1)).min(1),
});

const qaCitationSchema = z.object({
  claim_id: z.string().min(1),
  claim_text: z.string().min(1),
  evidence_id: z.string().min(1),
  content_identity: z.string().regex(/^[0-9a-f]{64}$/),
  section_id: z.string().uuid().nullable(),
  corpus_kind: z.enum(["source_evidence", "confirmed_knowledge"]),
  frozen_quote: z.string().min(1),
  deep_link: z.string().nullable(),
});

export const qaTurnSchema = z.object({
  id: z.string().uuid(),
  status: z.enum(["processing", "answered", "abstained", "failed"]),
  question: z.string().min(1),
  scope_snapshot: z.record(z.string(), z.unknown()),
  index_config_version: z.string().nullable(),
  ai_provider: z.string().min(1),
  ai_model: z.string().min(1),
  answer: z.string().nullable(),
  abstain_code: z.string().nullable(),
  error_code: z.string().nullable(),
  error_message: z.string().nullable(),
  warnings: z.array(z.string()),
  claims: z.array(qaClaimSchema),
  citations: z.array(qaCitationSchema),
});

export type QaTurn = z.infer<typeof qaTurnSchema>;

type RequestOptions = { signal?: AbortSignal };

function wait(milliseconds: number, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("The operation was aborted", "AbortError"));
      return;
    }
    const timeout = window.setTimeout(resolve, milliseconds);
    signal?.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timeout);
        reject(new DOMException("The operation was aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

export function createQaTurn(
  spaceId: string,
  request: { question: string; scopeNodeId: string; includeDescendants: boolean },
  idempotencyKey: string,
  options: { signal?: AbortSignal } = {},
) {
  return requestJson(
    `/api/v1/knowledge-spaces/${spaceId}/qa/turns`,
    qaTurnSchema,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        question: request.question,
        scope: {
          scope_node_id: request.scopeNodeId,
          include_descendants: request.includeDescendants,
        },
      }),
      signal: options.signal,
    },
  );
}

export async function waitForQaTurn(
  spaceId: string,
  key: string,
  options: RequestOptions & { intervalMs?: number; maxAttempts?: number } = {},
) {
  const { signal, intervalMs = 500, maxAttempts = 120 } = options;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const turn = await getQaTurnByIdempotencyKey(spaceId, key, { signal });
    if (turn.status !== "processing") return turn;
    await wait(intervalMs, signal);
  }
  throw new Error("问答仍在处理中，请稍后使用同一请求继续查询");
}

export function getQaTurnByIdempotencyKey(
  spaceId: string,
  key: string,
  options: { signal?: AbortSignal } = {},
) {
  return requestJson(
    `/api/v1/knowledge-spaces/${spaceId}/qa/turns/by-idempotency-key/${encodeURIComponent(key)}`,
    qaTurnSchema,
    { signal: options.signal },
  );
}
