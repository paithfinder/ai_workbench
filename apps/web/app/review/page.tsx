import { ScheduledEmptyState } from "@/components/page-states";

export default function ReviewPage() {
  return (
    <ScheduledEmptyState
      capability="到期知识间隔复习"
      day="D10"
      description="围绕已确认知识点安排透明、可追溯的复习。"
      detail="今日队列、答案揭示、四档反馈和下一次复习时间将在复习调度接口完成后实现。"
      eyebrow="SPACED REPETITION · 自适应间隔"
      title="间隔复习"
    />
  );
}
