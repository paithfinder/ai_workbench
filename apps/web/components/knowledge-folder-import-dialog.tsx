"use client";

import { type ChangeEvent, type KeyboardEvent as ReactKeyboardEvent, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import type { KnowledgeImportLimits } from "@/lib/knowledge-import";
import {
  createKnowledgeImport,
  getKnowledgeImportRequest,
  prepareKnowledgeImport,
  type KnowledgeImportManifest,
  type KnowledgeImportResponse,
  KnowledgeImportValidationError,
} from "@/lib/knowledge-import";
import type { KnowledgeNode } from "@/lib/knowledge";

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
}

function isUncertain(error: unknown) {
  return error instanceof ApiError && (error.status === 0 || error.status >= 500);
}

export function KnowledgeFolderImportDialog({
  limits,
  onClose,
  onSuccess,
  opener,
  parent,
  parentLabel,
  spaceId,
}: {
  limits: KnowledgeImportLimits;
  onClose: () => void;
  onSuccess: (result: KnowledgeImportResponse) => Promise<void>;
  opener: HTMLElement | null;
  parent: KnowledgeNode;
  parentLabel: string;
  spaceId: string;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const filesInputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const [manifest, setManifest] = useState<KnowledgeImportManifest | null>(null);
  const [issues, setIssues] = useState<string[]>([]);
  const [scanning, setScanning] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    closeRef.current?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape" || submitting) return;
      event.preventDefault();
      onClose();
    }
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [onClose, submitting]);

  useEffect(() => () => {
    if (opener?.isConnected) opener.focus();
  }, [opener]);

  function trapFocus(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Tab") return;
    const focusable = [...(dialogRef.current?.querySelectorAll<HTMLElement>(
      'a[href], button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])',
    ) ?? [])];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable.at(-1)!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  async function choose(event: ChangeEvent<HTMLInputElement>, mode: "folder" | "files") {
    const files = event.target.files;
    if (!files?.length) return;
    setScanning(true);
    setIssues([]);
    setMessage("");
    setManifest(null);
    try {
      setManifest(await prepareKnowledgeImport(files, limits, { mode }));
    } catch (error) {
      if (error instanceof KnowledgeImportValidationError) setIssues(error.issues);
      else setIssues([error instanceof Error ? error.message : "无法读取所选文件夹"]);
    } finally {
      setScanning(false);
      event.target.value = "";
    }
  }

  async function reconcile(current: KnowledgeImportManifest) {
    try {
      const result = await getKnowledgeImportRequest(spaceId, current.idempotencyKey);
      await onSuccess(result);
      return true;
    } catch {
      return false;
    }
  }

  async function submit() {
    if (!manifest || submitting) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setSubmitting(true);
    setUncertain(false);
    setMessage("");
    try {
      const result = await createKnowledgeImport(
        spaceId,
        {
          parentId: parent.kind === "root" ? null : parent.id,
          expectedParentVersion: parent.version,
          manifest,
        },
        { signal: controller.signal },
      );
      await onSuccess(result);
    } catch (error) {
      if (controller.signal.aborted) {
        setMessage("已取消等待；服务端结果仍可使用同一请求标识核对。");
        setUncertain(true);
      } else if (isUncertain(error)) {
        if (!(await reconcile(manifest))) {
          setUncertain(true);
          setMessage("无法确认服务端是否完成导入。请用同一按钮安全重试。");
        }
      } else {
        setMessage(error instanceof Error ? error.message : "导入失败，请重新检查文件夹");
      }
    } finally {
      abortRef.current = null;
      setSubmitting(false);
    }
  }

  function close() {
    if (submitting) return;
    onClose();
  }

  return <div className="knowledge-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}>
    <div aria-labelledby="knowledge-import-title" aria-modal="true" className="knowledge-modal knowledge-import-dialog" onKeyDown={trapFocus} ref={dialogRef} role="dialog">
      <div className="knowledge-dialog-head">
        <div><p className="state-kicker">DIRECT IMPORT · 无 AI</p><h2 id="knowledge-import-title">直接导入 Markdown / TXT</h2></div>
        <button aria-label="关闭直接导入" className="icon-button" disabled={submitting} onClick={close} ref={closeRef} type="button">×</button>
      </div>
      <p>导入到：<strong>{parentLabel}</strong></p>
      <p className="panel-caption">文件会直接创建为正式知识文档和 Revision，不创建来源、不解析、不调用 AI，也不进入候选队列；导入后会异步建立检索索引。</p>
      <input
        accept=".md,.txt"
        aria-label="选择 Markdown 或 TXT 文件"
        className="sr-only"
        multiple
        onChange={(event) => void choose(event, "files")}
        ref={filesInputRef}
        type="file"
      />
      <input
        accept=".md,.txt"
        aria-label="选择知识库文件夹"
        className="sr-only"
        multiple
        onChange={(event) => void choose(event, "folder")}
        ref={folderInputRef}
        type="file"
        {...({ webkitdirectory: "" } as Record<string, string>)}
      />
      <div className="form-actions">
        <button className="button" disabled={scanning || submitting} onClick={() => filesInputRef.current?.click()} type="button">{scanning ? "正在检查…" : "选择文件"}</button>
        <button className="button" disabled={scanning || submitting} onClick={() => folderInputRef.current?.click()} type="button">选择文件夹</button>
      </div>
      {manifest ? <div className="knowledge-import-summary" aria-live="polite">
        <div><span>顶层目录</span><strong>{manifest.rootName}</strong></div>
        <div><span>文件夹</span><strong>{manifest.folderCount}</strong></div>
        <div><span>文档</span><strong>{manifest.documents.length}</strong></div>
        <div><span>正文总量</span><strong>{formatBytes(manifest.totalBodyUtf8Bytes)}</strong></div>
        <p>同名顶层目录会创建一个新的副本，不覆盖或合并已有内容。</p>
      </div> : null}
      {issues.length ? <div className="knowledge-import-errors" role="alert"><strong>所选内容暂时不能导入</strong><ul>{issues.slice(0, 12).map((issue) => <li key={issue}>{issue}</li>)}</ul>{issues.length > 12 ? <p>另有 {issues.length - 12} 个问题。</p> : null}</div> : null}
      {message ? <p className={uncertain ? "knowledge-notice is-uncertain" : "inline-error"} role="alert">{message}</p> : null}
      <div className="form-actions">
        {submitting ? <button className="button" onClick={() => abortRef.current?.abort()} type="button">取消等待</button> : <button className="button" onClick={close} type="button">关闭</button>}
        <button className="button primary" disabled={!manifest || submitting || scanning} onClick={() => void submit()} type="button">{submitting ? "正在原子导入…" : uncertain ? "安全重试" : "开始导入"}</button>
      </div>
    </div>
  </div>;
}
