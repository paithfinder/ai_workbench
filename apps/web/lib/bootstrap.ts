import { z } from "zod";
import { requestJson } from "./api";

const nonNegativeInteger = z.number().int().nonnegative();

export const capabilitySchema = z.object({
  source_import: z.boolean(),
  extraction_review: z.boolean(),
  knowledge_tree: z.boolean(),
  trusted_qa: z.boolean(),
  spaced_review: z.boolean(),
  evidence_agent: z.boolean(),
});

export const bootstrapSchema = z.object({
  space: z.object({
    id: z.string().uuid(),
    slug: z.string().min(1),
    name: z.string().min(1),
  }),
  capabilities: capabilitySchema,
  statistics: z.object({
    sources: nonNegativeInteger,
    queued_jobs: nonNegativeInteger,
    activity_events: nonNegativeInteger,
  }),
  foundation_status: z.literal("ready"),
  max_upload_size_bytes: z.number().int().positive().optional(),
});

export type Bootstrap = z.infer<typeof bootstrapSchema>;
export type CapabilityKey = keyof Bootstrap["capabilities"];

export async function fetchBootstrap(): Promise<Bootstrap> {
  return requestJson("/api/v1/bootstrap", bootstrapSchema);
}
