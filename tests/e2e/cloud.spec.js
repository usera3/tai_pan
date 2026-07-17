const { test, expect } = require("@playwright/test");

const user = {
  id: "user-1",
  username: "lin",
  role: "user",
  must_change_password: false,
};

const admin = { ...user, id: "admin-1", username: "admin", role: "admin" };

function json(route, body, status = 200, headers = {}) {
  return route.fulfill({
    status,
    contentType: "application/json",
    headers,
    body: JSON.stringify(body),
  });
}

async function mockCloud(page, options = {}) {
  const state = {
    authenticated: options.authenticated ?? false,
    user: { ...(options.user || user) },
    loginUser: { ...(options.loginUser || options.user || user) },
    csrf: "csrf-memory-only",
    settings: { key_configured: true, custom_domain: "pan.cloudcode.xyz" },
    files: [
      { id: "tmp-1", ukey: "TMP-1", name: "temporary.txt", size: 2048, source: "tmp" },
      { id: "cloud-1", name: "permanent.pdf", size: 4096, source: "cloud" },
    ],
    links: [{ dkey: "MANUAL-1", link: "https://pan.example/manual", source: "tmp" }],
    users: [
      { ...admin, status: "active", storage_bytes: 0, created_at: "2026-07-17T00:00:00Z", updated_at: "2026-07-17T00:00:00Z", last_login_at: null },
      { ...user, status: "active", storage_bytes: 4096, created_at: "2026-07-17T00:00:00Z", updated_at: "2026-07-17T00:00:00Z", last_login_at: null },
    ],
    invitations: [],
    requests: [],
  };

  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (value) => { window.__copiedText = value; } },
    });
  });

  await page.context().route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (!url.pathname.startsWith("/api/") && !url.pathname.startsWith("/download/")) {
      return route.continue();
    }
    const record = {
      method: request.method(),
      path: `${url.pathname}${url.search}`,
      headers: request.headers(),
      body: request.postData() || "",
    };
    state.requests.push(record);

    if (url.pathname === "/api/auth/me") {
      return state.authenticated ? json(route, { user: state.user, csrf_token: state.csrf }) : json(route, { detail: "Authentication required" }, 401);
    }
    if (url.pathname === "/api/auth/login") {
      if (options.disabledLogin) return json(route, { detail: "Invalid username or password" }, 401);
      state.authenticated = true;
      state.user = { ...state.loginUser };
      return json(route, { user: state.user, csrf_token: state.csrf });
    }
    if (url.pathname === "/api/auth/register") {
      state.authenticated = true;
      state.user = { ...user, username: "new-user" };
      return json(route, { user: state.user, csrf_token: state.csrf }, 201);
    }
    if (url.pathname === "/api/auth/change-password") {
      state.user.must_change_password = false;
      return json(route, { user: state.user, csrf_token: "csrf-rotated" });
    }
    if (url.pathname === "/api/auth/logout") {
      state.authenticated = false;
      return json(route, { message: "Logged out" });
    }
    if (url.pathname === "/api/settings" && request.method() === "GET") return json(route, state.settings);
    if (url.pathname === "/api/settings" && request.method() === "PUT") {
      state.settings = { key_configured: true, custom_domain: "files.example.com" };
      return json(route, state.settings);
    }
    if (url.pathname === "/api/settings/test") return json(route, { ok: true, data: { used: 1024, total: 10240 }, message: "Connection successful" });
    if (url.pathname === "/api/settings/key") {
      state.settings.key_configured = false;
      return json(route, state.settings);
    }
    if (url.pathname === "/api/quota") return json(route, { ok: true, data: { used: 1024, total: 10240 }, message: "" });
    if (url.pathname === "/api/files" && request.method() === "GET") return json(route, { ok: true, data: state.files, message: "" });
    if (url.pathname === "/api/uploads") return json(route, { ok: true, data: { id: "uploaded", name: "upload.txt", source: record.body.includes('name=\"storage\"\r\n\r\ncloud') ? "cloud" : "tmp" }, message: "File uploaded" });
    if (url.pathname === "/api/files/cloud-1/download" && request.method() === "GET") {
      return route.fulfill({ status: 200, headers: { "Content-Disposition": 'attachment; filename="permanent.pdf"' }, body: "cloud-file" });
    }
    if (url.pathname === "/api/files/tmp-1/download" && request.method() === "POST") return json(route, { ok: true, data: { link: "/download/tmp-1", source: "tmp" }, message: "" });
    if (url.pathname === "/download/tmp-1") return route.fulfill({ status: 200, headers: { "Content-Disposition": 'attachment; filename="temporary.txt"' }, body: "tmp-file" });
    if (url.pathname.startsWith("/api/files/") && request.method() === "DELETE") {
      state.files = state.files.filter((file) => !url.pathname.includes(file.id));
      return json(route, { ok: true, data: null, message: "File deleted" });
    }
    if (url.pathname === "/api/links" && request.method() === "GET") return json(route, { ok: true, data: state.links, message: "" });
    if (url.pathname === "/api/links" && request.method() === "POST") {
      state.links.push({ dkey: "MANUAL-2", link: "https://pan.example/new", source: "tmp" });
      return json(route, { ok: true, data: state.links.at(-1), message: "Direct link created" });
    }
    if (url.pathname.startsWith("/api/links/") && request.method() === "DELETE") {
      state.links = state.links.filter((link) => !url.pathname.endsWith(link.dkey));
      return json(route, { ok: true, data: null, message: "Direct link deleted" });
    }
    if (url.pathname === "/api/admin/users" && request.method() === "GET") return json(route, state.users);
    if (url.pathname.startsWith("/api/admin/users/") && url.pathname.endsWith("/reset-password")) return json(route, { user: state.users[1], temporary_password: "temporary-password-value" });
    if (url.pathname.startsWith("/api/admin/users/") && request.method() === "PATCH") {
      const target = state.users[1];
      target.status = JSON.parse(record.body).status;
      return json(route, target);
    }
    if (url.pathname === "/api/admin/invitations" && request.method() === "GET") return json(route, state.invitations);
    if (url.pathname === "/api/admin/invitations" && request.method() === "POST") {
      const invitation = { id: "invite-1", status: "available", created_at: "2026-07-17T00:00:00Z", expires_at: null, used_by: null };
      state.invitations.push(invitation);
      return json(route, { invitation, code: "invitation-secret-value" }, 201);
    }
    if (url.pathname.startsWith("/api/admin/invitations/") && request.method() === "DELETE") {
      state.invitations = [];
      return json(route, { message: "Invitation revoked" });
    }
    return json(route, { detail: `Unhandled ${request.method()} ${url.pathname}` }, 500);
  });
  return state;
}

async function openCloud(page) {
  await page.goto("/static/cloud.html");
  await expect(page.locator("#boot-status")).toBeHidden();
}

async function signIn(page, username = "lin") {
  await page.locator("#login-username").fill(username);
  await page.locator("#login-password").fill("a-secure-login-password");
  await page.locator("#login-submit").click();
  await expect(page.locator("#app-shell")).toBeVisible();
}

function mutation(state, path, method) {
  return state.requests.find((request) => request.path.startsWith(path) && request.method === method);
}

test("login and invitation registration keep credentials in memory", async ({ page }) => {
  const state = await mockCloud(page, { authenticated: false });
  await openCloud(page);
  await expect(page.locator("#auth-shell")).toBeVisible();
  await expect(page.locator("#app-shell")).toBeHidden();

  await page.getByRole("tab", { name: "邀请注册" }).click();
  await page.locator("#register-username").fill("new-user");
  await page.locator("#register-password").fill("registration-password");
  await page.locator("#register-invitation").fill("invitation-secret");
  await page.locator("#register-submit").click();
  await expect(page.locator("#app-shell")).toBeVisible();

  const register = mutation(state, "/api/auth/register", "POST");
  expect(JSON.parse(register.body)).toEqual({ username: "new-user", password: "registration-password", invitation_code: "invitation-secret" });
  await page.locator("#logout-button").click();
  expect(mutation(state, "/api/auth/logout", "POST").headers["x-csrf-token"]).toBe(state.csrf);
  await expect(page.locator("#auth-shell")).toBeVisible();
});

test("authenticated reload recovers CSRF before a mutation", async ({ page }) => {
  const state = await mockCloud(page, { authenticated: true });
  await openCloud(page);
  await expect(page.locator("#app-shell")).toBeVisible();

  await page.reload();
  await expect(page.locator("#app-shell")).toBeVisible();
  await page.locator('[data-view="settings"]').click();
  await page.locator("#custom-domain").fill("reloaded.example.com");
  await page.locator("#settings-save").click();

  expect(mutation(state, "/api/settings", "PUT").headers["x-csrf-token"]).toBe(state.csrf);
});

test("forced password change blocks normal views until completion", async ({ page }) => {
  const forced = { ...admin, must_change_password: true };
  const state = await mockCloud(page, { authenticated: false, loginUser: forced });
  await openCloud(page);
  await page.locator("#login-username").fill("admin");
  await page.locator("#login-password").fill("temporary-password");
  await page.locator("#login-submit").click();
  await expect(page.locator("#password-shell")).toBeVisible();
  await expect(page.locator("#app-shell")).toBeHidden();

  await page.locator("#current-password").fill("temporary-password");
  await page.locator("#new-password").fill("a-new-secure-password");
  await page.locator("#confirm-password").fill("a-new-secure-password");
  await page.locator("#password-submit").click();
  await expect(page.locator("#app-shell")).toBeVisible();
  expect(mutation(state, "/api/auth/change-password", "POST").headers["x-csrf-token"]).toBe(state.csrf);
});

test("files, uploads, settings, links and background downloads use source and CSRF contracts", async ({ page }) => {
  const state = await mockCloud(page);
  await openCloud(page);
  await signIn(page);
  await expect(page.locator("#files-body .source-badge").filter({ hasText: /^钛盘$/ })).toBeVisible();
  await expect(page.locator("#files-body .source-badge").filter({ hasText: /^云端永久$/ })).toBeVisible();
  await expect(page.locator("#cloud-quota-text")).toContainText("4 KB");

  await page.locator("#upload-input").setInputFiles({ name: "upload.txt", mimeType: "text/plain", buffer: Buffer.from("upload") });
  await expect(page.locator("#upload-queue")).toContainText("upload.txt");
  await page.locator("#upload-all").click();
  await expect(page.locator("#upload-queue")).toContainText("完成");
  const defaultUpload = mutation(state, "/api/uploads", "POST");
  expect(defaultUpload.headers["x-csrf-token"]).toBe(state.csrf);
  expect(defaultUpload.body).toContain('name="model"\r\n\r\n2');
  expect(defaultUpload.body).toContain('name="storage"\r\n\r\ntmp');

  await page.locator("#upload-input").setInputFiles({ name: "permanent.txt", mimeType: "text/plain", buffer: Buffer.from("permanent") });
  await page.locator("#upload-storage").selectOption("cloud");
  await page.locator("#upload-all").click();
  const uploads = state.requests.filter((request) => request.path === "/api/uploads" && request.method === "POST");
  expect(uploads.at(-1).body).toContain('name="storage"\r\n\r\ncloud');

  const beforeCloud = page.url();
  const cloudDownload = page.waitForEvent("download");
  await page.locator('[data-file-id="cloud-1"] [data-action="download"]').click();
  await cloudDownload;
  expect(page.url()).toBe(beforeCloud);
  expect(mutation(state, "/api/files/cloud-1/download?source=cloud", "GET")).toBeTruthy();

  const beforeTmp = page.url();
  const tmpDownload = page.waitForEvent("download");
  await page.locator('[data-file-id="tmp-1"] [data-action="download"]').click();
  await tmpDownload;
  expect(page.url()).toBe(beforeTmp);
  expect(mutation(state, "/api/files/tmp-1/download?source=tmp", "POST").headers["x-csrf-token"]).toBe(state.csrf);

  await page.locator('[data-file-id="cloud-1"] [data-action="delete"]').click();
  await expect(page.locator("#confirm-dialog")).toBeVisible();
  expect(mutation(state, "/api/files/cloud-1?source=cloud", "DELETE")).toBeFalsy();
  await page.locator("#confirm-accept").click();
  expect(mutation(state, "/api/files/cloud-1?source=cloud", "DELETE").headers["x-csrf-token"]).toBe(state.csrf);

  await page.locator('[data-view="settings"]').click();
  await expect(page.locator("#key-status")).toContainText("已配置");
  await expect(page.locator("#api-key")).toHaveValue("");
  await page.locator("#api-key").fill("new-tmp-key");
  await page.locator("#custom-domain").fill("files.example.com");
  await page.locator("#settings-save").click();
  await expect(page.locator("#api-key")).toHaveValue("");
  expect(mutation(state, "/api/settings", "PUT").headers["x-csrf-token"]).toBe(state.csrf);
  await page.locator("#settings-test").click();
  expect(mutation(state, "/api/settings/test", "POST").headers["x-csrf-token"]).toBe(state.csrf);
  await page.locator("#settings-clear").click();
  await expect(page.locator("#confirm-dialog")).toBeVisible();
  await page.locator("#confirm-accept").click();
  expect(mutation(state, "/api/settings/key", "DELETE").headers["x-csrf-token"]).toBe(state.csrf);

  await page.locator('[data-view="links"]').click();
  await page.locator("#new-link").click();
  await page.locator("#link-ukey").fill("TMP-1");
  await page.locator("#link-submit").click();
  expect(mutation(state, "/api/links", "POST").headers["x-csrf-token"]).toBe(state.csrf);
  await page.locator('[data-link-key="MANUAL-2"] [data-action="copy"]').click();
  expect(await page.evaluate(() => window.__copiedText)).toBe("https://pan.example/new");
  await page.locator('[data-link-key="MANUAL-2"] [data-action="delete"]').click();
  await expect(page.locator("#confirm-dialog")).toBeVisible();
  await page.locator("#confirm-accept").click();
  expect(mutation(state, "/api/links/MANUAL-2", "DELETE").headers["x-csrf-token"]).toBe(state.csrf);
});

test("admin navigation and controls are role gated", async ({ page }) => {
  const state = await mockCloud(page, { loginUser: user });
  await openCloud(page);
  await signIn(page);
  await expect(page.locator('[data-view="admin"]')).toHaveCount(0);

  await page.locator("#logout-button").click();
  state.loginUser = { ...admin };
  await signIn(page, "admin");
  await expect(page.locator('[data-view="admin"]')).toBeVisible();
  await page.locator('[data-view="admin"]').click();
  await page.locator("#create-invitation").click();
  await page.locator("#invitation-submit").click();
  await expect(page.locator("#secret-dialog")).toBeVisible();
  await expect(page.locator("#secret-value")).toHaveValue("invitation-secret-value");
  expect(mutation(state, "/api/admin/invitations", "POST").headers["x-csrf-token"]).toBe(state.csrf);
  await page.locator("#secret-close").click();

  await page.locator('[data-user-id="user-1"] [data-action="disable"]').click();
  await page.locator("#confirm-accept").click();
  expect(JSON.parse(mutation(state, "/api/admin/users/user-1", "PATCH").body)).toEqual({ status: "disabled" });
  await page.locator('[data-user-id="user-1"] [data-action="restore"]').click();
  expect(JSON.parse(state.requests.filter((request) => request.path === "/api/admin/users/user-1" && request.method === "PATCH").at(-1).body)).toEqual({ status: "active" });
  await page.locator('[data-user-id="user-1"] [data-action="reset"]').click();
  await page.locator("#confirm-accept").click();
  await expect(page.locator("#secret-value")).toHaveValue("temporary-password-value");
});

test("disabled login has a complete error state", async ({ page }) => {
  await mockCloud(page, { authenticated: false, disabledLogin: true });
  await openCloud(page);
  await page.locator("#login-username").fill("disabled-user");
  await page.locator("#login-password").fill("disabled-password");
  await page.locator("#login-submit").click();
  await expect(page.locator("#login-message")).toContainText("用户名或密码不正确");
  await expect(page.locator("#auth-shell")).toBeVisible();
});

for (const viewport of [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`${viewport.name} layout has no document overflow or navigation overlap`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockCloud(page, { loginUser: admin });
    await openCloud(page);
    await signIn(page, "admin");
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
    const geometry = await page.evaluate(() => {
      const nav = document.querySelector("#primary-nav").getBoundingClientRect();
      const topbar = document.querySelector(".topbar").getBoundingClientRect();
      const active = document.querySelector(".view.is-active").getBoundingClientRect();
      return { nav, topbar, active, mobile: matchMedia("(max-width: 760px)").matches };
    });
    if (geometry.mobile) {
      expect(geometry.topbar.bottom).toBeLessThanOrEqual(geometry.nav.top);
      expect(geometry.active.top).toBeGreaterThanOrEqual(geometry.topbar.bottom);
    } else {
      expect(geometry.active.left).toBeGreaterThanOrEqual(geometry.nav.right);
      expect(geometry.active.top).toBeGreaterThanOrEqual(geometry.topbar.bottom);
    }
    await page.screenshot({ path: `test-results/cloud-${viewport.name}.png` });
  });
}
