import { ScheduledEmptyState } from "@/components/page-states";

export default function ExtractionPage() {
  return (
    <ScheduledEmptyState
      capability="候选队列与三栏审查"
      day="D4–D5"
      description="逐条核对来源与 AI 候选，确认后才写入知识树。"
      detail="候选队列、原文高亮、内容编辑与确认、忽略、待验证决策将在提炼契约稳定后实现。"
      eyebrow="AI EXTRACTION · 人工把关"
      title="提炼审查"
    />
  );
}
