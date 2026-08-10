import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { KnowledgeWorkspace, ancestorIds, visibleKnowledgeNodes } from "./knowledge-workspace";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  usePathname: () => "/knowledge",
  useRouter: () => ({ replace }),
  useSearchParams: () => new URLSearchParams(),
}));

const ids = {
  space: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
  root: "77881bd1-0c4a-4cdb-b0d8-7ed52d25273f",
  folder: "7e8ee491-cd97-435c-97d4-82cc1951300c",
  document: "a9a6e120-59b4-4ebc-a9b3-b4b81c7ee206",
  point: "c90d99d3-4080-4918-aae6-5d7dc59289c9",
  sourceNode: "247197b5-4e02-4442-a0a6-280151242198",
  source: "3f27ab12-8593-44b8-afc0-b5d3a1f76ab4",
  revision: "c3e1d289-31c1-444d-bda0-b5a4ea165142",
  evidence: "f21b429d-c60d-4f0b-85ba-d2ac82a2eb70",
  sourceVersion: "e44c926f-399b-4087-a727-e9e5c2a6b112",
  artifact: "0cc7c2c8-d03b-472c-b11e-e2e721700efa",
  section: "152ed6f1-ecf2-4aec-aa85-ad33c85944f7",
};
const now = "2026-08-05T12:00:00Z";
const base = { space_id: ids.space, path: "nroot", version: 1, sort_order: 0, origin_candidate_id: null, current_revision_id: null, source_id: null, source_version_id: null, created_at: now, updated_at: now, deleted_at: null };
const nodes = [
  { ...base, id: ids.root, parent_id: null, kind: "root", title: "我的知识库" },
  { ...base, id: ids.folder, parent_id: ids.root, kind: "folder", path: "nroot.nfolder", title: "研究", sort_order: 0 },
  { ...base, id: ids.document, parent_id: ids.folder, kind: "document", path: "nroot.nfolder.ndocument", title: "方法文档", current_revision_id: ids.revision },
  { ...base, id: ids.point, parent_id: ids.document, kind: "point", path: "nroot.nfolder.ndocument.npoint", title: "原子知识", current_revision_id: ids.revision },
  { ...base, id: ids.sourceNode, parent_id: ids.document, kind: "source", path: "nroot.nfolder.ndocument.nsource", title: "真实来源", source_id: ids.source, source_version_id: ids.sourceVersion, sort_order: 1 },
];
const revision = { id: ids.revision, node_id: ids.point, revision_number: 1, title: "原子知识", body: "只能以纯文本呈现的知识正文。", tags: ["方法"], conditions: [], exceptions: [], content_hash: "a".repeat(64), actor: "local", edit_reason: null, created_at: now };
const evidence = { id: ids.evidence, node_id: ids.point, revision_id: ids.revision, revision_number: 1, source_id: ids.source, source_title: "真实来源", source_version_id: ids.sourceVersion, source_version_number: 2, source_content_hash: "d".repeat(64), parse_artifact_id: ids.artifact, artifact_revision: 3, section_id: ids.section, section_ordinal: 4, quote_hash: "b".repeat(64), content_hash: "c".repeat(64), locator: { page: 4 }, frozen_quote: "冻结的可信来源原文。", deep_link: `/sources/${ids.source}?versionId=${ids.sourceVersion}&artifactId=${ids.artifact}&sectionId=${ids.section}`, anchor_status: "exact", created_at: now };
const bootstrap = { space: { id: ids.space, slug: "mine", name: "我的知识库" }, capabilities: { source_import: true, extraction_review: true, knowledge_tree: true, trusted_qa: false, spaced_review: false, evidence_agent: false }, statistics: { sources: 1, queued_jobs: 0, activity_events: 0 }, foundation_status: "ready" };

function json(payload: unknown, status = 200) { return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } }); }
function detail(nodeId: string) {
  const node = nodes.find((item) => item.id === nodeId)!;
  const hasRevision = node.kind === "document" || node.kind === "point";
  return { node, revision: hasRevision ? { ...revision, node_id: node.id, title: node.title, body: node.kind === "point" ? revision.body : "文档正文" } : null, evidence: node.kind === "point" ? [evidence] : [] };
}
function fetchMock() {
  return vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/v1/bootstrap")) return Promise.resolve(json(bootstrap));
    if (url.endsWith("/knowledge-tree")) return Promise.resolve(json({ items: nodes }));
    if (url.includes("/knowledge-search?")) return Promise.resolve(json({ items: [{ node: nodes[3], breadcrumb: "我的知识库 / 研究 / 方法文档 / 原子知识", ancestor_ids: [ids.root, ids.folder, ids.document], match_fields: ["body"] }] }));
    if (url.endsWith(`/knowledge-evidence/${ids.evidence}`)) return Promise.resolve(json(evidence));
    const match = url.match(/\/knowledge-nodes\/([^/?]+)$/);
    if (match && !init?.method) return Promise.resolve(json(detail(match[1])));
    throw new Error(`Unexpected request ${url}`);
  });
}
function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  return render(<QueryClientProvider client={client}><KnowledgeWorkspace /></QueryClientProvider>);
}

const typedNodes = nodes as Parameters<typeof visibleKnowledgeNodes>[0];

describe("knowledge tree projections", () => {
  it("projects visible hierarchy and returns ordered ancestors", () => {
    expect(visibleKnowledgeNodes(typedNodes, new Set([ids.root, ids.folder])).map((node) => node.id)).toEqual([ids.root, ids.folder, ids.document]);
    expect(ancestorIds(typedNodes, ids.point)).toEqual([ids.root, ids.folder, ids.document]);
  });
});

describe("KnowledgeWorkspace", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    replace.mockReset();
    vi.stubGlobal("fetch", fetchMock());
    vi.stubGlobal("crypto", { randomUUID: vi.fn(() => "190b4f7f-7dfd-482b-89b8-ef4349767531") });
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 1; });
  });

  it("renders tree/group semantics and keeps focused and selected nodes separate across all tree keys", async () => {
    renderWorkspace();
    const tree = await screen.findByRole("tree", { name: "知识目录" });
    const root = within(tree).getByRole("treeitem", { name: /我的知识库/ });
    await waitFor(() => expect(root).toHaveAttribute("aria-expanded", "true"));
    expect(within(tree).getByRole("group")).toBeInTheDocument();

    root.focus();
    await userEvent.keyboard("{ArrowRight}");
    const folder = within(tree).getByRole("treeitem", { name: /研究/ });
    expect(folder).toHaveFocus();
    expect(folder).toHaveAttribute("aria-selected", "false");
    await userEvent.keyboard("{Enter}");
    expect(folder).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("heading", { name: "研究" })).toBeInTheDocument();

    await userEvent.keyboard("{ArrowRight}");
    expect(folder).toHaveAttribute("aria-expanded", "true");
    await userEvent.keyboard("{ArrowRight}");
    const document = within(tree).getByRole("treeitem", { name: /方法文档/ });
    expect(document).toHaveFocus();
    expect(folder).toHaveAttribute("aria-selected", "true");
    await userEvent.keyboard("{ArrowLeft}");
    expect(folder).toHaveFocus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(folder).toHaveAttribute("aria-expanded", "false");
    await userEvent.keyboard("{End}");
    expect(folder).toHaveFocus();
    await userEvent.keyboard("{Home}");
    expect(root).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}{ArrowUp}");
    expect(root).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("searchbox", { name: "搜索知识" })).toHaveFocus();
  });

  it("uses server search, expands ancestors, clears search, and selects the result", async () => {
    renderWorkspace();
    const search = await screen.findByRole("searchbox", { name: "搜索知识" });
    await userEvent.type(search, "可信原文");
    const result = await screen.findByRole("button", { name: /原子知识/ });
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/knowledge-search?q="))).toBe(true);
    await userEvent.click(result);

    expect(search).toHaveValue("");
    const point = await screen.findByRole("treeitem", { name: /原子知识/ });
    expect(point).toHaveAttribute("aria-selected", "true");
    expect(point).toHaveFocus();
    expect(screen.getByRole("treeitem", { name: /方法文档/ })).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText("只能以纯文本呈现的知识正文。")).toBeInTheDocument();
  });

  it("does not offer revision editing for folders and restores dialog focus", async () => {
    renderWorkspace();
    const tree = await screen.findByRole("tree", { name: "知识目录" });
    await userEvent.click(within(tree).getByRole("treeitem", { name: /研究/ }));

    expect(await screen.findByRole("heading", { name: "研究" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑并新建修订" })).not.toBeInTheDocument();
    const deleteOpener = screen.getByRole("button", { name: "移到回收站" });
    await userEvent.click(deleteOpener);
    const dialog = await screen.findByRole("dialog", { name: "移到回收站" });
    expect(within(dialog).getByRole("button", { name: "关闭" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "移到回收站" })).not.toBeInTheDocument());
    expect(deleteOpener).toHaveFocus();
  });

  it("opens evidence as a trapped modal, deep-links, closes by Escape/backdrop, and restores the exact opener", async () => {
    renderWorkspace();
    const search = await screen.findByRole("searchbox", { name: "搜索知识" });
    await userEvent.type(search, "原子知识");
    await userEvent.click(await screen.findByRole("button", { name: /原子知识/ }));
    const opener = await screen.findByRole("button", { name: /冻结的可信来源原文/ });
    await userEvent.click(opener);

    const dialog = await screen.findByRole("dialog", { name: "来源引用" });
    const close = within(dialog).getByRole("button", { name: "关闭来源引用" });
    expect(close).toHaveFocus();
    await waitFor(() => expect(within(dialog).getByText("冻结的可信来源原文。")).toBeInTheDocument());
    expect(within(dialog).getByRole("link", { name: "在来源中查看" })).toHaveAttribute("href", `/sources/${ids.source}?versionId=${ids.sourceVersion}&artifactId=${ids.artifact}&sectionId=${ids.section}`);
    await userEvent.tab({ shift: true });
    expect(within(dialog).getByRole("link", { name: "在来源中查看" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "来源引用" })).not.toBeInTheDocument());
    expect(opener).toHaveFocus();

    await userEvent.click(opener);
    const reopened = await screen.findByRole("dialog", { name: "来源引用" });
    fireEvent.mouseDown(reopened.parentElement!);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "来源引用" })).not.toBeInTheDocument());
    expect(opener).toHaveFocus();
  });
});
