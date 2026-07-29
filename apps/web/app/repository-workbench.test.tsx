import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { type Repository } from "./api-client";
import { RepositoryWorkbench } from "./repository-workbench";

const repository = {
  authorization_epoch: 1,
  authorization_status: "authorized" as const,
  authorized_at: "2026-07-29T10:00:00Z",
  canonical_path: "D:\\workbench\\sample-repository",
  id: "11111111-1111-4111-8111-111111111111",
  indexing_state: "not_queued" as const,
  manifest_hash: "manifest-existing",
  name: "sample-repository",
  revoked_at: null,
  scan_state: "manifest_ready" as const,
  stats: { eligible_bytes: 0, eligible_files: 0 },
};

const space = {
  id: "22222222-2222-4222-8222-222222222222",
  name: "Personal Development",
  slug: "personal-development" as const,
};

function apiResponse(data: unknown) {
  return new Response(JSON.stringify({ data, error: null, request_id: "request-test" }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function mockLifecycle() {
  let currentRepositories: Repository[] = [repository];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/repositories") && (!init?.method || init.method === "GET")) {
      return apiResponse({ repositories: currentRepositories, space });
    }
    if (url.endsWith("/authorization-previews")) {
      return apiResponse({
        canonical_path: "D:\\workbench\\new-repository",
        display_name: "new-repository",
        expires_at: "2026-07-29T12:30:00Z",
        policy_version: "local-v1",
        preview_token: "opaque-preview-token",
        security_notice: "Only files inside this root may be read.",
        state: "ready",
      });
    }
    if (url.endsWith("/authorizations")) {
      const authorized: Repository = {
        ...repository,
        id: "33333333-3333-4333-8333-333333333333",
        canonical_path: "D:\\workbench\\new-repository",
        name: "new-repository",
        scan_state: "not_scanned",
        manifest_hash: null,
        stats: { eligible_bytes: 0, eligible_files: 0 },
      };
      currentRepositories = [...currentRepositories, authorized];
      return apiResponse({ outcome: "authorized", repository: authorized });
    }
    if (url.endsWith("/scans")) {
      currentRepositories = currentRepositories.map((item) => item.id === repository.id ? {
        ...item,
        scan_state: "manifest_ready" as const,
        stats: { eligible_bytes: 2048, eligible_files: 2 },
        manifest_hash: "manifest-1",
      } : item);
      return apiResponse({
        index_job_id: "55555555-5555-4555-8555-555555555555",
        indexing_state: "pending",
        manifest_hash: "manifest-1",
        outcome: "created",
        source_version_id: "44444444-4444-4444-8444-444444444444",
        stats: { directories_visited: 2, eligible_bytes: 2048, eligible_files: 2, files_seen: 3, skipped: { ignored: 1 } },
      });
    }
    if (url.endsWith("/revocation")) {
      const revoked = { ...repository, authorization_epoch: 2, authorization_status: "revoked" as const, revoked_at: "2026-07-29T11:00:00Z" };
      currentRepositories = currentRepositories.map((item) => item.id === repository.id ? revoked : item);
      return apiResponse({ outcome: "revoked", repository: revoked });
    }
    return new Response("not found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("RepositoryWorkbench", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("completes preview, explicit authorization, context selection, scan, and revoke with real API data", async () => {
    const user = userEvent.setup();
    const fetchMock = mockLifecycle();
    render(<div className="workspace-grid"><RepositoryWorkbench apiBaseUrl="http://api.test/api/v1" /></div>);

    const repositoryButton = await screen.findByRole("button", { name: /sample-repository/ });
    expect(repositoryButton.getAttribute("aria-pressed")).toBe("false");
    await user.click(repositoryButton);
    expect(repositoryButton.getAttribute("aria-pressed")).toBe("true");

    await user.type(screen.getByLabelText("绝对路径"), "D:\\workbench\\new-repository");
    await user.click(screen.getByRole("button", { name: "预览" }));
    const authorizationDialog = await screen.findByRole("dialog", { name: "授权 new-repository" });
    expect(within(authorizationDialog).getByText("D:\\workbench\\new-repository")).toBeTruthy();
    await user.click(within(authorizationDialog).getByRole("button", { name: "明确授权" }));
    expect(await screen.findByText(/仓库 new-repository 已授权/)).toBeTruthy();

    const newRepositoryButton = screen.getByRole("button", { name: /new-repository/ });
    expect(newRepositoryButton.getAttribute("aria-pressed")).toBe("false");
    expect(newRepositoryButton).toHaveProperty("disabled", true);

    const repositoryCard = repositoryButton.closest("article");
    if (!repositoryCard) throw new Error("repository card missing");
    await user.click(within(repositoryCard).getByRole("button", { name: "扫描" }));
    const scanDialog = await screen.findByRole("dialog", { name: "扫描 sample-repository" });
    await user.click(within(scanDialog).getByRole("button", { name: "开始扫描" }));
    expect(await screen.findByText("最近扫描：2 个可用文件，2.0 KB")).toBeTruthy();

    await user.click(within(repositoryCard).getByRole("button", { name: "撤销" }));
    const revokeDialog = await screen.findByRole("dialog", { name: "撤销 sample-repository" });
    expect(within(revokeDialog).getByText(/撤销不是数据清除.*历史清单与审计记录仍会保留/)).toBeTruthy();
    await user.click(within(revokeDialog).getByRole("button", { name: "撤销授权" }));
    await waitFor(() => expect(repositoryButton.getAttribute("aria-pressed")).toBe("false"));
    expect(repositoryButton).toHaveProperty("disabled", true);

    expect(fetchMock).toHaveBeenCalledWith("http://api.test/api/v1/repositories/local/authorizations", expect.objectContaining({
      body: JSON.stringify({ confirmation: true, preview_token: "opaque-preview-token" }),
    }));
    expect(fetchMock).toHaveBeenCalledWith("http://api.test/api/v1/repositories/11111111-1111-4111-8111-111111111111/scans", expect.objectContaining({
      body: JSON.stringify({ expected_authorization_epoch: 1 }),
    }));
  });

  it("traps dialog focus, closes on Escape, and restores focus", async () => {
    const user = userEvent.setup();
    mockLifecycle();
    render(<div className="workspace-grid"><RepositoryWorkbench apiBaseUrl="http://api.test/api/v1" /></div>);
    const input = screen.getByLabelText("绝对路径");
    await user.type(input, "D:\\workbench\\new-repository");
    const previewButton = screen.getByRole("button", { name: "预览" });
    await user.click(previewButton);

    const dialog = await screen.findByRole("dialog");
    const closeButton = within(dialog).getByRole("button", { name: "关闭确认对话框" });
    expect(document.activeElement).toBe(closeButton);
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(within(dialog).getByRole("button", { name: "明确授权" }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(previewButton);
  });

  it("supports arrow-key tab navigation", async () => {
    mockLifecycle();
    render(<div className="workspace-grid"><RepositoryWorkbench apiBaseUrl="http://api.test/api/v1" /></div>);
    const boundaryTab = screen.getByRole("tab", { name: "授权边界" });
    boundaryTab.focus();
    fireEvent.keyDown(boundaryTab, { key: "ArrowRight" });
    const activityTab = screen.getByRole("tab", { name: "扫描结果" });
    expect(document.activeElement).toBe(activityTab);
    expect(activityTab.getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tabpanel", { name: "扫描结果" })).toBeTruthy();
  });
});
