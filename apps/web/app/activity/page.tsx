import { ScheduledEmptyState } from "@/components/page-states";

export default function ActivityPage() {
  return (
    <ScheduledEmptyState
      capability="真实学习活动台账"
      day="D10"
      description="只记录由导入、审查、问答和复习产生的真实事件。"
      detail="活动时间线、筛选与统计将在事件接口就绪后实现；当前不生成与实际流程无关的记录。"
      eyebrow="LEARNING LEDGER · 真实事件"
      title="学习记录"
    />
  );
}
