import { SourceDetail } from "@/components/source-detail";

export default async function SourceDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ sourceId: string }>;
  searchParams: Promise<{ versionId?: string; artifactId?: string; sectionId?: string }>;
}) {
  const [{ sourceId }, deepLink] = await Promise.all([params, searchParams]);
  return <SourceDetail deepLink={deepLink} sourceId={sourceId} />;
}
