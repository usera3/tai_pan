"use strict";

const state = {
  activeView: "dashboard",
  settings: { key_configured: false, custom_domain: "pan.cloudcode.xyz" },
  activities: [],
  uploadQueue: [],
  files: [],
  filesPage: 1,
  links: [],
  linksPage: 1,
  uploading: false,
  pendingLinkUkey: "",
  pendingDeleteDkey: "",
};

const STORAGE_MODES = new Set([99, 0, 1, 2]);
const UPLOAD_STATUSES = new Set(["queued", "uploading", "processing", "complete", "failed"]);

const viewTitles = {
  dashboard: "概览",
  files: "文件",
  links: "直链",
  settings: "设置",
};

const byId = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const request = { ...options, headers: { ...(options.headers || {}) } };
  if (request.body && !(request.body instanceof FormData)) {
    request.headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(request.body);
  }
  const response = await fetch(path, request);
  const payload = await response.json().catch(() => ({ ok: false, message: "本地服务返回了无效响应" }));
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.message || `请求失败 (${response.status})`);
  }
  return payload.data;
}

function setText(id, value) {
  const element = byId(id);
  if (element) element.textContent = value == null || value === "" ? "-" : String(value);
}

function setConnection(status, label) {
  const dot = byId("sidebar-status-dot");
  dot.classList.remove("success", "error");
  if (status) dot.classList.add(status);
  setText("sidebar-status-text", label);
  setText("connection-label", label);
}

function initIcons() {
  try {
    if (window.lucide) window.lucide.createIcons();
  } catch (error) {
    console.warn("Icon initialization failed");
  }
}

function navigate(view) {
  if (!viewTitles[view]) view = "dashboard";
  state.activeView = view;
  document.querySelectorAll(".view").forEach((element) => {
    element.classList.toggle("is-active", element.id === `view-${view}`);
  });
  document.querySelectorAll(".nav-item").forEach((element) => {
    element.classList.toggle("is-active", element.dataset.view === view);
  });
  setText("page-title", viewTitles[view]);
  if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
  initIcons();
}

function showMessage(message, type = "") {
  const element = byId("settings-message");
  element.textContent = message;
  element.className = `form-message ${type}`.trim();
}

function toast(message) {
  const element = document.createElement("div");
  element.className = "toast";
  element.textContent = message;
  byId("toast-region").append(element);
  window.setTimeout(() => element.remove(), 3200);
}

function createElement(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function iconButton(icon, label, action, danger = false) {
  const button = createElement("button", `small-icon-button${danger ? " danger" : ""}`);
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  const iconNode = createElement("i");
  iconNode.dataset.lucide = icon;
  button.append(iconNode);
  button.addEventListener("click", action);
  return button;
}

function formatBytes(value) {
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) return "-";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

async function copyText(value) {
  await navigator.clipboard.writeText(String(value));
  toast("已复制");
}

function uploadIdentity(file) {
  return `${file.name}:${file.size}:${file.lastModified}`;
}

function addFiles(fileList) {
  const existing = new Set(state.uploadQueue.map((item) => item.id));
  Array.from(fileList).forEach((file) => {
    const id = uploadIdentity(file);
    if (!existing.has(id)) {
      state.uploadQueue.push({ id, file, status: "queued", progress: 0, message: "等待上传", ukey: "" });
      existing.add(id);
    }
  });
  renderUploadQueue();
}

function renderUploadQueue() {
  const container = byId("upload-queue");
  container.replaceChildren();
  state.uploadQueue.forEach((item) => {
    const row = createElement("div", "queue-item");
    const name = createElement("div", "queue-name");
    name.append(createElement("strong", "", item.file.name), createElement("small", "", formatBytes(item.file.size)));

    const status = createElement("div", "queue-status");
    const progress = createElement("div", `queue-progress${item.status === "processing" ? " processing" : ""}`);
    const progressValue = createElement("span");
    progressValue.style.width = `${item.progress}%`;
    progress.append(progressValue);
    status.append(progress, createElement("small", "", item.message));

    const actions = createElement("div", "queue-actions");
    if (item.status === "failed") actions.append(iconButton("rotate-ccw", "重试", () => retryUpload(item.id)));
    if (item.status === "complete" && item.ukey) actions.append(iconButton("link-2", "创建直链", () => openLinkDialog(item.ukey)));
    if (!["uploading", "processing"].includes(item.status)) actions.append(iconButton("x", "移除", () => removeUpload(item.id)));
    row.append(name, status, actions);
    container.append(row);
  });
  byId("upload-all").disabled = state.uploading || !state.uploadQueue.some((item) => ["queued", "failed"].includes(item.status));
  initIcons();
}

function removeUpload(id) {
  state.uploadQueue = state.uploadQueue.filter((item) => item.id !== id);
  renderUploadQueue();
}

async function retryUpload(id) {
  const item = state.uploadQueue.find((entry) => entry.id === id);
  if (!item || state.uploading) return;
  item.status = "queued";
  item.progress = 0;
  item.message = "等待重试";
  await uploadAll();
}

function uploadQueueItem(item, model) {
  return new Promise((resolve) => {
    const data = new FormData();
    data.append("file", item.file);
    data.append("model", String(model));
    const request = new XMLHttpRequest();
    item.status = "uploading";
    item.message = "上传到本地服务";
    renderUploadQueue();
    request.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      item.progress = Math.round((event.loaded / event.total) * 100);
      if (item.progress >= 100) {
        item.status = "processing";
        item.message = "钛盘正在处理";
      }
      renderUploadQueue();
    });
    request.addEventListener("load", () => {
      let payload;
      try { payload = JSON.parse(request.responseText); } catch (error) { payload = { ok: false, message: "无效响应" }; }
      if (request.status >= 200 && request.status < 300 && payload.ok !== false) {
        item.status = "complete";
        item.progress = 100;
        item.message = "上传完成";
        item.ukey = typeof payload.data === "string" ? payload.data : (payload.data && payload.data.ukey) || "";
      } else {
        item.status = "failed";
        item.message = payload.message || `上传失败 (${request.status})`;
      }
      renderUploadQueue();
      resolve();
    });
    request.addEventListener("error", () => {
      item.status = "failed";
      item.message = "无法连接本地服务";
      renderUploadQueue();
      resolve();
    });
    request.open("POST", "/api/uploads");
    request.send(data);
  });
}

async function uploadAll() {
  if (state.uploading) return;
  const model = Number(byId("storage-model").value);
  if (!STORAGE_MODES.has(model)) return;
  state.uploading = true;
  renderUploadQueue();
  for (const item of state.uploadQueue) {
    if (["queued", "failed"].includes(item.status)) await uploadQueueItem(item, model);
  }
  state.uploading = false;
  renderUploadQueue();
  await loadFiles();
  await refreshDashboard();
}

function normalizeRows(data) {
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.data)) return data.data;
  if (data && Array.isArray(data.list)) return data.list;
  return [];
}

async function loadFiles() {
  try {
    const data = await api(`/api/files?page=${state.filesPage}`);
    state.files = normalizeRows(data);
    renderFiles();
  } catch (error) {
    toast(error.message);
  }
}

function renderFiles() {
  const body = byId("files-body");
  body.replaceChildren();
  state.files.forEach((file) => {
    const row = document.createElement("tr");
    row.append(
      createElement("td", "", file.name || "-"),
      createElement("td", "", formatBytes(file.size)),
      createElement("td", "mono", file.ukey || "-"),
    );
    const actionsCell = createElement("td", "row-actions");
    if (file.ukey) {
      actionsCell.append(
        iconButton("copy", "复制 UKEY", () => copyText(file.ukey)),
        iconButton("link-2", "创建直链", () => openLinkDialog(file.ukey)),
      );
    }
    row.append(actionsCell);
    body.append(row);
  });
  byId("files-empty").style.display = state.files.length ? "none" : "grid";
  byId("files-page").textContent = `第 ${state.filesPage} 页`;
  byId("files-prev").disabled = state.filesPage <= 1;
  initIcons();
}

function openLinkDialog(ukey = "") {
  state.pendingLinkUkey = ukey;
  navigate("links");
  byId("link-ukey").value = ukey;
  byId("link-message").textContent = "";
  byId("link-dialog").showModal();
  initIcons();
}

function linkUrl(value) {
  if (!value) return "";
  if (/^https?:\/\//i.test(value)) return value;
  return `https://${state.settings.custom_domain}/${String(value).replace(/^\/+/, "")}`;
}

async function loadLinks() {
  try {
    const data = await api(`/api/links?page=${state.linksPage}`);
    state.links = normalizeRows(data);
    renderLinks();
  } catch (error) {
    toast(error.message);
  }
}

function renderLinks() {
  const body = byId("links-body");
  body.replaceChildren();
  state.links.forEach((link) => {
    const url = linkUrl(link.link);
    const row = document.createElement("tr");
    row.append(
      createElement("td", "", link.name || "-"),
      createElement("td", "link-cell", url || "-"),
      createElement("td", "", link.etime || "-"),
    );
    const actionsCell = createElement("td", "row-actions");
    if (url) {
      actionsCell.append(
        iconButton("copy", "复制链接", () => copyText(url)),
        iconButton("external-link", "打开链接", () => window.open(url, "_blank", "noopener")),
      );
    }
    if (link.dkey) actionsCell.append(iconButton("trash-2", "删除直链", () => openDeleteDialog(link.dkey), true));
    row.append(actionsCell);
    body.append(row);
  });
  byId("links-empty").style.display = state.links.length ? "none" : "grid";
  byId("links-page").textContent = `第 ${state.linksPage} 页`;
  byId("links-prev").disabled = state.linksPage <= 1;
  initIcons();
}

async function submitLink(event) {
  event.preventDefault();
  const button = byId("create-link-submit");
  const message = byId("link-message");
  button.disabled = true;
  message.textContent = "正在创建";
  message.className = "form-message";
  const validTime = byId("link-valid-time").value;
  const downloadLimit = byId("link-download-limit").value;
  try {
    const result = await api("/api/links", {
      method: "POST",
      body: {
        ukey: byId("link-ukey").value,
        valid_time: validTime ? Number(validTime) : null,
        download_limit: downloadLimit ? Number(downloadLimit) : null,
      },
    });
    byId("link-dialog").close();
    toast("直链已创建");
    state.linksPage = 1;
    await loadLinks();
    await refreshDashboard();
    const url = result && linkUrl(result.link);
    if (url) await copyText(url);
  } catch (error) {
    message.textContent = error.message;
    message.className = "form-message error";
  } finally {
    button.disabled = false;
  }
}

function openDeleteDialog(dkey) {
  state.pendingDeleteDkey = dkey;
  byId("delete-file").checked = false;
  byId("delete-message").textContent = "";
  byId("delete-dialog").showModal();
  initIcons();
}

async function submitDelete(event) {
  event.preventDefault();
  const button = byId("delete-link-submit");
  const message = byId("delete-message");
  button.disabled = true;
  message.textContent = "正在删除";
  try {
    const deleteFile = byId("delete-file").checked;
    await api(`/api/links/${encodeURIComponent(state.pendingDeleteDkey)}?delete_file=${deleteFile}`, { method: "DELETE" });
    byId("delete-dialog").close();
    toast("直链已删除");
    await loadLinks();
    await loadFiles();
    await refreshDashboard();
  } catch (error) {
    message.textContent = error.message;
    message.className = "form-message error";
  } finally {
    button.disabled = false;
  }
}

function updateSettingsUi(settings) {
  state.settings = settings;
  byId("custom-domain").value = settings.custom_domain;
  setText("domain-value", settings.custom_domain);
  const badge = byId("key-badge");
  badge.textContent = settings.key_configured ? "已配置" : "未配置";
  badge.className = `badge ${settings.key_configured ? "success" : "neutral"}`;
  setConnection(settings.key_configured ? "" : "error", settings.key_configured ? "等待测试" : "需要配置 Key");
}

async function loadSettings() {
  const settings = await api("/api/settings");
  updateSettingsUi(settings);
  return settings;
}

function arrayFromData(data) {
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.data)) return data.data;
  if (data && Array.isArray(data.list)) return data.list;
  return [];
}

async function refreshDashboard() {
  if (!state.settings.key_configured) {
    setConnection("error", "需要配置 Key");
    return;
  }
  setConnection("", "正在刷新");
  const results = await Promise.allSettled([
    api("/api/quota"),
    api("/api/files?page=1"),
    api("/api/links?page=1"),
  ]);
  if (results[0].status === "fulfilled") {
    const quota = results[0].value;
    const quotaValue = quota && typeof quota === "object" ? (quota.quota ?? quota.data) : quota;
    setText("quota-value", quotaValue == null ? "-" : formatBytes(quotaValue));
  }
  if (results[1].status === "fulfilled") setText("file-count", arrayFromData(results[1].value).length);
  if (results[2].status === "fulfilled") setText("link-count", arrayFromData(results[2].value).length);
  const failure = results.find((result) => result.status === "rejected");
  if (failure) {
    setConnection("error", failure.reason.message);
  } else {
    setConnection("success", "连接正常");
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const button = byId("save-settings");
  button.disabled = true;
  showMessage("正在保存");
  try {
    const settings = await api("/api/settings", {
      method: "PUT",
      body: {
        api_key: byId("api-key").value,
        custom_domain: byId("custom-domain").value,
      },
    });
    byId("api-key").value = "";
    updateSettingsUi(settings);
    showMessage("设置已保存", "success");
    toast("设置已保存");
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function testConnection() {
  const button = byId("test-connection");
  button.disabled = true;
  showMessage("正在测试连接");
  try {
    await api("/api/settings/test", { method: "POST" });
    setConnection("success", "连接正常");
    showMessage("连接测试成功", "success");
    await refreshDashboard();
  } catch (error) {
    setConnection("error", "连接失败");
    showMessage(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function clearKey() {
  if (!window.confirm("确认清除本地保存的 API Key？")) return;
  const button = byId("clear-key");
  button.disabled = true;
  try {
    const settings = await api("/api/settings/key", { method: "DELETE" });
    updateSettingsUi(settings);
    showMessage("API Key 已清除", "success");
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", async () => {
      navigate(button.dataset.view);
      await refreshCurrentView();
    });
  });
  document.querySelectorAll('[data-action="open-settings"]').forEach((button) => {
    button.addEventListener("click", () => navigate("settings"));
  });
  byId("settings-form").addEventListener("submit", saveSettings);
  byId("test-connection").addEventListener("click", testConnection);
  byId("clear-key").addEventListener("click", clearKey);
  byId("refresh-button").addEventListener("click", refreshCurrentView);
  byId("choose-files").addEventListener("click", () => byId("file-input").click());
  byId("file-input").addEventListener("change", (event) => addFiles(event.target.files));
  byId("upload-all").addEventListener("click", uploadAll);
  byId("files-refresh").addEventListener("click", loadFiles);
  byId("files-prev").addEventListener("click", async () => { if (state.filesPage > 1) { state.filesPage -= 1; await loadFiles(); } });
  byId("files-next").addEventListener("click", async () => { state.filesPage += 1; await loadFiles(); });
  byId("new-link").addEventListener("click", () => openLinkDialog());
  byId("links-refresh").addEventListener("click", loadLinks);
  byId("links-prev").addEventListener("click", async () => { if (state.linksPage > 1) { state.linksPage -= 1; await loadLinks(); } });
  byId("links-next").addEventListener("click", async () => { state.linksPage += 1; await loadLinks(); });
  byId("link-form").addEventListener("submit", submitLink);
  byId("delete-form").addEventListener("submit", submitDelete);
  document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => byId(button.dataset.closeDialog).close()));
  const zone = byId("upload-zone");
  ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.add("is-dragging"); }));
  ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.remove("is-dragging"); }));
  zone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
  zone.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") byId("file-input").click(); });
  window.addEventListener("hashchange", async () => {
    navigate(location.hash.slice(1));
    await refreshCurrentView();
  });
}

async function refreshCurrentView() {
  if (state.activeView === "dashboard") await refreshDashboard();
  if (state.activeView === "files") await loadFiles();
  if (state.activeView === "links") await loadLinks();
  if (state.activeView === "settings") await loadSettings();
}

async function boot() {
  bindEvents();
  navigate(location.hash.slice(1) || "dashboard");
  try {
    await loadSettings();
    await refreshCurrentView();
  } catch (error) {
    setConnection("error", "本地服务异常");
    toast(error.message);
  }
  initIcons();
}

document.addEventListener("DOMContentLoaded", boot);
