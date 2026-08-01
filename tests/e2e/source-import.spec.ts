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
      page.getByRole("heading", { name: "原件已保存，等待 D3 解析" }),
    ).toBeVisible({ timeout: 30_000 });
    await expect(page.getByLabel("本页当前上传")).toContainText(fixture.filename);
    await expect(page.getByText("本页面不会展示伪造的解析结果。", { exact: false })).toBeVisible();
    await expect(page.getByRole("heading", { name: "最近来源记录" })).toBeVisible();
    await expect(page.getByText("接口未返回最新版本或任务", { exact: false })).toBeVisible();
  });
}
