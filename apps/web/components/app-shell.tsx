"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

const navigation = [
  {
    label: "知识处理",
    items: [
      { href: "/", label: "今日学习", icon: "⌂" },
      { href: "/import", label: "导入知识", icon: "⇧" },
      { href: "/extraction", label: "提炼审查", icon: "✓" },
      { href: "/proposals", label: "Proposal 审核", icon: "◇" },
      { href: "/knowledge", label: "我的知识树", icon: "⌘" },
    ],
  },
  {
    label: "学习应用",
    items: [
      { href: "/qa", label: "可信问答", icon: "✦" },
      { href: "/review", label: "间隔复习", icon: "↻" },
      { href: "/activity", label: "学习记录", icon: "◷" },
    ],
  },
] as const;

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [isNavigationOpen, setIsNavigationOpen] = useState(false);

  useEffect(() => {
    if (!isNavigationOpen) return;

    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setIsNavigationOpen(false);
    };

    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [isNavigationOpen]);

  return (
    <>
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <header className="topbar">
        <button
          aria-controls="primary-sidebar"
          aria-expanded={isNavigationOpen}
          aria-label={isNavigationOpen ? "关闭导航" : "打开导航"}
          className="mobile-nav-toggle"
          onClick={() => setIsNavigationOpen((open) => !open)}
          type="button"
        >
          <span aria-hidden="true">{isNavigationOpen ? "×" : "☰"}</span>
        </button>
        <Link className="brand" href="/" aria-label="自序首页">
          <span className="brand-mark" aria-hidden="true">序</span>
          <span className="brand-copy">
            <strong>自序</strong>
            <small>PERSONAL WORKSPACE</small>
          </span>
        </Link>
        <div className="workspace-label" aria-label="当前工作台">
          <span aria-hidden="true">知</span>
          <strong>知识管理工作台</strong>
        </div>
        <div className="top-actions">
          <span className="local-pill">LOCAL · D8</span>
          <span className="avatar" aria-label="当前用户 MT">MT</span>
        </div>
      </header>

      <button
        aria-hidden={!isNavigationOpen}
        className={`sidebar-backdrop ${isNavigationOpen ? "is-open" : ""}`}
        onClick={() => setIsNavigationOpen(false)}
        tabIndex={isNavigationOpen ? 0 : -1}
        type="button"
      />
      <aside
        className={`sidebar ${isNavigationOpen ? "is-open" : ""}`}
        id="primary-sidebar"
      >
        <div className="library-context">
          <small>当前知识库</small>
          <strong>我的知识库</strong>
        </div>
        <nav aria-label="知识管理导航">
          {navigation.map((group) => (
            <div className="nav-group" key={group.label}>
              <p className="nav-label">{group.label}</p>
              {group.items.map((item) => {
                const isActive = pathname === item.href || (item.href === "/import" && pathname.startsWith("/sources/"));
                return (
                  <Link
                    aria-current={isActive ? "page" : undefined}
                    className={`nav-item ${isActive ? "active" : ""}`}
                    href={item.href}
                    key={item.href}
                    onClick={() => setIsNavigationOpen(false)}
                  >
                    <span className="nav-icon" aria-hidden="true">{item.icon}</span>
                    {item.label}
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>
        <p className="sidebar-note">
          D8 · 可信引用问答
          <span>限定知识边界，发布经服务端验证的回答与引用</span>
        </p>
      </aside>

      <main className="main" id="main-content" tabIndex={-1}>
        <div className="view">{children}</div>
      </main>
    </>
  );
}
