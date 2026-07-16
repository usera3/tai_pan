"use strict";

const state = {
  activeView: "dashboard",
  settings: { key_configured: false, custom_domain: "pan.cloudcode.xyz" },
  activities: [],
};

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
    setText("quota-value", quota && typeof quota === "object" ? (quota.quota ?? quota.data ?? "-") : quota);
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
    button.addEventListener("click", () => navigate(button.dataset.view));
  });
  document.querySelectorAll('[data-action="open-settings"]').forEach((button) => {
    button.addEventListener("click", () => navigate("settings"));
  });
  byId("settings-form").addEventListener("submit", saveSettings);
  byId("test-connection").addEventListener("click", testConnection);
  byId("clear-key").addEventListener("click", clearKey);
  byId("refresh-button").addEventListener("click", refreshCurrentView);
  window.addEventListener("hashchange", () => navigate(location.hash.slice(1)));
}

async function refreshCurrentView() {
  if (state.activeView === "dashboard") await refreshDashboard();
  if (state.activeView === "settings") await loadSettings();
}

async function boot() {
  bindEvents();
  navigate(location.hash.slice(1) || "dashboard");
  try {
    await loadSettings();
    await refreshDashboard();
  } catch (error) {
    setConnection("error", "本地服务异常");
    toast(error.message);
  }
  initIcons();
}

document.addEventListener("DOMContentLoaded", boot);
