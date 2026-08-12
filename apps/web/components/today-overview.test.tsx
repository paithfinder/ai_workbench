import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TodayOverview } from "./today-overview";

const readyPayload = {
  space: {
    id: "102ee035-406f-41a6-b46b-d6c1d4e80d11",
    slug: "my-knowledge-base",
    name: "我的知识库",
  },
  capabilities: {
    source_import: true,
    extraction_review: false,
    knowledge_tree: false,
    retrieval_debug: false,
    trusted_qa: false,
    spaced_review: false,
    evidence_agent: false,
  },
  statistics: {
    sources: 0,
    queued_jobs: 0,
    activity_events: 0,
  },
  foundation_status: "ready",
} as const;

function renderOverview() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  return render(
    <QueryClientProvider client={client}>
      <TodayOverview />
    </QueryClientProvider>,
  );
}

describe("TodayOverview", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows loading before rendering the space and real zero values", async () => {
    let resolveFetch: ((value: Response) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveFetch = resolve;
          }),
      ),
    );

    renderOverview();
    expect(screen.getByLabelText("正在加载首页数据")).toBeInTheDocument();

    resolveFetch?.(new Response(JSON.stringify(readyPayload)));

    expect(await screen.findByText("我的知识库已连接")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "今日学习" })).toHaveAttribute("id", "today-title");
    expect(screen.getByText("my-knowledge-base")).toBeInTheDocument();
    expect(screen.getAllByText("0")).toHaveLength(3);
    expect(screen.getByText("1")).toBeInTheDocument();
  });

  it("counts only capabilities enabled by the backend", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            ...readyPayload,
            capabilities: {
              ...readyPayload.capabilities,
              source_import: true,
              knowledge_tree: true,
            },
            statistics: {
              sources: 7,
              queued_jobs: 2,
              activity_events: 11,
            },
          }),
        ),
      ),
    );

    renderOverview();

    expect(await screen.findByText("我的知识库已连接")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getAllByText("2")).toHaveLength(2);
    expect(screen.getByText("11")).toBeInTheDocument();
  });

  it("shows an honest API error and retries", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("unavailable", { status: 503 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(readyPayload)));
    vi.stubGlobal("fetch", fetchMock);

    renderOverview();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "无法连接知识工作台 API",
    );

    await userEvent.click(screen.getByRole("button", { name: "重试连接" }));

    expect(await screen.findByText("我的知识库已连接")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("rejects payloads that do not match the frozen bootstrap contract", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            ...readyPayload,
            statistics: { ...readyPayload.statistics, sources: -1 },
          }),
        ),
      ),
    );

    renderOverview();

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("-1")).not.toBeInTheDocument();
  });
});
