"use client";

import Link from "next/link";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, createIdempotencyKey } from "@/lib/api";
import { fetchBootstrap } from "@/lib/bootstrap";
import { getKnowledgeTree } from "@/lib/knowledge";
import { createQaTurn, waitForQaTurn, type QaTurn } from "@/lib/qa";
import { previewRetrievalScope } from "@/lib/retrieval";
import { ScopeSummary, ScopeTree, scopeBreadcrumb } from "./retrieval-debug-workspace";
import { PageHeader } from "./page-states";

function errorMessage(error: unknown) {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "问答请求失败，请稍后重试";
}

function AnswerPanel({ turn }: { turn: QaTurn }) {
  if (turn.status === "failed") return <section className="qa-result panel-card" role="alert"><p className="state-kicker">FAILED</p><h2>无法生成可信回答</h2><p>{turn.error_message ?? "服务端未发布未经验证的回答。"}</p></section>;
  if (turn.status === "abstained") return <section className="qa-result qa-abstained panel-card" aria-live="polite"><p className="state-kicker">SAFE ABSTAIN · {turn.abstain_code}</p><h2>当前证据不足</h2><p>系统没有在所选知识范围内找到足以支持答案的证据，因此不会猜测。</p></section>;
  if (turn.status === "processing") return <section className="qa-result panel-card" aria-live="polite"><p className="state-kicker">PROCESSING</p><h2>正在验证答案与引用</h2></section>;
  return <section className="qa-answer" aria-live="polite">
    <article className="qa-result panel-card"><p className="state-kicker">TRUSTED ANSWER</p><h2>回答</h2><p className="qa-answer-text">{turn.answer}</p>{turn.warnings.length ? <p className="qa-warning">降级记录：{turn.warnings.join("；")}</p> : null}</article>
    <section aria-labelledby="qa-citations-title" className="qa-citations"><div className="results-heading"><div><p className="state-kicker">FROZEN EVIDENCE</p><h2 id="qa-citations-title">引用证据</h2></div><span>{turn.citations.length} citations</span></div><ol>{turn.citations.map((citation) => <li className="panel-card" key={`${citation.claim_id}-${citation.evidence_id}`}><header><span>{citation.evidence_id}</span><strong>{citation.corpus_kind === "source_evidence" ? "来源证据" : "确认知识"}</strong></header><h3>{citation.claim_text}</h3><blockquote>{citation.frozen_quote}</blockquote>{citation.deep_link ? <Link className="result-link" href={citation.deep_link}>查看冻结锚点 <span aria-hidden="true">↗</span></Link> : null}</li>)}</ol></section>
  </section>;
}

export function QaWorkspace() {
  const controller = useRef<AbortController | null>(null);
  const operation = useRef<{ fingerprint: string; key: string } | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [includeDescendants, setIncludeDescendants] = useState(true);
  const [question, setQuestion] = useState("");
  const [turn, setTurn] = useState<QaTurn | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const bootstrapQuery = useQuery({ queryKey: ["bootstrap"], queryFn: fetchBootstrap });
  const spaceId = bootstrapQuery.data?.space.id;
  const treeQuery = useQuery({ queryKey: ["knowledge-tree", spaceId], queryFn: ({ signal }) => getKnowledgeTree(spaceId!, { signal }), enabled: Boolean(spaceId) });
  const nodes = treeQuery.data ?? [];
  const selectedNodeId = selectedId || nodes.find((node) => node.kind === "root")?.id || nodes[0]?.id || "";
  const scopeQuery = useQuery({ queryKey: ["qa-scope", spaceId, selectedNodeId, includeDescendants], queryFn: ({ signal }) => previewRetrievalScope(spaceId!, { scopeNodeId: selectedNodeId, includeDescendants }, { signal }), enabled: Boolean(spaceId && selectedNodeId && bootstrapQuery.data?.capabilities.trusted_qa) });
  const ask = useMutation({
    mutationFn: async (normalized: string) => {
      controller.current?.abort();
      const requestController = new AbortController();
      controller.current = requestController;
      const fingerprint = JSON.stringify([normalized, selectedNodeId, includeDescendants]);
      const current = operation.current?.fingerprint === fingerprint ? operation.current : { fingerprint, key: createIdempotencyKey() };
      operation.current = current;
      try {
        const result = await createQaTurn(spaceId!, { question: normalized, scopeNodeId: selectedNodeId, includeDescendants }, current.key, { signal: requestController.signal });
        return result.status === "processing" ? waitForQaTurn(spaceId!, current.key, { signal: requestController.signal }) : result;
      } catch (error) {
        if (error instanceof ApiError && error.status === 0) return waitForQaTurn(spaceId!, current.key, { signal: requestController.signal });
        throw error;
      }
    },
    onSuccess: (result) => { controller.current = null; operation.current = null; setTurn(result); },
    onError: (error) => { if (!(error instanceof DOMException && error.name === "AbortError")) controller.current = null; },
  });
  useEffect(() => () => controller.current?.abort(), []);
  const reset = () => { controller.current?.abort(); controller.current = null; operation.current = null; setTurn(null); ask.reset(); };

  function submit(event: FormEvent) {
    event.preventDefault();
    const normalized = question.trim();
    if (!normalized) { setValidationError("请输入需要根据知识库回答的问题"); return; }
    setValidationError(null);
    setTurn(null);
    ask.mutate(normalized);
  }

  const header = (description: string) => <PageHeader eyebrow="TRUSTED QA · D8" headingId="qa-title" title="可信引用问答" description={description} />;
  if (bootstrapQuery.isPending) return <section aria-live="polite">{header("正在连接默认知识空间…")}<div className="panel-card loading-panel">正在读取问答能力…</div></section>;
  if (bootstrapQuery.isError || !spaceId) return <section>{header("连接知识空间后才能开始问答。")}<div className="state-card error-state" role="alert"><div><h2>无法连接问答工作台</h2><p>{errorMessage(bootstrapQuery.error)}</p></div></div></section>;
  if (!bootstrapQuery.data.capabilities.trusted_qa) return <section>{header("服务端可信引用链尚未开放。")}<div className="state-card"><div><p className="state-kicker">CAPABILITY · TRUSTED QA OFF</p><h2>可信问答尚未开放</h2></div></div></section>;
  if (treeQuery.isPending) return <section aria-live="polite">{header("正在载入可选知识范围…")}<div className="panel-card loading-panel">正在读取知识树…</div></section>;
  if (treeQuery.isError || !nodes.length) return <section>{header("先建立知识范围，再进行引用问答。")}<div className="state-card error-state" role="alert"><div><h2>{treeQuery.isError ? "无法读取知识树" : "知识树为空"}</h2><p>{treeQuery.isError ? errorMessage(treeQuery.error) : "当前没有可问答的知识节点。"}</p></div></div></section>;
  const breadcrumb = scopeBreadcrumb(nodes, selectedNodeId);
  return <section aria-labelledby="qa-title">{header(`在「${bootstrapQuery.data.space.name}」中限定范围，由服务端验证每条引用后再发布回答。`)}<div className="retrieval-boundary qa-boundary"><strong>D8 · VERIFIED CITATIONS</strong><span>回答只使用本次范围内的冻结证据；证据不足时安全拒答。</span><Link href="/qa/debug">打开检索调试</Link></div><div className="qa-grid"><aside aria-labelledby="qa-scope-tree-title" className="scope-panel panel-card"><div className="retrieval-panel-head"><p className="state-kicker">BOUNDARY</p><h2 id="qa-scope-tree-title">选择问答范围</h2><p>范围切换会清除旧回答，防止跨范围残留。</p></div><div className="scope-tree"><ScopeTree nodes={nodes} selectedId={selectedNodeId} onSelect={(nodeId) => { setSelectedId(nodeId); reset(); }} /></div><label className="descendant-toggle"><input checked={includeDescendants} onChange={(event) => { setIncludeDescendants(event.target.checked); reset(); }} type="checkbox" /><span><strong>包含全部下级</strong><small>关闭后仅使用所选节点</small></span></label></aside><main className="qa-main"><ScopeSummary breadcrumb={breadcrumb} includeDescendants={includeDescendants} pending={scopeQuery.isPending} summary={scopeQuery.data?.scope_summary} /><form className="retrieval-composer qa-composer panel-card" onSubmit={submit}><label htmlFor="qa-question"><span>问题</span><strong>基于当前范围生成带引用回答</strong></label><textarea aria-invalid={Boolean(validationError)} id="qa-question" onChange={(event) => { setQuestion(event.target.value); setValidationError(null); }} placeholder="例如：知识条目的审核流程是什么？" rows={4} value={question} /><button className="button primary" disabled={ask.isPending || scopeQuery.isError} type="submit">{ask.isPending ? "正在检索、回答并验证…" : "生成可信回答"}</button>{validationError ? <p className="field-error" role="alert">{validationError}</p> : null}{ask.isError ? <p className="inline-error" role="alert">{errorMessage(ask.error)}</p> : null}</form>{turn ? <AnswerPanel turn={turn} /> : <section className="qa-empty panel-card"><p className="state-kicker">READY</p><h2>等待问题</h2><p>选择范围并提问后，这里将展示回答、安全拒答和冻结引用。</p></section>}</main></div></section>;
}
