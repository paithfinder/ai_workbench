import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ScheduledEmptyState } from "./page-states";

describe("ScheduledEmptyState", () => {
  it("describes future delivery without presenting mock content", () => {
    render(
      <ScheduledEmptyState
        capability="真实资料导入"
        day="D2–D3"
        description="保存原始资料。"
        detail="接口完成后实现。"
        eyebrow="INTAKE"
        title="导入知识"
      />,
    );

    expect(screen.getByText("按日程待实现")).toBeInTheDocument();
    expect(screen.getByText("D2–D3")).toBeInTheDocument();
    expect(screen.getByText(/不提供演示数据/)).toBeInTheDocument();
  });
});
