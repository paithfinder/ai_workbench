import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "./app-shell";

const { usePathname } = vi.hoisted(() => ({
  usePathname: vi.fn(() => "/knowledge"),
}));

vi.mock("next/navigation", () => ({ usePathname }));

describe("AppShell", () => {
  beforeEach(() => {
    usePathname.mockReturnValue("/knowledge");
  });

  it("renders all routes and identifies the active route", () => {
    render(<AppShell><h1>页面内容</h1></AppShell>);

    expect(screen.getByRole("link", { name: "跳到主要内容" })).toHaveAttribute(
      "href",
      "#main-content",
    );
    expect(screen.getAllByRole("link")).toHaveLength(10);
    expect(screen.getByRole("link", { name: "我的知识树" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "Proposal 审核" })).toHaveAttribute(
      "href",
      "/proposals",
    );
  });

  it("marks proposal review navigation active", () => {
    usePathname.mockReturnValue("/proposals");
    render(<AppShell><h1>Proposal 审核页</h1></AppShell>);

    expect(screen.getByRole("link", { name: "Proposal 审核" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("keeps import navigation active on a source detail route", () => {
    usePathname.mockReturnValue("/sources/6ee8885f-e0ca-4e04-bf02-47d63b6c5881");
    render(<AppShell><h1>来源详情</h1></AppShell>);

    expect(screen.getByRole("link", { name: "导入知识" })).toHaveAttribute("aria-current", "page");
  });

  it("marks extraction review active and exposes D7 shell context", () => {
    usePathname.mockReturnValue("/extraction");
    render(<AppShell><h1>提炼审查页</h1></AppShell>);

    expect(screen.getByRole("link", { name: "提炼审查" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByText("LOCAL · D8")).toBeInTheDocument();
    expect(screen.getByText("D8 · 可信引用问答")).toBeInTheDocument();
  });

  it("opens and closes the responsive navigation", async () => {
    render(<AppShell><h1>页面内容</h1></AppShell>);
    const toggle = screen.getByRole("button", { name: "打开导航" });

    await userEvent.click(toggle);
    expect(screen.getByRole("button", { name: "关闭导航" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "打开导航" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});
