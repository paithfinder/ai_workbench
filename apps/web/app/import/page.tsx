import { ScheduledEmptyState } from "@/components/page-states";

export default function ImportPage() {
  return (
    <ScheduledEmptyState
      capability="真实资料导入"
      day="D2–D3"
      description="保存可定位的原始资料，再进入解析与提炼流程。"
      detail="文件上传、网页与文本导入、解析进度和失败重试将在来源与解析接口就绪后实现。"
      eyebrow="INTAKE · 原文先行"
      title="导入知识"
    />
  );
}
