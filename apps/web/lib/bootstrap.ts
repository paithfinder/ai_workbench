import { z } from "zod";
import { requestJson } from "./api";

const nonNegativeInteger = z.number().int().nonnegative();
const defaultKnowledgeImportLimits = {
  max_entries: 500,
  max_folders: 250,
  max_depth: 32,
  max_total_body_utf8_bytes: 5 * 1024 * 1024,
  max_document_characters: 20_000,
  max_relative_path_characters: 4_000,
  allowed_extensions: [".md", ".txt"],
};

export const capabilitySchema = z.object({
  source_import: z.boolean(),
  extraction_review: z.boolean(),
  knowledge_tree: z.boolean(),
  knowledge_folder_import: z.boolean().default(false),
  retrieval_debug: z.boolean(),
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
  limits: z.object({
    knowledge_import: z.object({
      max_entries: z.number().int().positive(),
      max_folders: z.number().int().positive(),
      max_depth: z.number().int().positive(),
      max_total_body_utf8_bytes: z.number().int().positive(),
      max_document_characters: z.number().int().positive(),
      max_relative_path_characters: z.number().int().positive(),
      allowed_extensions: z.array(z.string().startsWith(".")).min(1),
    }),
  }).default({ knowledge_import: defaultKnowledgeImportLimits }),
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
