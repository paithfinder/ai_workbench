"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { classifyHealthEnvelope } from "./health-status.mjs";

type ApiState = "checking" | "online" | "degraded" | "model-missing" | "offline";
type InspectorTab = "evidence" | "run";
type IconName =
  | "archive"
  | "branch"
  | "chevron"
  | "clock"
  | "database"
  | "file"
  | "folder"
  | "hash"
  | "link"
  | "notion"
  | "plus"
  | "search"
  | "send"
  | "terminal";

const repositories = [
  { id: "workbench", name: "ai-workbench", detail: "dev-ml · 当前项目", active: true },
  { id: "low-code", name: "ai-low-code", detail: "main · 仅作参考", active: false },
];

const knowledgeSources = [
  { id: "docs", name: "项目文档", detail: "12 个文件", icon: "file" as IconName },
  { id: "notion", name: "Notion", detail: "尚未连接", icon: "notion" as IconName },
  { id: "web", name: "网页资料", detail: "0 个来源", icon: "link" as IconName },
];

const suggestedPrompts = [
  "解释这个项目的整体架构",
  "API 的健康检查在哪里实现？",
  "梳理从提问到生成引用的完整链路",
];

const evidence = [
  {
    score: "0.94",
    kind: "代码",
    title: "应用入口与路由",
    path: "apps/api/src/ai_workbench_api/main.py",
    range: "L1–85",
    excerpt: "FastAPI 应用实例、版本化路由与生命周期配置。",
  },
  {
    score: "0.89",
    kind: "配置",
    title: "前端运行配置",
    path: "apps/web/next.config.ts",
    range: "L1–7",
    excerpt: "Next.js 工作台的基础运行配置。",
  },
  {
    score: "0.86",
    kind: "文档",
    title: "项目边界",
    path: "README.md",
    range: "L8–24",
    excerpt: "MVP 范围、数据来源与本地开发约束。",
  },
];

const runSteps = [
  { index: "01", title: "解析意图", detail: "识别问题类型与允许的数据边界", state: "complete", meta: "18 ms" },
  { index: "02", title: "制定检索计划", detail: "路径、符号、全文与语义检索", state: "complete", meta: "31 ms" },
  { index: "03", title: "并行检索", detail: "等待索引服务接入", state: "waiting", meta: "—" },
  { index: "04", title: "评估证据", detail: "充分性、冲突与引用校验", state: "idle", meta: "—" },
  { index: "05", title: "生成回答", detail: "输出事实、推断与证据不足项", state: "idle", meta: "—" },
];

function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.6,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };

  const paths: Record<IconName, React.ReactNode> = {
    archive: <><path d="M4 7.5h16v12H4z" /><path d="M3 4.5h18v3H3zM9 11h6" /></>,
    branch: <><circle cx="6" cy="5" r="2" /><circle cx="18" cy="7" r="2" /><circle cx="6" cy="19" r="2" /><path d="M6 7v10M8 9c5 0 4-2 8-2" /></>,
    chevron: <path d="m9 18 6-6-6-6" />,
    clock: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>,
    database: <><ellipse cx="12" cy="5.5" rx="7.5" ry="3" /><path d="M4.5 5.5v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-6M4.5 11.5v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-6" /></>,
    file: <><path d="M6 3.5h8l4 4v13H6z" /><path d="M14 3.5v4h4M9 12h6M9 16h5" /></>,
    folder: <path d="M3.5 6.5h6l2 2h9v10.5h-17z" />,
    hash: <><path d="M9 3 7 21M17 3l-2 18M4 9h17M3 15h17" /></>,
    link: <><path d="m9.5 14.5 5-5" /><path d="M7.2 16.8 5.7 18.3a3.5 3.5 0 0 1-5-5l3-3a3.5 3.5 0 0 1 5 0M16.8 7.2l1.5-1.5a3.5 3.5 0 0 1 5 5l-3 3a3.5 3.5 0 0 1-5 0" transform="translate(-1.5)" /></>,
    notion: <><path d="M5 4.5h13.5v15H5z" /><path d="m8 16 .2-8 5.8 8V8M7.5 7.5h2M13 7.5h3" /></>,
    plus: <path d="M12 5v14M5 12h14" />,
    search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m15.5 15.5 4 4" /></>,
    send: <><path d="m4 4 17 8-17 8 3-8z" /><path d="M7 12h14" /></>,
    terminal: <><path d="m5 7 4 4-4 4M11 16h7" /><rect x="2.5" y="3.5" width="19" height="17" rx="1" /></>,
  };

  return <svg {...common}>{paths[name]}</svg>;
}

function StatusMark({ state }: { state: ApiState }) {
  return (
    <span className={`status-mark status-mark--${state}`} aria-hidden="true">
      <span />
    </span>
  );
}

export default function Home() {
  const [apiState, setApiState] = useState<ApiState>("checking");
  const [selectedSources, setSelectedSources] = useState<string[]>(["workbench", "docs"]);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("evidence");
  const [prompt, setPrompt] = useState("");
  const [notice, setNotice] = useState("");
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 3500);
    const apiBase = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1").replace(/\/$/, "");

    fetch(`${apiBase}/health`, { signal: controller.signal, cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error("Health check failed");
        const envelope: unknown = await response.json();
        setApiState(classifyHealthEnvelope(envelope));
      })
      .catch(() => setApiState("offline"))
      .finally(() => window.clearTimeout(timeout));

    return () => {
      controller.abort();
      window.clearTimeout(timeout);
    };
  }, []);

  useEffect(() => {
    return () => {
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
    };
  }, []);

  const sourceLabel = useMemo(() => `${selectedSources.length} 个来源已启用`, [selectedSources.length]);

  function toggleSource(id: string) {
    setSelectedSources((current) =>
      current.includes(id) ? current.filter((source) => source !== id) : [...current, id],
    );
  }

  function showNotice(message: string) {
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    setNotice(message);
    noticeTimer.current = setTimeout(() => setNotice(""), 2800);
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!prompt.trim()) return;
    showNotice("Day 1 仅完成工作台与健康检查；问答 API 将在后续接入。");
  }

  const apiLabels: Record<ApiState, string> = {
    checking: "检查 API",
    online: "服务就绪",
    degraded: "数据库不可用",
    "model-missing": "模型未配置",
    offline: "API 未连接",
  };
  const apiLabel = apiLabels[apiState];

  return (
    <main className="workbench-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <span className="brand-index">01</span>
          <div>
            <p className="brand-name">FIELDNOTE</p>
            <p className="brand-subtitle">Personal agent workbench</p>
          </div>
        </div>

        <div className="topbar-context" aria-label="当前工作区">
          <span className="context-label">WORKSPACE</span>
          <span className="context-value">ai-workbench</span>
          <span className="context-divider" />
          <Icon name="branch" size={14} />
          <span className="context-value">dev-ml</span>
        </div>

        <div className="topbar-status">
          <div className="api-status" aria-live="polite">
            <StatusMark state={apiState} />
            <span>{apiLabel}</span>
          </div>
          <button className="icon-button" type="button" aria-label="打开命令面板" onClick={() => showNotice("命令面板将在后续迭代开放。") }>
            <Icon name="terminal" />
          </button>
          <div className="avatar" aria-label="本地用户">MT</div>
        </div>
      </header>

      <div className="workspace-grid">
        <aside className="sources-panel" aria-label="仓库与知识源">
          <div className="panel-heading panel-heading--dark">
            <div>
              <span className="eyebrow">CONTEXT / 01</span>
              <h2>资料边界</h2>
            </div>
            <button type="button" className="dark-icon-button" aria-label="添加资料源" onClick={() => showNotice("资料源接入将在索引服务完成后开放。") }>
              <Icon name="plus" />
            </button>
          </div>

          <div className="source-scroll">
            <section className="source-section" aria-labelledby="repositories-title">
              <div className="section-label-row">
                <h3 id="repositories-title">仓库</h3>
                <span>02</span>
              </div>
              <div className="source-list">
                {repositories.map((repository) => {
                  const selected = selectedSources.includes(repository.id);
                  return (
                    <button
                      className={`source-item ${selected ? "source-item--selected" : ""}`}
                      type="button"
                      key={repository.id}
                      aria-pressed={selected}
                      onClick={() => toggleSource(repository.id)}
                    >
                      <span className="source-icon"><Icon name="folder" /></span>
                      <span className="source-copy">
                        <strong>{repository.name}</strong>
                        <small>{repository.detail}</small>
                      </span>
                      <span className="selection-box" aria-hidden="true">{selected ? "✓" : ""}</span>
                    </button>
                  );
                })}
              </div>
            </section>

            <section className="source-section" aria-labelledby="knowledge-title">
              <div className="section-label-row">
                <h3 id="knowledge-title">知识源</h3>
                <span>03</span>
              </div>
              <div className="source-list">
                {knowledgeSources.map((source) => {
                  const selected = selectedSources.includes(source.id);
                  return (
                    <button
                      className={`source-item ${selected ? "source-item--selected" : ""}`}
                      type="button"
                      key={source.id}
                      aria-pressed={selected}
                      onClick={() => toggleSource(source.id)}
                    >
                      <span className="source-icon"><Icon name={source.icon} /></span>
                      <span className="source-copy">
                        <strong>{source.name}</strong>
                        <small>{source.detail}</small>
                      </span>
                      <span className="selection-box" aria-hidden="true">{selected ? "✓" : ""}</span>
                    </button>
                  );
                })}
              </div>
            </section>

            <section className="index-card" aria-label="索引状态">
              <div className="index-card-title">
                <Icon name="database" />
                <span>索引概况</span>
              </div>
              <div className="index-metric">
                <strong>—</strong>
                <span>向量片段</span>
              </div>
              <div className="index-rule"><span /></div>
              <p>等待摄取管道建立。当前页面不会读取你的本地文件。</p>
            </section>
          </div>

          <footer className="sources-footer">
            <span>{sourceLabel}</span>
            <span>READ ONLY</span>
          </footer>
        </aside>

        <section className="conversation-panel" aria-labelledby="conversation-title">
          <div className="conversation-masthead">
            <div className="issue-line">
              <span>SESSION 0001</span>
              <span>29 JUL 2026</span>
            </div>
            <h1 id="conversation-title">Ask with evidence.</h1>
            <p>从你的代码与资料中提问。每个结论都应当能追溯到原始上下文。</p>
          </div>

          <div className="empty-state-graphic" aria-hidden="true">
            <div className="orbital orbital--outer"><span className="orbit-dot" /></div>
            <div className="orbital orbital--inner" />
            <div className="graphic-core"><Icon name="search" size={24} /></div>
            <span className="graphic-coordinate graphic-coordinate--top">EVIDENCE</span>
            <span className="graphic-coordinate graphic-coordinate--right">SYNTHESIS</span>
            <span className="graphic-coordinate graphic-coordinate--bottom">CITATION</span>
          </div>

          <div className="prompt-suggestions" aria-label="示例问题">
            <span className="prompt-suggestions-label">从这里开始</span>
            <div className="prompt-suggestion-list">
              {suggestedPrompts.map((suggestion, index) => (
                <button key={suggestion} type="button" onClick={() => setPrompt(suggestion)}>
                  <span>0{index + 1}</span>
                  <span>{suggestion}</span>
                  <Icon name="chevron" size={14} />
                </button>
              ))}
            </div>
          </div>

          <div className="composer-wrap">
            <form className="composer" onSubmit={handleSubmit}>
              <label htmlFor="agent-prompt" className="sr-only">向工作台提问</label>
              <textarea
                id="agent-prompt"
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                    event.preventDefault();
                    event.currentTarget.form?.requestSubmit();
                  }
                }}
                placeholder="询问仓库结构、实现细节或技术文档…"
                rows={2}
              />
              <div className="composer-footer">
                <div className="composer-meta">
                  <span><Icon name="archive" size={14} /> {sourceLabel}</span>
                  <span className="composer-divider" />
                  <span>只读模式</span>
                </div>
                <button type="submit" className="send-button" disabled={!prompt.trim()} aria-label="发送问题">
                  <span>发送</span>
                  <Icon name="send" />
                </button>
              </div>
            </form>
            <p className="composer-hint">Ctrl / ⌘ + Enter 发送 · 模型回答功能尚未接入</p>
          </div>
        </section>

        <aside className="inspector-panel" aria-label="证据与运行过程">
          <div className="inspector-tabs" role="tablist" aria-label="检查器视图">
            <button
              type="button"
              role="tab"
              id="evidence-tab"
              aria-controls="evidence-panel"
              aria-selected={inspectorTab === "evidence"}
              className={inspectorTab === "evidence" ? "active" : ""}
              onClick={() => setInspectorTab("evidence")}
            >
              证据 <span>03</span>
            </button>
            <button
              type="button"
              role="tab"
              id="run-tab"
              aria-controls="run-panel"
              aria-selected={inspectorTab === "run"}
              className={inspectorTab === "run" ? "active" : ""}
              onClick={() => setInspectorTab("run")}
            >
              运行过程 <span>05</span>
            </button>
          </div>

          {inspectorTab === "evidence" ? (
            <div className="inspector-content" role="tabpanel" id="evidence-panel" aria-labelledby="evidence-tab">
              <div className="inspector-intro">
                <span className="eyebrow eyebrow--ink">EVIDENCE / VERIFIED</span>
                <h2>回答所依据的原文</h2>
                <p>引用将在服务端校验文件版本、行号与内容哈希。</p>
              </div>

              <div className="evidence-list">
                {evidence.map((item, index) => (
                  <article className="evidence-card" key={item.path}>
                    <div className="evidence-card-meta">
                      <span className="evidence-number">0{index + 1}</span>
                      <span className="evidence-kind">{item.kind}</span>
                      <span className="evidence-score">{item.score}</span>
                    </div>
                    <h3>{item.title}</h3>
                    <button type="button" className="evidence-path" onClick={() => showNotice("精确引用查看器将在检索链路完成后开放。") }>
                      <Icon name="file" size={14} />
                      <span>{item.path}</span>
                      <b>{item.range}</b>
                    </button>
                    <p>{item.excerpt}</p>
                  </article>
                ))}
              </div>

              <div className="verification-note">
                <Icon name="hash" size={15} />
                <p><strong>可验证引用</strong><br />示例内容仅用于展示界面结构，不代表当前索引结果。</p>
              </div>
            </div>
          ) : (
            <div className="inspector-content" role="tabpanel" id="run-panel" aria-labelledby="run-tab">
              <div className="inspector-intro">
                <span className="eyebrow eyebrow--ink">TRACE / LANGGRAPH</span>
                <h2>一次回答的路径</h2>
                <p>步骤会随服务端事件逐项展开，并保留耗时与工具输入。</p>
              </div>

              <ol className="run-list">
                {runSteps.map((step) => (
                  <li className={`run-step run-step--${step.state}`} key={step.index}>
                    <div className="run-step-index">{step.index}</div>
                    <div className="run-step-copy">
                      <div><h3>{step.title}</h3><span>{step.meta}</span></div>
                      <p>{step.detail}</p>
                    </div>
                  </li>
                ))}
              </ol>

              <div className="run-summary">
                <div><Icon name="clock" /><span>总耗时</span><strong>—</strong></div>
                <div><Icon name="hash" /><span>Token</span><strong>—</strong></div>
              </div>
            </div>
          )}

          <footer className="inspector-footer">
            <span>TRACE ID</span>
            <code>not-started</code>
          </footer>
        </aside>
      </div>

      {notice ? <div className="notice" role="status">{notice}</div> : null}
    </main>
  );
}
