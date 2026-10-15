let config = {
  enabled: false,
  interval_seconds: 600,
  notify_current_stock_on_start: false,
  isvoro_seckill: false,
  affiliate_id: "",
  affiliates: {},
  merchant_order: ["vmiss", "vps_soy", "akile", "isvoro", "dmit", "bwh"],
  watch_order: [],
  notify_order: [],
  fetcher: { mode: "requests", flaresolverr_url: "http://127.0.0.1:8191/v1", timeout_seconds: 60 },
  telegram: {},
  products: [],
};
const editorNode = document.querySelector("#product-editors");
const listNode = document.querySelector("#product-list");
const messageNode = document.querySelector("#message");
const blockedHelpNode = document.querySelector("#blocked-help");
let lastStatus = null;
const toastNode = document.querySelector("#toast");
let toastTimer;
let actionRunning = false;
let updateUnit = sessionStorage.getItem("hostwatch-update-unit") || "";
let managementUsers = [];
let monitorService = null;
let savedConfig = null;
let serviceRefreshTimer, serviceRefreshPending = false;
let serviceTransitionStartedAt = 0;
let exchangeRateData = null;
let exchangeClockTimer;
let exchangeRateRefreshTimer;
const SERVICE_TRANSITION_TIMEOUT_MS = 60_000;
const compareSelection = new Set();
const openProducts = new Set();
const productTests = new Map();
let activeTest = null;
let productDrag = null;
const seckillTestId = "isvoro-seckill";
const cloudscraperTestId = "cloudscraper-probe";

const pages = {
  overview: { title: "运行概览", description: "查看监控服务、商品库存与最近检查结果。" },
  catalog: { title: "VPS 库存大盘", description: "浏览商家全部公开套餐或地区分类，选择你关心的 VPS 加入补货提醒。" },
  monitor: { title: "VPS 库存监控", description: "集中查看你关注的 VPS，查看库存与配置、编辑备注和线路，直接购买，不发送通知。" },
  notifications: { title: "VPS 库存通知", description: "选择需要 Telegram 库存提醒的商品，管理通知和测试。" },
  affiliates: { title: "AFF 管理", description: "统一管理各商家推广码，保存后应用到新的购买入口。" },
  telegram: { title: "Telegram 通知", description: "配置通知机器人，接收库存提醒与商品测试结果。" },
  requests: { title: "网页请求", description: "设置各商家请求方式，配置轻量请求与浏览器请求，测试访问效果。" },
  management: { title: "管理员管理", description: "管理服务与账号，查看日志、备份及更新程序。" },
};
let currentPage = null;

function navigatePage(name, { history = true, focus = true } = {}) {
  if (name === "products") { navigatePage("notifications", { history, focus }); openAdvancedProducts(); return; }
  if (!Object.hasOwn(pages, name)) name = "overview";
  if (history && location.hash !== `#${name}`) window.history.pushState(null, "", `#${name}`);
  if (currentPage === name) return;
  currentPage = name;
  document.querySelectorAll("[data-page]").forEach((panel) => {
    panel.hidden = panel.dataset.page !== name;
    panel.classList.toggle("page-enter", !panel.hidden);
  });
  document.querySelectorAll("[data-page-link]").forEach((link) => {
    if (link.dataset.pageLink === name) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  const heading = document.querySelector("#page-title");
  heading.textContent = pages[name].title;
  document.querySelector("#page-description").textContent = pages[name].description;
  document.title = `${pages[name].title} | HostWatch`;
  if (focus) {
    heading.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "instant" });
    document.querySelector('[data-page-link][aria-current="page"]').scrollIntoView({ block: "nearest", inline: "nearest", behavior: "instant" });
  }
  window.dispatchEvent(new CustomEvent("hostwatch-page-change", { detail: name }));
}

document.querySelectorAll("[data-page-link]").forEach((link) => {
  link.addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    navigatePage(link.dataset.pageLink);
  });
});
window.addEventListener("hashchange", () => navigatePage(location.hash.slice(1), { history: false }));
navigatePage(location.hash.slice(1), { history: false, focus: false });

function setMessage(message, kind = "", notify = true) {
  messageNode.textContent = message;
  messageNode.className = kind;
  if (!notify) return;
  clearTimeout(toastTimer);
  toastNode.className = `toast ${kind}`;
  toastNode.hidden = false;
  document.querySelector("#toast-label").textContent = ({ pending: "操作提示", success: "操作成功", error: "操作失败" })[kind] || "操作提示";
  document.querySelector("#toast-message").textContent = message;
  // Routine success feedback stays in the toast; errors also remain inline.
  if (kind === "success") toastTimer = setTimeout(() => { toastNode.hidden = true; }, 6500);
}

document.querySelector("#toast-close").addEventListener("click", () => {
  clearTimeout(toastTimer);
  toastNode.hidden = true;
});

async function requestJSON(url, options = {}) {
  const controller = new AbortController();
  const { timeoutMs = 40000, signal, ...fetchOptions } = options;
  const cancel = () => controller.abort();
  if (signal?.aborted) cancel();
  else signal?.addEventListener("abort", cancel, { once: true });
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { cache: "no-store", ...fetchOptions, signal: controller.signal });
    let result;
    try { result = await response.json(); }
    catch {
      if (!response.ok) throw new Error(`请求失败（HTTP ${response.status}），请检查登录状态或服务器。`);
      throw new Error("服务器响应无法识别，请刷新状态核对结果。");
    }
    if (!response.ok) throw Object.assign(new Error(result.error || `请求失败（HTTP ${response.status}）`), { status: response.status });
    return result;
  } catch (error) {
    if (controller.signal.aborted) throw new Error("请求超时，请核对结果及反向代理超时设置后重试。");
    if (error instanceof TypeError) throw new Error("网络连接失败，请检查连接并刷新状态核对结果。");
    throw error;
  } finally { clearTimeout(timer); signal?.removeEventListener("abort", cancel); }
}

async function runAction(button, label, message, action, notify = true) {
  if (actionRunning) return;
  cancelProductDrag();
  actionRunning = true;
  document.querySelector(".shell").inert = !activeTest;
  const controls = [...document.querySelectorAll("button,input,select")].filter((control) => control.id !== "toast-close" && !control.classList.contains("product-test-stop"));
  const previousStates = controls.map((control) => control.disabled);
  const previousLabel = button?.textContent;
  controls.forEach((control) => { control.disabled = true; });
  if (button) {
    button.textContent = label;
    button.setAttribute("aria-busy", "true");
  }
  setMessage(message, "pending", notify);
  try { await action(); }
  catch (error) { setMessage(error.message, "error"); }
  finally {
    controls.forEach((control, index) => { control.disabled = previousStates[index]; });
    if (button) {
      button.textContent = previousLabel;
      button.removeAttribute("aria-busy");
    }
    actionRunning = false;
    document.querySelector(".shell").inert = false;
    renderMerchantOrder();
    renderProductList();
    syncTestControls();
    updateDiscardControl();
    window.dispatchEvent(new Event("hostwatch-action-change"));
  }
}

function text(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
}

function optionPressed(id) {
  return document.getElementById(id).getAttribute("aria-pressed") === "true";
}
function setOptionPressed(id, pressed) {
  const button = document.getElementById(id);
  button.setAttribute("aria-pressed", String(Boolean(pressed)));
  button.querySelector("[data-option-state]").textContent = pressed ? button.dataset.on : button.dataset.off;
}
document.querySelectorAll("[data-config-option]").forEach(button => button.addEventListener("click", () => {
  setOptionPressed(button.id, !optionPressed(button.id));
  updateDraftState();
  if (button.id === "isvoro-seckill") renderSeckill();
}));

const filterMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const filterAnimations = new Map();
function animateFilterResult(node) {
  if (!node) return;
  filterAnimations.get(node)?.cancel();
  filterAnimations.delete(node);
  if (filterMotion.matches || !node.isConnected || node.hidden || !node.animate) return;
  const animation = node.animate([
    { opacity: .5, transform: "translateY(4px)" },
    { opacity: 1, transform: "translateY(0)" },
  ], { duration: 180, easing: "ease-out" });
  animation.id = "filter-transition";
  filterAnimations.set(node, animation);
  const cleanup = () => { if (filterAnimations.get(node) === animation) filterAnimations.delete(node); };
  animation.finished.then(cleanup, cleanup);
}
filterMotion.addEventListener("change", () => {
  if (!filterMotion.matches) return;
  for (const animation of filterAnimations.values()) animation.cancel();
  filterAnimations.clear();
});

async function managementRequest(value) {
  return requestJSON("/api/manage", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) });
}

async function refreshManagement() {
  let state;
  try { state = await requestJSON("/api/manage"); }
  catch (error) {
    monitorService = null;
    renderMonitorState();
    scheduleServiceRefresh();
    throw error;
  }
  const previousActive = monitorService?.active;
  monitorService = state.monitor;
  if (["activating", "deactivating"].includes(monitorService?.active)) {
    if (previousActive !== monitorService.active || !serviceTransitionStartedAt) serviceTransitionStartedAt = Date.now();
    monitorService.transition_timeout = Date.now() - serviceTransitionStartedAt >= SERVICE_TRANSITION_TIMEOUT_MS;
  } else {
    serviceTransitionStartedAt = 0;
    monitorService.transition_timeout = false;
  }
  scheduleServiceRefresh();
  renderMonitorState();
  const serviceLabel = (value) => ({ active: "运行中", inactive: "已停止", failed: "运行失败", activating: "启动中", deactivating: "停止中", unknown: "未知" })[value] || value;
  const monitorState = document.querySelector("#manage-monitor-state");
  monitorState.textContent = serviceLabel(state.monitor.active);
  monitorState.className = `state ${serviceTone(state.monitor.active)}`;
  document.querySelector("#manage-monitor-enabled").textContent = ` · 开机启动：${state.monitor.enabled === "enabled" ? "已开启" : state.monitor.enabled === "disabled" ? "已关闭" : state.monitor.enabled}`;
  const adminState = document.querySelector("#manage-admin-state");
  adminState.textContent = serviceLabel(state.admin.active);
  adminState.className = `state ${serviceTone(state.admin.active)}`;
  managementUsers = state.users;
  renderManagementUsers();
  const backups = document.querySelector("#manage-backups");
  backups.replaceChildren(...(state.backups.length ? state.backups.map((item) => text("span", `${item.name} · ${formatBytes(item.size)}`)) : [text("span", "暂无备份")]));
  if (updateUnit) await refreshUpdateStatus();
  return state;
}

function serviceTone(active) {
  if (monitorService?.transition_timeout && ["activating", "deactivating"].includes(active)) return "error";
  return active === "active" ? "ok" : ["inactive", "failed"].includes(active) ? "error" : ["activating", "deactivating"].includes(active) ? "pending" : "";
}
function scheduleServiceRefresh() {
  clearTimeout(serviceRefreshTimer);
  const transition = ["activating", "deactivating"].includes(monitorService?.active);
  const delay = transition && !monitorService?.transition_timeout ? 2000 : 10000;
  serviceRefreshTimer = setTimeout(refreshLiveState, delay);
}
async function refreshLiveState() {
  if (document.hidden || actionRunning || serviceRefreshPending) { scheduleServiceRefresh(); return; }
  serviceRefreshPending = true;
  try { await Promise.allSettled([refreshManagement(), loadStatus()]); }
  finally { serviceRefreshPending = false; scheduleServiceRefresh(); }
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) void refreshLiveState(); });

function renderManagementUsers() {
  const users = document.querySelector("#manage-users");
  users.replaceChildren(...managementUsers.map((username) => {
    if (username.startsWith("(")) return text("span", username);
    const button = text("button", username);
    button.type = "button";
    button.addEventListener("click", () => { document.querySelector("#manager-username").value = username; });
    return button;
  }));
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

async function refreshUpdateStatus() {
  const result = await managementRequest({ action: "update-status", unit: updateUnit });
  const finished = result.ActiveState === "failed" || (result.ActiveState === "active" && result.SubState === "exited");
  const label = finished ? (result.Result === "success" ? "更新完成。" : `更新失败：${result.Result}`) : `更新任务：${result.ActiveState || result.LoadState || "等待中"}`;
  document.querySelector("#manage-task-status").textContent = label;
  if (result.logs) document.querySelector("#manage-logs").textContent = result.logs;
  return { ...result, finished };
}

async function performManagementAction(action, extra = {}) {
  const result = await managementRequest({ action, ...extra });
  if (result.update_unit) {
    updateUnit = result.update_unit;
    sessionStorage.setItem("hostwatch-update-unit", updateUnit);
    document.querySelector("#manage-task-status").textContent = result.message;
    setMessage(result.message, "success");
    pollUpdateStatus();
  } else {
    setMessage(result.message || "管理员操作已完成。", "success");
  }
  if (action.startsWith("monitor-")) {
    if (action === "monitor-start" || action === "monitor-stop") {
      config.enabled = action === "monitor-start";
      if (savedConfig) savedConfig = JSON.stringify({ ...JSON.parse(savedConfig), enabled: config.enabled });
      updateDraftState();
    }
  }
  if (action.startsWith("admin-")) {
    document.querySelector("#manager-password").value = "";
    document.querySelector("#manager-confirm").value = "";
    if (action === "admin-add") managementUsers = [...managementUsers.filter((name) => !name.startsWith("(")), extra.username].sort();
    if (action === "admin-delete") managementUsers = managementUsers.filter((name) => name !== extra.username);
    renderManagementUsers();
    if (action !== "admin-restart") {
      setMessage(`${result.message}后续请求如出现登录提示，请使用新的管理员凭据登录。`, "success");
      return;
    }
  }
  if (!result.update_unit && action !== "admin-restart") {
    try { await refreshManagement(); }
    catch (error) { setMessage(`${result.message || "操作已完成。"}状态刷新失败：${error.message}`, "error"); }
  }
}

function pollUpdateStatus() {
  let polling = false;
  const timer = setInterval(async () => {
    if (polling || actionRunning) return;
    polling = true;
    try {
      const result = await refreshUpdateStatus();
      if (result.finished || result.LoadState === "not-found") {
        clearInterval(timer);
        setMessage(result.Result === "success" ? "HostWatch 更新完成。管理页可能需要重新载入。" : `HostWatch 更新结束：${result.Result || result.LoadState}`, result.Result === "success" ? "success" : "error");
        updateUnit = "";
        sessionStorage.removeItem("hostwatch-update-unit");
        await refreshManagement();
      }
    } catch (error) {
      document.querySelector("#manage-task-status").textContent = `更新可能正在重启服务，等待管理页恢复：${error.message}`;
    } finally { polling = false; }
  }, 4000);
  setTimeout(() => {
    clearInterval(timer);
    if (updateUnit) document.querySelector("#manage-task-status").textContent = "更新仍未返回最终结果，请点击刷新状态核对，或查看服务器更新任务日志。";
  }, 10 * 60 * 1000);
}

function bindManagementActions() {
  document.querySelector("#manage-refresh").addEventListener("click", (event) => runAction(event.currentTarget, "刷新中…", "正在读取服务器服务和账号状态…", async () => {
    await refreshManagement();
    setMessage("服务器服务、管理员账号和备份列表已刷新。", "success");
  }));
  for (const [id, action, label, message] of [
    ["manage-restart-admin", "admin-restart", "安排中…", "正在安排管理页服务重启…"],
    ["manage-backup", "backup", "备份中…", "正在备份程序和配置…"],
    ["manage-update", "update", "启动中…", "正在启动 HostWatch 更新任务…"],
  ]) {
    document.querySelector(`#${id}`).addEventListener("click", (event) => {
      runAction(event.currentTarget, label, message, () => performManagementAction(action));
    });
  }
  document.querySelector("#manage-load-logs").addEventListener("click", (event) => runAction(event.currentTarget, "载入中…", "正在读取最近服务日志…", async () => {
    const result = await requestJSON("/api/manage/logs");
    document.querySelector("#manage-logs").textContent = result.logs || "暂无日志。";
    setMessage("最近服务日志已载入。", "success");
  }));
  document.querySelector("#manage-add-user").addEventListener("click", (event) => runAction(event.currentTarget, "添加中…", "正在添加管理页管理员…", () => performManagementAction("admin-add", managerCredentials())));
  document.querySelector("#manage-change-password").addEventListener("click", (event) => runAction(event.currentTarget, "更新中…", "正在更新管理员密码…", () => performManagementAction("admin-passwd", managerCredentials())));
  document.querySelector("#manage-delete-user").addEventListener("click", (event) => {
    const username = document.querySelector("#manager-username").value.trim();
    if (!username || !window.confirm(`确认删除管理员 ${username}？该账号将立即失去访问权限。`)) return;
    runAction(event.currentTarget, "删除中…", "正在删除管理员…", () => performManagementAction("admin-delete", { username }));
  });
}

function managerCredentials() {
  return {
    username: document.querySelector("#manager-username").value.trim(),
    password: document.querySelector("#manager-password").value,
    confirmation: document.querySelector("#manager-confirm").value,
  };
}

function inputField(labelText, value, key, type = "text", placeholder = "") {
  const label = text("label", labelText);
  const input = document.createElement("input");
  input.value = value ?? "";
  input.type = type;
  input.placeholder = placeholder;
  input.dataset.key = key;
  label.append(input);
  return label;
}

function selectField(labelText, value, key, options) {
  const label = text("label", labelText);
  const select = document.createElement("select");
  select.dataset.key = key;
  for (const [optionValue, optionLabel] of options) {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = optionLabel;
    select.append(option);
  }
  select.value = value;
  label.append(select);
  return label;
}

const soyGroups = [["8", "深港 · CNIX"], ["5", "香港 · 三网直连"], ["6", "香港 · Global"], ["4", "新加坡 · Global"], ["2", "日本 · SoftBank"], ["3", "日本 · IIJ"], ["7", "英国 · Global"]];
const akileAreas = [["14", "中国澳门"], ["5", "日本"], ["13", "意大利"], ["15", "荷兰"], ["8", "韩国"], ["6", "中国台湾"], ["7", "新加坡"], ["3", "中国香港"], ["2", "美国"], ["11", "英国"], ["1", "德国"], ["16", "越南"]];
// Public directory identities observed on 2026-10-15; never cache stock here.
let isvoroTypes = [{"id":62,"name":"中国澳门","slug":"MO","children":[{"id":63,"name":"Mo Lite","slug":"molite"}]},{"id":24,"name":"中国香港","slug":"cnhk","children":[{"id":56,"name":"HK Lite","slug":"hklite"},{"id":27,"name":"HK PROD","slug":"HK-prod"},{"id":46,"name":"HK CN2","slug":"HKCN2"},{"id":47,"name":"HK BGP","slug":"hkbgp"},{"id":57,"name":"HKT","slug":"hkt"}]},{"id":35,"name":"美国","slug":"us","children":[{"id":38,"name":"USLAPro","slug":"USLAPro"},{"id":53,"name":"US9929","slug":"us9929"},{"id":45,"name":"USLAPR","slug":"uslapr"},{"id":36,"name":"USLACA","slug":"USLACA"},{"id":61,"name":"USLACA2","slug":"uslaca2"}]},{"id":54,"name":"德国","slug":"DE","children":[{"id":55,"name":"DE-9929","slug":"de9929"}]},{"id":58,"name":"英国","slug":"GB","children":[{"id":59,"name":"GB ISP","slug":"gbisp"}]},{"id":48,"name":"物理机","slug":"Dedicated","children":[]}];
const isvoroLines = new Map([["27",{"id":27,"name":"HK PROD","slug":"HK-prod","products":[{"id":188,"name":"HK PROD MiNi","slug":"hkpordmini"},{"id":189,"name":"HK PROD Lite","slug":"hkprodlite"},{"id":190,"name":"HK PROD Starter","slug":"hkprodstarter"},{"id":191,"name":"HK PROD Standard","slug":"hkprodstandard"},{"id":192,"name":"HK PROD Pro","slug":"hkprodpro"},{"id":193,"name":"HK PROD Max","slug":"hkprodmax"},{"id":194,"name":"HK PROD Ultra","slug":"hkprodultra"},{"id":195,"name":"HK PROD Pro Max","slug":"hkprodpromax"},{"id":196,"name":"HK PROD UltraPlus","slug":"hkprodultraplus"}]}]]);

function isvoroOptions(control, rows, value, savedName, placeholder) {
  control.replaceChildren(new Option(placeholder, ""), ...rows.map((row) => new Option(`${row.name}（${row.id}）`, String(row.id))));
  if (value && !rows.some((row) => String(row.id) === value)) control.add(new Option(`${savedName || "已保存条目"}（${value}，请刷新核对）`, value));
  control.value = value || "";
}

function renderIsvoroFields(article, reset = "", animate = false) {
  const get = (key) => article.querySelector(`[data-key="isvoro_${key}"]`);
  const item = config.products.find((product) => product.id === article.dataset.id) || {};
  isvoroOptions(get("type"), isvoroTypes, get("type").value, item.type_name, "请选择类型");
  const kind = isvoroTypes.find((row) => String(row.id) === get("type").value);
  const lines = kind ? kind.children.length ? kind.children : [kind] : [];
  isvoroOptions(get("line"), lines, reset === "type" ? "" : get("line").value, item.category_name, "请选择地区 / 线路");
  const products = isvoroLines.get(get("line").value)?.products || [];
  isvoroOptions(get("product"), products, reset ? "" : get("product").value, item.product_name, "请选择套餐");
  if (animate) {
    if (reset !== "line") animateFilterResult(get("line").closest("label"));
    animateFilterResult(get("product").closest("label"));
  }
}

async function loadIsvoroDirectory(article, refresh = false) {
  article.isvoroController?.abort();
  const controller = new AbortController();
  article.isvoroController = controller;
  const get = (key) => article.querySelector(`[data-key="isvoro_${key}"]`);
  const note = article.querySelector(".isvoro-status");
  note.textContent = "正在读取 Isvoro 商品目录…";
  try {
    if (refresh) {
      const result = await requestJSON("/api/isvoro/categories", { signal: controller.signal });
      if (controller.signal.aborted || !article.isConnected) return;
      isvoroTypes = result.categories;
      renderIsvoroFields(article, "", true);
    }
    const kind = isvoroTypes.find((row) => String(row.id) === get("type").value);
    const line = (kind?.children.length ? kind.children : kind ? [kind] : []).find((row) => String(row.id) === get("line").value);
    if (line && (refresh || !isvoroLines.has(String(line.id)))) {
      const result = await requestJSON(`/api/isvoro/category/${encodeURIComponent(line.slug)}`, { signal: controller.signal });
      if (controller.signal.aborted || !article.isConnected || get("line").value !== String(line.id)) return;
      isvoroLines.set(String(line.id), result.category);
      renderIsvoroFields(article, "", true);
    }
    if (controller.signal.aborted || !article.isConnected) return;
    syncEditor(article);
    updateDraftState();
    note.textContent = line ? `目录已载入：${line.name}，请选择需要监控的套餐。目录列表不代表实时库存，请点击“测试此商品”。` : "类型目录已载入，请选择地区 / 线路。";
  } catch (error) {
    if (!controller.signal.aborted && article.isConnected) {
      note.textContent = `目录读取失败：${error.message}。已保留当前选择，可点击刷新重试；目录读取使用普通 HTTPS。`;
      setMessage(note.textContent, "error");
    }
  }
}

function soyCategory(url) {
  try {
    const query = new URL(url).searchParams;
    const fid = query.get("fid") || "", gid = query.get("gid") || "";
    return { fid, gid, preset: !fid && !gid ? "" : fid === "1" && soyGroups.some(([id]) => id === gid) ? gid : "custom" };
  } catch { return { fid: "", gid: "", preset: "custom" }; }
}

function syncSoyCategory(article, source) {
  if (article.querySelector('[data-key="adapter"]').value !== "vps_soy") return;
  const get = (key) => article.querySelector(`[data-key="soy_${key}"]`);
  if (["soy_group", "soy_fid", "soy_gid"].includes(source)) {
    if (source === "soy_group" && get("group").value === "custom") return;
    if (source === "soy_group") {
      get("fid").value = get("group").value ? "1" : "";
      get("gid").value = get("group").value;
    }
    let url;
    try { url = new URL(get("url").value); }
    catch { url = new URL("https://www.vps.soy/cart"); }
    for (const key of ["fid", "gid"]) {
      const value = get(key).value.trim();
      if (value) url.searchParams.set(key, value);
      else url.searchParams.delete(key);
    }
    get("url").value = url.href;
  }
  if (["soy_url", "soy_group", "soy_fid", "soy_gid"].includes(source)) {
    const category = soyCategory(get("url").value);
    get("fid").value = category.fid;
    get("gid").value = category.gid;
    get("group").value = category.preset;
  }
}

function productEditor(item, index) {
  const article = text("details", "", "product-editor section");
  article.dataset.id = item.id;
  article.dataset.tone = "purple";
  article.open = openProducts.has(item.id);
  article.addEventListener("toggle", () => {
    if (!article.isConnected) return;
    if (article.open) openProducts.add(item.id);
    else openProducts.delete(item.id);
  });
  const head = text("summary", "", "editor-head");
  const identity = text("span", "", "editor-identity");
  identity.append(text("strong", item.name || `新商品 ${index + 1}`), text("span", "", "editor-meta"));
  head.append(identity, text("span", "", "section-toggle"));
  head.lastElementChild.setAttribute("aria-hidden", "true");
  const body = text("div", "", "section-body");
  const actions = text("div", "", "actions");
  for (const [type, label] of [["watch", "库存监控"], ["notify", "库存通知"]]) {
    const button = text("button", "", `membership-toggle membership-${type}`); button.type = "button"; button.dataset.membership = type;
    const syncButton = () => {
      const selected = membership(item, type);
      button.textContent = selected ? `✓ 已加入${label}` : `＋ 加入${label}`;
      button.setAttribute("aria-pressed", String(selected));
      button.title = selected ? `点击移出${label}，保存配置后生效` : `点击加入${label}，保存配置后生效`;
    };
    syncButton(); button.disabled = actionRunning;
    button.addEventListener("click", () => {
      if (actionRunning) return;
      setMembership(item, type, !membership(item, type)); syncButton();
      updateProductEditorTitle(article, item); renderOverview(); updateDraftState();
      setMessage(`已${membership(item, type) ? "加入" : "移出"}${label}，保存配置后生效。`, "pending");
    });
    actions.append(button);
  }
  const test = text("button", "测试此商品", "product-test");
  test.type = "button";
  test.addEventListener("click", () => testProduct(item.id, test));
  actions.append(test);
  const stop = text("button", "停止测试", "product-test-stop danger");
  stop.type = "button";
  stop.addEventListener("click", () => stopProductTest());
  actions.append(stop);
  const remove = text("button", "移除", "danger product-remove");
  remove.type = "button";
  remove.addEventListener("click", () => {
    config.products = config.products.filter((product) => product.id !== item.id);
    openProducts.delete(item.id);
    productTests.delete(item.id);
    renderProducts();
    renderOverview();
    updateDraftState();
    setMessage("商品已移除，保存后生效。", "pending");
  });
  actions.append(remove);

  const grid = text("div", "", "form-grid");
  grid.append(
    selectField("监控适配器", item.adapter || (item.category_url ? "vmiss" : "html"), "adapter", [["html", "通用 HTTPS 网页"], ["vmiss", "VMISS WHMCS"], ["vps_soy", "VPS.SOY 商品页"], ["akile", "Akile 商品库存"], ["isvoro", "Isvoro 商品库存"], ["dmit", "DMIT 商品库存"], ["bwh", "搬瓦工商品库存"]]),
    inputField("厂商名称", item.provider || (item.adapter === "vmiss" || item.category_url ? "VMISS" : "自定义网站"), "provider", "text", "例如 ExampleHost"),
    inputField("商品名称", item.name, "name", "text", "例如 US VPS 2 GB"),
  );
  const modeWrap = text("div", "", "span-2");
  const vmissFields = text("div", "", "form-grid span-2");
  vmissFields.dataset.adapterFields = "vmiss";
  vmissFields.append(
    inputField("商品分类页 URL", item.category_url, "category_url", "url", "https://app.vmiss.com/store/example"),
    inputField("商品标识（slug / 完整名称 / product ID）", item.selector, "selector", "text", "basic 或 product44"),
    selectField("推广跳转类型", item.pid ? "pid" : "gid", "target", [["pid", "单个商品（pid）"], ["gid", "商品组（gid）"]]),
    inputField(item.pid ? "pid（商品 ID）" : "gid（商品组 ID）", item.pid || item.gid, "target_id", "text", "例如 44"),
  );
  const htmlFields = text("div", "", "form-grid span-2");
  htmlFields.dataset.adapterFields = "html";
  htmlFields.append(
    inputField("监控商品页 URL（公开 HTTPS 页面）", item.url || item.category_url, "url", "url", "https://shop.example.com/product"),
    inputField("CSS 库存元素选择器（需匹配一个元素）", item.stock_selector, "stock_selector", "text", ".stock-status"),
    selectField("库存识别方式", item.stock_mode || "quantity", "stock_mode", [["quantity", "读取精确数量"], ["availability", "有货 / 缺货状态"]]),
    inputField("购买链接（留空使用商品页）", item.purchase_url, "purchase_url", "url", "https://shop.example.com/checkout-entry"),
  );
  const quantity = inputField("数量正则（第一个捕获组为数量）", item.stock_pattern || "(\\d+)", "stock_pattern", "text", "(\\d+)" );
  quantity.dataset.stockMode = "quantity";
  const available = inputField("有货文本正则", item.available_pattern || "(?i)in stock|available|有货|现货", "available_pattern", "text");
  available.dataset.stockMode = "availability";
  const unavailable = inputField("缺货文本正则", item.unavailable_pattern || "(?i)out of stock|sold out|缺货|售罄", "unavailable_pattern", "text");
  unavailable.dataset.stockMode = "availability";
  htmlFields.append(quantity, available, unavailable);
  const soyFields = text("div", "", "form-grid span-2");
  soyFields.dataset.adapterFields = "vps_soy";
  const soyUrl = item.adapter === "vps_soy" ? item.url : "https://www.vps.soy/cart?fid=1&gid=8";
  const soy = soyCategory(soyUrl);
  soyFields.append(
    selectField("地区 / 线路（GID）", soy.preset, "soy_group", [["", "网站默认分类"], ...soyGroups.map(([id, name]) => [id, `${name}（${id}）`]), ["custom", "自定义地区 / 产品类型"]]),
    inputField("产品类型 FID（留空使用网站默认）", soy.fid, "soy_fid", "text", "例如：1"),
    inputField("地区 / 线路 GID（留空使用网站默认）", soy.gid, "soy_gid", "text", "例如：日本 SoftBank 2、深港 CNIX 8"),
    inputField("VPS.SOY 商品分类页（与 FID / GID 同步）", soyUrl, "soy_url", "url", "https://www.vps.soy/cart?fid=1&gid=2"),
    inputField("套餐完整名称（精确匹配）", item.adapter === "vps_soy" ? item.selector : "深港IX-MINI", "soy_selector", "text", "深港IX-MINI"),
    text("p", "FID 选择产品类型，GID 选择地区/线路；支持直接粘贴官网分类页网址（含 fid、gid）或编辑上方分组，双向同步。快捷分组对应当前 NAT（FID=1），后续新分组可手动填写。切换分组后需填写该页套餐完整名称，例如日本 SoftBank（GID=2）的“日本软银”；仅改商品显示名称不会切换套餐。负库存按缺货处理，未匹配会显示当前分组及本页套餐。", "hint span-2"),
  );
  const akileFields = text("div", "", "form-grid span-2");
  akileFields.dataset.adapterFields = "akile";
  const akileArea = String(item.area_id || "");
  akileFields.append(
    selectField("地区识别方式", item.area_mode || "auto", "akile_area_mode", [["auto", "自动识别地区（推荐）"], ["manual", "指定地区 ID（用于区分重名商品）"]]),
    selectField("监控地区（areaId）", akileAreas.some(([id]) => id === akileArea) ? akileArea : akileArea ? "custom" : "", "akile_area_id", [["", "请选择地区"], ...akileAreas.map(([id, name]) => [id, `${name}（${id}）`]), ["custom", "自定义地区 ID"]]),
    inputField("自定义地区 ID", akileArea, "akile_custom_area_id", "text", "填写官网新增地区的 areaId"),
    inputField("线路完整名称（精确匹配）", item.node_name || "HKL-TW", "akile_node_name", "text", "HKL-TW"),
    inputField("套餐完整名称（精确匹配）", item.adapter === "akile" ? item.selector : "HKL-TW-Mini", "akile_selector", "text", "HKL-TW-Mini"),
    text("p", "默认按线路＋套餐完整名称在实际商品目录中自动识别地区；官网切换地区后地址栏可能仍保留旧 areaId，请勿据此判断。HKL-TW 属于台湾（6），MOLite 属于澳门（14）。自动模式忽略旧地区 ID，重名时需指定地区；推荐普通 HTTPS 请求。", "hint span-2"),
  );
  const isvoroFields = text("div", "", "form-grid span-2");
  isvoroFields.dataset.adapterFields = "isvoro";
  const selectedType = String(item.type_id || "24"), selectedLine = String(item.category_id || "27"), selectedProduct = String(item.product_id || "188");
  for (const [label, key, value] of [["类型", "type", selectedType], ["地区 / 线路", "line", selectedLine], ["套餐", "product", selectedProduct]]) {
    isvoroFields.append(selectField(label, value, `isvoro_${key}`, [[value, value]]));
  }
  const refreshIsvoro = text("button", "刷新 Isvoro 目录", "");
  refreshIsvoro.type = "button";
  refreshIsvoro.addEventListener("click", () => loadIsvoroDirectory(article, true));
  isvoroFields.append(refreshIsvoro, text("p", "请按类型 → 地区/线路 → 套餐选择。切换上一级后需重新选择下一级；物理机等无子分类的类型使用自身分类。月付/年付属于购买周期，共享套餐库存，提醒入口定位到套餐。", "hint span-2"), text("p", "可点击刷新获取官网最新目录。测试按所选三个 ID 校验归属并读取实时库存，不会开启自动监控。", "hint span-2 isvoro-status"));
  const dmitFields = text("div", "", "form-grid span-2");
  dmitFields.dataset.adapterFields = "dmit";
  dmitFields.append(
    inputField("DMIT 价格 / 分类页", item.adapter === "dmit" ? item.category_url : "https://www.dmit.io/pages/pricing", "dmit_category_url", "url", "https://www.dmit.io/cart.php?gid=分类ID"),
    inputField("商品 PID（购买链接中的 pid）", item.adapter === "dmit" ? item.pid : "", "dmit_pid", "text", "填写实际商品 PID"),
    inputField("价格页套餐标识（大盘自动填写，缺货无 PID 时使用）", item.adapter === "dmit" ? item.plan_key : "", "dmit_plan_key", "text", "例如 LAX:AS3:PREMIUM:TINY"),
    text("p", "按 PID 或完整套餐标识区分地区、硬件平台和线路。缺货套餐没有 PID 时保留套餐标识，补货后自动读取购买 PID。分类页必须包含此商品；不要粘贴加购物车地址。缺少明确库存标记时显示未知。网页请求方式在“网页请求”中设置，DMIT 大盘刷新也使用已保存的商家设置。", "hint span-2"),
  );
  const bwhFields = text("div", "", "form-grid span-2");
  bwhFields.dataset.adapterFields = "bwh";
  bwhFields.append(
    inputField("搬瓦工商品页 URL", item.adapter === "bwh" ? item.category_url : "https://bwh81.net/order/ecommerce/Los%20Angeles/USCA_9", "bwh_category_url", "url", "https://bwh81.net/order/ecommerce/Los%20Angeles/USCA_9"),
    inputField("套餐标识（大盘自动填写）", item.adapter === "bwh" ? (item.bwh_offer_key || item.selector) : "", "bwh_offer_key", "text", "例如 87:USCA_9:quarterly:4999"),
    inputField("商品 PID（购买入口）", item.adapter === "bwh" ? item.pid : "", "bwh_pid", "text", "例如 87"),
    text("p", "搬瓦工使用公开 catalog.json 精确匹配商品、机房和计费周期；地区会自动显示中文。建议从 VPS 库存大盘添加，缺货商品也会保留套餐标识等待恢复。AFF 管理中的推广码会应用到带机房配置的购买入口。", "hint span-2"),
  );
  modeWrap.append(vmissFields, htmlFields, soyFields, akileFields, isvoroFields, dmitFields, bwhFields);
  const result = text("div", "", "product-test-result");
  result.setAttribute("role", "status");
  result.setAttribute("aria-live", "polite");
  body.append(actions, text("p", "单次检查使用当前商品配置，无需保存商品或开启监控；结果通过已保存的 Telegram 机器人发送（含缺货和错误），不受自动提醒开关影响。请先保存机器人 Token 和 Chat ID；停止检查不发送结果。", "hint"), result, grid, modeWrap);
  article.append(head, body);
  renderProductTest(article, item.id);
  updateProductEditorTitle(article, item);

  article.addEventListener("input", (event) => { syncSoyCategory(article, event.target.dataset.key); syncEditor(article); });
  article.addEventListener("change", (event) => {
    if (event.target.dataset.key === "adapter") article.isvoroController?.abort();
    syncSoyCategory(article, event.target.dataset.key);
    if (["isvoro_type", "isvoro_line"].includes(event.target.dataset.key)) {
      article.isvoroController?.abort();
      renderIsvoroFields(article, event.target.dataset.key === "isvoro_type" ? "type" : "line", true);
      void loadIsvoroDirectory(article);
    }
    syncEditor(article);
    updateAdapterFields(article);
    if (["adapter", "soy_group", "akile_area_mode", "akile_area_id"].includes(event.target.dataset.key)) {
      for (const fields of article.querySelectorAll("[data-adapter-fields]:not([hidden])")) animateFilterResult(fields);
    }
  });
  renderIsvoroFields(article);
  updateAdapterFields(article);
  return article;
}

function syncEditor(article) {
  const item = config.products.find((product) => product.id === article.dataset.id);
  if (!item) return;
  const get = (key) => article.querySelector(`[data-key="${key}"]`);
  item.adapter = get("adapter").value;
  item.provider = get("provider").value.trim();
  item.name = get("name").value.trim();
  if (item.adapter === "vmiss") {
    item.category_url = get("category_url").value.trim();
    item.selector = get("selector").value.trim();
    const target = get("target").value;
    item.gid = target === "gid" ? get("target_id").value.trim() : "";
    item.pid = target === "pid" ? get("target_id").value.trim() : "";
  } else if (item.adapter === "dmit") {
    item.category_url = get("dmit_category_url").value.trim();
    item.pid = get("dmit_pid").value.trim();
    item.plan_key = get("dmit_plan_key").value.trim().toUpperCase();
  } else if (item.adapter === "bwh") {
    item.category_url = get("bwh_category_url").value.trim();
    item.bwh_offer_key = get("bwh_offer_key").value.trim();
    item.selector = item.bwh_offer_key;
    item.pid = get("bwh_pid").value.trim();
  } else if (item.adapter === "vps_soy") {
    item.url = get("soy_url").value.trim();
    item.selector = get("soy_selector").value.trim();
  } else if (item.adapter === "isvoro") {
    for (const [field, key] of [["type", "type"], ["line", "category"], ["product", "product"]]) {
      item[`${key}_id`] = get(`isvoro_${field}`).value;
      item[`${key}_name`] = get(`isvoro_${field}`).selectedOptions[0]?.textContent.replace(/（[^）]*）$/, "") || "";
    }
  } else if (item.adapter === "akile") {
    item.area_mode = get("akile_area_mode").value;
    item.area_id = get("akile_area_id").value === "custom" ? get("akile_custom_area_id").value.trim() : get("akile_area_id").value;
    item.node_name = get("akile_node_name").value.trim();
    item.selector = get("akile_selector").value.trim();
  } else {
    item.url = get("url").value.trim();
    item.stock_selector = get("stock_selector").value.trim();
    item.stock_mode = get("stock_mode").value;
    item.purchase_url = get("purchase_url").value.trim();
    item.stock_pattern = get("stock_pattern").value.trim();
    item.available_pattern = get("available_pattern").value.trim();
    item.unavailable_pattern = get("unavailable_pattern").value.trim();
  }
  updateProductEditorTitle(article, item);
  if (productTests.has(item.id) && activeTest?.id !== item.id) {
    productTests.delete(item.id);
    renderProductTest(article, item.id);
  }
  renderOverview();
}

function updateProductEditorTitle(article, item) {
  article.querySelector(".editor-head strong").textContent = item.name || "未命名商品";
  article.querySelector(".editor-meta").textContent = `${item.provider || "自定义网站"} · ${item.enabled ? "参与自动监控" : "不参与自动监控"}`;
}

function renderProductTest(article, id) {
  const node = article.querySelector(".product-test-result");
  const result = productTests.get(id);
  node.hidden = !result;
  const resultRow = node.closest(".notification-result-row");
  if (resultRow) resultRow.hidden = !result;
  if (!result) { node.textContent = ""; return; }
  node.className = `product-test-result ${result.pending || result.cancelled ? "pending" : result.ok ? "success" : "error"}`;
  node.textContent = result.pending ? (result.notifying ? `${result.ok ? `库存检查完成：${stockLabel(result)}` : `测试失败：${result.error}`}。正在发送 Telegram 测试结果…` : result.stopUnconfirmed ? "停止状态尚未确认，可再次点击“停止测试”；不会因页面超时就认为后台已经停止。" : result.phase === "cleaning" ? "本地库存检查已结束，正在清理浏览器会话；清理等待最多约 10 秒，失败会明确提示。" : result.stopping ? "停止请求已提交，正在终止本次测试进程…" : "正在读取此商品库存，可点击“停止测试”，页面其他功能仍可使用。") : result.cancelled ? "测试已停止，未改动自动监控和库存历史。" : result.ok
    ? `测试成功：${stockLabel(result)}。${new Date(result.checked_at).toLocaleString()} · 耗时 ${result.duration_seconds} 秒。自动监控状态保持不变。`
    : `测试失败：${result.error}${result.checked_at ? `（${new Date(result.checked_at).toLocaleString()}）` : ""}`;
  if (result.warning) node.textContent += ` ${result.warning}`;
  if (result.matched_product) {
    const matched = result.matched_product;
    node.append(text("span", matched.type_name
      ? `实际匹配：${matched.type_name}（${matched.type_id}） → ${matched.category_name}（${matched.category_id}） → ${matched.plan_name}（${matched.product_id}）`
      : `实际匹配：${matched.area_name}（地区 ID=${matched.area_id}） · ${matched.node_name} · ${matched.plan_name}`, "test-notification"));
  }
  if (result.catalog_url) node.append(text("span", `检查分类：${result.catalog_url}`, "test-notification"));
  if (result.notification) node.append(text("span", result.notification.message, `test-notification ${result.notification.status === "sent" ? "success" : "error"}`));
}

function syncTestControls() {
  renderSeckill();
  renderCloudscraperTest();
  document.querySelector("#stock-test-bar").hidden = !activeTest;
  document.querySelector("#stock-test-status").textContent = activeTest?.notifying ? "库存检查已结束，正在发送 Telegram 结果，页面仍可操作。" : activeTest?.stopping ? "正在停止测试并释放资源…" : activeTest?.id === cloudscraperTestId ? "cloudscraper 访问测试正在运行，可停止测试，页面仍可操作。" : "商品测试正在运行，页面仍可操作。";
  updateDiscardControl();
  const globalStop = document.querySelector("#stock-test-stop");
  globalStop.disabled = Boolean(activeTest?.stopping || activeTest?.notifying);
  globalStop.textContent = activeTest?.stopUnconfirmed ? "重试停止" : "停止测试";
  document.querySelectorAll(".product-editor, .watchlist-item").forEach((article) => {
    article.querySelectorAll(".membership-toggle").forEach(button => { button.disabled = actionRunning; });
    const running = activeTest?.id === article.dataset.id;
    const test = article.querySelector(".product-test");
    test.disabled = Boolean(activeTest) || actionRunning;
    test.textContent = running ? (activeTest.notifying ? "发送结果中…" : "测试中…") : "测试通知";
    const stop = article.querySelector(".product-test-stop");
    stop.hidden = !running || Boolean(activeTest?.notifying);
    stop.disabled = Boolean(activeTest?.stopping);
    stop.textContent = activeTest?.stopping ? "停止中…" : activeTest?.stopUnconfirmed ? "重试停止" : "停止测试";
    article.querySelector(".product-remove").disabled = running || actionRunning;
    renderProductTest(article, article.dataset.id);
  });
}

async function stopProductTest() {
  const task = activeTest;
  if (!task || task.stopping || task.notifying) return;
  task.stopping = true;
  task.stopUnconfirmed = false;
  productTests.set(task.id, { pending: true, stopping: true });
  setMessage("正在停止本次测试并释放资源…", "pending");
  syncTestControls();
  try {
    // Stop immediately, even if the start response or a status request is stuck.
    const result = await requestJSON("/api/products/test/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task_id: task.taskId }), timeoutMs: 10000 });
    if (activeTest !== task) return;
    if (!result.pending) {
      task.finalResult = result;
      task.resolveStopped(result);
      task.pollController?.abort();
      task.startController?.abort();
    } else {
      if (result.notifying) { task.notifying = true; task.stopping = false; }
      productTests.set(task.id, { ...result, stopping: true });
      syncTestControls();
      clearTimeout(task.retryStopTimer);
      task.retryStopTimer = setTimeout(() => allowStopRetry(task), 10000);
    }
  } catch (error) {
    if (activeTest !== task) return;
    allowStopRetry(task);
    setMessage(`停止请求失败：${error.message} 可再次点击停止测试。`, "error");
    syncTestControls();
  }
}

function allowStopRetry(task) {
  if (activeTest !== task || task.notifying || task.finalResult) return;
  task.stopping = false;
  task.stopUnconfirmed = true;
  productTests.set(task.id, { ...productTests.get(task.id), pending: true, stopping: false, stopUnconfirmed: true });
  syncTestControls();
}

async function testProduct(id) {
  if (activeTest || actionRunning) return;
  const task = { id, taskId: crypto.randomUUID(), stopping: false };
  const seckill = id === seckillTestId;
  const probe = id === cloudscraperTestId;
  const item = seckill || probe ? null : structuredClone(config.products.find((product) => product.id === id));
  const fetcher = collectConfig().fetcher;
  task.draft = seckill || probe ? null : JSON.stringify({ product: item, fetcher });
  if (probe) task.probeDraft = JSON.stringify(fetcher.cloudscraper);
  activeTest = task;
  sessionStorage.setItem("hostwatch-stock-test", JSON.stringify({ id, taskId: task.taskId }));
  productTests.set(id, { pending: true });
  setMessage(probe ? "正在测试 cloudscraper 访问效果，可随时停止；页面其他功能仍可使用。" : "正在单次测试商品，可随时点击“停止测试”；页面其他功能仍可使用。", "pending");
  syncTestControls();
  task.startController = new AbortController();
  task.start = requestJSON("/api/products/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task_id: task.taskId, ...(probe ? { kind: "cloudscraper_probe", target: document.querySelector("#cloudscraper-test-target").value } : seckill ? { kind: "isvoro_seckill" } : { product: item }), fetcher }), timeoutMs: 15000, signal: task.startController.signal });
  await followProductTest(task, task.start);
}

function productTestStatus(task) {
  task.pollController = new AbortController();
  return requestJSON(`/api/products/test/status/${encodeURIComponent(task.taskId)}`, { timeoutMs: 10000, signal: task.pollController.signal });
}

async function followProductTest(task, initial) {
  task.stopCompleted = new Promise((resolve) => { task.resolveStopped = resolve; });
  try {
    let result;
    try { result = await Promise.race([initial, task.stopCompleted]); }
    catch (startError) {
      try { result = await Promise.race([productTestStatus(task), task.stopCompleted]); }
      catch (statusError) {
        if (statusError.status === 404) throw startError;
        result = { pending: true };
      }
    }
    while (result.pending) {
      productTests.set(task.id, { ...result, stopping: task.stopping || result.stopping, stopUnconfirmed: task.stopUnconfirmed });
      if (result.notifying) {
        task.notifying = true;
        task.stopping = false;
        productTests.set(task.id, result);
      }
      syncTestControls();
      await Promise.race([new Promise((resolve) => setTimeout(resolve, 700)), task.stopCompleted]);
      if (task.finalResult) { result = task.finalResult; break; }
      try { result = await Promise.race([productTestStatus(task), task.stopCompleted]); }
      catch (error) {
        if (error.status === 404) { result = { ok: false, error: "测试任务已不存在，服务器可能已重启" }; break; }
        if (task.stopping) allowStopRetry(task);
        setMessage(`测试状态暂时无法读取：${error.message} 尚未确认测试结束，可尝试停止或重试停止。`, "error");
        await Promise.race([new Promise((resolve) => setTimeout(resolve, 2300)), task.stopCompleted]);
        if (task.finalResult) { result = task.finalResult; break; }
      }
    }
    if (task.draft && task.draft !== JSON.stringify({ product: config.products.find((product) => product.id === task.id), fetcher: collectConfig().fetcher })) {
      result.warning = [result.warning, "此结果使用测试开始时的配置；页面已修改，请按需重新测试。"].filter(Boolean).join(" ");
    }
    if (task.id === seckillTestId) result.checked_at ||= new Date().toISOString();
    if (task.probeDraft && task.probeDraft !== JSON.stringify(collectConfig().fetcher.cloudscraper)) result.warning = "此结果使用测试开始时的 cloudscraper 参数；当前参数已修改，请按需重新测试。";
    productTests.set(task.id, result);
    const notificationIssue = result.notification && !["sent", "skipped"].includes(result.notification.status);
    const summary = result.cancelled ? "测试已停止。" : result.ok ? (task.id === cloudscraperTestId ? "cloudscraper 访问测试完成，结果已显示在网页请求中。" : task.id === seckillTestId ? "秒杀单次测试完成，结果已显示在秒杀区域。" : "单次库存测试完成，结果已显示在商品下方。") : `${task.id === cloudscraperTestId ? "cloudscraper 访问" : "单次库存"}测试失败：${result.error}`;
    setMessage([summary, result.notification?.message, result.warning].filter(Boolean).join(" "), result.warning || notificationIssue || (!result.ok && !result.cancelled) ? "error" : "success");
  } catch (error) {
    productTests.set(task.id, { ok: false, error: error.message, ...(task.id === seckillTestId ? { checked_at: new Date().toISOString() } : {}) });
    setMessage(error.message, "error");
  } finally {
    clearTimeout(task.retryStopTimer);
    task.pollController?.abort();
    task.startController?.abort();
    if (activeTest === task) activeTest = null;
    sessionStorage.removeItem("hostwatch-stock-test");
    renderProductList();
    syncTestControls();
  }
}
document.querySelector("#stock-test-stop").addEventListener("click", stopProductTest);
document.querySelector("#seckill-test").addEventListener("click", () => testProduct(seckillTestId));
document.querySelector("#seckill-stop").addEventListener("click", stopProductTest);
document.querySelector("#cloudscraper-test").addEventListener("click", () => testProduct(cloudscraperTestId));
document.querySelector("#cloudscraper-test-stop").addEventListener("click", stopProductTest);

function renderCloudscraperTest() {
  const running = activeTest?.id === cloudscraperTestId;
  const button = document.querySelector("#cloudscraper-test"), stop = document.querySelector("#cloudscraper-test-stop");
  button.disabled = Boolean(activeTest) || actionRunning;
  button.textContent = running ? "访问测试中…" : "测试 cloudscraper 访问";
  stop.hidden = !running;
  stop.disabled = Boolean(activeTest?.stopping);
  stop.textContent = activeTest?.stopUnconfirmed ? "重试停止" : activeTest?.stopping ? "停止中…" : "停止测试";
  const result = productTests.get(cloudscraperTestId), node = document.querySelector("#cloudscraper-test-result");
  node.hidden = !result;
  if (!result) return;
  node.className = `product-test-result ${result.pending || result.cancelled ? "pending" : result.ok ? "success" : "error"}`;
  node.textContent = result.pending ? (result.stopUnconfirmed ? "停止尚未确认，可再次点击停止。" : result.stopping ? "正在终止本次访问测试…" : "正在读取目标页面，可随时停止；不启动浏览器。") : result.cancelled ? "访问测试已停止，未修改配置或库存。" : !result.ok ? `访问失败：${result.error}` : [
    `${result.target_name} · HTTP ${result.status_code} · cloudscraper ${result.library_version}`,
    `耗时 ${result.duration_seconds} 秒 · 响应 ${(result.response_bytes / 1024).toFixed(1)} KB${Number.isFinite(result.peak_memory_mb) ? ` · 测试进程峰值内存 ${result.peak_memory_mb} MB` : ""}`,
    `页面标题：${result.title}`, `访问地址：${result.url}`,
    "页面读取成功；具体库存识别请继续使用商品“测试通知”。峰值内存仅指本次测试进程。",
  ].join("\n");
  if (result.warning) node.textContent += `\n${result.warning}`;
}

function renderSeckill() {
  const running = activeTest?.id === seckillTestId;
  const testButton = document.querySelector("#seckill-test"), stopButton = document.querySelector("#seckill-stop");
  testButton.disabled = Boolean(activeTest) || actionRunning;
  testButton.textContent = running ? (activeTest.notifying ? "发送结果中…" : "测试中…") : "测试秒杀通知";
  stopButton.hidden = !running || Boolean(activeTest?.notifying);
  stopButton.disabled = Boolean(activeTest?.stopping);
  stopButton.textContent = activeTest?.stopUnconfirmed ? "重试停止" : activeTest?.stopping ? "停止中…" : "停止测试";
  const testResult = productTests.get(seckillTestId), automatic = lastStatus?.seckill;
  const manual = testResult && (testResult.pending || !automatic || !testResult.checked_at || Date.parse(testResult.checked_at) >= Date.parse(automatic.checked_at));
  const result = manual ? testResult : automatic;
  const status = document.querySelector("#seckill-status");
  const changed = optionPressed("isvoro-seckill") !== Boolean(config.isvoro_seckill);
  const setting = changed ? "提醒开关有未保存修改。" : config.isvoro_seckill ? "自动提醒已开启，随监控服务运行。" : "自动提醒未开启。";
  let message = !result ? "尚无秒杀检查记录。" : result.pending ? (result.notifying ? "秒杀检查已完成，正在发送 Telegram 结果…" : "正在读取秒杀活动，可停止测试。") : result.cancelled ? "秒杀测试已停止。" : !result.ok ? `秒杀检查失败：${result.error}` : `${manual ? "单次测试" : "最近自动检查"}：${result.activities.length ? `${result.activities.length} 个活动` : "暂无秒杀活动"}。${result.checked_at ? new Date(result.checked_at).toLocaleString() : ""}`;
  if (manual && result.notification) message += ` ${result.notification.message}`;
  if (result?.warning) message += ` ${result.warning}`;
  status.textContent = setting + " " + message;
  const signature = JSON.stringify([result?.activities, collectAffiliates().isvoro]);
  const target = document.querySelector("#seckill-activities");
  if (target.dataset.signature === signature) return;
  target.dataset.signature = signature; target.replaceChildren();
  for (const campaign of (result?.activities || []).slice(0, 6)) {
    const card = text("article", "", "campaign-card");
    card.append(text("h3", `${campaign.running ? "进行中" : "即将开始"} · ${campaign.title}`));
    card.append(text("div", `${new Date(campaign.starts_at).toLocaleString()} 至 ${new Date(campaign.ends_at).toLocaleString()}`, "subtle"));
    const list = document.createElement("ul");
    for (const item of campaign.items.slice(0, 6)) list.append(text("li", `${item.name} · ${item.plan} · ${item.price} · 剩余 ${item.available} 份 · ${item.specs} · ${item.traffic}`));
    card.append(list);
    const link = text("a", "查看秒杀活动 / 手动购买", "buy-link"); link.href = affiliatePurchaseURL("https://isvoro.com/seckill", "isvoro"); link.target = "_blank"; link.rel = "noopener noreferrer"; card.append(link); target.append(card);
  }
  if ((result?.activities?.length || 0) > 6) target.append(text("p", "更多活动请前往 Isvoro 秒杀活动页查看。", "hint"));
}

function restoreProductTest() {
  if (activeTest) return;
  try {
    const saved = JSON.parse(sessionStorage.getItem("hostwatch-stock-test"));
    if (!saved?.id || !/^[a-zA-Z0-9-]{16,64}$/.test(saved.taskId)) return;
    activeTest = { ...saved, stopping: false, start: Promise.resolve() };
    productTests.set(saved.id, { pending: true });
    syncTestControls();
    void followProductTest(activeTest, productTestStatus(activeTest));
  } catch { sessionStorage.removeItem("hostwatch-stock-test"); }
}

function updateAdapterFields(article) {
  const adapter = article.querySelector('[data-key="adapter"]').value;
  article.querySelectorAll("[data-adapter-fields]").forEach((node) => {
    node.hidden = node.dataset.adapterFields !== adapter;
    node.querySelectorAll("input,select").forEach((control) => { control.disabled = node.hidden; });
  });
  const mode = article.querySelector('[data-key="stock_mode"]')?.value;
  article.querySelectorAll("[data-stock-mode]").forEach((label) => {
    label.hidden = label.dataset.stockMode !== mode;
    label.querySelector("input").disabled = label.hidden;
  });
  const areaId = article.querySelector('[data-key="akile_area_id"]');
  const manualArea = article.querySelector('[data-key="akile_area_mode"]').value === "manual";
  areaId.disabled = adapter !== "akile" || !manualArea;
  areaId.closest("label").hidden = !manualArea;
  const customArea = article.querySelector('[data-key="akile_custom_area_id"]');
  customArea.closest("label").hidden = !manualArea || areaId.value !== "custom";
  customArea.disabled = adapter !== "akile" || customArea.closest("label").hidden;
  const customSoy = article.querySelector('[data-key="soy_group"]').value === "custom";
  for (const key of ["soy_fid", "soy_gid"]) {
    const control = article.querySelector(`[data-key="${key}"]`);
    control.closest("label").hidden = !customSoy;
    control.disabled = adapter !== "vps_soy" || !customSoy;
  }
}

function editorProducts() {
  const notified = orderedProducts("notify"), ids = new Set(notified.map(item => item.id));
  return [...notified, ...config.products.filter(item => !ids.has(item.id))];
}
function syncProductEditorOrder() {
  const nodes = new Map([...editorNode.children].map(node => [node.dataset.id, node]));
  const focused = editorNode.contains(document.activeElement) ? document.activeElement : null;
  editorProducts().forEach((item, index) => {
    const node = nodes.get(item.id);
    if (node && editorNode.children[index] !== node) editorNode.insertBefore(node, editorNode.children[index] || null);
  });
  if (focused && document.activeElement !== focused) focused.focus({ preventScroll: true });
}
function renderProducts() {
  editorNode.replaceChildren(...editorProducts().map(productEditor));
  renderProductList();
  renderOverview();
  syncTestControls();
}

function stockLabel(product) {
  if (product.available <= 0) return "缺货";
  return product.stock_mode === "availability" ? "有货" : `${product.available} 件`;
}

function productMonitorLabel(item, saved) {
  if (!saved) return "新商品待保存";
  if (JSON.stringify(item) !== JSON.stringify(saved)) return "商品修改待保存";
  if (!saved.enabled) return "未参与监控";
  const active = monitorService?.active;
  if (active === "active") return config.enabled ? "监控运行中" : "暂停尚未生效（服务仍在运行）";
  if (active === "inactive") return config.enabled ? "监控服务已停止" : "监控已暂停";
  return ({ failed: "监控服务运行失败", activating: "监控服务启动中", deactivating: "监控服务停止中" })[active] || "监控服务状态未知";
}

const merchantNames = { vmiss: "VMISS", vps_soy: "VPS.SOY", akile: "Akile", isvoro: "Isvoro", dmit: "DMIT", bwh: "搬瓦工" };
const merchantHosts = { vmiss: "app.vmiss.com", vps_soy: "www.vps.soy", akile: "akile.ai", isvoro: "isvoro.com", dmit: "www.dmit.io", bwh: "bwh81.net" };
function merchantOrder() {
  return [...new Set([...(config.merchant_order || []), ...Object.keys(merchantNames)])].filter((key) => Object.hasOwn(merchantNames, key));
}
function affiliatePurchaseURL(raw, provider, item = {}, affiliates = config.affiliates || {}) {
  try {
    const url = new URL(raw);
    const acceptedHosts = provider === "bwh" ? ["bwh81.net", "www.bwh81.net", "bandwagonhost.com", "www.bandwagonhost.com"] : [merchantHosts[provider]];
    if (url.protocol !== "https:" || !acceptedHosts.includes(url.hostname) || url.username || url.password || url.port && url.port !== "443") return raw;
    const code = affiliates[provider] ?? (provider === "vmiss" ? config.affiliate_id || "" : "");
    url.searchParams.delete("aff"); url.searchParams.delete("aff_code");
    if (provider === "vmiss" && code && (item.pid || item.gid)) return `https://${merchantHosts.vmiss}/aff.php?${new URLSearchParams({ aff: code, [item.pid ? "pid" : "gid"]: item.pid || item.gid })}`;
    if (provider === "dmit" && code && (item.pid || url.searchParams.get("pid"))) return `https://${merchantHosts.dmit}/aff.php?${new URLSearchParams({ aff: code, pid: item.pid || url.searchParams.get("pid") })}`;
    if (provider === "bwh" && code) {
      const target = new URL("https://bwh81.net/aff.php"); target.searchParams.set("aff", code);
      for (const [key, value] of url.searchParams) if (key !== "a") target.searchParams.append(key, value);
      return target.href;
    }
    if (code) url.searchParams.set(provider === "akile" ? "aff_code" : "aff", code);
    return url.href;
  } catch { return raw; }
}
function collectAffiliates() {
  return Object.fromEntries([...document.querySelectorAll("[data-affiliate]")].map((input) => [input.dataset.affiliate, input.value.trim()]));
}
function collectAffiliateNotes() {
  return Object.fromEntries([...document.querySelectorAll("[data-affiliate-note]")].map((input) => [input.dataset.affiliateNote, input.value.trim()]));
}
function renderAffiliatePreviews() {
  for (const [key, raw] of Object.entries(collectAffiliates())) {
    let code = raw, valid = true;
    if (code.includes("://")) {
      try {
        const url = new URL(code), parameter = key === "akile" ? "aff_code" : "aff";
        const acceptedHosts = key === "bwh" ? ["bwh81.net", "www.bwh81.net", "bandwagonhost.com", "www.bandwagonhost.com"] : [merchantHosts[key]];
        valid = url.protocol === "https:" && acceptedHosts.includes(url.hostname) && !url.username && !url.password && (!url.port || url.port === "443") && url.searchParams.getAll(parameter).length === 1;
        code = url.searchParams.get(parameter) || "";
      } catch { valid = false; }
    }
    valid = valid && (!code || (["vmiss", "dmit", "bwh"].includes(key) ? /^\d{1,20}$/ : /^[A-Za-z0-9_-]{1,128}$/).test(code));
    const state = document.querySelector(`[data-affiliate-state="${key}"]`), link = document.querySelector(`[data-affiliate-preview="${key}"]`);
    state.textContent = !valid ? "推广码或商家链接格式无效，请修改后保存。" : code ? "已填写，保存后在购买入口添加推广参数。" : "未设置，使用普通商品入口。";
    state.className = valid ? "hint" : "hint error";
    link.hidden = !valid || !code;
    if (valid && code) {
      const entry = ["vmiss", "dmit", "bwh"].includes(key) ? `https://${merchantHosts[key]}/aff.php` : key === "akile" ? "https://akile.ai/register" : `https://${merchantHosts[key]}/`;
      link.href = affiliatePurchaseURL(entry, key, {}, { [key]: code });
      link.textContent = link.href;
    } else link.removeAttribute("href");
  }
}
function renderMerchantOrder(focusKey = null, direction = null) {
  const order = merchantOrder();
  document.querySelector("#merchant-order-list").replaceChildren(...order.map((key, index) => {
    const row = text("div", "", "merchant-order-row"), actions = text("div", "", "actions");
    row.append(text("strong", `${index + 1}. ${merchantNames[key]}`));
    for (const [offset, label] of [[-1, "上移"], [1, "下移"]]) {
      const button = text("button", label); button.type = "button"; button.dataset.orderKey = key; button.dataset.offset = offset;
      button.setAttribute("aria-label", `${label} ${merchantNames[key]}`);
      button.disabled = actionRunning || index + offset < 0 || index + offset >= order.length;
      button.addEventListener("click", () => {
        const next = merchantOrder(); [next[index], next[index + offset]] = [next[index + offset], next[index]];
        config.merchant_order = next; renderMerchantOrder(key, offset); renderProductList(); updateDraftState();
        setMessage("商家顺序已调整，点击底部“保存配置”后保存到服务器。", "pending");
      }); actions.append(button);
    }
    row.append(actions); return row;
  }));
  const tabs = document.querySelector(".catalog-merchants");
  for (const key of order) tabs.append(tabs.querySelector(`[data-catalog-provider="${key}"]`));
  const filter = document.querySelector("#watchlist-provider"), selected = filter.value || "all";
  filter.replaceChildren(new Option("全部商家", "all"), ...order.map((key) => new Option(merchantNames[key], key)), new Option("其他商家", "other")); filter.value = selected;
  const add = document.querySelector("#add-merchant"), chosen = add.value;
  for (const key of [...order, "html"]) { const option = [...add.options].find((option) => option.value === key); if (option) add.append(option); } add.value = chosen;
  if (focusKey) document.querySelector(`[data-order-key="${focusKey}"][data-offset="${direction}"]:not(:disabled)`)?.focus({ preventScroll: true });
}

function membership(item, type) {
  const explicit = "watch_enabled" in item || "notify_enabled" in item;
  return item[`${type}_enabled`] ?? (!explicit && Boolean(item.enabled));
}
function setMembership(item, type, enabled) {
  const watch = membership(item, "watch"), notify = membership(item, "notify");
  item.watch_enabled = watch; item.notify_enabled = notify;
  item[`${type}_enabled`] = enabled; item.enabled = item.watch_enabled || item.notify_enabled;
}
function productSnapshot(item, status) {
  const data = { ...(item.snapshot || {}), ...status, region: status?.region || item.snapshot?.region || item.category_name || "—" };
  // Older directory caches combined hardware and network text in one field.
  if (!data.traffic && data.specs) {
    const fields = data.specs.split(/\s*·\s*|\s+(?=(?:CPU|内存|硬盘|带宽|流量|端口|架构|网络)\s*[:：])/i);
    if (fields.length > 1) {
      const network = value => /流量|带宽|traffic|bandwidth|[GM]bps/i.test(value);
      data.specs = fields.filter(value => !network(value)).join(" · ");
      data.traffic = fields.filter(network).join(" · ");
    }
  }
  if (item.adapter === "dmit" && /^[1-9][0-9]{0,9}$/.test(String(item.pid || ""))) data.url = `https://www.dmit.io/cart.php?a=add&pid=${item.pid}`;
  // Correct older Akile snapshots without changing other vendors' zero allowances.
  if (item.adapter === "akile" && data.traffic) data.traffic = data.traffic.replace(/(^|[·@|]\s*)0(?:\.0+)?\s*GB\s*(?:流量)?(?=\s*(?:[·@|]|$))/gi, "$1无限制");
  if (item.adapter === "vps_soy" && data.specs) data.specs = soyHardware(data.specs);
  return data;
}
function soyHardware(raw) {
  const fields = [
    ["CPU", /(?:^|[\s·/])(?:CPU|vCPU|处理器)\s*[:：]\s*(\d+(?:\.\d+)?)(?:\s*(?:核|cores?|vCPU))?/i],
    ["内存", /(?:^|[\s·/])(?:内存|Memory|RAM)\s*[:：]\s*(\d+(?:\.\d+)?)\s*(Mi?B|Gi?B|M|G)\b/i],
    ["硬盘", /(?:^|[\s·/])(?:硬盘|磁盘|Storage|Disk)\s*[:：]\s*(\d+(?:\.\d+)?)\s*([KMGT]i?B)\b/i],
  ];
  const values = fields.flatMap(([label, pattern]) => {
    const match = String(raw).match(pattern);
    return match ? [`${label}：${match[1]} ${label === "CPU" ? "核" : ({ M: "MB", G: "GB" }[match[2].toUpperCase()] || match[2])}`] : [];
  });
  if (values.length) return values.join(" · ");
  // Some saved snapshots already use compact C/G/GB fields.
  return String(raw).match(/^\s*\d+(?:\.\d+)?C\s*\/\s*\d+(?:\.\d+)?G\s*\/\s*\d+(?:\.\d+)?[KMGT]i?B\b/i)?.[0].trim() || "—";
}
function compactDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "时间未知" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
function renderSpecs(cell, raw, network = false) {
  const value = raw || "—";
  const label = network ? formatNetwork(value) : formatHardware(value);
  const chip = text("span", label, network ? "network-detail" : "spec-chip"); chip.title = value; cell.append(chip);
}
function compactUnits(value) {
  return value.replace(/(\d)\s+(核|[KMGT]i?B|[KMGT]?bps)\b/gi, "$1$2").replace(/(\d)\s+核/g, "$1核");
}
function formatHardware(raw) {
  const value = String(raw || "—").split(/\s*·\s*/).join(" / ");
  const memoryG = (amount, unit) => `${Number(amount) / (/^Mi?B$/i.test(unit) ? 1024 : 1)}G`;
  return compactUnits(value
    .replace(/(?:CPU\s*[:：]?\s*)?(\d+(?:\.\d+)?)\s*(?:核|v?CPU\b|cores?\b)/gi, "$1C")
    .replace(/(?:内存|memory|RAM)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(Mi?B|Gi?B|M|G)\b/gi, (_, amount, unit) => memoryG(amount, unit.toUpperCase() === "M" ? "MB" : unit))
    .replace(/(?:硬盘|磁盘|disk|storage)\s*[:：]?\s*(?=\d)/gi, "")
    .replace(/(\d+(?:\.\d+)?)\s*(Mi?B)\b/gi, (_, amount, unit) => memoryG(amount, unit))
    .replace(/(\d+(?:\.\d+)?C\s*\/\s*)(\d+(?:\.\d+)?)\s*(Gi?B)\b/gi, (_, cpu, amount, unit) => cpu + memoryG(amount, unit))).replace(/\s*\/\s*/g, "/");
}
function formatNetwork(raw) {
  const parts = String(raw || "—").split(/\s*(?:·|@|\||\n)\s*|\s+\/\s+/).map(value => compactUnits(value
    .replace(/^(?:流量|带宽|traffic|bandwidth)\s*[:：]?\s*/i, "").replace(/(?:流量|带宽|\s+(?:traffic|bandwidth|port))$/i, "").trim())).filter(Boolean);
  // Some sites list bandwidth first; keep the display as traffic@bandwidth.
  const bandwidth = value => /[KMGT]?bps|[KMGT]?B\/s|带宽|bandwidth/i.test(value);
  return [...parts.filter(value => !bandwidth(value)), ...parts.filter(bandwidth)].join("@") || "—";
}
const currencySymbols = {
  CNY: "¥", CNH: "¥", RMB: "¥", USD: "$", CAD: "CA$", AUD: "A$", NZD: "NZ$", HKD: "HK$", SGD: "S$", TWD: "NT$",
  JPY: "JP¥", EUR: "€", GBP: "£", KRW: "₩", RUB: "₽", INR: "₹", TRY: "₺", THB: "฿", VND: "₫", IDR: "Rp", MYR: "RM",
  BRL: "R$", PHP: "₱", CHF: "Fr.",
};
// Display-only reference rates; merchant checkout remains authoritative.
const cnyRates = {
  USD: 7.2, CAD: 5.1, AUD: 4.7, NZD: 4.25, HKD: 0.92, SGD: 5.35, TWD: 0.225,
  JPY: 0.048, EUR: 7.8, GBP: 9.05, KRW: 0.0052, RUB: 0.08, INR: 0.086, TRY: 0.2,
  THB: 0.2, VND: 0.00029, IDR: 0.00045, MYR: 1.62, BRL: 1.35, PHP: 0.126, CHF: 8.1,
};
function currencyFromPrice(raw) {
  const value = String(raw || "");
  const codes = Object.keys(cnyRates).join("|");
  let match = value.match(new RegExp(`\\b(${codes})\\s*[$¥￥]?\\s*[+-]?\\d`, "i"));
  if (match) return match[1].toUpperCase();
  match = value.match(new RegExp(`[+-]?\\d[\\d,]*(?:\\.\\d+)?\\s*(${codes})\\b`, "i"));
  if (match) return match[1].toUpperCase();
  match = value.match(/(?:CA\$|A\$|NZ\$|HK\$|S\$|NT\$|JP¥|R\$|RM|Rp|Fr\.|[$€£₩₽₹₺฿₫₱])\s*[+-]?\d/i);
  if (!match) return "";
  const currency = ({ "CA$": "CAD", "A$": "AUD", "NZ$": "NZD", "HK$": "HKD", "S$": "SGD", "NT$": "TWD", "JP¥": "JPY", "R$": "BRL", RM: "MYR", "FR.": "CHF", "$": "USD", "€": "EUR", "£": "GBP", "₩": "KRW", "₽": "RUB", "₹": "INR", "₺": "TRY", "฿": "THB", "₫": "VND", "₱": "PHP" })[match[0].match(/^(CA\$|A\$|NZ\$|HK\$|S\$|NT\$|JP¥|R\$|RM|Fr\.|[$€£₩₽₹₺฿₫₱])/i)?.[1]?.toUpperCase()] || "";
  return currency === "CNY" ? "" : currency;
}
function usedCurrencies() {
  const values = [...(config.products || []), ...(lastStatus?.products || [])].flatMap(item => {
    const snapshot = lastStatus?.products?.find(product => product.id === item.id) || item.snapshot || item;
    return [snapshot.price, item.price];
  });
  return [...new Set(values.flatMap(value => String(value || "").split(/[；;]/).map(currencyFromPrice).filter(Boolean)))].sort();
}
function renderExchangeRates() {
  const timeNode = document.querySelector("#exchange-time"), noteNode = document.querySelector("#exchange-rate-note"), listNode = document.querySelector("#exchange-rate-list");
  if (!timeNode || !noteNode || !listNode) return;
  timeNode.textContent = `当前时间 ${new Date().toLocaleString("zh-CN", { hour12: false })}`;
  const currencies = usedCurrencies();
  listNode.replaceChildren(...currencies.map(currency => {
    const rate = cnyRates[currency];
    return rate ? text("span", `1 ${currency} ≈ ¥${Number(rate).toFixed(rate < 0.1 ? 4 : 2)}`, "exchange-rate-item") : null;
  }).filter(Boolean));
  if (!currencies.length) noteNode.textContent = "当前商品未使用外币价格";
  else if (exchangeRateData?.error) noteNode.textContent = exchangeRateData.error;
  else {
    const updated = exchangeRateData?.fetched_at ? new Date(Number(exchangeRateData.fetched_at) * 1000).toLocaleString("zh-CN", { hour12: false }) : "内置参考值";
    noteNode.textContent = `汇率更新 ${updated}${exchangeRateData?.stale ? " · 使用缓存" : ""}`;
  }
}
async function loadExchangeRates() {
  try {
    exchangeRateData = await requestJSON("/api/rates", { timeoutMs: 12000 });
    Object.entries(exchangeRateData.rates || {}).forEach(([code, foreignPerCny]) => {
      const value = Number(foreignPerCny);
      if (code !== "CNY" && Number.isFinite(value) && value > 0) cnyRates[code] = 1 / value;
    });
  }
  catch (error) { exchangeRateData = { rates: {}, error: `实时汇率读取失败：${error.message}` }; }
  renderExchangeRates();
  if (savedConfig !== null) {
    renderProductList();
    renderOverview();
    renderSeckill();
  }
}
const billingCycles = [
  [/(?:per\s+)?semi[- ]?annual(?:ly)?|semiannually|half[- ]?year(?:ly)?/gi, "半年"],
  [/(?:per\s+)?quarter(?:ly)?/gi, "季"],
  [/(?:per\s+)?annual(?:ly)?|year(?:ly)?|year/gi, "年"],
  [/(?:per\s+)?monthly|month|mo/gi, "月"],
  [/(?:per\s+)?weekly|week/gi, "周"],
  [/(?:per\s+)?daily|day/gi, "日"],
];
function formatBillingCycle(raw) {
  return billingCycles.reduce((value, [pattern, replacement]) => value.replace(pattern, replacement), String(raw || ""));
}
function formatPrice(raw) {
  const codes = Object.keys(currencySymbols).join("|"), amount = "([+-]?\\d[\\d,]*(?:\\.\\d+)?)";
  return String(raw || "—")
    .replace(new RegExp(`(?<![A-Za-z])(?:${codes})\\s*[$¥￥]?\\s*${amount}`, "gi"), (match, value) => currencySymbols[match.match(/[A-Za-z]+/)[0].toUpperCase()] + value)
    .replace(new RegExp(`[$¥￥]?\\s*${amount}\\s*(${codes})(?![A-Za-z])`, "gi"), (_, value, code) => currencySymbols[code.toUpperCase()] + value)
    .replace(/￥/g, "¥").replace(/([¥$€£₩₽₹₺฿₫₱])\s+(?=\d)/g, "$1").replace(/(¥\d[\d,.]*)\s*元/g, "$1");
}
function priceConversion(raw) {
  const value = String(raw || "");
  const amount = "([+-]?\\d[\\d,]*(?:\\.\\d+)?)";
  const codes = Object.keys(cnyRates).join("|");
  let match = value.match(new RegExp(`\\b(${codes})\\s*[$¥￥]?\\s*${amount}`, "i"));
  let currency = match?.[1]?.toUpperCase(), number = match?.[2];
  if (!match) {
    match = value.match(new RegExp(amount + "[ ]*(" + codes + ")", "i"));
    currency = match?.[2]?.toUpperCase(); number = match?.[1];
  }
  if (!match) {
    match = value.match(new RegExp(`(?:CA\\$|A\\$|NZ\\$|HK\\$|S\\$|NT\\$|JP¥|R\\$|RM|Fr\\.|[$€£₩₽₹₺฿₫₱])\\s*${amount}`, "i"));
    const symbol = match?.[0]?.match(/^(CA\$|A\$|NZ\$|HK\$|S\$|NT\$|JP¥|R\$|RM|Fr\.|[$€£₽₩₹₺฿₫₱])/i)?.[1]?.toUpperCase();
    const symbols = { "CA$": "CAD", "A$": "AUD", "NZ$": "NZD", "HK$": "HKD", "S$": "SGD", "NT$": "TWD", "JP¥": "JPY", "R$": "BRL", RM: "MYR", "FR.": "CHF", $: "USD", "€": "EUR", "£": "GBP", "₩": "KRW", "₽": "RUB", "₹": "INR", "₺": "TRY", "฿": "THB", "₫": "VND", "₱": "PHP" };
    currency = symbols[symbol]; number = match?.[1];
  }
  if (!currency || !number) return "";
  const cny = Number(number.replace(/,/g, "")) * cnyRates[currency];
  if (!Number.isFinite(cny)) return "";
  const cycle = formatBillingCycle(value).match(/(?:\/|per\s+)\s*(月|季|半年|年|周|日)/i)?.[1] || "";
  return `折合 ¥${cny.toFixed(1)}${cycle ? `/${cycle}` : ""}`;
}
function billingMonths(raw) {
  const value = String(raw || "").toLowerCase();
  if (/semi[- ]?annual|half[- ]?year|半年/.test(value)) return 6;
  if (/annual|year|年/.test(value)) return 12;
  if (/quarter|季/.test(value)) return 3;
  const months = value.match(/(\d+(?:\.\d+)?)\s*(?:month|个月)/);
  if (months) return Number(months[1]);
  if (/weekly|week|周/.test(value)) return 1 / 4;
  if (/daily|day|日/.test(value)) return 1 / 30;
  return /monthly|month|月/.test(value) ? 1 : null;
}
function monthlyCnyPrice(raw) {
  const amount = "([+-]?\\d[\\d,]*(?:\\.\\d+)?)", codes = Object.keys(cnyRates).join("|");
  const values = String(raw || "").split(/[；;]/).map(value => value.trim()).filter(Boolean).map(value => {
    let currency = "", number = "";
    let match = value.match(new RegExp(`\\b(${codes})\\s*[$¥￥]?\\s*${amount}`, "i"));
    if (match) { currency = match[1].toUpperCase(); number = match[2]; }
    if (!match) {
      match = value.match(new RegExp(`${amount}\\s*(${codes})\\b`, "i"));
      if (match) { number = match[1]; currency = match[2].toUpperCase(); }
    }
    if (!match) {
      match = value.match(new RegExp(`(CA\\$|A\\$|NZ\\$|HK\\$|S\\$|NT\\$|JP¥|R\\$|RM|Fr\\.|[$€£₩₽₹₺฿₫₱¥￥])\\s*${amount}`, "i"));
      const symbols = { "CA$": "CAD", "A$": "AUD", "NZ$": "NZD", "HK$": "HKD", "S$": "SGD", "NT$": "TWD", "JP¥": "JPY", "R$": "BRL", RM: "MYR", "FR.": "CHF", $: "USD", "€": "EUR", "£": "GBP", "₩": "KRW", "₽": "RUB", "₹": "INR", "₺": "TRY", "฿": "THB", "₫": "VND", "₱": "PHP", "¥": "CNY", "￥": "CNY" };
      if (match) { currency = symbols[match[1].toUpperCase()] || ""; number = match[2]; }
    }
    const months = billingMonths(value), rate = currency === "CNY" ? 1 : cnyRates[currency];
    const parsed = Number(String(number).replace(/,/g, ""));
    return currency && Number.isFinite(rate) && Number.isFinite(parsed) && parsed >= 0 && months > 0 ? parsed * rate / months : null;
  }).filter(value => Number.isFinite(value));
  return values.length ? Math.min(...values) : null;
}
function sortByMonthlyPrice(items, mode, getPrice) {
  if (!mode || mode === "default") return items;
  const direction = mode === "desc" ? -1 : 1;
  return items.map((item, index) => ({ item, index, price: monthlyCnyPrice(getPrice(item)) })).sort((left, right) => {
    if (left.price === null && right.price === null) return left.index - right.index;
    if (left.price === null) return 1;
    if (right.price === null) return -1;
    const result = left.price - right.price;
    return result ? result * direction : left.index - right.index;
  }).map(entry => entry.item);
}
function priceTextWithConversion(raw) {
  return String(raw || "—").split(/[；;]/).map(value => value.trim()).filter(Boolean).map(value => {
    const original = formatBillingCycle(formatPrice(value));
    const converted = priceConversion(value);
    return `${original}${converted ? `\n${converted}` : ""}`;
  }).join("；") || "—";
}
function lineSummary(item) {
  const values = [item.line_telecom, item.line_unicom, item.line_mobile].map(value => String(value || "").trim()).filter(Boolean);
  const unique = [...new Set(values.map(value => value.toLowerCase()))];
  const label = values.length === 3 && unique.length === 1 ? values[0] : values.length ? "多线路" : "待填写";
  return `三网 · ${label}`;
}
function renderLineEditor(cell, item) {
  const summary = text("div", "", "line-summary");
  summary.append(text("span", "线路总结", "line-summary-label"), text("strong", lineSummary(item), "line-summary-value"));
  cell.append(summary);
  const lines = text("div", "", "carrier-lines");
  for (const [key, label, name] of [["telecom", "电", "电信"], ["unicom", "联", "联通"], ["mobile", "移", "移动"]]) {
    const field = `line_${key}`, wrap = text("label", "", `carrier-line ${key}`), input = document.createElement("input");
    const value = text("span", "", "carrier-value"), measure = text("span", "", "carrier-measure");
    measure.setAttribute("aria-hidden", "true");
    wrap.append(text("span", `${label}:`)); input.className = "carrier-input"; input.value = item[field] || "";
    input.placeholder = "填写线路"; input.maxLength = 500; input.disabled = actionRunning;
    input.setAttribute("aria-label", `${item.name} ${name}线路`);
    const fit = () => { measure.textContent = input.value || input.placeholder; input.title = input.value; };
    fit();
    input.addEventListener("input", () => { fit(); item[field] = input.value; updateDraftState(); });
    value.append(measure, input); wrap.append(value); lines.append(wrap);
  }
  if (item.line_label) {
    const legacy = text("div", "", "legacy-line"), input = document.createElement("input");
    legacy.append(text("span", "原线路：")); input.className = "manual-field"; input.value = item.line_label; input.maxLength = 500;
    input.setAttribute("aria-label", `${item.name} 原线路`); input.disabled = actionRunning;
    input.addEventListener("input", () => { item.line_label = input.value; updateDraftState(); }); legacy.append(input); lines.append(legacy);
  }
  cell.append(lines);
}
function expandedPrices(target) {
  return new Set([...target.querySelectorAll(".price-options[open]")].map(detail => detail.closest("[data-id]")?.dataset.id));
}
function renderPrice(cell, raw, expanded = false) {
  const prices = String(raw || "—").split(/[；;]/).map(p => p.trim()).filter(Boolean);
  const appendPrice = (target, price, className = "price-main") => {
    const stack = text("span", "", "price-stack");
    stack.append(text("span", formatBillingCycle(formatPrice(price)), className));
    const converted = priceConversion(price);
    if (converted) stack.append(text("span", converted, "price-converted"));
    target.append(stack);
  };
  if (prices.length < 2) { appendPrice(cell, prices[0]); return; }
  const details = text("details", "", "price-options"), summary = text("summary", "", "price-main");
  details.open = expanded;
  summary.title = "展开其他计费周期"; appendPrice(summary, prices[0]); details.append(summary);
  prices.slice(1).forEach(price => { const row = text("div"); appendPrice(row, price, "price-option-main"); details.append(row); }); cell.append(details);
}
function moreActions(id, name, buttons) {
  const wrap = text("div", "", "more-actions"), trigger = text("button", "更多 ⋯"), panel = text("div", "", "action-popover");
  panel.id = `product-actions-${id}`; panel.popover = "auto"; panel.setAttribute("role", "group"); panel.setAttribute("aria-label", `${name} 的更多操作`);
  trigger.type = "button"; trigger.setAttribute("aria-label", `${name} 更多操作`); trigger.setAttribute("aria-expanded", "false"); trigger.dataset.menuTrigger = "";
  trigger.setAttribute("popovertarget", panel.id); trigger.disabled = actionRunning;
  trigger.addEventListener("click", () => {
    const rect = trigger.getBoundingClientRect();
    panel.style.left = `${Math.max(12, Math.min(rect.right - 175, innerWidth - 187))}px`;
    panel.style.top = `${Math.max(12, Math.min(rect.bottom + 5, innerHeight - buttons.length * 38 - 24))}px`;
  });
  panel.addEventListener("toggle", event => {
    trigger.setAttribute("aria-expanded", String(event.newState === "open"));
    if (event.newState === "closed" && panel.isConnected) requestAnimationFrame(() => {
      if (!panel.isConnected || actionRunning) return;
      const restoreFocus = document.activeElement === trigger;
      renderProductList();
      if (restoreFocus) document.getElementById(panel.id)?.previousElementSibling?.focus({ preventScroll: true });
    });
  });
  buttons.forEach(button => { button.addEventListener("click", () => { if (panel.matches(":popover-open")) panel.hidePopover(); }, { capture: true }); panel.append(button); });
  wrap.append(trigger, panel); return wrap;
}
function closeActionMenus() { document.querySelectorAll(".action-popover:popover-open").forEach(panel => panel.hidePopover()); }
window.addEventListener("resize", closeActionMenus);
window.addEventListener("scroll", closeActionMenus, { passive: true });
window.addEventListener("hostwatch-page-change", closeActionMenus);

function orderedProducts(type) {
  const ranks = new Map((config[`${type}_order`] || []).map((id, index) => [id, index]));
  const merchants = merchantOrder();
  return config.products.filter(item => membership(item, type)).sort((a, b) =>
    (ranks.get(a.id) ?? Infinity) - (ranks.get(b.id) ?? Infinity) ||
    (merchants.indexOf(a.adapter) + 1 || 99) - (merchants.indexOf(b.adapter) + 1 || 99));
}
function restoreProductSortFocus(target, id) {
  if (id) target.querySelector(`[data-sort-id="${CSS.escape(id)}"]`)?.focus({ preventScroll: true });
}
function commitProductOrder(type, visibleIds, focusId) {
  closeActionMenus();
  const selected = new Set(visibleIds);
  let index = 0;
  // Filtered-out products keep their positions; only visible slots are reordered.
  config[`${type}_order`] = orderedProducts(type).map(item => selected.has(item.id) ? visibleIds[index++] : item.id);
  renderProductList(); updateDraftState();
  const target = type === "watch" ? listNode : document.querySelector("#notification-list");
  restoreProductSortFocus(target, focusId);
  document.querySelector("#product-sort-status").textContent = "商品顺序已调整，请保存配置；两个列表分别保存顺序。";
}
function finishProductDrag(cancelled = false) {
  const drag = productDrag;
  if (!drag) return;
  productDrag = null;
  cancelAnimationFrame(drag.frame);
  drag.group.classList.remove("is-dragging"); document.body.classList.remove("sorting-products");
  if (drag.container.hasPointerCapture(drag.pointerId)) drag.container.releasePointerCapture(drag.pointerId);
  const ids = [...drag.container.querySelectorAll(".sortable-product")].map(node => node.dataset.id);
  if (!cancelled && drag.started && ids.some((id, i) => id !== drag.original[i])) commitProductOrder(drag.type, ids, drag.id);
  else if (drag.started) { renderProductList(); restoreProductSortFocus(drag.type === "watch" ? listNode : document.querySelector("#notification-list"), drag.id); }
}
function cancelProductDrag() { finishProductDrag(true); }
function placeDraggedProduct() {
  const drag = productDrag;
  if (!drag?.started) return;
  const next = [...drag.container.querySelectorAll(".sortable-product")].find(node => {
    if (node === drag.group) return false;
    const rect = node.getBoundingClientRect(); return drag.y < rect.top + rect.height / 2;
  });
  if (next) { if (drag.group.nextElementSibling !== next) drag.container.insertBefore(drag.group, next); }
  else if (drag.container.lastElementChild !== drag.group) drag.container.append(drag.group);
}
function scrollProductDrag() {
  const drag = productDrag;
  if (!drag?.started) return;
  const edge = 80, speed = drag.y < edge ? -Math.min(16, (edge - drag.y) / 4) : drag.y > innerHeight - edge ? Math.min(16, (drag.y - innerHeight + edge) / 4) : 0;
  if (speed) { window.scrollBy(0, speed); placeDraggedProduct(); }
  drag.frame = requestAnimationFrame(scrollProductDrag);
}
function productSortHandle(item, type, group, count) {
  group.classList.add("sortable-product");
  const handle = text("button", "⠿", "sort-handle"); handle.type = "button"; handle.dataset.sortId = item.id;
  handle.disabled = actionRunning || count < 2;
  handle.setAttribute("aria-label", `${item.name} 拖动排序`);
  handle.title = "上下拖动调整顺序；也可聚焦后按 ↑ / ↓，拖动时按 Esc 取消";
  handle.addEventListener("pointerdown", event => {
    if (event.button !== 0 || !event.isPrimary || actionRunning || productDrag) return;
    productDrag = { handle, group, type, id: item.id, pointerId: event.pointerId, y: event.clientY, startY: event.clientY, container: group.parentElement, original: [...group.parentElement.querySelectorAll(".sortable-product")].map(node => node.dataset.id), started: false };
    // Capture on the stationary parent so moving a row never loses the pointer.
    productDrag.container.setPointerCapture(event.pointerId); handle.focus({ preventScroll: true }); closeActionMenus();
  });
  handle.addEventListener("keydown", event => {
    if (actionRunning || productDrag || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    const ids = [...group.parentElement.querySelectorAll(".sortable-product")].map(node => node.dataset.id), from = ids.indexOf(item.id), to = from + (event.key === "ArrowUp" ? -1 : 1);
    if (to < 0 || to >= ids.length) return;
    [ids[from], ids[to]] = [ids[to], ids[from]]; commitProductOrder(type, ids, item.id);
  });
  return handle;
}

function compareLineSummary(item) {
  const lines = [["电信", item.line_telecom], ["联通", item.line_unicom], ["移动", item.line_mobile]]
    .filter(([, value]) => value).map(([label, value]) => `${label}：${value}`);
  return lines.length ? lines : item.line_label ? [item.line_label] : ["—"];
}

function renderCompareBar() {
  const bar = document.querySelector("#watch-compare-bar");
  if (!bar) return;
  const count = compareSelection.size;
  bar.hidden = count === 0;
  document.querySelector("#watch-compare-count").textContent = `已选 ${count} 项`;
  document.querySelector("#watch-compare-note").textContent = count < 2 ? "再选择至少 1 个商品开始参数对比" : "仅用于临时对比，不会修改监控配置";
  const start = document.querySelector("#watch-compare-start");
  start.disabled = count < 2 || actionRunning;
  document.querySelector("#watch-compare-clear").disabled = actionRunning;
}

function renderCompareContent() {
  const target = document.querySelector("#watch-compare-content");
  if (!target) return;
  const latest = new Map((lastStatus?.products || []).map(p => [p.id, p]));
  const products = config.products.filter(item => compareSelection.has(item.id) && membership(item, "watch"));
  if (products.length < 2) {
    target.replaceChildren(text("p", "请选择至少 2 个库存监控商品。", "empty"));
    return;
  }
  const table = text("table", "", "watch-compare-table");
  const head = document.createElement("thead"), headRow = document.createElement("tr");
  headRow.append(text("th", "对比维度"));
  products.forEach(item => {
    const th = document.createElement("th");
    th.append(text("strong", item.name || "未命名商品"), text("small", item.provider || item.adapter));
    headRow.append(th);
  });
  head.append(headRow); table.append(head);
  const body = document.createElement("tbody");
  const rows = [
    ["产品名称", item => productSnapshot(item, latest.get(item.id)).name || item.name],
    ["地区", item => productSnapshot(item, latest.get(item.id)).region || "—"],
    ["备注", item => item.remarks || "—"],
    ["线路", item => compareLineSummary(item)],
    ["硬件配置", item => formatHardware(productSnapshot(item, latest.get(item.id)).specs)],
    ["流量 / 带宽", item => formatNetwork(productSnapshot(item, latest.get(item.id)).traffic)],
    ["价格", item => priceTextWithConversion(productSnapshot(item, latest.get(item.id)).price)],
    ["状态", item => {
      const p = productSnapshot(item, latest.get(item.id));
      return { value: Number.isInteger(p.available) ? stockLabel(p) : "未知", tone: !Number.isInteger(p.available) ? "pending" : p.available > 0 ? "ok" : "error" };
    }],
    ["快速购买", item => {
      const p = productSnapshot(item, latest.get(item.id)), raw = p.url || item.purchase_url;
      return raw && /^https:\/\//i.test(raw) ? affiliatePurchaseURL(raw, item.adapter, item) : "";
    }],
  ];
  rows.forEach(([label, getter]) => {
    const row = document.createElement("tr"); row.append(text("th", label));
    products.forEach(item => {
      const cell = document.createElement("td"), value = getter(item);
      if (label === "线路" && Array.isArray(value)) {
        cell.append(...value.map(line => text("span", line, "compare-line")));
      } else if (label === "状态") {
        cell.append(text("span", value.value, `state ${value.tone}`));
      } else if (label === "快速购买" && value) {
        const link = text("a", "直接购买", "buy-link"); link.href = value; link.target = "_blank"; link.rel = "noopener noreferrer"; cell.append(link);
      } else {
        cell.textContent = value || "—";
      }
      row.append(cell);
    });
    body.append(row);
  });
  table.append(body);
  const wrap = text("div", "", "watch-compare-table-wrap"); wrap.append(table); target.replaceChildren(wrap);
}

function openCompareDialog() {
  if (compareSelection.size < 2) return;
  const dialog = document.querySelector("#watch-compare-dialog");
  renderCompareContent();
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeCompareDialog() {
  const dialog = document.querySelector("#watch-compare-dialog");
  if (dialog?.open && typeof dialog.close === "function") dialog.close();
  else dialog?.removeAttribute("open");
}

function updateCompareSelection(id, checked) {
  if (checked) compareSelection.add(id); else compareSelection.delete(id);
  renderCompareBar();
}

window.addEventListener("pointermove", event => {
  const drag = productDrag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  drag.y = event.clientY;
  if (!drag.started && Math.abs(drag.y - drag.startY) < 5) return;
  event.preventDefault();
  if (!drag.started) { drag.started = true; drag.group.classList.add("is-dragging"); document.body.classList.add("sorting-products"); scrollProductDrag(); }
  placeDraggedProduct();
}, { passive: false });
window.addEventListener("pointerup", event => { if (productDrag?.pointerId === event.pointerId) finishProductDrag(); });
window.addEventListener("pointercancel", event => { if (productDrag?.pointerId === event.pointerId) cancelProductDrag(); });
window.addEventListener("lostpointercapture", event => { if (productDrag?.container === event.target) cancelProductDrag(); });
window.addEventListener("keydown", event => { if (event.key === "Escape" && productDrag) { event.preventDefault(); cancelProductDrag(); } });
window.addEventListener("blur", cancelProductDrag);
window.addEventListener("hostwatch-page-change", cancelProductDrag);

function renderProductList() {
  if (productDrag) return;
  renderNotificationList();
  // Keep a focused manual editor alive during status polling.
  if (listNode.querySelector(".action-popover:popover-open") || listNode.contains(document.activeElement) && document.activeElement.matches("input")) return;
  const focusedSortId = listNode.querySelector(".sort-handle:focus")?.dataset.sortId;
  const expanded = expandedPrices(listNode);
  const latest = new Map((lastStatus?.products || []).map(p => [p.id, p]));
  const watched = orderedProducts("watch");
  const watchedIds = new Set(watched.map(item => item.id));
  for (const id of compareSelection) if (!watchedIds.has(id)) compareSelection.delete(id);
  const search = document.querySelector("#watchlist-search").value.trim().toLowerCase();
  const provider = document.querySelector("#watchlist-provider").value, stock = document.querySelector("#watchlist-stock").value;
  const visible = sortByMonthlyPrice(watched.filter(item => {
    const p = productSnapshot(item, latest.get(item.id));
    return (!search || [item.name, item.provider, p.region, item.remarks, item.line_label, item.line_telecom, item.line_unicom, item.line_mobile].join(" ").toLowerCase().includes(search)) &&
      (provider === "all" || provider === item.adapter || provider === "other" && !merchantNames[item.adapter]) &&
      (stock === "all" || stock === "in" && p.available > 0 || stock === "out" && p.available === 0 || stock === "unknown" && !Number.isInteger(p.available));
  }), document.querySelector("#watchlist-price-sort").value, item => productSnapshot(item, latest.get(item.id)).price);
  document.querySelector("#watchlist-count").textContent = `共 ${watched.length} 个 · 显示 ${visible.length} 个`;
  document.querySelector("#watchlist-note").textContent = "此列表仅检查和展示库存，不发送消息。加入库存通知后才会推送。后台检查更新商品数据，暂停后保留历史快照。";
  renderCompareBar();
  if (!visible.length) { listNode.replaceChildren(text("div", watched.length ? "没有符合条件的商品。" : "从库存大盘点击“加入库存监控”，添加关心的商品。", "empty")); return; }
  const wrap = text("div", "", "catalog-table-wrap"); wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "关注商品库存表");
  const table = text("table", "", "watch-table"), head = document.createElement("thead"), tr = document.createElement("tr");
  for (const label of ["对比", "商家", "产品名称", "地区", "备注", "线路", "硬件配置", "流量 / 带宽", "价格", "状态", "操作"]) { const th = text("th", label); th.scope = "col"; tr.append(th); }
  head.append(tr); const body = document.createElement("tbody");
  for (const item of visible) {
    const runtime = latest.get(item.id), p = productSnapshot(item, runtime), row = document.createElement("tr"); row.dataset.id = item.id;
    const cell = (value = "", cls = "") => { const td = text("td", value, cls); row.append(td); return td; };
    const compareCell = cell("", "compare-cell");
    const compare = document.createElement("input"); compare.type = "checkbox"; compare.className = "compare-check"; compare.checked = compareSelection.has(item.id); compare.disabled = actionRunning; compare.setAttribute("aria-label", `选择 ${item.name} 进行对比`);
    compare.addEventListener("change", () => updateCompareSelection(item.id, compare.checked)); compareCell.append(compare);
    const merchant = text("div", "", "merchant-sort"); merchant.append(productSortHandle(item, "watch", row, visible.length), text("span", item.provider)); cell().append(merchant);
    const identity = cell(); identity.append(text("strong", p.name || item.name));
    const time = runtime ? runtime.checked_at || lastStatus?.last_checked : p.checked_at;
    const updated = text("small", time ? `读取 ${compactDate(time)}` : "等待首次读取"); updated.title = time ? new Date(time).toLocaleString() : "尚无商品快照"; identity.append(updated); cell(p.region || "—");
    for (const [key, label] of [["remarks", "备注"]]) {
      const input = document.createElement("input"); input.className = "manual-field"; input.value = item[key] || ""; input.maxLength = 500; input.placeholder = "管理员填写";
      input.setAttribute("aria-label", `${item.name} ${label}`); input.disabled = actionRunning;
      input.addEventListener("input", () => { item[key] = input.value; updateDraftState(); }); cell().append(input);
    }
    renderLineEditor(cell(), item);
    renderSpecs(cell("", "watch-specs"), p.specs); renderSpecs(cell("", "watch-specs"), p.traffic, true); renderPrice(cell("", "watch-price"), p.price, expanded.has(item.id));
    cell().append(text("span", Number.isInteger(p.available) ? stockLabel(p) : "未知", `state ${!Number.isInteger(p.available) ? "pending" : p.available > 0 ? "ok" : "error"}`));
    const actions = text("div", "", "watch-operations");
    const raw = p.url || item.purchase_url;
    if (raw && /^https:\/\//i.test(raw)) { const link = text("a", "直接购买", "buy-link"); link.href = affiliatePurchaseURL(raw, item.adapter, item); link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link); }
    else actions.append(text("small", "等待购买链接"));
    const notifyActive = membership(item, "notify");
    const notify = text("button", notifyActive ? "✓ 已加入库存通知" : "＋ 加入库存通知", "membership-toggle membership-notify"); notify.type = "button"; notify.disabled = actionRunning || notifyActive; notify.setAttribute("aria-pressed", String(notifyActive));
    notify.addEventListener("click", () => { setMembership(item, "notify", true); renderProducts(); updateDraftState(); setMessage("已加入库存通知，保存配置后生效。", "pending"); });
    const remove = text("button", "移出监控", "danger"); remove.type = "button"; remove.disabled = actionRunning;
    remove.addEventListener("click", () => { setMembership(item, "watch", false); renderProducts(); updateDraftState(); setMessage("已移出库存监控，通知选择保持原样，请保存配置。", "pending"); });
    actions.append(moreActions(`watch-${item.id}`, item.name, [notify, remove])); cell().append(actions); body.append(row);
  }
  table.append(head, body); wrap.append(table); listNode.replaceChildren(wrap);
  renderCompareBar();
  if (document.querySelector("#watch-compare-dialog")?.open) renderCompareContent();
  restoreProductSortFocus(listNode, focusedSortId);
}

function renderNotificationList() {
  if (productDrag) return;
  syncProductEditorOrder();
  if (document.querySelector("#notification-list .action-popover:popover-open")) return;
  const focusedSortId = document.querySelector("#notification-list .sort-handle:focus")?.dataset.sortId;
  const expanded = expandedPrices(document.querySelector("#notification-list"));
  const statusProducts = new Map((lastStatus?.products || []).map(p => [p.id, p]));
  const savedProducts = new Map((savedConfig ? JSON.parse(savedConfig).products : []).map(p => [p.id, p]));
  const selected = sortByMonthlyPrice(orderedProducts("notify"), document.querySelector("#notification-price-sort").value, item => {
    const runtime = statusProducts.get(item.id), testResult = productTests.get(item.id);
    const source = testResult?.ok && !testResult.pending && !testResult.cancelled && !testResult.warning && (!runtime || Date.parse(testResult.checked_at) > Date.parse(runtime.checked_at || lastStatus?.last_checked || 0)) ? { ...runtime, ...testResult, error: "", url: testResult.url || testResult.purchase_url } : runtime;
    return productSnapshot(item, source).price;
  });
  document.querySelector("#notification-count").textContent = `共 ${selected.length} 个`;
  document.querySelector("#notification-note").textContent = "商品信息来自商家快照、后台检查或最近一次成功测试。测试通知可在暂停时执行；下架或未匹配商品保留配置并显示未知，可在高级中手动添加及修改。";
  const target = document.querySelector("#notification-list");
  if (!selected.length) { target.replaceChildren(text("div", "尚未选择通知商品，可从大盘、库存监控或下方高级管理添加。", "empty")); return; }
  const wrap = text("div", "", "catalog-table-wrap"); wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "通知商品库存表");
  const table = text("table", "", "watch-table notification-table"), head = document.createElement("thead"), headings = document.createElement("tr");
  for (const label of ["商家", "产品名称", "地区", "硬件配置", "流量 / 带宽", "价格", "状态", "操作"]) { const th = text("th", label); th.scope = "col"; headings.append(th); }
  head.append(headings); table.append(head);
  for (const item of selected) {
    const runtime = statusProducts.get(item.id), testResult = productTests.get(item.id);
    const successfulTest = testResult?.ok && !testResult.pending && !testResult.cancelled && !testResult.warning;
    const testIsLatest = successfulTest && (!runtime || Date.parse(testResult.checked_at) > Date.parse(runtime.checked_at || lastStatus?.last_checked || 0));
    const source = testIsLatest ? { ...runtime, ...testResult, error: "", url: testResult.url || testResult.purchase_url, region: testResult.region || testResult.matched_product?.area_name || runtime?.region } : runtime;
    const p = productSnapshot(item, source);
    const body = text("tbody", "", "watchlist-item"); body.dataset.id = item.id;
    const row = document.createElement("tr");
    const cell = (value = "", cls = "") => { const td = text("td", value, cls); row.append(td); return td; };
    const merchant = text("div", "", "merchant-sort"); merchant.append(productSortHandle(item, "notify", body, selected.length), text("span", item.provider)); cell().append(merchant);
    const identity = cell(); identity.append(text("strong", p.name || item.name));
    const savedItem = savedProducts.get(item.id);
    if (!savedItem || JSON.stringify(item) !== JSON.stringify(savedItem)) identity.append(text("small", "修改待保存", "draft-note"));
    const time = testIsLatest ? testResult.checked_at : runtime ? runtime.checked_at || lastStatus?.last_checked : p.checked_at;
    const updated = text("small", time ? `${testIsLatest ? "测试" : "读取"} ${compactDate(time)}` : "等待首次读取"); updated.title = time ? new Date(time).toLocaleString() : "尚无商品快照"; identity.append(updated);
    cell(p.region || "—"); renderSpecs(cell("", "watch-specs"), p.specs); renderSpecs(cell("", "watch-specs"), p.traffic, true); renderPrice(cell("", "watch-price"), p.price, expanded.has(item.id));
    const stock = cell();
    stock.append(text("span", Number.isInteger(p.available) ? stockLabel(p) : "未知", `state ${!Number.isInteger(p.available) ? "pending" : p.available > 0 ? "ok" : "error"}`));
    if (p.error) stock.append(text("small", p.error, "error"));
    const actions = text("div", "", "watch-operations");
    const raw = p.url || item.purchase_url;
    if (raw && /^https:\/\//i.test(raw)) { const link = text("a", "直接购买", "buy-link"); link.href = affiliatePurchaseURL(raw, item.adapter, item); link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link); }
    const test = text("button", "测试通知", "product-test"); test.type = "button"; test.addEventListener("click", () => testProduct(item.id));
    const stop = text("button", "停止测试", "product-test-stop danger"); stop.type = "button"; stop.addEventListener("click", stopProductTest);
    const edit = text("button", "编辑商品"); edit.type = "button"; edit.disabled = actionRunning;
    edit.addEventListener("click", () => { openProducts.add(item.id); renderProducts(); openAdvancedProducts(); editorNode.querySelector(`[data-id="${CSS.escape(item.id)}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" }); });
    const remove = text("button", "移出通知", "danger product-remove"); remove.type = "button";
    remove.addEventListener("click", () => { if (actionRunning) return; setMembership(item, "notify", false); renderProducts(); updateDraftState(); setMessage("已移出库存通知，库存监控选择保留，请保存配置。", "pending"); });
    actions.append(test, stop, moreActions(`notify-${item.id}`, item.name, [edit, remove])); cell().append(actions); body.append(row);
    const resultRow = text("tr", "", "notification-result-row"), resultCell = document.createElement("td"); resultCell.colSpan = 8;
    resultCell.append(text("div", "", "product-test-result")); resultRow.append(resultCell); body.append(resultRow); table.append(body);
  }
  wrap.append(table); target.replaceChildren(wrap); syncTestControls();
  restoreProductSortFocus(target, focusedSortId);
}

function renderOverview() {
  const enabled = config.products.filter((item) => item.enabled);
  const vendors = new Set(enabled.map((item) => item.provider).filter(Boolean));
  const latest = new Map((lastStatus?.products || []).map((product) => [product.id, product]));
  const inStock = enabled.filter((item) => (latest.get(item.id)?.available || 0) > 0).length;
  document.querySelector("#overview-total").textContent = String(config.products.length);
  document.querySelector("#overview-total-note").textContent = `${vendors.size} 个商家 · ${config.products.filter(p => membership(p, "watch")).length} 个监控 / ${config.products.filter(p => membership(p, "notify")).length} 个通知`;
  document.querySelector("#overview-stock").textContent = String(inStock);
  document.querySelector("#overview-interval").textContent = intervalLabel(config.interval_seconds ?? 600);
  renderOverviewRecent();
  renderMonitorState();
}

function renderOverviewRecent() {
  const target = document.querySelector("#overview-recent");
  const expanded = expandedPrices(target);
  const latest = new Map((lastStatus?.products || []).map(p => [p.id, p]));
  const products = config.products.filter(item => item.enabled).slice(0, 6);
  if (!products.length) { target.replaceChildren(text("div", "还没有关注的商品，从库存大盘加入后将在这里显示。", "empty")); return; }
  const wrap = text("div", "", "catalog-table-wrap"), table = text("table", "", "overview-table"), head = document.createElement("thead"), heading = document.createElement("tr");
  wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "关注商品最近库存");
  ["商品", "地区", "价格", "库存", "关注方式"].forEach(label => { const th = text("th", label); th.scope = "col"; heading.append(th); }); head.append(heading); table.append(head);
  const body = document.createElement("tbody");
  products.forEach(item => {
    const p = productSnapshot(item, latest.get(item.id)), row = document.createElement("tr"); row.dataset.id = item.id;
    const cell = (value = "", cls = "") => { const td = text("td", value, cls); row.append(td); return td; };
    const identity = cell(); identity.append(text("strong", p.name || item.name), text("small", item.provider));
    cell().textContent = p.region || "—"; renderPrice(cell("", "overview-price"), p.price, expanded.has(item.id));
    cell().append(text("span", Number.isInteger(p.available) ? stockLabel(p) : "未知", `state ${!Number.isInteger(p.available) ? "pending" : p.available > 0 ? "ok" : "error"}`));
    const tags = cell(); if (membership(item, "watch")) tags.append(text("span", "库存监控", "membership-tag")); if (membership(item, "notify")) tags.append(text("span", "TG 通知", "membership-tag notify"));
    body.append(row);
  }); table.append(body); wrap.append(table); target.replaceChildren(wrap);
}

function renderMonitorState() {
  const active = monitorService?.active;
  const transitionTimedOut = monitorService?.transition_timeout;
  const labels = { active: "监控服务运行中", inactive: "监控服务已停止", failed: "监控服务运行失败", activating: transitionTimedOut ? "监控服务启动超时" : "监控服务启动中", deactivating: transitionTimedOut ? "监控服务停止超时" : "监控服务停止中" };
  const label = labels[active] || "监控服务状态未知";
  let note = config.enabled ? "已保存：开启自动监控。" : "已保存：暂停自动监控。";
  if (!savedConfig) note = "正在读取配置。";
  if (!monitorService) note += "请刷新状态核对服务器实际运行情况。";
  else if (active === "active") note += config.enabled ? "服务已启动，库存检查是否成功请查看最近检查结果。" : "暂停尚未生效，服务仍在运行，请重新暂停监控。";
  else if (active === "activating" && transitionTimedOut) note += "超过 60 秒仍未确认启动；请刷新状态并查看服务日志，必要时重试启动。";
  else if (active === "activating") note += "服务尚未确认启动成功，正在自动刷新；超过 60 秒会提示启动超时。";
  else if (active === "deactivating" && transitionTimedOut) note += "超过 60 秒仍未确认停止；请刷新状态并查看服务日志，必要时重试暂停。";
  else if (active === "deactivating") note += "服务正在停止，正在自动刷新最终状态。";
  else if (config.enabled) note += "服务尚未运行，请查看最近检查错误及服务日志，修正配置后再开始监控。";
  else note += "点击“开始监控”后开始检查。";
  const runtime = document.querySelector("#monitor-runtime");
  const tone = serviceTone(active);
  runtime.textContent = label;
  runtime.className = `state ${tone}`;
  document.querySelector("#monitor-runtime-note").textContent = note;
  const overview = document.querySelector("#overview-state");
  overview.textContent = label;
  overview.className = `overview-value state ${tone}`;
  const header = document.querySelector("#header-monitor-state");
  header.textContent = label;
  header.className = `state ${tone}`;
  document.querySelector("#overview-state-note").textContent = active === "active" ? "后台持续检查，关闭网页后仍运行" : active === "activating" && transitionTimedOut ? "启动超过 60 秒未完成，请查看服务日志" : active === "activating" ? "正在确认启动状态，页面自动刷新" : active === "failed" ? "请检查配置或查看服务日志" : active === "deactivating" && transitionTimedOut ? "停止超过 60 秒未完成，请查看服务日志" : active === "deactivating" ? "正在停止，等待最终状态" : active === "inactive" ? "已暂停，保留最近库存记录" : "等待服务器返回服务状态";
  renderProductList();
  window.dispatchEvent(new Event("hostwatch-service-change"));
}

function collectConfig(enabled = config.enabled) {
  return {
    enabled,
    interval_seconds: Number(document.querySelector("#interval").value || NaN),
    affiliate_id: config.affiliate_id || "",
    affiliates: collectAffiliates(),
    affiliate_notes: collectAffiliateNotes(),
    merchant_order: merchantOrder(),
    watch_order: config.watch_order || [],
    notify_order: config.notify_order || [],
    notify_current_stock_on_start: optionPressed("notify-current"),
    isvoro_seckill: optionPressed("isvoro-seckill"),
    fetcher: { provider_modes: collectProviderModes(), mode: document.querySelector("#fetcher-mode").value, flaresolverr_url: document.querySelector("#flaresolverr-url").value.trim(), timeout_seconds: Number(document.querySelector("#flaresolverr-timeout").value), wait_seconds: Number(document.querySelector("#fetcher-wait").value), cloudscraper: { connect_timeout_seconds: Number(document.querySelector("#cloudscraper-connect-timeout").value), read_timeout_seconds: Number(document.querySelector("#cloudscraper-read-timeout").value), challenge_delay_seconds: Number(document.querySelector("#cloudscraper-challenge-delay").value) } },
    telegram: { bot_token: document.querySelector("#telegram-token").value.trim(), chat_id: document.querySelector("#telegram-chat-id").value.trim(), disabled: optionPressed("telegram-disabled"), clear: optionPressed("telegram-clear") },
    products: config.products.map((item) => ({ ...item })),
  };
}

function hasUnsavedChanges() {
  return savedConfig !== null && JSON.stringify(collectConfig()) !== savedConfig;
}

function intervalLabel(seconds) {
  if (!Number.isInteger(seconds) || seconds < 0) return "未设置";
  if (seconds === 0) return "连续检查";
  return seconds % 60 === 0 ? `${seconds / 60} 分钟` : `${seconds} 秒`;
}

function updateDraftState() {
  if (savedConfig === null) return;
  const dirty = hasUnsavedChanges();
  updateDiscardControl();
  const node = document.querySelector("#monitor-draft");
  node.textContent = dirty ? "有未保存的修改，请点击底部“保存配置”；单次测试可直接使用当前填写的商品配置。" : "页面配置已保存。";
  node.className = dirty ? "hint draft-note" : "hint";
  const saveState = document.querySelector("#save-state");
  saveState.textContent = dirty ? "有未保存的修改" : "全部配置已保存";
  saveState.className = dirty ? "draft-note" : "";
  const seconds = Number(document.querySelector("#interval").value || NaN);
  const valid = Number.isInteger(seconds) && seconds >= 0 && seconds <= 86400;
  document.querySelector("#interval-description").textContent = valid ? (seconds === 0 ? "本轮检查完成后立即检查下一轮，不额外等待；各轮顺序执行。" : `每轮检查完成后等待 ${intervalLabel(seconds)}，再检查下一轮；开始监控后立即进行第一次检查。`) : "检查间隔必须是 0–86400 秒的整数。";
  document.querySelector("#interval-preset").value = [0, 1, 5, 10, 30, 60, 300, 600, 1800, 3600].includes(seconds) ? String(seconds) : "";
  window.dispatchEvent(new Event("hostwatch-draft-change"));
}

function updateDiscardControl() {
  document.querySelector("#discard-changes").disabled = actionRunning || Boolean(activeTest) || !savedConfig || !hasUnsavedChanges();
}
async function discardChanges() {
  // Fetch first: a read failure must leave the draft intact.
  const saved = await requestJSON("/api/config");
  productTests.clear();
  applyConfig(saved);
  setMessage("已取消未保存的修改，恢复服务器已保存的配置；未执行保存或更改服务状态。", "success");
}

function collectProviderModes() {
  return Object.fromEntries([...document.querySelectorAll("[data-provider-mode]")].filter((input) => input.value !== "legacy").map((input) => [input.dataset.providerMode, input.value]));
}
function applyProviderModes(fetcher) {
  for (const select of document.querySelectorAll("[data-provider-mode]")) {
    select.querySelector('[value="legacy"]')?.remove();
    const key = select.dataset.providerMode, explicit = fetcher.provider_modes?.[key];
    const previous = [...new Set(config.products.filter((item) => item.adapter === key).map((item) => item.request_mode || "inherit"))];
    if (!explicit && previous.length > 1) select.add(new Option("保留已有商品的不同请求方式", "legacy"));
    select.value = explicit || (previous.length > 1 ? "legacy" : previous[0] || "inherit");
  }
}
function updateFetcherStatus() {
  const mode = document.querySelector("#fetcher-mode").value;
  const names = { requests: "普通 HTTPS 请求", flaresolverr: "FlareSolverr 浏览器请求", cloudscraper: "cloudscraper 请求" };
  document.querySelector("#fetcher-status").textContent = `全局默认使用 ${names[mode] || "未知请求方式"}；商家选择优先于全局设置，保存后自动应用。${mode === "cloudscraper" ? "cloudscraper 无需浏览器服务，不能完整执行网页 JavaScript，也不保证通过所有 Cloudflare 校验。" : ""}`;
  const legacy = [...document.querySelectorAll("[data-provider-mode]")].filter((input) => input.value === "legacy");
  document.querySelector("#provider-request-note").textContent = legacy.length ? "部分商家原有商品使用不同请求方式，目前保留。为该商家选择统一方式并保存后，会覆盖这些旧选择。" : "新增商品自动使用对应商家设置，测试使用当前页面填写的请求方式；后台自动监控使用已保存的设置。";
}

function openAdvancedProducts() {
  const panel = document.querySelector("#advanced-products"); panel.open = true;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadConfig() {
  applyConfig(await requestJSON("/api/config"));
}

function applyConfig(value) {
  config = value;
  config.products.forEach(item => { setMembership(item, "watch", membership(item, "watch")); });
  document.querySelector("#interval").value = config.interval_seconds ?? 600;
  for (const input of document.querySelectorAll("[data-affiliate]")) input.value = config.affiliates?.[input.dataset.affiliate] ?? (input.dataset.affiliate === "vmiss" ? config.affiliate_id || "" : "");
  for (const input of document.querySelectorAll("[data-affiliate-note]")) input.value = config.affiliate_notes?.[input.dataset.affiliateNote] ?? "";
  renderAffiliatePreviews(); renderMerchantOrder();
  setOptionPressed("notify-current", config.notify_current_stock_on_start);
  setOptionPressed("isvoro-seckill", config.isvoro_seckill);
  const fetcher = config.fetcher || {};
  document.querySelector("#fetcher-mode").value = fetcher.mode || "requests";
  document.querySelector("#flaresolverr-url").value = fetcher.flaresolverr_url || "http://127.0.0.1:8191/v1";
  document.querySelector("#flaresolverr-timeout").value = fetcher.timeout_seconds || 60;
  document.querySelector("#fetcher-wait").value = fetcher.wait_seconds || 0;
  const cloud = fetcher.cloudscraper || {};
  document.querySelector("#cloudscraper-connect-timeout").value = cloud.connect_timeout_seconds ?? 10;
  document.querySelector("#cloudscraper-read-timeout").value = cloud.read_timeout_seconds ?? 25;
  document.querySelector("#cloudscraper-challenge-delay").value = cloud.challenge_delay_seconds ?? 5;
  applyProviderModes(fetcher);
  updateFetcherStatus();
  const telegram = config.telegram || {};
  document.querySelector("#telegram-token").value = "";
  document.querySelector("#telegram-chat-id").value = telegram.chat_id || "";
  setOptionPressed("telegram-disabled", telegram.disabled);
  setOptionPressed("telegram-clear", false);
  document.querySelector("#telegram-status").textContent = telegram.configured ? "Telegram 已配置（token 不会回显）。" : (telegram.disabled ? "Telegram 已停用。" : "Telegram 尚未完整配置。");
  document.querySelector("#telegram-chats").replaceChildren();
  renderProducts();
  savedConfig = JSON.stringify(collectConfig());
  updateDraftState();
  renderMonitorState();
  renderExchangeRates();
}

async function loadStatus() {
  lastStatus = await requestJSON("/api/status");
  const state = document.querySelector("#state");
  state.textContent = ({ partial: "部分商品待核对", ok: "上次检查成功", blocked: "上次检查被拦截", error: "上次检查失败", unknown: "尚无检查记录" })[lastStatus.state] || lastStatus.state || "尚无检查记录";
  state.className = `state ${lastStatus.state || ""}`;
  document.querySelector("#last-checked").textContent = lastStatus.last_checked ? `上次检查：${new Date(lastStatus.last_checked).toLocaleString()}` : "";
  blockedHelpNode.hidden = lastStatus.state !== "blocked";
  document.querySelector("#blocked-message").textContent = lastStatus.message || "检测到拒绝访问或安全验证。请确认厂商允许当前的自动检查方式后再恢复。";
  const errorNode = document.querySelector("#request-error");
  errorNode.hidden = !["error", "partial"].includes(lastStatus.state);
  errorNode.textContent = ["error", "partial"].includes(lastStatus.state) ? `最近检查提示：${lastStatus.message || "请查看服务日志"}` : "";
  renderProductList();
  renderOverview();
  renderSeckill();
  renderExchangeRates();
}

async function reloadConfig(notify = true) {
  const results = await Promise.allSettled([loadConfig(), loadStatus(), refreshManagement(), loadExchangeRates()]);
  const failures = results.map((result, index) => result.status === "rejected"
    ? `${["配置", "检查结果", "服务状态", "汇率"][index]}载入失败：${result.reason.message}` : "").filter(Boolean);
  if (failures.length) throw new Error(failures.join("；"));
  setMessage("配置和监控状态已载入。", "success", notify);
  restoreProductTest();
}

function addProduct(item) {
  addProducts([item]);
}
function addProducts(items) {
  config.products.push(...items);
  openProducts.add(items[0].id);
  renderProducts();
  navigatePage("notifications"); openAdvancedProducts();
  editorNode.querySelector(`[data-id="${CSS.escape(items[0].id)}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  setMessage(items.length === 1 ? "商品已添加，请填写商品信息后保存。" : `已添加 ${items.length} 个商品，可先测试，将需要的套餐加入库存监控或通知后保存。`, "pending");
  updateDraftState();
}
// Merchant defaults live in one registry so future presets share the same entry.
const merchantPresets = {
  html: { label: "其他商家 · 通用 HTTPS 网页", hint: "填写商品网址及库存规则，适用于其他公开商品网页。", product: { adapter: "html", provider: "自定义网站", name: "", url: "", stock_selector: "", stock_mode: "quantity", stock_pattern: "([\\d,]+)", available_pattern: "(?i)in stock|available|有货|现货", unavailable_pattern: "(?i)out of stock|sold out|缺货|售罄", purchase_url: "" } },
  vmiss: { label: "VMISS", hint: "填写分类页、商品选择器和分组 ID，使用 VMISS 专用解析。", product: { adapter: "vmiss", provider: "VMISS", name: "", category_url: "", selector: "", gid: "" } },
  vps_soy: { label: "VPS.SOY", hint: "默认深港IX-MINI（FID=1、GID=8），支持切换地区/线路分组。", product: { adapter: "vps_soy", provider: "VPS.SOY", name: "深港IX-MINI", url: "https://www.vps.soy/cart?fid=1&gid=8", selector: "深港IX-MINI", request_mode: "inherit" } },
  akile: { label: "Akile", hint: "默认仅添加 HKL-TW-Mini，自动识别地区，请求方式在“网页请求”中设置。", product: { adapter: "akile", provider: "Akile", name: "HKL-TW-Mini", area_mode: "auto", area_id: "6", node_name: "HKL-TW", selector: "HKL-TW-Mini", request_mode: "inherit" } },
  isvoro: { label: "Isvoro", hint: "按类型、地区/线路、套餐分别选择，默认中国香港 → HK PROD → HK PROD MiNi。", product: { adapter: "isvoro", provider: "Isvoro", name: "HK PROD MiNi", type_id: "24", type_name: "中国香港", category_id: "27", category_name: "HK PROD", product_id: "188", product_name: "HK PROD MiNi", request_mode: "inherit" } },
  dmit: { label: "DMIT", hint: "优先从库存大盘选择套餐；也可填写官网分类页与购买链接中的 PID。AFF 默认 22544，可在 AFF 管理中修改。", product: { adapter: "dmit", provider: "DMIT", name: "", category_url: "https://www.dmit.io/pages/pricing", pid: "", request_mode: "inherit" } },
  bwh: { label: "搬瓦工", hint: "读取公开 catalog.json，默认洛杉矶 USCA_9 电商 VPS；地区和套餐会自动显示中文。AFF 默认 84938，可在 AFF 管理中修改。", product: { adapter: "bwh", provider: "搬瓦工", name: "", category_url: "https://bwh81.net/order/ecommerce/Los%20Angeles/USCA_9", bwh_offer_key: "", selector: "", pid: "", request_mode: "inherit" } },
};
const merchantSelect = document.querySelector("#add-merchant");
for (const [key, preset] of Object.entries(merchantPresets)) {
  merchantSelect.add(new Option(preset.label, key));
}
function updateMerchantHint() { document.querySelector("#add-merchant-hint").textContent = merchantPresets[merchantSelect.value].hint; }
merchantSelect.addEventListener("change", updateMerchantHint);
updateMerchantHint();
document.querySelector("#open-products").addEventListener("click", () => {
  navigatePage("notifications"); openAdvancedProducts();
  merchantSelect.focus({ preventScroll: true });
});
document.querySelector("#overview-open-catalog").addEventListener("click", () => navigatePage("catalog"));
document.querySelector("#overview-open-monitor").addEventListener("click", () => navigatePage("monitor"));
document.querySelector("#overview-open-notifications").addEventListener("click", () => navigatePage("notifications"));
document.querySelector("#notification-browse").addEventListener("click", () => navigatePage("catalog"));
document.querySelector("#watchlist-browse").addEventListener("click", () => navigatePage("catalog"));
for (const id of ["watchlist-search", "watchlist-provider", "watchlist-stock"]) document.getElementById(id).addEventListener(id === "watchlist-search" ? "input" : "change", () => {
  renderProductList();
  animateFilterResult(listNode);
});
for (const [id, key] of [["watchlist-price-sort", "hostwatch-watch-price-sort"], ["notification-price-sort", "hostwatch-notification-price-sort"]]) {
  const select = document.getElementById(id), saved = sessionStorage.getItem(key);
  if (saved && ["default", "asc", "desc"].includes(saved)) select.value = saved;
  select.addEventListener("change", () => {
    sessionStorage.setItem(key, select.value);
    if (id === "watchlist-price-sort") { renderProductList(); animateFilterResult(listNode); }
    else { renderNotificationList(); animateFilterResult(document.querySelector("#notification-list")); }
  });
}
document.querySelector("#watch-compare-start").addEventListener("click", openCompareDialog);
document.querySelector("#watch-compare-clear").addEventListener("click", () => {
  compareSelection.clear();
  closeCompareDialog();
  renderProductList();
});
document.querySelector("#watch-compare-close").addEventListener("click", closeCompareDialog);
document.querySelector("#watch-compare-dialog").addEventListener("click", event => {
  if (event.target === event.currentTarget) closeCompareDialog();
});
document.querySelector("#watch-compare-dialog").addEventListener("cancel", event => {
  event.preventDefault();
  closeCompareDialog();
});
for (const input of document.querySelectorAll("[data-affiliate]")) input.addEventListener("input", renderAffiliatePreviews);
for (const input of document.querySelectorAll("[data-provider-mode]")) input.addEventListener("change", updateFetcherStatus);
document.querySelector("#merchant-order-toggle").addEventListener("click", (event) => {
  const panel = document.querySelector("#merchant-order-panel"); panel.hidden = !panel.hidden;
  event.currentTarget.setAttribute("aria-expanded", String(!panel.hidden));
});
document.querySelector("#add").addEventListener("click", () => {
  const item = { ...structuredClone(merchantPresets[merchantSelect.value].product), id: crypto.randomUUID(), enabled: false };
  const existing = item.adapter === "akile" && config.products.find((product) => product.adapter === "akile" && (product.area_mode !== "manual" || String(product.area_id) === item.area_id) && product.node_name?.trim().toLowerCase() === item.node_name.toLowerCase() && product.selector?.trim().toLowerCase() === item.selector.toLowerCase());
  if (existing) {
    openProducts.add(existing.id);
    renderProducts();
    navigatePage("notifications"); openAdvancedProducts();
    editorNode.querySelector(`[data-id="${CSS.escape(existing.id)}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    setMessage("HKL-TW-Mini 已存在，已展开配置；监控其他套餐可修改此商品的套餐完整名称后再添加。", "success");
    return;
  }
  addProduct(item);
});
document.querySelector("#refresh-status").addEventListener("click", (event) => runAction(event.currentTarget, "刷新中…", "正在刷新监控状态…", async () => {
  await Promise.all([loadStatus(), refreshManagement()]);
  setMessage("监控状态已刷新；此次操作不会触发库存检查。", "success");
}));
document.querySelector("#fetcher-mode").addEventListener("change", updateFetcherStatus);
document.querySelector("#fetcher-test").addEventListener("click", (event) => runAction(event.currentTarget, "检测中…", "正在测试 FlareSolverr 服务连接，不访问商品网站…", async () => {
  const result = await requestJSON("/api/fetcher/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(collectConfig().fetcher) });
  document.querySelector("#fetcher-health").textContent = `版本 ${result.version}：${result.message}`;
  setMessage(`FlareSolverr ${result.version} 服务连接成功；实际库存请在 VPS 库存通知中点击“测试通知”，无需开启监控。`, "success");
}));
document.querySelector("#interval").addEventListener("input", () => { config.interval_seconds = Number(document.querySelector("#interval").value || NaN); renderOverview(); });
document.querySelector("#interval-preset").addEventListener("change", (event) => {
  if (event.target.value) {
    document.querySelector("#interval").value = event.target.value;
    config.interval_seconds = Number(event.target.value);
    renderOverview();
    updateDraftState();
  }
});
document.querySelector(".shell").addEventListener("input", (event) => {
  if (!event.target.closest("#page-catalog, .watchlist-filters") && event.target.id !== "interval-preset") updateDraftState();
});
document.querySelector(".shell").addEventListener("change", (event) => {
  if (!event.target.closest("#page-catalog, .watchlist-filters")) updateDraftState();
});
document.querySelector("#monitor-start").addEventListener("click", (event) => {
  if (hasUnsavedChanges()) { setMessage("请先点击底部“保存配置”，再开始监控；未保存商品也可以先单次测试。", "pending"); return; }
  runAction(event.currentTarget, "启动中…", "正在使用已保存配置开始监控…", () => performManagementAction("monitor-start"));
});
document.querySelector("#monitor-stop").addEventListener("click", (event) => runAction(event.currentTarget, "暂停中…", "正在暂停自动监控；页面未保存修改会保留…", () => performManagementAction("monitor-stop")));
document.querySelector("#telegram-test").addEventListener("click", (event) => runAction(event.currentTarget, "发送中…", "正在发送 Telegram 测试消息…", async () => {
  await requestJSON("/api/telegram/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ bot_token: document.querySelector("#telegram-token").value.trim(), chat_id: document.querySelector("#telegram-chat-id").value.trim() }) });
  setMessage("Telegram 测试消息已发送。", "success");
}));
document.querySelector("#telegram-load-chats").addEventListener("click", (event) => runAction(event.currentTarget, "读取中…", "正在读取 Telegram 最近聊天…", async () => {
  const chatsNode = document.querySelector("#telegram-chats");
  chatsNode.replaceChildren();
  const result = await requestJSON("/api/telegram/chats");
  if (!result.chats.length) {
    const message = "读取完成，暂无聊天记录；请先打开 bot 并发送 /start 后重试。";
    chatsNode.append(text("p", message, "hint"));
    setMessage(message, "success");
    return;
  }
  for (const chat of result.chats) {
    const option = text("button", "", "chat-option");
    option.type = "button";
    option.append(text("span", `${chat.name}（${chat.type}）`), text("span", chat.id));
    option.addEventListener("click", () => { document.querySelector("#telegram-chat-id").value = chat.id; updateDraftState(); setMessage(`已选择 ${chat.name}，保存后生效。`, "pending"); });
    chatsNode.append(option);
  }
  setMessage(`已读取 ${result.chats.length} 个聊天。`, "success");
}));

async function saveConfig() {
  const next = collectConfig();
  const result = await requestJSON("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(next) });
  document.querySelector("#telegram-token").value = "";
  applyConfig(result.config);
  if (result.service?.ok) {
    setMessage(result.config.enabled ? "配置已保存并应用，自动监控保持开启。" : "配置已保存，自动监控保持暂停。可先测试商品，再点击“开始监控”。", "success");
  } else {
    setMessage(`配置已保存，但自动应用服务失败：${result.service?.error || "请在监控设置中重试"}`, "error");
  }
  try { await Promise.all([refreshManagement(), loadStatus()]); }
  catch (error) { document.querySelector("#manage-task-status").textContent = `配置已保存，管理状态刷新失败：${error.message}`; }
}
document.querySelector("#discard-changes").addEventListener("click", (event) => { if (!activeTest) runAction(event.currentTarget, "恢复中…", "正在恢复已保存配置…", discardChanges); });
document.querySelector("#save").addEventListener("click", (event) => runAction(event.currentTarget, "保存中…", "正在保存配置…", () => saveConfig()));
renderExchangeRates();
exchangeClockTimer = setInterval(renderExchangeRates, 30_000);
exchangeRateRefreshTimer = setInterval(() => { if (!document.hidden) void loadExchangeRates(); }, 60 * 60 * 1000);
runAction(null, "", "正在载入配置和监控状态…", () => reloadConfig(false), false);
bindManagementActions();
if (updateUnit) pollUpdateStatus();
