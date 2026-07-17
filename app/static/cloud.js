"use strict";

const CLOUD_QUOTA_BYTES = 1024 * 1024 * 1024;
const state = {
  user: null,
  csrf: null,
  files: [],
  links: [],
  settings: null,
  users: [],
  invitations: [],
  uploads: [],
  uploading: false,
  confirmAction: null,
};

const viewTitles = { files: "文件", links: "直链", settings: "设置", admin: "管理" };
const byId = (id) => document.getElementById(id);

function initIcons() {
  if (window.lucide) window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
}

function setMessage(element, message = "", tone = "") {
  element.textContent = message;
  element.className = `form-message${tone ? ` ${tone}` : ""}`;
}

function setBusy(button, busy, busyLabel = "处理中") {
  if (!button) return;
  if (busy) {
    button.dataset.label = button.innerHTML;
    button.disabled = true;
    button.textContent = busyLabel;
  } else {
    if (button.dataset.label) button.innerHTML = button.dataset.label;
    delete button.dataset.label;
    button.disabled = false;
    initIcons();
  }
}

function toast(message) {
  const item = document.createElement("div");
  item.className = "toast";
  item.textContent = message;
  byId("toast-region").append(item);
  window.setTimeout(() => item.remove(), 3200);
}

function errorMessage(status, detail) {
  if (status === 401) return "用户名或密码不正确";
  if (status === 403) return "没有权限执行此操作";
  if (status === 409) return "当前状态不允许此操作";
  if (status === 413) return "文件超过大小或空间限制";
  if (status === 422) return "提交内容无效";
  if (status === 429) return "操作过于频繁，请稍后再试";
  if (typeof detail === "string" && detail.length <= 120) return detail;
  return "请求失败，请稍后重试";
}

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  let body = options.body;
  if (body && !(body instanceof FormData) && typeof body !== "string") {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  if (!["GET", "HEAD"].includes(method) && state.csrf) headers.set("X-CSRF-Token", state.csrf);
  const response = await fetch(path, { method, headers, body, credentials: "same-origin" });
  let payload = {};
  try { payload = await response.json(); } catch (_error) { payload = {}; }
  if (!response.ok) {
    if (response.status === 401 && state.user) showAuth("会话已失效，请重新登录");
    const error = new Error(errorMessage(response.status, payload.detail || payload.message));
    error.status = response.status;
    throw error;
  }
  return payload;
}

function showAuth(message = "") {
  state.user = null;
  state.csrf = null;
  state.files = [];
  state.links = [];
  state.settings = null;
  removeAdminNav();
  byId("boot-status").hidden = true;
  byId("password-shell").hidden = true;
  byId("app-shell").hidden = true;
  byId("auth-shell").hidden = false;
  byId("login-form").reset();
  byId("register-form").reset();
  setMessage(byId("login-message"), message, message ? "error" : "");
  setMessage(byId("register-message"));
  activateAuthTab("login");
  byId("login-username").focus();
}

function activateAuthTab(name) {
  const login = name === "login";
  byId("login-tab").classList.toggle("is-active", login);
  byId("register-tab").classList.toggle("is-active", !login);
  byId("login-tab").setAttribute("aria-selected", String(login));
  byId("register-tab").setAttribute("aria-selected", String(!login));
  byId("login-form").hidden = !login;
  byId("register-form").hidden = login;
}

function ensureAdminNav() {
  if (byId("admin-nav")) return;
  const button = document.createElement("button");
  button.id = "admin-nav";
  button.className = "nav-item";
  button.type = "button";
  button.dataset.view = "admin";
  button.innerHTML = '<i data-lucide="shield"></i><span>管理</span>';
  byId("primary-nav").append(button);
  button.addEventListener("click", () => activateView("admin"));
}

function removeAdminNav() {
  byId("admin-nav")?.remove();
  byId("view-admin").hidden = true;
  document.documentElement.style.setProperty("--nav-count", "3");
}

async function showAuthenticated(user, csrfToken) {
  state.user = user;
  state.csrf = csrfToken;
  byId("boot-status").hidden = true;
  byId("auth-shell").hidden = true;
  if (user.must_change_password) {
    byId("app-shell").hidden = true;
    byId("password-shell").hidden = false;
    byId("password-user").textContent = user.username;
    byId("password-form").reset();
    setMessage(byId("password-message"));
    byId("current-password").focus();
    return;
  }

  byId("password-shell").hidden = true;
  byId("app-shell").hidden = false;
  byId("sidebar-username").textContent = user.username;
  byId("topbar-username").textContent = user.username;
  byId("sidebar-role").textContent = user.role === "admin" ? "管理员" : "用户";
  if (user.role === "admin") {
    ensureAdminNav();
    byId("view-admin").hidden = false;
    document.documentElement.style.setProperty("--nav-count", "4");
  } else {
    removeAdminNav();
  }
  activateView("files");
  initIcons();
  await loadFiles();
}

async function bootstrap() {
  initIcons();
  try {
    const response = await fetch("/api/auth/me", { credentials: "same-origin" });
    if (!response.ok) {
      showAuth();
      return;
    }
    const payload = await response.json();
    if (!payload.user || typeof payload.csrf_token !== "string" || !payload.csrf_token) {
      showAuth("请重新登录以继续");
      return;
    }
    await showAuthenticated(payload.user, payload.csrf_token);
  } catch (_error) {
    showAuth("无法连接服务，请稍后重试");
  }
}

async function submitLogin(event) {
  event.preventDefault();
  const button = byId("login-submit");
  setBusy(button, true, "登录中");
  setMessage(byId("login-message"));
  try {
    const payload = await api("/api/auth/login", {
      method: "POST",
      body: { username: byId("login-username").value, password: byId("login-password").value },
    });
    byId("login-form").reset();
    await showAuthenticated(payload.user, payload.csrf_token);
  } catch (error) {
    setMessage(byId("login-message"), error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function submitRegistration(event) {
  event.preventDefault();
  const button = byId("register-submit");
  setBusy(button, true, "创建中");
  setMessage(byId("register-message"));
  try {
    const payload = await api("/api/auth/register", {
      method: "POST",
      body: {
        username: byId("register-username").value,
        password: byId("register-password").value,
        invitation_code: byId("register-invitation").value,
      },
    });
    byId("register-form").reset();
    await showAuthenticated(payload.user, payload.csrf_token);
  } catch (error) {
    setMessage(byId("register-message"), error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function changePassword(event) {
  event.preventDefault();
  const current = byId("current-password").value;
  const next = byId("new-password").value;
  if (next !== byId("confirm-password").value) {
    setMessage(byId("password-message"), "两次输入的新密码不一致", "error");
    return;
  }
  const button = byId("password-submit");
  setBusy(button, true, "保存中");
  try {
    const payload = await api("/api/auth/change-password", { method: "POST", body: { current_password: current, new_password: next } });
    byId("password-form").reset();
    await showAuthenticated(payload.user, payload.csrf_token);
  } catch (error) {
    setMessage(byId("password-message"), error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function logout() {
  const button = byId("logout-button");
  setBusy(button, true, "");
  try { await api("/api/auth/logout", { method: "POST" }); } catch (error) { toast(error.message); }
  showAuth();
  setBusy(button, false);
}

function activateView(name) {
  if (name === "admin" && state.user?.role !== "admin") return;
  document.querySelectorAll("[data-view-panel]").forEach((panel) => panel.classList.toggle("is-active", panel.dataset.viewPanel === name));
  document.querySelectorAll(".nav-item[data-view]").forEach((button) => button.classList.toggle("is-active", button.dataset.view === name));
  byId("view-title").textContent = viewTitles[name];
  if (name === "files" && state.files.length === 0) loadFiles();
  if (name === "links") loadLinks();
  if (name === "settings") loadSettings();
  if (name === "admin") loadAdmin();
}

function normalizeList(payload) {
  const value = payload?.data ?? payload;
  if (Array.isArray(value)) return value;
  if (Array.isArray(value?.data)) return value.data;
  if (Array.isArray(value?.list)) return value.list;
  return [];
}

function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function fileIdentity(file) {
  return String(file.id || file.ukey || file.ukey_id || "");
}

function fileName(file) {
  return String(file.name || file.filename || file.original_name || fileIdentity(file));
}

function iconButton(icon, label, action, className = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `small-icon-button${className ? ` ${className}` : ""}`;
  button.title = label;
  button.setAttribute("aria-label", label);
  button.dataset.action = action;
  button.innerHTML = `<i data-lucide="${icon}"></i>`;
  return button;
}

async function loadFiles() {
  byId("files-state").hidden = false;
  byId("files-state").className = "empty-state";
  byId("files-state").textContent = "正在载入文件";
  byId("files-table-wrap").hidden = true;
  try {
    const payload = await api("/api/files?source=all");
    state.files = normalizeList(payload);
    renderFiles();
  } catch (error) {
    byId("files-state").textContent = error.message;
    byId("files-state").classList.add("error");
  }
}

function renderFiles() {
  const body = byId("files-body");
  body.replaceChildren();
  const cloudUsed = state.files.filter((file) => file.source === "cloud").reduce((total, file) => total + (Number(file.size) || 0), 0);
  const percent = Math.min(100, (cloudUsed / CLOUD_QUOTA_BYTES) * 100);
  byId("cloud-quota-text").textContent = `${formatBytes(cloudUsed)} / 1 GB`;
  byId("cloud-quota-bar").style.width = `${percent}%`;
  byId("cloud-quota-bar").parentElement.setAttribute("aria-valuenow", String(Math.round(percent)));
  if (state.files.length === 0) {
    byId("files-state").hidden = false;
    byId("files-state").textContent = "暂无文件";
    byId("files-table-wrap").hidden = true;
    return;
  }
  state.files.forEach((file) => {
    const id = fileIdentity(file);
    const source = file.source === "cloud" ? "cloud" : "tmp";
    const row = document.createElement("tr");
    row.dataset.fileId = id;
    const name = document.createElement("td");
    name.className = "file-name";
    name.textContent = fileName(file);
    name.title = fileName(file);
    const sourceCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `source-badge${source === "cloud" ? " cloud" : ""}`;
    badge.textContent = source === "cloud" ? "云端永久" : "钛盘";
    sourceCell.append(badge);
    const size = document.createElement("td");
    size.textContent = formatBytes(file.size || file.size_bytes || 0);
    const actions = document.createElement("td");
    actions.className = "row-actions";
    actions.append(iconButton("download", "下载", "download"), iconButton("trash-2", "删除", "delete", "danger"));
    actions.querySelector('[data-action="download"]').addEventListener("click", () => downloadFile(file));
    actions.querySelector('[data-action="delete"]').addEventListener("click", () => confirmDeleteFile(file));
    row.append(name, sourceCell, size, actions);
    body.append(row);
  });
  byId("files-state").hidden = true;
  byId("files-table-wrap").hidden = false;
  initIcons();
}

function backgroundDownload(url) {
  const frame = document.createElement("iframe");
  frame.title = "后台下载";
  frame.src = url;
  byId("download-bin").append(frame);
  window.setTimeout(() => frame.remove(), 5000);
}

async function downloadFile(file) {
  const id = encodeURIComponent(fileIdentity(file));
  if (file.source === "cloud") {
    backgroundDownload(`/api/files/${id}/download?source=cloud`);
    return;
  }
  try {
    const payload = await api(`/api/files/${id}/download?source=tmp`, { method: "POST" });
    const link = payload?.data?.link || payload?.data?.url;
    if (!link) throw new Error("下载地址不可用");
    const frame = document.createElement("iframe");
    frame.title = "后台下载";
    frame.src = link;
    byId("download-bin").append(frame);
    window.setTimeout(() => frame.remove(), 5000);
  } catch (error) { toast(error.message); }
}

function openConfirm(title, copy, action) {
  state.confirmAction = action;
  byId("confirm-title").textContent = title;
  byId("confirm-copy").textContent = copy;
  byId("confirm-dialog").showModal();
}

function confirmDeleteFile(file) {
  openConfirm("删除文件", `确认删除“${fileName(file)}”？`, async () => {
    try {
      await api(`/api/files/${encodeURIComponent(fileIdentity(file))}?source=${file.source === "cloud" ? "cloud" : "tmp"}`, { method: "DELETE" });
      toast("文件已删除");
      await loadFiles();
    } catch (error) { toast(error.message); }
  });
}

function queueFiles(files) {
  for (const file of files) {
    state.uploads.push({ id: `${Date.now()}-${Math.random()}`, file, status: "queued", progress: 0, message: "等待上传" });
  }
  byId("upload-input").value = "";
  renderUploads();
}

function renderUploads() {
  const container = byId("upload-queue");
  container.replaceChildren();
  if (state.uploads.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "暂无待上传文件";
    container.append(empty);
  }
  state.uploads.forEach((item) => {
    const row = document.createElement("div");
    row.className = "queue-item";
    const name = document.createElement("div");
    name.className = "queue-name";
    const strong = document.createElement("strong");
    strong.textContent = item.file.name;
    const small = document.createElement("small");
    small.textContent = formatBytes(item.file.size);
    name.append(strong, small);
    const status = document.createElement("div");
    status.className = "queue-status";
    const progress = document.createElement("div");
    progress.className = "queue-progress";
    const bar = document.createElement("span");
    bar.style.width = `${item.progress}%`;
    progress.append(bar);
    const statusText = document.createElement("div");
    statusText.textContent = item.message;
    status.append(progress, statusText);
    const actions = document.createElement("div");
    actions.className = "queue-actions";
    if (item.status === "failed") {
      const retry = iconButton("rotate-ccw", "重试上传", "retry");
      retry.addEventListener("click", () => { item.status = "queued"; item.progress = 0; item.message = "等待重试"; uploadAll(); });
      actions.append(retry);
    }
    if (!["uploading"].includes(item.status)) {
      const remove = iconButton("x", "移除", "remove");
      remove.addEventListener("click", () => { state.uploads = state.uploads.filter((entry) => entry.id !== item.id); renderUploads(); });
      actions.append(remove);
    }
    row.append(name, status, actions);
    container.append(row);
  });
  byId("upload-all").disabled = state.uploading || !state.uploads.some((item) => ["queued", "failed"].includes(item.status));
  initIcons();
}

function uploadItem(item) {
  return new Promise((resolve) => {
    const form = new FormData();
    form.append("storage", byId("upload-storage").value);
    form.append("model", byId("upload-model").value);
    form.append("file", item.file, item.file.name);
    const xhr = new XMLHttpRequest();
    item.status = "uploading";
    item.message = "上传中";
    renderUploads();
    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      item.progress = Math.round((event.loaded / event.total) * 100);
      renderUploads();
    });
    xhr.addEventListener("load", () => {
      let payload = {};
      try { payload = JSON.parse(xhr.responseText); } catch (_error) { payload = {}; }
      if (xhr.status >= 200 && xhr.status < 300 && payload.ok !== false) {
        item.status = "complete";
        item.progress = 100;
        item.message = "完成";
      } else {
        item.status = "failed";
        item.message = errorMessage(xhr.status, payload.detail || payload.message);
      }
      renderUploads();
      resolve();
    });
    xhr.addEventListener("error", () => { item.status = "failed"; item.message = "上传连接失败"; renderUploads(); resolve(); });
    xhr.open("POST", "/api/uploads");
    if (state.csrf) xhr.setRequestHeader("X-CSRF-Token", state.csrf);
    xhr.send(form);
  });
}

async function uploadAll() {
  if (state.uploading) return;
  state.uploading = true;
  renderUploads();
  for (const item of state.uploads) if (["queued", "failed"].includes(item.status)) await uploadItem(item);
  state.uploading = false;
  renderUploads();
  await loadFiles();
}

function updateUploadControls() {
  const permanent = byId("upload-storage").value === "cloud";
  byId("upload-model").disabled = permanent;
  byId("upload-model").closest("label").style.opacity = permanent ? "0.55" : "1";
}

async function loadSettings() {
  setMessage(byId("settings-message"), "正在载入");
  byId("api-key").value = "";
  try {
    state.settings = await api("/api/settings");
    byId("custom-domain").value = state.settings.custom_domain || "";
    renderKeyStatus();
    setMessage(byId("settings-message"));
  } catch (error) { setMessage(byId("settings-message"), error.message, "error"); }
}

function renderKeyStatus() {
  const configured = Boolean(state.settings?.key_configured);
  byId("key-status").textContent = configured ? "已配置" : "未配置";
  byId("key-status").className = `status-badge${configured ? " configured" : ""}`;
  byId("settings-test").disabled = !configured;
  byId("settings-clear").disabled = !configured;
}

async function saveSettings(event) {
  event.preventDefault();
  const button = byId("settings-save");
  setBusy(button, true, "保存中");
  try {
    state.settings = await api("/api/settings", { method: "PUT", body: { api_key: byId("api-key").value, custom_domain: byId("custom-domain").value } });
    byId("api-key").value = "";
    renderKeyStatus();
    setMessage(byId("settings-message"), "设置已保存", "success");
  } catch (error) {
    byId("api-key").value = "";
    setMessage(byId("settings-message"), error.message, "error");
  } finally { setBusy(button, false); }
}

async function testSettings() {
  const button = byId("settings-test");
  setBusy(button, true, "测试中");
  try { await api("/api/settings/test", { method: "POST" }); setMessage(byId("settings-message"), "连接成功", "success"); }
  catch (error) { setMessage(byId("settings-message"), error.message, "error"); }
  finally { setBusy(button, false); }
}

function clearSettingsKey() {
  openConfirm("清除 API Key", "确认清除当前钛盘 API Key？", async () => {
    try {
      state.settings = await api("/api/settings/key", { method: "DELETE" });
      byId("api-key").value = "";
      renderKeyStatus();
      setMessage(byId("settings-message"), "API Key 已清除", "success");
    } catch (error) { setMessage(byId("settings-message"), error.message, "error"); }
  });
}

async function loadLinks() {
  byId("links-state").hidden = false;
  byId("links-state").className = "empty-state";
  byId("links-state").textContent = "正在载入直链";
  byId("links-table-wrap").hidden = true;
  try { state.links = normalizeList(await api("/api/links")); renderLinks(); }
  catch (error) { byId("links-state").textContent = error.message; byId("links-state").classList.add("error"); }
}

function linkKey(link) { return String(link.dkey || link.direct_key || ""); }
function linkUrl(link) { return String(link.link || link.url || ""); }

function renderLinks() {
  const body = byId("links-body");
  body.replaceChildren();
  if (state.links.length === 0) {
    byId("links-state").hidden = false;
    byId("links-state").textContent = "暂无直链";
    byId("links-table-wrap").hidden = true;
    return;
  }
  state.links.forEach((link) => {
    const row = document.createElement("tr");
    row.dataset.linkKey = linkKey(link);
    const key = document.createElement("td"); key.className = "mono"; key.textContent = linkKey(link);
    const url = document.createElement("td"); url.className = "link-cell"; url.textContent = linkUrl(link); url.title = linkUrl(link);
    const actions = document.createElement("td"); actions.className = "row-actions";
    const copy = iconButton("copy", "复制直链", "copy");
    copy.addEventListener("click", async () => { await navigator.clipboard.writeText(linkUrl(link)); toast("直链已复制"); });
    const remove = iconButton("trash-2", "删除直链", "delete", "danger");
    remove.addEventListener("click", () => openConfirm("删除直链", `确认删除 ${linkKey(link)}？`, async () => {
      try { await api(`/api/links/${encodeURIComponent(linkKey(link))}`, { method: "DELETE" }); await loadLinks(); toast("直链已删除"); }
      catch (error) { toast(error.message); }
    }));
    actions.append(copy, remove);
    row.append(key, url, actions);
    body.append(row);
  });
  byId("links-state").hidden = true;
  byId("links-table-wrap").hidden = false;
  initIcons();
}

async function createLink(event) {
  event.preventDefault();
  const payload = { ukey: byId("link-ukey").value };
  if (byId("link-valid-time").value) payload.valid_time = Number(byId("link-valid-time").value);
  if (byId("link-download-limit").value) payload.download_limit = Number(byId("link-download-limit").value);
  const button = byId("link-submit");
  setBusy(button, true, "创建中");
  try {
    await api("/api/links", { method: "POST", body: payload });
    byId("link-dialog").close();
    byId("link-form").reset();
    await loadLinks();
    toast("直链已创建");
  } catch (error) { setMessage(byId("link-message"), error.message, "error"); }
  finally { setBusy(button, false); }
}

async function loadAdmin() {
  if (state.user?.role !== "admin") return;
  byId("users-state").hidden = false;
  byId("users-state").textContent = "正在载入用户";
  byId("users-table-wrap").hidden = true;
  byId("invitations-state").hidden = false;
  byId("invitations-state").textContent = "正在载入邀请";
  byId("invitations-table-wrap").hidden = true;
  try {
    [state.users, state.invitations] = await Promise.all([api("/api/admin/users"), api("/api/admin/invitations")]);
    renderUsers();
    renderInvitations();
  } catch (error) {
    byId("users-state").textContent = error.message;
    byId("users-state").className = "empty-state error";
    byId("invitations-state").textContent = error.message;
    byId("invitations-state").className = "empty-state error";
  }
}

function renderUsers() {
  const body = byId("users-body");
  body.replaceChildren();
  if (!state.users.length) { byId("users-state").hidden = false; byId("users-state").textContent = "暂无用户"; return; }
  state.users.forEach((user) => {
    const row = document.createElement("tr"); row.dataset.userId = user.id;
    const name = document.createElement("td"); name.textContent = user.username; if (user.role === "admin") name.append(" · 管理员");
    const statusCell = document.createElement("td");
    const badge = document.createElement("span"); badge.className = `status-badge${user.status === "disabled" ? " disabled" : " configured"}`; badge.textContent = user.status === "disabled" ? "已停用" : "正常"; statusCell.append(badge);
    const storage = document.createElement("td"); storage.textContent = formatBytes(user.storage_bytes);
    const actions = document.createElement("td"); actions.className = "row-actions";
    if (user.role === "user") {
      if (user.status === "disabled") {
        const restore = iconButton("user-check", "恢复用户", "restore"); restore.addEventListener("click", () => updateUserStatus(user, "active")); actions.append(restore);
      } else {
        const disable = iconButton("user-x", "停用用户", "disable", "danger"); disable.addEventListener("click", () => openConfirm("停用用户", `确认停用 ${user.username}？`, () => updateUserStatus(user, "disabled"))); actions.append(disable);
      }
      const reset = iconButton("key-round", "重置密码", "reset"); reset.addEventListener("click", () => openConfirm("重置密码", `确认重置 ${user.username} 的密码？`, () => resetUserPassword(user))); actions.append(reset);
    }
    row.append(name, statusCell, storage, actions); body.append(row);
  });
  byId("users-state").hidden = true; byId("users-table-wrap").hidden = false; initIcons();
}

async function updateUserStatus(user, status) {
  try { await api(`/api/admin/users/${encodeURIComponent(user.id)}`, { method: "PATCH", body: { status } }); await loadAdmin(); toast(status === "active" ? "用户已恢复" : "用户已停用"); }
  catch (error) { toast(error.message); }
}

async function resetUserPassword(user) {
  try {
    const payload = await api(`/api/admin/users/${encodeURIComponent(user.id)}/reset-password`, { method: "POST" });
    showSecret("临时密码", payload.temporary_password);
    await loadAdmin();
  } catch (error) { toast(error.message); }
}

function invitationStatus(item) {
  return { available: "可用", used: "已使用", expired: "已过期" }[item.status] || item.status;
}

function renderInvitations() {
  const body = byId("invitations-body"); body.replaceChildren();
  if (!state.invitations.length) { byId("invitations-state").hidden = false; byId("invitations-state").textContent = "暂无邀请"; return; }
  state.invitations.forEach((item) => {
    const row = document.createElement("tr"); row.dataset.invitationId = item.id;
    const statusCell = document.createElement("td"); const badge = document.createElement("span"); badge.className = `status-badge${item.status === "available" ? " configured" : " expired"}`; badge.textContent = invitationStatus(item); statusCell.append(badge);
    const created = document.createElement("td"); created.textContent = formatDate(item.created_at);
    const expiry = document.createElement("td"); expiry.textContent = item.expires_at ? formatDate(item.expires_at) : "不限";
    const actions = document.createElement("td"); actions.className = "row-actions";
    if (item.status === "available") { const revoke = iconButton("trash-2", "撤销邀请", "revoke", "danger"); revoke.addEventListener("click", () => openConfirm("撤销邀请", "确认撤销此邀请码？", async () => { try { await api(`/api/admin/invitations/${encodeURIComponent(item.id)}`, { method: "DELETE" }); await loadAdmin(); } catch (error) { toast(error.message); } })); actions.append(revoke); }
    row.append(statusCell, created, expiry, actions); body.append(row);
  });
  byId("invitations-state").hidden = true; byId("invitations-table-wrap").hidden = false; initIcons();
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "-" : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

async function createInvitation(event) {
  event.preventDefault();
  const button = byId("invitation-submit"); setBusy(button, true, "创建中");
  const value = byId("invitation-expiry").value;
  try {
    const payload = await api("/api/admin/invitations", { method: "POST", body: { expires_at: value ? new Date(value).toISOString() : null } });
    byId("invitation-dialog").close(); byId("invitation-form").reset(); showSecret("邀请码", payload.code); await loadAdmin();
  } catch (error) { setMessage(byId("invitation-message"), error.message, "error"); }
  finally { setBusy(button, false); }
}

function showSecret(title, value) {
  byId("secret-title").textContent = title;
  byId("secret-value").value = value;
  byId("secret-dialog").showModal();
}

function bindEvents() {
  byId("login-tab").addEventListener("click", () => activateAuthTab("login"));
  byId("register-tab").addEventListener("click", () => activateAuthTab("register"));
  byId("login-form").addEventListener("submit", submitLogin);
  byId("register-form").addEventListener("submit", submitRegistration);
  byId("password-form").addEventListener("submit", changePassword);
  byId("logout-button").addEventListener("click", logout);
  document.querySelectorAll(".nav-item[data-view]").forEach((button) => button.addEventListener("click", () => activateView(button.dataset.view)));
  byId("refresh-files").addEventListener("click", loadFiles);
  byId("choose-files").addEventListener("click", () => byId("upload-input").click());
  byId("upload-input").addEventListener("change", (event) => queueFiles(event.target.files));
  byId("upload-storage").addEventListener("change", updateUploadControls);
  byId("upload-all").addEventListener("click", uploadAll);
  byId("settings-form").addEventListener("submit", saveSettings);
  byId("settings-test").addEventListener("click", testSettings);
  byId("settings-clear").addEventListener("click", clearSettingsKey);
  byId("new-link").addEventListener("click", () => { byId("link-form").reset(); setMessage(byId("link-message")); byId("link-dialog").showModal(); });
  byId("link-form").addEventListener("submit", createLink);
  byId("link-close").addEventListener("click", () => byId("link-dialog").close());
  byId("link-cancel").addEventListener("click", () => byId("link-dialog").close());
  byId("confirm-accept").addEventListener("click", async (event) => { event.preventDefault(); const action = state.confirmAction; state.confirmAction = null; byId("confirm-dialog").close(); if (action) await action(); });
  byId("create-invitation").addEventListener("click", () => { byId("invitation-form").reset(); setMessage(byId("invitation-message")); byId("invitation-dialog").showModal(); });
  byId("invitation-form").addEventListener("submit", createInvitation);
  byId("invitation-close").addEventListener("click", () => byId("invitation-dialog").close());
  byId("invitation-cancel").addEventListener("click", () => byId("invitation-dialog").close());
  byId("secret-copy").addEventListener("click", async () => { await navigator.clipboard.writeText(byId("secret-value").value); toast("已复制"); });
  byId("secret-dialog").addEventListener("close", () => { byId("secret-value").value = ""; });
  document.querySelectorAll("[data-admin-tab]").forEach((button) => button.addEventListener("click", () => {
    const users = button.dataset.adminTab === "users";
    document.querySelectorAll("[data-admin-tab]").forEach((item) => item.classList.toggle("is-active", item === button));
    byId("admin-users-panel").hidden = !users;
    byId("admin-invitations-panel").hidden = users;
  }));
}

bindEvents();
updateUploadControls();
bootstrap();
