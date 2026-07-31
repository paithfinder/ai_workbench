import { z } from "zod";

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
});

export type Bootstrap = z.infer<typeof bootstrapSchema>;
export type CapabilityKey = keyof Bootstrap["capabilities"];

const DEFAULT_API_URL = "http://localhost:8000";

function getBootstrapUrl() {
  const baseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? DEFAULT_API_URL;
  return `${baseUrl.replace(/\/$/, "")}/api/v1/bootstrap`;
}

export async function fetchBootstrap(): Promise<Bootstrap> {
  const response = await fetch(getBootstrapUrl(), {
    headers: { Accept: "application/json" },
  });

  if (!response.ok) {
    throw new Error(`首页数据请求失败（HTTP ${response.status}）`);
  }

  const payload: unknown = await response.json();
  const parsed = bootstrapSchema.safeParse(payload);

  if (!parsed.success) {
    throw new Error("首页数据格式与当前应用不兼容");
  }

  return parsed.data;
}
