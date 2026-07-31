import { ScheduledEmptyState } from "@/components/page-states";

export default function QaPage() {
  return (
    <ScheduledEmptyState
      capability="范围可见的知识问答"
      day="D7–D9"
      description="先确定检索范围，再基于已确认知识和来源回答。"
      detail="范围摘要、检索调试、回答身份、引用、流式状态与失败恢复将随 RAG 接口分阶段接入。"
      eyebrow="KNOWLEDGE Q&A · 范围可见"
      title="知识问答"
    />
  );
}
