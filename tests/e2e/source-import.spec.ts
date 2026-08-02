import { expect, test } from "@playwright/test";
import path from "node:path";

const WEB_URL = process.env.WEB_BASE_URL ?? "http://localhost:3000";
const fixtureDirectory = path.resolve(process.cwd(), "tests/e2e/fixtures");

for (const fixture of [
  { filename: "sample.pdf", label: "PDF" },
  { filename: "sample.md", label: "Markdown" },
  { filename: "sample.txt", label: "纯文本" },
]) {
  test(`imports a real ${fixture.filename} source`, async ({ page }) => {
    test.setTimeout(45_000);
    await page.goto(`${WEB_URL}/import`);

    await expect(page.getByRole("heading", { name: "导入知识" })).toBeVisible();
    await page.getByLabel("选择文件或拖放到这里").setInputFiles(
      path.join(fixtureDirectory, fixture.filename),
    );
    await expect(page.getByText(fixture.label, { exact: false }).first()).toBeVisible();
    await page.getByRole("button", { name: "校验并导入" }).click();

    await expect(
      page.getByRole("heading", { name: "来源任务已完成" }),
    ).toBeVisible({ timeout: 30_000 });
    await expect(page.getByLabel("本页当前导入")).toContainText(fixture.filename);
    await expect(page.getByRole("link", { name: "查看来源详情" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "最近来源记录" })).toBeVisible();
    await expect(page.getByText("打开来源详情可查看真实版本", { exact: false })).toBeVisible();
  });
}
