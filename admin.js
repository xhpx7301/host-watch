let config = {
  enabled: false,
  interval_seconds: 600,
  notify_current_stock_on_start: false,
  affiliate_id: "",
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
const openProducts = new Set();
const productTests = new Map();
let activeTest = null;

function setMessage(message, kind = "", notify = true) {
  messageNode.textContent = message;
  messageNode.className = kind;
  if (!notify) return;
  clearTimeout(toastTimer);
  toastNode.className = `toast ${kind}`;
  toastNode.hidden = false;
  document.querySelector("#toast-label").textContent = ({ pending: "操作提示", success: "操作成功", error: "操作失败" })[kind] || "操作提示";
  document.querySelector("#toast-message").textContent = message;
  // Errors and in-progress actions stay visible until dismissed or replaced.
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
    syncTestControls();
  }
}

function text(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
}

async function managementRequest(value) {
  return requestJSON("/api/manage", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) });
}

async function refreshManagement() {
  let state;
  try { state = await requestJSON("/api/manage"); }
  catch (error) {
    monitorService = null;
    renderMonitorState();
    throw error;
  }
  monitorService = state.monitor;
  renderMonitorState();
  const serviceLabel = (value) => ({ active: "运行中", inactive: "已停止", failed: "运行失败", activating: "启动中", deactivating: "停止中", unknown: "未知" })[value] || value;
  const monitorState = document.querySelector("#manage-monitor-state");
  monitorState.textContent = serviceLabel(state.monitor.active);
  monitorState.className = state.monitor.active === "active" ? "state ok" : "state error";
  document.querySelector("#manage-monitor-enabled").textContent = ` · 开机启动：${state.monitor.enabled === "enabled" ? "已开启" : state.monitor.enabled === "disabled" ? "已关闭" : state.monitor.enabled}`;
  const adminState = document.querySelector("#manage-admin-state");
  adminState.textContent = serviceLabel(state.admin.active);
  adminState.className = state.admin.active === "active" ? "state ok" : "state error";
  managementUsers = state.users;
  renderManagementUsers();
  const backups = document.querySelector("#manage-backups");
  backups.replaceChildren(...(state.backups.length ? state.backups.map((item) => text("span", `${item.name} · ${formatBytes(item.size)}`)) : [text("span", "暂无备份")]));
  if (updateUnit) await refreshUpdateStatus();
  return state;
}

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
  document.querySelector("#open-management").addEventListener("click", () => {
    const panel = document.querySelector("#management-panel");
    panel.open = true;
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
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

function checkField(labelText, checked, key) {
  const label = text("label", labelText, "check");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = Boolean(checked);
  input.dataset.key = key;
  label.prepend(input);
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

function renderIsvoroFields(article, reset = "") {
  const get = (key) => article.querySelector(`[data-key="isvoro_${key}"]`);
  const item = config.products.find((product) => product.id === article.dataset.id) || {};
  isvoroOptions(get("type"), isvoroTypes, get("type").value, item.type_name, "请选择类型");
  const kind = isvoroTypes.find((row) => String(row.id) === get("type").value);
  const lines = kind ? kind.children.length ? kind.children : [kind] : [];
  isvoroOptions(get("line"), lines, reset === "type" ? "" : get("line").value, item.category_name, "请选择地区 / 线路");
  const products = isvoroLines.get(get("line").value)?.products || [];
  isvoroOptions(get("product"), products, reset ? "" : get("product").value, item.product_name, "请选择套餐");
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
      renderIsvoroFields(article);
    }
    const kind = isvoroTypes.find((row) => String(row.id) === get("type").value);
    const line = (kind?.children.length ? kind.children : kind ? [kind] : []).find((row) => String(row.id) === get("line").value);
    if (line && (refresh || !isvoroLines.has(String(line.id)))) {
      const result = await requestJSON(`/api/isvoro/category/${encodeURIComponent(line.slug)}`, { signal: controller.signal });
      if (controller.signal.aborted || !article.isConnected || get("line").value !== String(line.id)) return;
      isvoroLines.set(String(line.id), result.category);
      renderIsvoroFields(article);
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
  actions.append(checkField("参与自动监控", item.enabled, "enabled"));
  const test = text("button", "测试此商品", "product-test");
  test.type = "button";
  test.addEventListener("click", () => testProduct(item.id, test));
  actions.append(test);
  const stop = text("button", "停止测试", "product-test-stop danger");
  stop.type = "button";
  stop.addEventListener("click", () => stopProductTest());
  actions.append(stop);
  const remove = text("button", "移除", "danger");
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
    selectField("监控适配器", item.adapter || (item.category_url ? "vmiss" : "html"), "adapter", [["html", "通用 HTTPS 网页"], ["vmiss", "VMISS WHMCS"], ["vps_soy", "VPS.SOY 商品页"], ["akile", "Akile 商品库存"], ["isvoro", "Isvoro 商品库存"]]),
    inputField("厂商名称", item.provider || (item.adapter === "vmiss" || item.category_url ? "VMISS" : "自定义网站"), "provider", "text", "例如 ExampleHost"),
    inputField("商品名称", item.name, "name", "text", "例如 US VPS 2 GB"),
    selectField("此商品的请求方式", item.request_mode || "inherit", "request_mode", [["inherit", "跟随全局设置"], ["requests", "普通 HTTPS 请求"], ["flaresolverr", "FlareSolverr 浏览器请求"]]),
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
  modeWrap.append(vmissFields, htmlFields, soyFields, akileFields, isvoroFields);
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
      renderIsvoroFields(article, event.target.dataset.key === "isvoro_type" ? "type" : "line");
      void loadIsvoroDirectory(article);
    }
    syncEditor(article);
    updateAdapterFields(article);
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
  item.enabled = get("enabled").checked;
  item.request_mode = get("request_mode").value;
  if (item.adapter === "vmiss") {
    item.category_url = get("category_url").value.trim();
    item.selector = get("selector").value.trim();
    const target = get("target").value;
    item.gid = target === "gid" ? get("target_id").value.trim() : "";
    item.pid = target === "pid" ? get("target_id").value.trim() : "";
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
  document.querySelector("#stock-test-bar").hidden = !activeTest;
  document.querySelector("#stock-test-status").textContent = activeTest?.notifying ? "库存检查已结束，正在发送 Telegram 结果，页面仍可操作。" : activeTest?.stopping ? "正在停止测试并释放资源…" : "商品测试正在运行，页面仍可操作。";
  const globalStop = document.querySelector("#stock-test-stop");
  globalStop.disabled = Boolean(activeTest?.stopping || activeTest?.notifying);
  globalStop.textContent = activeTest?.stopUnconfirmed ? "重试停止" : "停止测试";
  editorNode.querySelectorAll(".product-editor").forEach((article) => {
    const running = activeTest?.id === article.dataset.id;
    const test = article.querySelector(".product-test");
    test.disabled = Boolean(activeTest) || actionRunning;
    test.textContent = running ? (activeTest.notifying ? "发送结果中…" : "测试中…") : "测试此商品";
    const stop = article.querySelector(".product-test-stop");
    stop.hidden = !running || Boolean(activeTest?.notifying);
    stop.disabled = Boolean(activeTest?.stopping);
    stop.textContent = activeTest?.stopping ? "停止中…" : activeTest?.stopUnconfirmed ? "重试停止" : "停止测试";
    article.querySelector(".actions > .danger:not(.product-test-stop)").disabled = running || actionRunning;
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
  const item = structuredClone(config.products.find((product) => product.id === id));
  const fetcher = collectConfig().fetcher;
  task.draft = JSON.stringify({ product: item, fetcher });
  activeTest = task;
  sessionStorage.setItem("hostwatch-stock-test", JSON.stringify({ id, taskId: task.taskId }));
  productTests.set(id, { pending: true });
  setMessage("正在单次测试商品，可随时点击“停止测试”；页面其他功能仍可使用。", "pending");
  syncTestControls();
  task.startController = new AbortController();
  task.start = requestJSON("/api/products/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task_id: task.taskId, product: item, fetcher }), timeoutMs: 15000, signal: task.startController.signal });
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
    productTests.set(task.id, result);
    const notificationIssue = result.notification && !["sent", "skipped"].includes(result.notification.status);
    const summary = result.cancelled ? "测试已停止。" : result.ok ? "单次库存测试完成，结果已显示在该商品配置内。" : `单次库存测试失败：${result.error}`;
    setMessage([summary, result.notification?.message, result.warning].filter(Boolean).join(" "), result.warning || notificationIssue || (!result.ok && !result.cancelled) ? "error" : "success");
  } catch (error) {
    productTests.set(task.id, { ok: false, error: error.message });
    setMessage(error.message, "error");
  } finally {
    clearTimeout(task.retryStopTimer);
    task.pollController?.abort();
    task.startController?.abort();
    if (activeTest === task) activeTest = null;
    sessionStorage.removeItem("hostwatch-stock-test");
    syncTestControls();
  }
}
document.querySelector("#stock-test-stop").addEventListener("click", stopProductTest);

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

function renderProducts() {
  editorNode.replaceChildren(...config.products.map(productEditor));
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

function renderProductList() {
  const statusProducts = new Map((lastStatus?.products || []).map((product) => [product.id, product]));
  const savedProducts = new Map((savedConfig ? JSON.parse(savedConfig).products : []).map((product) => [product.id, product]));
  if (!config.products.length) {
    listNode.replaceChildren(text("div", "尚未添加商品。添加 VMISS 商品或通用 HTTPS 商品页开始监控。", "empty"));
    return;
  }
  const rows = config.products.map((item) => {
    const status = statusProducts.get(item.id);
    const article = text("article", "", "product");
    const identity = text("div", "", "product-name");
    identity.append(text("strong", item.name || "未命名商品"), text("span", `${item.provider || "自定义网站"} · ${productMonitorLabel(item, savedProducts.get(item.id))}`));
    const stock = text("div", "", "metric");
    stock.append(text("span", "最近库存", "metric-label"), text("strong", status ? stockLabel(status) : "暂无数据", `metric-value ${status?.available > 0 ? "in" : "out"}`));
    const checked = text("div", "", "metric");
    checked.append(text("span", "上次检查结果", "metric-label"), text("strong", status ? (status.available > 0 ? "有货" : "缺货") : "暂无记录", "metric-value"));
    const updated = text("div", "", "metric");
    updated.append(text("span", "最近检查", "metric-label"), text("strong", lastStatus?.last_checked ? new Date(lastStatus.last_checked).toLocaleString() : "尚无记录", "metric-value"));
    const actions = text("div", "", "product-actions");
    const purchaseUrl = status?.purchase_url || item.purchase_url || item.url || item.category_url;
    if (purchaseUrl && item.enabled && status?.available > 0) {
      const link = document.createElement("a");
      link.href = purchaseUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "手动购买";
      actions.append(link);
    }
    const edit = text("button", "配置", "");
    edit.type = "button";
    edit.addEventListener("click", () => {
      document.querySelector("#product-editors").closest("details").open = true;
      const editor = editorNode.querySelector(`[data-id="${CSS.escape(item.id)}"]`);
      editor.open = true;
      openProducts.add(item.id);
      editor.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    actions.append(edit);
    article.append(identity, stock, checked, updated, actions);
    return article;
  });
  listNode.replaceChildren(...rows);
}

function renderOverview() {
  const enabled = config.products.filter((item) => item.enabled);
  const vendors = new Set(enabled.map((item) => item.provider).filter(Boolean));
  const latest = new Map((lastStatus?.products || []).map((product) => [product.id, product]));
  const inStock = enabled.filter((item) => (latest.get(item.id)?.available || 0) > 0).length;
  document.querySelector("#overview-total").textContent = String(config.products.length);
  document.querySelector("#overview-total-note").textContent = `页面配置：${vendors.size} 个厂商 · ${enabled.length} 个商品参与检查`;
  document.querySelector("#overview-stock").textContent = String(inStock);
  document.querySelector("#overview-interval").textContent = intervalLabel(config.interval_seconds ?? 600);
  renderMonitorState();
}

function renderMonitorState() {
  const active = monitorService?.active;
  const labels = { active: "监控服务运行中", inactive: "监控服务已停止", failed: "监控服务运行失败", activating: "监控服务启动中", deactivating: "监控服务停止中" };
  const label = labels[active] || "监控服务状态未知";
  let note = config.enabled ? "已保存：开启自动监控。" : "已保存：暂停自动监控。";
  if (!savedConfig) note = "正在读取配置。";
  if (!monitorService) note += "请刷新状态核对服务器实际运行情况。";
  else if (active === "active") note += config.enabled ? "服务已启动，库存检查是否成功请查看最近检查结果。" : "暂停尚未生效，服务仍在运行，请重新暂停监控。";
  else if (config.enabled) note += "服务尚未运行，可点击“开始监控”恢复。";
  else note += "点击“开始监控”后开始检查。";
  const runtime = document.querySelector("#monitor-runtime");
  const tone = active === "active" ? "ok" : ["inactive", "failed"].includes(active) ? "error" : ["activating", "deactivating"].includes(active) ? "pending" : "";
  runtime.textContent = label;
  runtime.className = `state ${tone}`;
  document.querySelector("#monitor-runtime-note").textContent = note;
  const overview = document.querySelector("#overview-state");
  overview.textContent = label;
  overview.className = `overview-value state ${tone}`;
  document.querySelector("#overview-state-note").textContent = note;
  renderProductList();
}

function collectConfig(enabled = config.enabled) {
  return {
    enabled,
    interval_seconds: Number(document.querySelector("#interval").value || NaN),
    affiliate_id: document.querySelector("#affiliate").value.trim(),
    notify_current_stock_on_start: document.querySelector("#notify-current").checked,
    fetcher: { mode: document.querySelector("#fetcher-mode").value, flaresolverr_url: document.querySelector("#flaresolverr-url").value.trim(), timeout_seconds: Number(document.querySelector("#flaresolverr-timeout").value), wait_seconds: Number(document.querySelector("#fetcher-wait").value) },
    telegram: { bot_token: document.querySelector("#telegram-token").value.trim(), chat_id: document.querySelector("#telegram-chat-id").value.trim(), disabled: document.querySelector("#telegram-disabled").checked, clear: document.querySelector("#telegram-clear").checked },
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
  const node = document.querySelector("#monitor-draft");
  node.textContent = dirty ? "有未保存的修改，请点击底部“保存配置”；单次测试可直接使用当前填写的商品配置。" : "页面配置已保存。";
  node.className = dirty ? "hint draft-note" : "hint";
  const seconds = Number(document.querySelector("#interval").value || NaN);
  const valid = Number.isInteger(seconds) && seconds >= 0 && seconds <= 86400;
  document.querySelector("#interval-description").textContent = valid ? (seconds === 0 ? "本轮检查完成后立即检查下一轮，不额外等待；各轮顺序执行。" : `每轮检查完成后等待 ${intervalLabel(seconds)}，再检查下一轮；开始监控后立即进行第一次检查。`) : "检查间隔必须是 0–86400 秒的整数。";
  document.querySelector("#interval-preset").value = [0, 1, 5, 10, 30, 60, 300, 600, 1800, 3600].includes(seconds) ? String(seconds) : "";
}

function updateFetcherStatus() {
  const enabled = document.querySelector("#fetcher-mode").value === "flaresolverr";
  document.querySelector("#fetcher-status").textContent = enabled
    ? "全局使用 FlareSolverr 浏览器请求，支持所有适配器；商品可单独覆盖。浏览器验证仍可能失败，请查看具体错误。"
    : "全局使用普通 HTTPS 请求；需要浏览器的商品可单独选择 FlareSolverr。保存后自动应用。";
}

async function loadConfig() {
  applyConfig(await requestJSON("/api/config"));
}

function applyConfig(value) {
  config = value;
  document.querySelector("#interval").value = config.interval_seconds ?? 600;
  document.querySelector("#affiliate").value = config.affiliate_id || "";
  document.querySelector("#notify-current").checked = Boolean(config.notify_current_stock_on_start);
  const fetcher = config.fetcher || {};
  document.querySelector("#fetcher-mode").value = fetcher.mode || "requests";
  document.querySelector("#flaresolverr-url").value = fetcher.flaresolverr_url || "http://127.0.0.1:8191/v1";
  document.querySelector("#flaresolverr-timeout").value = fetcher.timeout_seconds || 60;
  document.querySelector("#fetcher-wait").value = fetcher.wait_seconds || 0;
  updateFetcherStatus();
  const telegram = config.telegram || {};
  document.querySelector("#telegram-token").value = "";
  document.querySelector("#telegram-chat-id").value = telegram.chat_id || "";
  document.querySelector("#telegram-disabled").checked = Boolean(telegram.disabled);
  document.querySelector("#telegram-clear").checked = false;
  document.querySelector("#telegram-status").textContent = telegram.configured ? "Telegram 已配置（token 不会回显）。" : (telegram.disabled ? "Telegram 已停用。" : "Telegram 尚未完整配置。");
  document.querySelector("#telegram-chats").replaceChildren();
  renderProducts();
  savedConfig = JSON.stringify(collectConfig());
  updateDraftState();
  renderMonitorState();
}

async function loadStatus() {
  lastStatus = await requestJSON("/api/status");
  const state = document.querySelector("#state");
  state.textContent = ({ ok: "上次检查成功", blocked: "上次检查被拦截", error: "上次检查失败", unknown: "尚无检查记录" })[lastStatus.state] || lastStatus.state || "尚无检查记录";
  state.className = `state ${lastStatus.state || ""}`;
  document.querySelector("#last-checked").textContent = lastStatus.last_checked ? `上次检查：${new Date(lastStatus.last_checked).toLocaleString()}` : "";
  blockedHelpNode.hidden = lastStatus.state !== "blocked";
  document.querySelector("#blocked-message").textContent = lastStatus.message || "检测到拒绝访问或安全验证。请确认厂商允许当前的自动检查方式后再恢复。";
  const errorNode = document.querySelector("#request-error");
  errorNode.hidden = lastStatus.state !== "error";
  errorNode.textContent = lastStatus.state === "error" ? `上次检查失败：${lastStatus.message || "请查看服务日志"}` : "";
  renderProductList();
  renderOverview();
}

async function reloadConfig(notify = true) {
  const results = await Promise.allSettled([loadConfig(), loadStatus(), refreshManagement()]);
  const failures = results.map((result, index) => result.status === "rejected"
    ? `${["配置", "检查结果", "服务状态"][index]}载入失败：${result.reason.message}` : "").filter(Boolean);
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
  document.querySelector("#product-editors").closest("details").open = true;
  editorNode.querySelector(`[data-id="${CSS.escape(items[0].id)}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  setMessage(items.length === 1 ? "商品已添加，请填写商品信息后保存。" : `已添加 ${items.length} 个商品，可先测试，勾选需要监控的套餐后保存。`, "pending");
  updateDraftState();
}
// Merchant defaults live in one registry so future presets share the same entry.
const merchantPresets = {
  html: { label: "其他商家 · 通用 HTTPS 网页", hint: "填写商品网址及库存规则，适用于其他公开商品网页。", product: { adapter: "html", provider: "自定义网站", name: "", url: "", stock_selector: "", stock_mode: "quantity", stock_pattern: "([\\d,]+)", available_pattern: "(?i)in stock|available|有货|现货", unavailable_pattern: "(?i)out of stock|sold out|缺货|售罄", purchase_url: "" } },
  vmiss: { label: "VMISS", hint: "填写分类页、商品选择器和分组 ID，使用 VMISS 专用解析。", product: { adapter: "vmiss", provider: "VMISS", name: "", category_url: "", selector: "", gid: "" } },
  vps_soy: { label: "VPS.SOY", hint: "默认深港IX-MINI（FID=1、GID=8），支持切换地区/线路分组。", product: { adapter: "vps_soy", provider: "VPS.SOY", name: "深港IX-MINI", url: "https://www.vps.soy/cart?fid=1&gid=8", selector: "深港IX-MINI", request_mode: "requests" } },
  akile: { label: "Akile", hint: "默认仅添加 HKL-TW-Mini，自动识别地区，使用普通 HTTPS 请求。", product: { adapter: "akile", provider: "Akile", name: "HKL-TW-Mini", area_mode: "auto", area_id: "6", node_name: "HKL-TW", selector: "HKL-TW-Mini", request_mode: "requests" } },
  isvoro: { label: "Isvoro", hint: "按类型、地区/线路、套餐分别选择，默认中国香港 → HK PROD → HK PROD MiNi。", product: { adapter: "isvoro", provider: "Isvoro", name: "HK PROD MiNi", type_id: "24", type_name: "中国香港", category_id: "27", category_name: "HK PROD", product_id: "188", product_name: "HK PROD MiNi", request_mode: "requests" } },
};
const merchantSelect = document.querySelector("#add-merchant");
for (const [key, preset] of Object.entries(merchantPresets)) {
  merchantSelect.add(new Option(preset.label, key));
}
function updateMerchantHint() { document.querySelector("#add-merchant-hint").textContent = merchantPresets[merchantSelect.value].hint; }
merchantSelect.addEventListener("change", updateMerchantHint);
updateMerchantHint();
document.querySelector("#add").addEventListener("click", () => {
  const item = { ...structuredClone(merchantPresets[merchantSelect.value].product), id: crypto.randomUUID(), enabled: false };
  const existing = item.adapter === "akile" && config.products.find((product) => product.adapter === "akile" && (product.area_mode !== "manual" || String(product.area_id) === item.area_id) && product.node_name?.trim().toLowerCase() === item.node_name.toLowerCase() && product.selector?.trim().toLowerCase() === item.selector.toLowerCase());
  if (existing) {
    openProducts.add(existing.id);
    renderProducts();
    editorNode.closest("details").open = true;
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
  setMessage(`FlareSolverr ${result.version} 服务连接成功；实际库存请在商品配置中点击“测试此商品”，无需开启监控。`, "success");
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
  if (event.target.id !== "interval-preset") updateDraftState();
});
document.querySelector(".shell").addEventListener("change", updateDraftState);
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
document.querySelector("#save").addEventListener("click", (event) => runAction(event.currentTarget, "保存中…", "正在保存配置…", () => saveConfig()));
runAction(null, "", "正在载入配置和监控状态…", () => reloadConfig(false), false);
bindManagementActions();
if (updateUnit) pollUpdateStatus();
