"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import {
  ApiClientError,
  createApiClient,
  type AuthorizationPreview,
  type Repository,
  type RepositoryList,
  type ScanResult,
} from "./api-client";
import { ConfirmDialog } from "./confirm-dialog";
import { Icon } from "./workbench-icon";

type DialogState =
  | { kind: "authorize"; preview: AuthorizationPreview }
  | { kind: "scan"; repository: Repository }
  | { kind: "revoke"; repository: Repository }
  | null;

type ActionState = "idle" | "preview" | "authorize" | "scan" | "revoke";

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = -1;
  do {
    value /= 1024;
    unit += 1;
  } while (value >= 1024 && unit < units.length - 1);
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`;
}

function statusLabel(repository: Repository) {
  if (repository.authorization_status === "revoked") return "已撤销";
  if (repository.authorization_status === "pending") return "待授权";
  if (repository.scan_state === "not_scanned") return "已授权 · 未扫描";
  const indexingLabels: Record<Repository["indexing_state"], string> = {
    not_queued: "清单已生成",
    pending: "索引已排队",
    running: "索引进行中",
    succeeded: "索引完成",
    failed: "索引失败",
    cancelled: "索引已取消",
  };
  return indexingLabels[repository.indexing_state];
}

function errorMessage(error: unknown) {
  if (error instanceof ApiClientError) {
    return error.requestId ? `${error.message} · 请求 ${error.requestId}` : error.message;
  }
  return error instanceof Error ? error.message : "操作失败，请重试。";
}

function upsertRepository(list: RepositoryList, repository: Repository): RepositoryList {
  const exists = list.repositories.some((item) => item.id === repository.id);
  return {
    ...list,
    repositories: exists
      ? list.repositories.map((item) => item.id === repository.id ? repository : item)
      : [...list.repositories, repository],
  };
}

export function RepositoryWorkbench({ apiBaseUrl }: { apiBaseUrl: string }) {
  const api = useMemo(() => createApiClient(apiBaseUrl), [apiBaseUrl]);
  const [data, setData] = useState<RepositoryList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [path, setPath] = useState("");
  const [dialog, setDialog] = useState<DialogState>(null);
  const [action, setAction] = useState<ActionState>("idle");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [scanResults, setScanResults] = useState<Record<string, ScanResult>>({});
  const [notice, setNotice] = useState("");
  const listRequestRef = useRef<{ controller: AbortController; generation: number } | null>(null);
  const listGenerationRef = useRef(0);

  async function loadRepositories() {
    listRequestRef.current?.controller.abort();
    const controller = new AbortController();
    const generation = ++listGenerationRef.current;
    listRequestRef.current = { controller, generation };
    setLoading(true);
    setError("");
    try {
      const repositories = await api.listRepositories(controller.signal);
      if (listRequestRef.current?.generation !== generation) return;
      setData(repositories);
      setSelectedIds((current) => current.filter((id) =>
        repositories.repositories.some((repo) =>
          repo.id === id
          && repo.authorization_status === "authorized"
          && repo.scan_state === "manifest_ready",
        ),
      ));
    } catch (loadError) {
      if (controller.signal.aborted || listRequestRef.current?.generation !== generation) return;
      setError(errorMessage(loadError));
    } finally {
      if (listRequestRef.current?.generation === generation) {
        listRequestRef.current = null;
        setLoading(false);
      }
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    const generation = ++listGenerationRef.current;
    listRequestRef.current = { controller, generation };
    api.listRepositories(controller.signal)
      .then((repositories) => {
        if (listRequestRef.current?.generation !== generation) return;
        setData(repositories);
        setSelectedIds((current) => current.filter((id) =>
          repositories.repositories.some((repo) =>
            repo.id === id
            && repo.authorization_status === "authorized"
            && repo.scan_state === "manifest_ready",
          ),
        ));
      })
      .catch((loadError: unknown) => {
        if (controller.signal.aborted || listRequestRef.current?.generation !== generation) return;
        setError(errorMessage(loadError));
      })
      .finally(() => {
        if (listRequestRef.current?.generation === generation) {
          listRequestRef.current = null;
          setLoading(false);
        }
      });
    return () => {
      controller.abort();
      if (listRequestRef.current?.generation === generation) listRequestRef.current = null;
    };
  }, [api]);

  function invalidateRepositoryLoad() {
    listRequestRef.current?.controller.abort();
    listRequestRef.current = null;
    listGenerationRef.current += 1;
    setLoading(false);
  }

  async function createPreview(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!path.trim()) return;
    setAction("preview");
    setError("");
    try {
      const preview = await api.previewAuthorization({ path: path.trim() });
      setDialog({ kind: "authorize", preview });
    } catch (previewError) {
      setError(errorMessage(previewError));
    } finally {
      setAction("idle");
    }
  }

  async function authorize() {
    if (dialog?.kind !== "authorize") return;
    invalidateRepositoryLoad();
    setAction("authorize");
    setError("");
    try {
      const result = await api.authorizeRepository({
        confirmation: true,
        preview_token: dialog.preview.preview_token,
      });
      invalidateRepositoryLoad();
      setData((current) => current ? upsertRepository(current, result.repository) : current);
      setPath("");
      setDialog(null);
      setNotice(`仓库 ${result.repository.name} 已授权。请在资料边界中单独选择是否用于上下文。`);
      if (!data) await loadRepositories();
    } catch (authorizeError) {
      setError(errorMessage(authorizeError));
    } finally {
      setAction("idle");
    }
  }

  async function scan() {
    if (dialog?.kind !== "scan") return;
    const repository = dialog.repository;
    invalidateRepositoryLoad();
    setAction("scan");
    setError("");
    try {
      const result = await api.scanRepository(repository.id, {
        expected_authorization_epoch: repository.authorization_epoch,
      });
      setScanResults((current) => ({ ...current, [repository.id]: result }));
      setDialog(null);
      setNotice(result.outcome === "created" ? "扫描清单已创建。" : "扫描完成；仓库内容未变化。");
      await loadRepositories();
    } catch (scanError) {
      setError(errorMessage(scanError));
    } finally {
      setAction("idle");
    }
  }

  async function revoke() {
    if (dialog?.kind !== "revoke") return;
    const repository = dialog.repository;
    invalidateRepositoryLoad();
    setAction("revoke");
    setError("");
    try {
      const result = await api.revokeRepository(repository.id, {
        expected_authorization_epoch: repository.authorization_epoch,
      });
      invalidateRepositoryLoad();
      setData((current) => current ? upsertRepository(current, result.repository) : current);
      setSelectedIds((current) => current.filter((id) => id !== repository.id));
      setDialog(null);
      setNotice(result.outcome === "already_revoked" ? "仓库授权此前已撤销。" : "仓库授权已撤销。" );
    } catch (revokeError) {
      setError(errorMessage(revokeError));
    } finally {
      setAction("idle");
    }
  }

  function toggleContext(repository: Repository) {
    if (repository.authorization_status !== "authorized" || repository.scan_state !== "manifest_ready") return;
    setSelectedIds((current) => current.includes(repository.id)
      ? current.filter((id) => id !== repository.id)
      : [...current, repository.id]);
  }

  const repositoryCount = data?.repositories.length;
  const selectedRepositories = data?.repositories.filter((repository) => selectedIds.includes(repository.id)) ?? [];

  return (
    <>
      <aside className="sources-panel" aria-label="仓库与上下文">
        <div className="panel-heading panel-heading--dark">
          <div>
            <span className="eyebrow">CONTEXT / 01</span>
            <h2>资料边界</h2>
          </div>
          <span className="panel-counter" aria-label={repositoryCount === undefined ? "仓库数量未知" : `${repositoryCount} 个仓库`}>
            {repositoryCount === undefined ? "—" : String(repositoryCount).padStart(2, "0")}
          </span>
        </div>

        <div className="source-scroll">
          <section className="authorization-section" aria-labelledby="authorization-title">
            <div className="section-label-row">
              <h3 id="authorization-title">本地仓库授权</h3>
              <span>EXPLICIT</span>
            </div>
            <form className="authorization-form" onSubmit={createPreview}>
              <label htmlFor="repository-path">绝对路径</label>
              <div className="path-entry">
                <input
                  id="repository-path"
                  value={path}
                  onChange={(event) => setPath(event.target.value)}
                  placeholder="D:\\workbench\\project"
                  autoComplete="off"
                  disabled={action !== "idle"}
                />
                <button type="submit" disabled={!path.trim() || action !== "idle"}>
                  {action === "preview" ? "检查中" : "预览"}
                </button>
              </div>
              <p>预览不会授予访问权限；确认授权后仍需单独选择上下文。</p>
            </form>
          </section>

          <section className="source-section" aria-labelledby="repositories-title">
            <div className="section-label-row">
              <h3 id="repositories-title">已登记仓库</h3>
              <button type="button" className="text-button text-button--dark" onClick={() => loadRepositories()} disabled={loading}>
                {loading ? "读取中" : "刷新"}
              </button>
            </div>

            {error ? <div className="panel-error" role="alert">{error}</div> : null}
            {loading ? <p className="source-empty" role="status">正在读取仓库登记表…</p> : null}
            {!loading && !error && data?.repositories.length === 0 ? (
              <p className="source-empty">尚无已登记仓库。先输入路径并检查授权预览。</p>
            ) : null}

            <div className="repository-list">
              {data?.repositories.map((repository) => {
                const selected = selectedIds.includes(repository.id);
                const authorized = repository.authorization_status === "authorized";
                const contextReady = authorized && repository.scan_state === "manifest_ready";
                return (
                  <article className={`repository-card ${selected ? "repository-card--selected" : ""}`} key={repository.id}>
                    <button
                      type="button"
                      className="repository-context-toggle"
                      aria-pressed={selected}
                      disabled={!contextReady}
                      title={contextReady ? "选择或移出提问上下文" : "完成授权与扫描后才能选择上下文"}
                      onClick={() => toggleContext(repository)}
                    >
                      <span className="source-icon"><Icon name="folder" /></span>
                      <span className="source-copy">
                        <strong>{repository.name}</strong>
                        <small title={repository.canonical_path}>{repository.canonical_path}</small>
                      </span>
                      <span className="selection-box" aria-hidden="true">{selected ? "✓" : ""}</span>
                    </button>
                    <div className="repository-status-row">
                      <span className={`repository-state repository-state--${repository.authorization_status}`}>{statusLabel(repository)}</span>
                      <span>{repository.stats.eligible_files} files · {formatBytes(repository.stats.eligible_bytes)}</span>
                    </div>
                    <div className="repository-actions">
                      <button
                        type="button"
                        onClick={() => setDialog({ kind: "scan", repository })}
                        disabled={!authorized || action !== "idle"}
                      >扫描</button>
                      <button
                        type="button"
                        onClick={() => setDialog({ kind: "revoke", repository })}
                        disabled={!authorized || action !== "idle"}
                      >撤销</button>
                    </div>
                    {scanResults[repository.id] ? (
                      <p className="scan-result" role="status">
                        最近扫描：{scanResults[repository.id].stats.eligible_files} 个可用文件，{formatBytes(scanResults[repository.id].stats.eligible_bytes)}
                      </p>
                    ) : null}
                  </article>
                );
              })}
            </div>
          </section>
        </div>

        <footer className="sources-footer">
          <span>{selectedRepositories.length} 个仓库用于上下文</span>
          <span>READ ONLY</span>
        </footer>
      </aside>

      <section className="conversation-panel" aria-labelledby="conversation-title">
        <div className="conversation-masthead">
          <div className="issue-line"><span>SESSION / NOT STARTED</span><span>LOCAL-FIRST</span></div>
          <h1 id="conversation-title">Ask with evidence.</h1>
          <p>仓库授权只建立可访问边界。选择上下文是独立动作；未选择的仓库不会被纳入后续问答。</p>
        </div>

        <div className="empty-state-graphic" aria-hidden="true">
          <div className="orbital orbital--outer"><span className="orbit-dot" /></div>
          <div className="orbital orbital--inner" />
          <div className="graphic-core"><Icon name="search" size={24} /></div>
          <span className="graphic-coordinate graphic-coordinate--top">BOUNDARY</span>
          <span className="graphic-coordinate graphic-coordinate--right">CONTEXT</span>
          <span className="graphic-coordinate graphic-coordinate--bottom">EVIDENCE</span>
        </div>

        <section className="context-ledger" aria-labelledby="context-ledger-title">
          <div className="context-ledger-heading">
            <div><span className="eyebrow eyebrow--ink">CONTEXT / SELECTED</span><h2 id="context-ledger-title">本次提问的资料范围</h2></div>
            <span>{String(selectedRepositories.length).padStart(2, "0")}</span>
          </div>
          {selectedRepositories.length ? (
            <ul>{selectedRepositories.map((repository) => <li key={repository.id}><Icon name="folder" /><span>{repository.name}</span><small>{repository.canonical_path}</small></li>)}</ul>
          ) : (
            <p>尚未选择上下文。请在左侧从已授权仓库中明确选择。</p>
          )}
        </section>

        <div className="composer-wrap">
          <div className="composer composer--disabled" aria-disabled="true">
            <label htmlFor="agent-prompt" className="sr-only">向工作台提问</label>
            <textarea id="agent-prompt" placeholder="问答服务尚未接入；此处不会生成模拟回答。" rows={2} disabled />
            <div className="composer-footer">
              <div className="composer-meta"><span><Icon name="archive" size={14} /> {selectedRepositories.length} 个仓库已选择</span><span className="composer-divider" /><span>只读模式</span></div>
              <button type="button" className="send-button" disabled><span>发送</span><Icon name="send" /></button>
            </div>
          </div>
          <p className="composer-hint">未接入问答 API · 不展示模拟证据或运行进度</p>
        </div>
      </section>

      <RepositoryInspector repositories={data?.repositories ?? []} scanResults={scanResults} />

      {notice ? <div className="notice" role="status"><span>{notice}</span><button type="button" onClick={() => setNotice("")} aria-label="关闭通知"><Icon name="x" size={14} /></button></div> : null}

      {dialog?.kind === "authorize" ? (
        <ConfirmDialog
          title={`授权 ${dialog.preview.display_name}`}
          description="授权允许服务读取该规范化路径内的仓库文件。此操作不会自动把仓库加入提问上下文。"
          confirmLabel="明确授权"
          busyLabel="授权中…"
          busy={action === "authorize"}
          onClose={() => action === "idle" && setDialog(null)}
          onConfirm={authorize}
        >
          <dl className="confirmation-details">
            <div><dt>规范路径</dt><dd>{dialog.preview.canonical_path}</dd></div>
            <div><dt>安全说明</dt><dd>{dialog.preview.security_notice}</dd></div>
            <div><dt>策略版本</dt><dd>{dialog.preview.policy_version}</dd></div>
            <div><dt>预览过期</dt><dd>{new Date(dialog.preview.expires_at).toLocaleString("zh-CN")}</dd></div>
          </dl>
        </ConfirmDialog>
      ) : null}

      {dialog?.kind === "scan" ? (
        <ConfirmDialog
          title={`扫描 ${dialog.repository.name}`}
          description="扫描会读取当前授权边界内的文件元数据并生成真实清单；结果由 API 返回后才显示。"
          confirmLabel="开始扫描"
          busyLabel="扫描中…"
          busy={action === "scan"}
          onClose={() => action === "idle" && setDialog(null)}
          onConfirm={scan}
        >
          <p className="dialog-path">{dialog.repository.canonical_path}</p>
        </ConfirmDialog>
      ) : null}

      {dialog?.kind === "revoke" ? (
        <ConfirmDialog
          title={`撤销 ${dialog.repository.name}`}
          description="撤销后该仓库会立即退出本次上下文，后续扫描请求将不再被允许。撤销不是数据清除：历史清单与审计记录仍会保留。"
          confirmLabel="撤销授权"
          busyLabel="撤销中…"
          dangerous
          busy={action === "revoke"}
          onClose={() => action === "idle" && setDialog(null)}
          onConfirm={revoke}
        >
          <p className="dialog-path">{dialog.repository.canonical_path}</p>
        </ConfirmDialog>
      ) : null}
    </>
  );
}

function RepositoryInspector({
  repositories,
  scanResults,
}: {
  repositories: Repository[];
  scanResults: Record<string, ScanResult>;
}) {
  const tabs = ["boundary", "activity"] as const;
  type Tab = typeof tabs[number];
  const [tab, setTab] = useState<Tab>("boundary");

  function moveTab(event: React.KeyboardEvent<HTMLButtonElement>, current: Tab) {
    const index = tabs.indexOf(current);
    let nextIndex = index;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = tabs.length - 1;
    else return;
    event.preventDefault();
    setTab(tabs[nextIndex]);
    document.getElementById(`${tabs[nextIndex]}-tab`)?.focus();
  }

  return (
    <aside className="inspector-panel" aria-label="仓库检查器">
      <div className="inspector-tabs" role="tablist" aria-label="仓库检查器视图">
        <button id="boundary-tab" type="button" role="tab" aria-controls="boundary-panel" aria-selected={tab === "boundary"} tabIndex={tab === "boundary" ? 0 : -1} className={tab === "boundary" ? "active" : ""} onClick={() => setTab("boundary")} onKeyDown={(event) => moveTab(event, "boundary")}>授权边界</button>
        <button id="activity-tab" type="button" role="tab" aria-controls="activity-panel" aria-selected={tab === "activity"} tabIndex={tab === "activity" ? 0 : -1} className={tab === "activity" ? "active" : ""} onClick={() => setTab("activity")} onKeyDown={(event) => moveTab(event, "activity")}>扫描结果</button>
      </div>

      {tab === "boundary" ? (
        <div className="inspector-content" role="tabpanel" id="boundary-panel" aria-labelledby="boundary-tab">
          <div className="inspector-intro"><span className="eyebrow eyebrow--ink">AUTHORIZATION / LEDGER</span><h2>明确授权记录</h2><p>这里只展示 API 返回的登记状态，不推断文件内容或索引完成度。</p></div>
          {repositories.length ? <div className="boundary-list">{repositories.map((repository) => (
            <article key={repository.id}>
              <div><span className={`repository-state repository-state--${repository.authorization_status}`}>{statusLabel(repository)}</span><code>epoch {repository.authorization_epoch}</code></div>
              <h3>{repository.name}</h3>
              <p>{repository.canonical_path}</p>
              <dl><div><dt>已授权</dt><dd>{repository.authorized_at ? new Date(repository.authorized_at).toLocaleString("zh-CN") : "—"}</dd></div><div><dt>已撤销</dt><dd>{repository.revoked_at ? new Date(repository.revoked_at).toLocaleString("zh-CN") : "—"}</dd></div></dl>
            </article>
          ))}</div> : <p className="inspector-empty">尚无仓库授权记录。</p>}
        </div>
      ) : (
        <div className="inspector-content" role="tabpanel" id="activity-panel" aria-labelledby="activity-tab">
          <div className="inspector-intro"><span className="eyebrow eyebrow--ink">SCAN / OBSERVED</span><h2>本次会话的扫描结果</h2><p>只有完成真实扫描请求后才出现统计与清单哈希。</p></div>
          {Object.keys(scanResults).length ? <div className="scan-list">{Object.entries(scanResults).map(([id, result]) => {
            const repository = repositories.find((item) => item.id === id);
            return <article key={id}><span>{result.outcome === "created" ? "新清单" : "内容未变"}</span><h3>{repository?.name ?? id}</h3><dl><div><dt>可用文件</dt><dd>{result.stats.eligible_files}</dd></div><div><dt>可用体积</dt><dd>{formatBytes(result.stats.eligible_bytes)}</dd></div><div><dt>遍历目录</dt><dd>{result.stats.directories_visited}</dd></div><div><dt>清单哈希</dt><dd><code>{result.manifest_hash}</code></dd></div></dl></article>;
          })}</div> : <p className="inspector-empty">本次会话尚未执行扫描。不会显示占位进度或虚构计数。</p>}
        </div>
      )}
      <footer className="inspector-footer"><span>CONTRACT</span><code>api/v1 · runtime validated</code></footer>
    </aside>
  );
}
