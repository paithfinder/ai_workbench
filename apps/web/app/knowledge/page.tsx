import { Suspense } from "react";
import { KnowledgeWorkspace } from "@/components/knowledge-workspace";

export default function KnowledgePage() {
  return <Suspense fallback={<div className="panel-card loading-panel">正在打开知识树…</div>}><KnowledgeWorkspace /></Suspense>;
}
