import type { components } from "@ai-workbench/api-contract";
import { expect, test, type Page } from "@playwright/test";

type Repository = components["schemas"]["RepositorySummaryResponse"];

const repository: Repository = {
  authorization_epoch: 1,
  authorization_status: "authorized" as const,
  authorized_at: "2026-07-29T10:00:00Z",
  canonical_path: "D:\\workbench\\sample-repository",
  id: "11111111-1111-4111-8111-111111111111",
  indexing_state: "not_queued" as const,
  manifest_hash: null as string | null,
  name: "sample-repository",
  revoked_at: null as string | null,
  scan_state: "not_scanned" as "not_scanned" | "manifest_ready",
  stats: { eligible_bytes: 0, eligible_files: 0 },
};

function envelope(data: unknown) {
  return { data, error: null, request_id: "e2e-request" };
}

async function mockApi(page: Page) {
  let repositories: Repository[] = [repository];
  await page.route("http://127.0.0.1:8000/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/health")) {
      await route.fulfill({ json: envelope({ app: "ai-workbench", database: "connected", model_configured: true, status: "ok", version: "0.1.0" }) });
      return;
    }
    if (url.pathname.endsWith("/repositories") && route.request().method() === "GET") {
      await route.fulfill({ json: envelope({ repositories, space: { id: "22222222-2222-4222-8222-222222222222", name: "Personal Development", slug: "personal-development" } }) });
      return;
    }
    if (url.pathname.endsWith("/authorization-previews")) {
      await route.fulfill({ json: envelope({ canonical_path: "D:\\workbench\\new-repository", display_name: "new-repository", expires_at: "2026-07-29T12:30:00Z", policy_version: "local-v1", preview_token: "preview-e2e", security_notice: "Read access is confined to this root.", state: "ready" }) });
      return;
    }
    if (url.pathname.endsWith("/authorizations")) {
      const created: Repository = {
        ...repository,
        id: "33333333-3333-4333-8333-333333333333",
        name: "new-repository",
        canonical_path: "D:\\workbench\\new-repository",
        scan_state: "not_scanned",
        manifest_hash: null,
        stats: { eligible_bytes: 0, eligible_files: 0 },
      };
      repositories = [...repositories, created];
      await route.fulfill({ json: envelope({ outcome: "authorized", repository: created }) });
      return;
    }
    if (url.pathname.endsWith("/scans")) {
      repository.scan_state = "manifest_ready";
      repository.manifest_hash = "manifest-e2e";
      repository.stats = { eligible_bytes: 1024, eligible_files: 1 };
      await route.fulfill({ json: envelope({ index_job_id: "55555555-5555-4555-8555-555555555555", indexing_state: "pending", manifest_hash: "manifest-e2e", outcome: "created", source_version_id: "44444444-4444-4444-8444-444444444444", stats: { directories_visited: 1, eligible_bytes: 1024, eligible_files: 1, files_seen: 1, skipped: {} } }) });
      return;
    }
    if (url.pathname.endsWith("/revocation")) {
      const revoked = { ...repository, authorization_epoch: 2, authorization_status: "revoked" as const, revoked_at: "2026-07-29T11:00:00Z" };
      repositories = repositories.map((item) => item.id === repository.id ? revoked : item);
      await route.fulfill({ json: envelope({ outcome: "revoked", repository: revoked }) });
      return;
    }
    await route.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
}

test("repository workbench is keyboard accessible and does not overflow target viewports", async ({ page }) => {
  await mockApi(page);
  for (const width of [375, 768, 901, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Ask with evidence." })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
    const inspector = page.getByRole("complementary", { name: "仓库检查器" });
    await expect(inspector).toBeVisible();
    const inspectorBox = await inspector.boundingBox();
    expect(inspectorBox).not.toBeNull();
    expect(inspectorBox!.x).toBeGreaterThanOrEqual(0);
    expect(inspectorBox!.x + inspectorBox!.width).toBeLessThanOrEqual(width);
  }

  const firstTab = page.getByRole("tab", { name: "授权边界" });
  await firstTab.focus();
  await firstTab.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "扫描结果" })).toBeFocused();
  await expect(page.getByRole("tabpanel", { name: "扫描结果" })).toContainText("不会显示占位进度或虚构计数");
});

test("runs preview, confirm, scan, context selection, and revoke against mocked API", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  await page.getByLabel("绝对路径").fill("D:\\workbench\\new-repository");
  await page.getByRole("button", { name: "预览" }).click();
  await expect(page.getByRole("dialog", { name: "授权 new-repository" })).toContainText("D:\\workbench\\new-repository");
  await page.getByRole("button", { name: "明确授权" }).click();
  await expect(page.getByRole("button", { name: /new-repository/ })).toBeDisabled();

  const sampleCard = page.getByRole("article").filter({ hasText: "sample-repository" }).first();
  await sampleCard.getByRole("button", { name: "扫描" }).click();
  await page.getByRole("button", { name: "开始扫描" }).click();
  await expect(sampleCard).toContainText("最近扫描：1 个可用文件，1.0 KB");

  const contextToggle = sampleCard.getByRole("button", { name: /sample-repository/ });
  await expect(contextToggle).toBeEnabled();
  await contextToggle.click();
  await expect(contextToggle).toHaveAttribute("aria-pressed", "true");

  await sampleCard.getByRole("button", { name: "撤销" }).click();
  await expect(page.getByRole("dialog", { name: "撤销 sample-repository" })).toContainText("撤销不是数据清除");
  await page.getByRole("button", { name: "撤销授权" }).click();
  await expect(contextToggle).toHaveAttribute("aria-pressed", "false");
  await expect(contextToggle).toBeDisabled();
  await expect(page.getByText("尚未选择上下文。请在左侧从已授权仓库中明确选择。")).toBeVisible();
});
