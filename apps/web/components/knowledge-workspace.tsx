"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { ApiError, createIdempotencyKey } from "@/lib/api";
import { fetchBootstrap } from "@/lib/bootstrap";
import {
  createKnowledgeNode,
  deleteKnowledgeNode,
  editKnowledgeNode,
  getKnowledgeEvidence,
  getKnowledgeNode,
  getKnowledgeTree,
  getKnowledgeWriteRequest,
  moveKnowledgeNode,
  searchKnowledge,
  type KnowledgeEvidence,
  type KnowledgeNode,
  type KnowledgeSearchItem,
  type MutableKnowledgeNodeKind,
} from "@/lib/knowledge";
import { KnowledgeFolderImportDialog } from "./knowledge-folder-import-dialog";
import { PageHeader } from "./page-states";

type DialogMode = "folder" | "document" | "edit" | "delete" | null;
type Notice = { kind: "error" | "success" | "uncertain"; message: string };
type Operation = { key: string; fingerprint: string };

const expandableKinds = new Set<KnowledgeNode["kind"]>(["root", "folder", "document"]);
const appendRevisionKinds = new Set<KnowledgeNode["kind"]>(["document", "point"]);
const moveOrDeleteKinds = new Set<KnowledgeNode["kind"]>(["folder", "document", "point"]);
const kindLabels: Record<KnowledgeNode["kind"], string> = {
  root: "知识库",
  folder: "文件夹",
  document: "文档",
  point: "知识点",
  source: "来源",
};
const kindMarks: Record<KnowledgeNode["kind"], string> = {
  root: "根",
  folder: "目",
  document: "文",
  point: "点",
  source: "源",
};

function errorMessage(error: unknown) {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "请求失败，请稍后重试";
}

function isUncertainFailure(error: unknown) {
  return error instanceof ApiError && (error.status === 0 || error.status >= 500);
}

function childrenIndex(nodes: KnowledgeNode[]) {
  const index = new Map<string | null, KnowledgeNode[]>();
  for (const node of nodes) index.set(node.parent_id, [...(index.get(node.parent_id) ?? []), node]);
  for (const children of index.values()) {
    children.sort((left, right) => left.sort_order - right.sort_order || (left.title ?? "").localeCompare(right.title ?? "", "zh-CN"));
  }
  return index;
}

export function visibleKnowledgeNodes(nodes: KnowledgeNode[], expanded: Set<string>) {
  const index = childrenIndex(nodes);
  const visible: KnowledgeNode[] = [];
  const visit = (parentId: string | null) => {
    for (const node of index.get(parentId) ?? []) {
      visible.push(node);
      if (expanded.has(node.id)) visit(node.id);
    }
  };
  visit(null);
  return visible;
}

export function ancestorIds(nodes: KnowledgeNode[], nodeId: string) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const result: string[] = [];
  let current = byId.get(nodeId);
  const seen = new Set<string>();
  while (current?.parent_id && !seen.has(current.parent_id)) {
    seen.add(current.parent_id);
    result.unshift(current.parent_id);
    current = byId.get(current.parent_id);
  }
  return result;
}

function creationParent(nodes: KnowledgeNode[], selected: KnowledgeNode | null) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  let current = selected;
  while (current && current.kind !== "root" && current.kind !== "folder") {
    current = current.parent_id ? byId.get(current.parent_id) ?? null : null;
  }
  return current ?? nodes.find((node) => node.kind === "root") ?? null;
}

function nodePathLabel(nodes: KnowledgeNode[], node: KnowledgeNode) {
  const byId = new Map(nodes.map((item) => [item.id, item]));
  const labels = [node.title ?? kindLabels[node.kind]];
  let current = node;
  while (current.parent_id) {
    const parent = byId.get(current.parent_id);
    if (!parent) break;
    labels.unshift(parent.title ?? kindLabels[parent.kind]);
    current = parent;
  }
  return labels.join(" / ");
}

function ModalFrame({ children, labelId, onClose, initialFocusRef, opener }: {
  children: ReactNode;
  labelId: string;
  onClose: () => void;
  initialFocusRef: RefObject<HTMLElement | null>;
  opener?: HTMLElement | null;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    initialFocusRef.current?.focus();
    function handleDocumentKey(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }
    }
    document.addEventListener("keydown", handleDocumentKey, true);
    return () => {
      document.removeEventListener("keydown", handleDocumentKey, true);
      if (opener?.isConnected) opener.focus();
    };
  }, [initialFocusRef, onClose, opener]);

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

  return <div className="knowledge-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div aria-labelledby={labelId} aria-modal="true" className="knowledge-modal" onKeyDown={trapFocus} ref={dialogRef} role="dialog">
      {children}
    </div>
  </div>;
}

function KnowledgeDialog({ busy, detail, mode, onClose, onSubmit, opener, selected }: {
  busy: boolean;
  detail: Awaited<ReturnType<typeof getKnowledgeNode>> | undefined;
  mode: Exclude<DialogMode, null>;
  onClose: () => void;
  onSubmit: (values: { title: string; body: string; reason: string }) => Promise<void>;
  opener: HTMLElement | null;
  selected: KnowledgeNode | null;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const [title, setTitle] = useState(mode === "edit" ? detail?.revision?.title ?? selected?.title ?? "" : "");
  const [body, setBody] = useState(mode === "edit" ? detail?.revision?.body ?? "" : "");
  const [reason, setReason] = useState("");
  const labels = mode === "folder" ? ["新建文件夹", "文件夹名称"] : mode === "document" ? ["新建文档", "文档标题"] : mode === "edit" ? ["保存新修订", "知识标题"] : ["移到回收站", ""];
  function submit(event: FormEvent) {
    event.preventDefault();
    void onSubmit({ title: title.trim(), body, reason: reason.trim() });
  }
  return <ModalFrame initialFocusRef={closeRef} labelId="knowledge-dialog-title" onClose={onClose} opener={opener}>
    <div className="knowledge-dialog-head"><div><p className="state-kicker">KNOWLEDGE · D6</p><h2 id="knowledge-dialog-title">{labels[0]}</h2></div><button aria-label="关闭" className="icon-button" onClick={onClose} ref={closeRef} type="button">×</button></div>
    {mode === "delete" ? <p>“{selected?.title ?? kindLabels[selected?.kind ?? "document"]}”及其下级节点将从知识树隐藏。历史修订和冻结证据仍由服务端保留。</p> : <form className="knowledge-form" id="knowledge-dialog-form" onSubmit={submit}>
      <label><span>{labels[1]}</span><input autoFocus maxLength={500} onChange={(event) => setTitle(event.target.value)} required value={title} /></label>
      {mode === "document" || mode === "edit" ? <label><span>{mode === "edit" ? "本次修订正文" : "正文（可选）"}</span><textarea maxLength={20_000} onChange={(event) => setBody(event.target.value)} rows={9} value={body} /></label> : null}
      {mode === "edit" ? <><p className="panel-caption">当前修订的冻结证据会复制到新修订；保存前请确认修改后的正文仍由这些来源支持。</p><label><span>修改说明（可选）</span><input maxLength={1000} onChange={(event) => setReason(event.target.value)} value={reason} /></label></> : null}
    </form>}
    <div className="form-actions"><button className="button" disabled={busy} onClick={onClose} type="button">取消</button><button className={`button ${mode === "delete" ? "danger" : "primary"}`} disabled={busy || (mode !== "delete" && !title.trim())} form={mode === "delete" ? undefined : "knowledge-dialog-form"} onClick={mode === "delete" ? () => void onSubmit({ title: "", body: "", reason: "" }) : undefined} type={mode === "delete" ? "button" : "submit"}>{busy ? "正在保存…" : labels[0]}</button></div>
  </ModalFrame>;
}

function CitationDrawer({ evidence, loading, onClose, opener }: {
  evidence: KnowledgeEvidence | undefined;
  loading: boolean;
  onClose: () => void;
  opener: HTMLElement | null;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const href = evidence?.deep_link ?? null;
  return <ModalFrame initialFocusRef={closeRef} labelId="citation-title" onClose={onClose} opener={opener}>
    <div className="citation-drawer-head"><div><p className="state-kicker">FROZEN EVIDENCE · 只读</p><h2 id="citation-title">来源引用</h2></div><button aria-label="关闭来源引用" className="icon-button" onClick={onClose} ref={closeRef} type="button">×</button></div>
    {loading ? <p aria-live="polite" className="muted-message">正在读取冻结证据…</p> : evidence ? <>
      <blockquote>{evidence.frozen_quote || "此引用没有可显示的摘录。"}</blockquote>
      <dl className="citation-facts"><div><dt>来源</dt><dd>{evidence.source_title}</dd></div><div><dt>修订</dt><dd>Revision {evidence.revision_number}</dd></div><div><dt>来源版本</dt><dd>v{evidence.source_version_number} · {evidence.source_version_id}</dd></div><div><dt>结构分段</dt><dd>#{evidence.section_ordinal} · {evidence.section_id}</dd></div><div><dt>证据哈希</dt><dd>{evidence.quote_hash}</dd></div></dl>
      {href ? <a className="button primary" href={href}>在来源中查看</a> : <p className="inline-error">当前知识树中找不到此证据对应的来源节点。</p>}
    </> : <p className="inline-error" role="alert">无法读取这条冻结证据。</p>}
  </ModalFrame>;
}

type RecursiveTreeProps = {
  childMap: Map<string | null, KnowledgeNode[]>;
  expanded: Set<string>;
  focusedId: string | null;
  items: KnowledgeNode[];
  level: number;
  onFocus: (id: string, focusDom?: boolean) => void;
  onKeyDown: (event: ReactKeyboardEvent<HTMLElement>, node: KnowledgeNode) => void;
  onSelect: (id: string) => void;
  selectedId: string | null;
};

function RecursiveTreeItems(props: RecursiveTreeProps) {
  const { childMap, expanded, focusedId, items, level, onFocus, onKeyDown, onSelect, selectedId } = props;
  return <>{items.map((node) => {
    const childNodes = childMap.get(node.id) ?? [];
    const expandable = expandableKinds.has(node.kind) && childNodes.length > 0;
    const open = expandable && expanded.has(node.id);
    return <li key={node.id} role="none"><div aria-expanded={expandable ? open : undefined} aria-level={level} aria-selected={selectedId === node.id} className={`knowledge-tree-item kind-${node.kind}`} data-node-id={node.id} onClick={() => { onFocus(node.id); onSelect(node.id); }} onFocus={() => onFocus(node.id)} onKeyDown={(event) => onKeyDown(event, node)} role="treeitem" tabIndex={focusedId === node.id ? 0 : -1}><span aria-hidden="true" className="tree-toggle">{expandable ? open ? "▾" : "▸" : "·"}</span><span aria-hidden="true" className={`node-mark node-${node.kind}`}>{kindMarks[node.kind]}</span><span className="tree-item-copy"><strong>{node.title ?? kindLabels[node.kind]}</strong><small>{kindLabels[node.kind]}</small></span></div>{open ? <ul role="group"><RecursiveTreeItems {...props} items={childNodes} level={level + 1} /></ul> : null}</li>;
  })}</>;
}

export function KnowledgeWorkspace() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const initialSelectedId = searchParams.get("node");
  const initialEvidenceId = searchParams.get("evidence");
  const [selectedId, setSelectedId] = useState<string | null>(initialSelectedId);
  const [evidenceId, setEvidenceId] = useState<string | null>(initialEvidenceId);
  const [query, setQuery] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [focusedId, setFocusedId] = useState<string | null>(initialSelectedId);
  const [citationOpener, setCitationOpener] = useState<HTMLElement | null>(null);
  const [dialog, setDialog] = useState<DialogMode>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importOpener, setImportOpener] = useState<HTMLElement | null>(null);
  const [dialogOpener, setDialogOpener] = useState<HTMLElement | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const closeDialog = useCallback(() => {
    if (!busyRef.current) setDialog(null);
  }, []);
  const [notice, setNotice] = useState<Notice | null>(null);
  const pendingTreeFocusRef = useRef<string | null>(null);
  const operationRef = useRef<Operation | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const bootstrapQuery = useQuery({ queryKey: ["bootstrap"], queryFn: fetchBootstrap });
  const spaceId = bootstrapQuery.data?.space.id;
  const treeQuery = useQuery({ queryKey: ["knowledge-tree", spaceId], queryFn: ({ signal }) => getKnowledgeTree(spaceId!, { signal }), enabled: Boolean(spaceId) });
  const nodes = useMemo(() => treeQuery.data ?? [], [treeQuery.data]);
  const childMap = useMemo(() => childrenIndex(nodes), [nodes]);
  const rootIds = useMemo(() => nodes.filter((node) => node.kind === "root").map((node) => node.id), [nodes]);
  const initialAncestorIds = useMemo(
    () => initialSelectedId && nodes.some((node) => node.id === initialSelectedId) ? ancestorIds(nodes, initialSelectedId) : [],
    [initialSelectedId, nodes],
  );
  const effectiveExpanded = useMemo(
    () => new Set([...expanded, ...rootIds, ...initialAncestorIds].filter((nodeId) => !collapsed.has(nodeId))),
    [collapsed, expanded, initialAncestorIds, rootIds],
  );
  const visible = useMemo(() => visibleKnowledgeNodes(nodes, effectiveExpanded), [nodes, effectiveExpanded]);
  const visibleFocusedId = focusedId && visible.some((node) => node.id === focusedId) ? focusedId : null;
  const effectiveFocusedId = visibleFocusedId ?? visible[0]?.id ?? null;
  const selected = nodes.find((node) => node.id === selectedId) ?? null;
  const detailQuery = useQuery({ queryKey: ["knowledge-node", spaceId, selectedId], queryFn: ({ signal }) => getKnowledgeNode(spaceId!, selectedId!, { signal }), enabled: Boolean(spaceId && selectedId) });
  const searchQuery = useQuery({ queryKey: ["knowledge-search", spaceId, searchTerm], queryFn: ({ signal }) => searchKnowledge(spaceId!, searchTerm, { signal }), enabled: Boolean(spaceId && searchTerm) });
  const evidenceQuery = useQuery({ queryKey: ["knowledge-evidence", spaceId, evidenceId], queryFn: ({ signal }) => getKnowledgeEvidence(spaceId!, evidenceId!, { signal }), enabled: Boolean(spaceId && evidenceId) });
  const evidenceMatchesSelection = Boolean(
    evidenceQuery.data
      && detailQuery.data?.revision
      && evidenceQuery.data.node_id === selectedId
      && evidenceQuery.data.revision_id === detailQuery.data.revision.id,
  );

  useEffect(() => {
    const timer = window.setTimeout(() => setSearchTerm(query.trim()), 220);
    return () => window.clearTimeout(timer);
  }, [query]);
  useEffect(() => {
    const nodeId = pendingTreeFocusRef.current;
    if (!nodeId || !visible.some((node) => node.id === nodeId)) return;
    pendingTreeFocusRef.current = null;
    document.querySelector<HTMLElement>(`[data-node-id="${nodeId}"]`)?.focus();
  }, [visible]);

  const updateUrl = useCallback((nodeId: string | null, nextEvidenceId: string | null = null) => {
    setSelectedId(nodeId);
    setEvidenceId(nextEvidenceId);
    const params = new URLSearchParams(searchParams.toString());
    if (nodeId) params.set("node", nodeId);
    else params.delete("node");
    if (nextEvidenceId) params.set("evidence", nextEvidenceId);
    else params.delete("evidence");
    router.replace(`${pathname}${params.size ? `?${params}` : ""}`, { scroll: false });
  }, [pathname, router, searchParams]);

  const closeCitation = useCallback(() => updateUrl(selectedId, null), [selectedId, updateUrl]);

  function toggle(nodeId: string, force?: boolean) {
    const shouldOpen = force ?? !effectiveExpanded.has(nodeId);
    if (shouldOpen) {
      setCollapsed((current) => {
        const next = new Set(current);
        next.delete(nodeId);
        return next;
      });
      setExpanded((current) => new Set([...current, nodeId]));
    } else {
      setCollapsed((current) => new Set([...current, nodeId]));
      setExpanded((current) => {
        const next = new Set(current);
        next.delete(nodeId);
        return next;
      });
    }
  }

  function focusNode(nodeId: string, focusDom = false) {
    setFocusedId(nodeId);
    if (focusDom) window.requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-node-id="${nodeId}"]`)?.focus());
  }

  function focusAt(index: number) {
    const target = visible[Math.max(0, Math.min(visible.length - 1, index))];
    if (target) focusNode(target.id, true);
  }

  function selectSearchResult(result: KnowledgeSearchItem) {
    const node = result.node;
    pendingTreeFocusRef.current = node.id;
    setCollapsed((current) => {
      const next = new Set(current);
      for (const id of result.ancestor_ids) next.delete(id);
      return next;
    });
    setExpanded((current) => new Set([...current, ...result.ancestor_ids]));
    setQuery("");
    setSearchTerm("");
    updateUrl(node.id);
    setFocusedId(node.id);
  }

  function onTreeKey(event: ReactKeyboardEvent<HTMLElement>, node: KnowledgeNode) {
    const index = visible.findIndex((item) => item.id === node.id);
    const children = childMap.get(node.id) ?? [];
    if (event.key === "ArrowDown") focusAt(index + 1);
    else if (event.key === "ArrowUp") focusAt(index - 1);
    else if (event.key === "Home") focusAt(0);
    else if (event.key === "End") focusAt(visible.length - 1);
    else if (event.key === "ArrowRight" && expandableKinds.has(node.kind) && children.length) {
      if (!effectiveExpanded.has(node.id)) toggle(node.id, true);
      else focusNode(children[0].id, true);
    } else if (event.key === "ArrowLeft") {
      if (effectiveExpanded.has(node.id) && children.length) toggle(node.id, false);
      else if (node.parent_id) focusNode(node.parent_id, true);
    } else if (event.key === "Enter") updateUrl(node.id);
    else if (event.key === "Escape") {
      setQuery("");
      setSearchTerm("");
      searchRef.current?.focus();
    } else return;
    event.preventDefault();
  }

  function getOperation(fingerprint: string) {
    if (operationRef.current?.fingerprint === fingerprint) return operationRef.current;
    const operation = { fingerprint, key: createIdempotencyKey() };
    operationRef.current = operation;
    return operation;
  }

  async function reconcile(operation: Operation) {
    if (!spaceId) return false;
    try {
      const request = await getKnowledgeWriteRequest(spaceId, operation.key);
      operationRef.current = null;
      if (request.operation === "delete" && request.result.node_id === selectedId) {
        setFocusedId(null);
        updateUrl(null);
      }
      setNotice({ kind: "success", message: `服务端已完成${request.operation}操作，页面已核对结果。` });
      await queryClient.invalidateQueries({ queryKey: ["knowledge-tree", spaceId] });
      await queryClient.invalidateQueries({ queryKey: ["knowledge-node", spaceId] });
      return true;
    } catch {
      return false;
    }
  }

  async function mutate(values: { title: string; body: string; reason: string }) {
    if (!spaceId || !dialog) return;
    const parent = creationParent(nodes, selected);
    const fingerprint = JSON.stringify({ dialog, selectedId, version: selected?.version, revision: detailQuery.data?.revision?.id, values, parent: parent?.id });
    const operation = getOperation(fingerprint);
    busyRef.current = true;
    setBusy(true);
    setNotice(null);
    try {
      if (dialog === "folder" || dialog === "document") {
        if (!parent) throw new Error("找不到新节点的父级，请重新加载知识树");
        const outcome = await createKnowledgeNode(spaceId, { parentId: parent.kind === "root" ? null : parent.id, kind: dialog as MutableKnowledgeNodeKind, expectedVersion: parent.version, title: values.title, body: values.body }, operation.key);
        updateUrl(outcome.node_id);
      } else if (dialog === "edit" && selected && detailQuery.data?.revision) {
        await editKnowledgeNode(spaceId, selected.id, { expectedVersion: selected.version, expectedRevisionId: detailQuery.data.revision.id, title: values.title, body: values.body, reason: values.reason || undefined }, operation.key);
      } else if (dialog === "delete" && selected) {
        await deleteKnowledgeNode(spaceId, selected.id, selected.version, operation.key);
        setFocusedId(null);
        setDialogOpener(null);
        updateUrl(null);
      }
      operationRef.current = null;
      setDialog(null);
      setNotice({ kind: "success", message: "知识树已保存。" });
      await queryClient.invalidateQueries({ queryKey: ["knowledge-tree", spaceId] });
      await queryClient.invalidateQueries({ queryKey: ["knowledge-node", spaceId] });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        operationRef.current = null;
        setDialog(null);
        await queryClient.invalidateQueries({ queryKey: ["knowledge-tree", spaceId] });
        await queryClient.invalidateQueries({ queryKey: ["knowledge-node", spaceId] });
        setNotice({ kind: "error", message: "该节点已被其他操作更新。已加载最新版本；请重新打开编辑并核对差异。" });
      } else if (isUncertainFailure(error) && await reconcile(operation)) {
        setDialog(null);
      } else {
        if (!isUncertainFailure(error)) operationRef.current = null;
        setNotice({ kind: isUncertainFailure(error) ? "uncertain" : "error", message: isUncertainFailure(error) ? "无法确认服务端是否完成操作。可用同一按钮重试；请求标识会保持不变。" : errorMessage(error) });
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  async function move(parentId: string) {
    if (!spaceId || !selected) return;
    const fingerprint = JSON.stringify({ action: "move", selected: selected.id, version: selected.version, parentId });
    const operation = getOperation(fingerprint);
    busyRef.current = true;
    setBusy(true);
    setNotice(null);
    try {
      await moveKnowledgeNode(spaceId, selected.id, { parentId: parentId || null, expectedVersion: selected.version }, operation.key);
      operationRef.current = null;
      await queryClient.invalidateQueries({ queryKey: ["knowledge-tree", spaceId] });
      await queryClient.invalidateQueries({ queryKey: ["knowledge-node", spaceId] });
      setNotice({ kind: "success", message: "节点已移动。" });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        operationRef.current = null;
        await queryClient.invalidateQueries({ queryKey: ["knowledge-tree", spaceId] });
        await queryClient.invalidateQueries({ queryKey: ["knowledge-node", spaceId] });
        setNotice({ kind: "error", message: "节点版本或目标位置已变化，已加载最新状态。" });
      } else if (!(isUncertainFailure(error) && await reconcile(operation))) {
        setNotice({ kind: isUncertainFailure(error) ? "uncertain" : "error", message: isUncertainFailure(error) ? "无法确认移动是否完成；再次选择同一位置可安全重试。" : errorMessage(error) });
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  function openDialog(event: ReactMouseEvent<HTMLElement>, mode: Exclude<DialogMode, null>) {
    if (busy) return;
    setDialogOpener(event.currentTarget);
    setDialog(mode);
  }

  function openEvidence(event: ReactMouseEvent<HTMLButtonElement>, id: string) {
    setCitationOpener(event.currentTarget);
    updateUrl(selectedId, id);
  }

  async function completeFolderImport(rootNodeId: string, summary: { folder_count: number; document_count: number }) {
    if (!spaceId) return;
    setImportOpen(false);
    const targetId = creationParent(nodes, selected)?.id;
    setCollapsed((current) => {
      const next = new Set(current);
      if (targetId) next.delete(targetId);
      return next;
    });
    setExpanded((current) => new Set([
      ...current,
      ...ancestorIds(nodes, targetId ?? ""),
      ...(targetId ? [targetId] : []),
      rootNodeId,
    ]));
    pendingTreeFocusRef.current = rootNodeId;
    updateUrl(rootNodeId);
    setFocusedId(rootNodeId);
    setNotice({ kind: "success", message: `已直接导入 ${summary.folder_count} 个文件夹和 ${summary.document_count} 篇文档；未调用 AI。` });
    await queryClient.invalidateQueries({ queryKey: ["knowledge-tree", spaceId] });
  }

  const pageHeader = (description: string) => <PageHeader eyebrow="KNOWLEDGE TREE · D6" headingId="knowledge-title" title="我的知识树" description={description} />;
  if (bootstrapQuery.isPending || treeQuery.isPending) return <section aria-live="polite">{pageHeader("正在读取稳定层级与冻结证据…")}<div className="panel-card loading-panel">正在装订知识目录…</div></section>;
  if (bootstrapQuery.isError || treeQuery.isError || !spaceId) return <section>{pageHeader("知识树只显示服务端保存的真实节点。")}<div className="state-card error-state" role="alert"><div><h2>无法读取知识树</h2><p>{errorMessage(bootstrapQuery.error ?? treeQuery.error)}</p></div><button className="button primary" onClick={() => { void bootstrapQuery.refetch(); void treeQuery.refetch(); }} type="button">重新加载</button></div></section>;

  const rootItems = childMap.get(null) ?? [];
  const searchResults = searchQuery.data ?? [];
  const appendRevisionAllowed = selected && appendRevisionKinds.has(selected.kind);
  const moveOrDeleteAllowed = selected && moveOrDeleteKinds.has(selected.kind);
  const moveTargets = selected?.kind === "point" ? nodes.filter((node) => node.kind === "document" && node.id !== selected.id) : nodes.filter((node) => (node.kind === "root" || node.kind === "folder") && node.id !== selected?.id && !ancestorIds(nodes, node.id).includes(selected?.id ?? ""));
  const selectedParent = nodes.find((node) => node.id === selected?.parent_id);
  const moveValue = selectedParent?.kind === "root" ? "" : selected?.parent_id ?? "";
  const importParent = creationParent(nodes, selected);

  return <section aria-labelledby="knowledge-title">
    {pageHeader("浏览人工确认的知识层级；每条引用都回到保存时的原始分段。")}
    <div className="knowledge-toolbar">
      <div className="knowledge-search"><label><span className="sr-only">搜索知识</span><input aria-controls="knowledge-search-results" onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); setQuery(""); setSearchTerm(""); } else if (event.key === "ArrowDown" && searchResults[0]) { event.preventDefault(); document.querySelector<HTMLElement>("#knowledge-search-results button")?.focus(); } }} placeholder="搜索标题、正文或来源" ref={searchRef} type="search" value={query} /></label>
        {query.trim() ? <div className="knowledge-search-results" id="knowledge-search-results"><p aria-live="polite">{searchQuery.isPending ? "正在搜索…" : `${searchResults.length} 个结果`}</p>{searchQuery.isError ? <p className="inline-error" role="alert">{errorMessage(searchQuery.error)}</p> : <ul>{searchResults.map((result, index) => <li key={result.node.id}><button onClick={() => selectSearchResult(result)} onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); setQuery(""); setSearchTerm(""); searchRef.current?.focus(); } else if (event.key === "ArrowDown") { event.preventDefault(); (event.currentTarget.closest("li")?.nextElementSibling?.querySelector("button") as HTMLElement | null)?.focus(); } else if (event.key === "ArrowUp") { event.preventDefault(); const previous = event.currentTarget.closest("li")?.previousElementSibling?.querySelector("button") as HTMLElement | null; if (previous) previous.focus(); else searchRef.current?.focus(); } else if (event.key === "Home") { event.preventDefault(); (event.currentTarget.closest("ul")?.querySelector("button") as HTMLElement | null)?.focus(); } else if (event.key === "End") { event.preventDefault(); (event.currentTarget.closest("ul")?.lastElementChild?.querySelector("button") as HTMLElement | null)?.focus(); } else if (event.key === "Enter") { event.preventDefault(); selectSearchResult(result); } }} type="button"><span>{kindLabels[result.node.kind]}</span><strong>{result.node.title ?? kindLabels[result.node.kind]}</strong><small>{result.breadcrumb}</small><i aria-hidden="true">{String(index + 1).padStart(2, "0")}</i></button></li>)}</ul>}</div> : null}
      </div>
      <span className="knowledge-count">{nodes.length} 个节点</span>
      {bootstrapQuery.data.capabilities.knowledge_folder_import ? <button className="button" disabled={!importParent || busy} onClick={(event) => { setImportOpener(event.currentTarget); setImportOpen(true); }} type="button">导入文件夹</button> : null}
      <button className="button" onClick={(event) => openDialog(event, "folder")} type="button">新建文件夹</button><button className="button primary" onClick={(event) => openDialog(event, "document")} type="button">新建文档</button>
    </div>
    <div aria-atomic="true" aria-live="polite" className={`knowledge-notice ${notice ? `is-${notice.kind}` : ""}`} role={notice?.kind === "error" ? "alert" : "status"}>{notice?.message ?? ""}</div>
    <div className="knowledge-workspace panel-card">
      <aside className="knowledge-tree-panel"><div className="knowledge-panel-heading"><p className="state-kicker">CONTENTS · 稳定目录</p><h2>知识目录</h2><p>焦点移动不会更改右侧选择。</p></div>{rootItems.length ? <ul aria-label="知识目录" className="knowledge-tree" role="tree"><RecursiveTreeItems childMap={childMap} expanded={effectiveExpanded} focusedId={effectiveFocusedId} items={rootItems} level={1} onFocus={focusNode} onKeyDown={onTreeKey} onSelect={(id) => updateUrl(id)} selectedId={selectedId} /></ul> : <div className="knowledge-empty"><strong>知识树还是空的</strong><p>新建文件夹或文档开始整理。</p></div>}</aside>
      <main className="knowledge-detail-panel">{!selectedId ? <div className="knowledge-blank"><span aria-hidden="true">序</span><h2>选择一条知识</h2><p>方向键只移动目录焦点；按 Enter 或点击后，详情才会切换。</p></div> : detailQuery.isPending ? <p aria-live="polite" className="muted-message">正在读取节点详情…</p> : detailQuery.isError || !detailQuery.data ? <div className="inline-error" role="alert">{errorMessage(detailQuery.error)}<button className="text-button" onClick={() => detailQuery.refetch()} type="button">重试</button></div> : <article className="knowledge-document"><header><div><p className="state-kicker">{kindLabels[detailQuery.data.node.kind].toUpperCase()} · VERSION {detailQuery.data.node.version}</p><h2>{detailQuery.data.revision?.title ?? detailQuery.data.node.title ?? kindLabels[detailQuery.data.node.kind]}</h2><p>{nodePathLabel(nodes, detailQuery.data.node)}</p></div><span className={`detail-kind kind-${detailQuery.data.node.kind}`}>{kindMarks[detailQuery.data.node.kind]}</span></header>{detailQuery.data.revision ? <><div className="knowledge-body">{detailQuery.data.revision.body || "这个节点还没有正文。"}</div>{detailQuery.data.revision.tags.length ? <ul className="knowledge-tags" aria-label="知识标签">{detailQuery.data.revision.tags.map((tag) => <li key={tag}>{tag}</li>)}</ul> : null}</> : <div className="knowledge-folder-note">{detailQuery.data.node.kind === "source" ? "来源节点只记录不可变来源版本，不承载可编辑正文。" : "此节点不承载正文。可在这里继续组织下级知识。"}</div>}<section aria-labelledby="citations-title" className="knowledge-citations"><div><p className="state-kicker">EVIDENCE · 冻结引用</p><h3 id="citations-title">来源证据</h3></div>{detailQuery.data.evidence.length ? <ul>{detailQuery.data.evidence.map((item, index) => <li key={item.id}><button onClick={(event) => openEvidence(event, item.id)} type="button"><span>{String(index + 1).padStart(2, "0")}</span><strong>{item.frozen_quote || "查看冻结证据"}</strong><small>Revision {item.revision_number} · {item.section_id}</small></button></li>)}</ul> : <p className="muted-message">当前修订没有来源证据。</p>}</section>{moveOrDeleteAllowed ? <footer className="knowledge-detail-actions">{appendRevisionAllowed ? <button className="button primary" onClick={(event) => openDialog(event, "edit")} type="button">编辑并新建修订</button> : null}<label><span>移动到</span><select aria-label="移动到" disabled={busy} onChange={(event) => { if (event.target.value !== moveValue) void move(event.target.value); }} value={moveValue}>{moveTargets.map((node) => <option key={node.id} value={node.kind === "root" ? "" : node.id}>{nodePathLabel(nodes, node)}</option>)}</select></label><button className="button danger" onClick={(event) => openDialog(event, "delete")} type="button">移到回收站</button></footer> : null}</article>}</main>
    </div>
    {evidenceId ? <CitationDrawer evidence={evidenceMatchesSelection ? evidenceQuery.data : undefined} loading={evidenceQuery.isPending || detailQuery.isPending} onClose={closeCitation} opener={citationOpener} /> : null}
    {importOpen && importParent ? <KnowledgeFolderImportDialog limits={bootstrapQuery.data.limits.knowledge_import} onClose={() => setImportOpen(false)} onSuccess={async (result) => completeFolderImport(result.root_node_id, result.summary)} opener={importOpener} parent={importParent} parentLabel={nodePathLabel(nodes, importParent)} spaceId={spaceId} /> : null}
    {dialog ? <KnowledgeDialog busy={busy} detail={detailQuery.data} mode={dialog} onClose={closeDialog} onSubmit={mutate} opener={dialogOpener} selected={selected} /> : null}
  </section>;
}
