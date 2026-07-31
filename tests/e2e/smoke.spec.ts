import { expect, test } from "@playwright/test";

const WEB_URL = process.env.WEB_BASE_URL ?? "http://localhost:3000";
const API_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

test("web shell is reachable", async ({ page }) => {
  const response = await page.goto(WEB_URL);

  expect(response?.ok()).toBeTruthy();
  await expect(page.getByRole("heading", { name: "今日学习" })).toBeVisible();
  await expect(page.getByText("我的知识库", { exact: true }).first()).toBeVisible();
});

test("API live health endpoint is reachable", async ({ request }) => {
  const response = await request.get(`${API_URL}/health/live`);

  expect(response.ok()).toBeTruthy();
  expect(response.headers()["content-type"]).toContain("application/json");
  expect(await response.json()).toMatchObject({ status: "ok" });
});
