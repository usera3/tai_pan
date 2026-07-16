const { test, expect } = require("@playwright/test");

async function mockApi(page, calls = []) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    calls.push(`${request.method()} ${path}`);
    let data = {};

    if (path === "/api/settings" && request.method() === "GET") {
      data = { key_configured: true, custom_domain: "pan.cloudcode.xyz" };
    } else if (path === "/api/settings" && request.method() === "PUT") {
      data = { key_configured: true, custom_domain: "pan.cloudcode.xyz" };
    } else if (path === "/api/settings/key") {
      data = { key_configured: false, custom_domain: "pan.cloudcode.xyz" };
    } else if (path === "/api/quota") {
      data = { quota: 10737418240 };
    } else if (path === "/api/files") {
      data = [{ ukey: "FILE-UKEY-1", name: "report.pdf", size: 2048 }];
    } else if (path === "/api/files/FILE-UKEY-1/download" && request.method() === "POST") {
      data = { dkey: "DOWNLOAD-DKEY", link: "/d/DOWNLOAD-DKEY" };
    } else if (path === "/api/files/FILE-UKEY-1" && request.method() === "DELETE") {
      data = { deleted: true };
    } else if (path === "/api/links" && request.method() === "GET") {
      data = [{ dkey: "DIRECT-DKEY-1", name: "report.pdf", link: "/d/DIRECT-DKEY-1", size: 2048, etime: "永久" }];
    } else if (path === "/api/links" && request.method() === "POST") {
      data = { dkey: "DIRECT-DKEY-2", link: "/d/DIRECT-DKEY-2" };
    } else if (path.startsWith("/api/links/") && request.method() === "DELETE") {
      data = { deleted: true };
    } else if (path === "/api/uploads") {
      data = "UPLOADED-UKEY";
    } else if (path === "/api/settings/test") {
      data = { quota: 10737418240 };
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, data, message: "" }),
    });
  });
}

test.beforeEach(async ({ page }) => {
  page.apiCalls = [];
  await mockApi(page, page.apiCalls);
});

test("navigates dashboard and settings without exposing the key", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "概览" })).toBeVisible();
  await expect(page.locator("#connection-label")).toHaveText("连接正常");

  await page.getByRole("button", { name: "设置" }).click();
  await expect(page.getByRole("heading", { name: "连接设置" })).toBeVisible();
  await expect(page.locator("#key-badge")).toHaveText("已配置");
  await expect(page.locator("#api-key")).toHaveValue("");
});

test("uploads a queued file and retains its returned UKEY state", async ({ page }) => {
  await page.goto("/#files");
  await page.locator("#file-input").setInputFiles({
    name: "notes.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("local upload"),
  });

  await expect(page.locator(".queue-item")).toContainText("notes.txt");
  await page.locator("#upload-all").click();
  await expect(page.locator(".queue-item")).toContainText("上传完成");
  await expect(page.locator("#files-body")).toContainText("report.pdf");
});

test("creates and deletes a direct link through explicit dialogs", async ({ page }) => {
  await page.goto("/#links");
  await expect(page.locator("#links-body")).toContainText("report.pdf");

  await page.locator("#new-link").click();
  await expect(page.locator("#link-dialog")).toBeVisible();
  await page.locator("#link-ukey").fill("FILE-UKEY-1");
  await page.locator("#link-valid-time").fill("60");
  await page.locator("#link-download-limit").fill("3");
  await page.locator("#create-link-submit").click();
  await expect(page.locator("#link-dialog")).not.toBeVisible();

  await page.getByRole("button", { name: "删除直链" }).first().click();
  await expect(page.locator("#delete-dialog")).toBeVisible();
  await page.locator("#delete-file").check();
  await page.locator("#delete-link-submit").click();
  await expect(page.locator("#delete-dialog")).not.toBeVisible();
});

test("downloads and deletes a file through explicit file actions", async ({ page }) => {
  await page.addInitScript(() => {
    window.__downloadTarget = "";
    window.open = () => ({
      location: { replace: (value) => { window.__downloadTarget = value; } },
      close: () => {},
    });
  });
  await page.goto("/#files");

  await page.getByRole("button", { name: "下载文件" }).click();
  await expect.poll(() => page.evaluate(() => window.__downloadTarget)).toBe("https://pan.cloudcode.xyz/d/DOWNLOAD-DKEY");

  await page.getByRole("button", { name: "删除文件" }).click();
  await expect(page.locator("#file-delete-dialog")).toBeVisible();
  await expect(page.locator("#file-delete-dialog")).toContainText("report.pdf");
  await expect(page.locator("#file-delete-dialog")).toContainText("全部相关直链");
  await page.locator("#delete-file-submit").click();
  await expect(page.locator("#file-delete-dialog")).not.toBeVisible();
  expect(page.apiCalls).toContain("POST /api/files/FILE-UKEY-1/download");
  expect(page.apiCalls).toContain("DELETE /api/files/FILE-UKEY-1");
});

test("mobile file actions remain visible before horizontal scrolling", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#files");

  const box = await page.getByRole("button", { name: "下载文件" }).boundingBox();

  await expect(page.getByRole("columnheader", { name: "大小" })).toBeHidden();
  await expect(page.getByRole("columnheader", { name: "UKEY" })).toBeHidden();
  expect(box).not.toBeNull();
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(390);
});

for (const viewport of [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`${viewport.name} layout has no document-level horizontal overflow`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto("/");
    for (const view of ["dashboard", "files", "links", "settings"]) {
      await page.locator(`.nav-item[data-view="${view}"]`).click();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(1);
      await page.screenshot({ path: `/tmp/tmp-link-manager-${viewport.name}-${view}.png`, fullPage: true });
    }
  });
}
