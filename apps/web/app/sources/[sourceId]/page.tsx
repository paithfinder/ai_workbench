import { SourceDetail } from "@/components/source-detail";

export default async function SourceDetailPage({ params }: { params: Promise<{ sourceId: string }> }) {
  const { sourceId } = await params;
  return <SourceDetail sourceId={sourceId} />;
}
