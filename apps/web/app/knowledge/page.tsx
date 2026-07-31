import { ScheduledEmptyState } from "@/components/page-states";

export default function KnowledgePage() {
  return (
    <ScheduledEmptyState
      capability="可搜索的知识树"
      day="D6"
      description="用稳定层级组织已确认知识点，并保留来源引用。"
      detail="知识树、搜索、详情和来源抽屉将在目录与引用接口可用后实现，并补充完整键盘导航。"
      eyebrow="KNOWLEDGE TREE · 稳定层级"
      title="我的知识树"
    />
  );
}
