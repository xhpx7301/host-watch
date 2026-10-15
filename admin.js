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

function setMessage(message, kind = "") {
  messageNode.textContent = message;
  messageNode.className = kind;
}

function text(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
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

function productEditor(item, index) {
  const article = text("article", "", "product-editor");
  article.dataset.id = item.id;
  const head = text("div", "", "editor-head");
  head.append(text("strong", item.name || `新商品 ${index + 1}`));
  const actions = text("div", "", "actions");
  actions.append(checkField("启用此商品", item.enabled, "enabled"));
  const remove = text("button", "移除", "danger");
  remove.type = "button";
  remove.addEventListener("click", () => {
    config.products = config.products.filter((product) => product.id !== item.id);
    renderProducts();
    renderOverview();
  });
  actions.append(remove);
  head.append(actions);

  const grid = text("div", "", "form-grid");
  grid.append(
    selectField("监控适配器", item.adapter || (item.category_url ? "vmiss" : "html"), "adapter", [["html", "通用 HTTPS 网页"], ["vmiss", "VMISS WHMCS"]]),
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
  modeWrap.append(vmissFields, htmlFields);
  article.append(head, grid, modeWrap);

  article.addEventListener("input", () => syncEditor(article));
  article.addEventListener("change", () => {
    syncEditor(article);
    updateAdapterFields(article);
  });
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
  if (item.adapter === "vmiss") {
    item.category_url = get("category_url").value.trim();
    item.selector = get("selector").value.trim();
    const target = get("target").value;
    item.gid = target === "gid" ? get("target_id").value.trim() : "";
    item.pid = target === "pid" ? get("target_id").value.trim() : "";
  } else {
    item.url = get("url").value.trim();
    item.stock_selector = get("stock_selector").value.trim();
    item.stock_mode = get("stock_mode").value;
    item.purchase_url = get("purchase_url").value.trim();
    item.stock_pattern = get("stock_pattern").value.trim();
    item.available_pattern = get("available_pattern").value.trim();
    item.unavailable_pattern = get("unavailable_pattern").value.trim();
  }
  article.querySelector(".editor-head strong").textContent = item.name || "未命名商品";
  renderOverview();
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
}

function renderProducts() {
  editorNode.replaceChildren(...config.products.map(productEditor));
  renderProductList();
  renderOverview();
}

function stockLabel(product) {
  if (product.available <= 0) return "缺货";
  return product.stock_mode === "availability" ? "有货" : `${product.available} 件`;
}

function renderProductList() {
  const statusProducts = new Map((lastStatus?.products || []).map((product) => [product.id, product]));
  if (!config.products.length) {
    listNode.replaceChildren(text("div", "尚未添加商品。添加 VMISS 商品或通用 HTTPS 商品页开始监控。", "empty"));
    return;
  }
  const rows = config.products.map((item) => {
    const status = statusProducts.get(item.id);
    const article = text("article", "", "product");
    const identity = text("div", "", "product-name");
    identity.append(text("strong", item.name || "未命名商品"), text("span", `${item.provider || "自定义网站"}${item.enabled ? " · 监控中" : " · 已停用"}`));
    const stock = text("div", "", "metric");
    stock.append(text("span", "最近库存", "metric-label"), text("strong", status ? stockLabel(status) : "暂无数据", `metric-value ${status?.available > 0 ? "in" : "out"}`));
    const checked = text("div", "", "metric");
    checked.append(text("span", "商品状态", "metric-label"), text("strong", item.enabled ? (status ? (status.available > 0 ? "有货" : "缺货") : "等待检查") : "已停用", "metric-value"));
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
      article.scrollIntoView({ behavior: "smooth", block: "center" });
      editorNode.querySelector(`[data-id="${CSS.escape(item.id)}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
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
  document.querySelector("#overview-total-note").textContent = `${vendors.size} 个厂商 · ${enabled.length} 个启用`;
  document.querySelector("#overview-stock").textContent = String(inStock);
  document.querySelector("#overview-interval").textContent = `${Math.round((config.interval_seconds || 600) / 60)} 分钟`;
  const stateLabel = ({ ok: "运行正常", blocked: "访问被拦截", error: "检查异常", unknown: "等待检查" })[lastStatus?.state] || "等待检查";
  document.querySelector("#overview-state").textContent = config.enabled ? stateLabel : "监控已停用";
  document.querySelector("#overview-state-note").textContent = config.enabled ? (lastStatus?.message || "等待下一次检查") : "启用后按设定间隔检查";
}

function updateFetcherStatus() {
  const enabled = document.querySelector("#fetcher-mode").value === "flaresolverr";
  document.querySelector("#flaresolverr-url").disabled = !enabled;
  document.querySelector("#flaresolverr-timeout").disabled = !enabled;
  document.querySelector("#fetcher-status").textContent = enabled
    ? "只用于你拥有或明确获准测试的站点。保存后重启监控服务。"
    : "VMISS 普通 HTTPS 请求已启用。保存后重启监控服务。";
}

async function loadConfig() {
  const response = await fetch("/api/config", { cache: "no-store" });
  if (!response.ok) throw new Error(`读取配置失败（HTTP ${response.status}）`);
  config = await response.json();
  document.querySelector("#enabled").checked = Boolean(config.enabled);
  document.querySelector("#interval").value = config.interval_seconds || 600;
  document.querySelector("#affiliate").value = config.affiliate_id || "";
  document.querySelector("#notify-current").checked = Boolean(config.notify_current_stock_on_start);
  const fetcher = config.fetcher || {};
  document.querySelector("#fetcher-mode").value = fetcher.mode || "requests";
  document.querySelector("#flaresolverr-url").value = fetcher.flaresolverr_url || "http://127.0.0.1:8191/v1";
  document.querySelector("#flaresolverr-timeout").value = fetcher.timeout_seconds || 60;
  updateFetcherStatus();
  const telegram = config.telegram || {};
  document.querySelector("#telegram-token").value = "";
  document.querySelector("#telegram-chat-id").value = telegram.chat_id || "";
  document.querySelector("#telegram-disabled").checked = Boolean(telegram.disabled);
  document.querySelector("#telegram-clear").checked = false;
  document.querySelector("#telegram-status").textContent = telegram.configured ? "Telegram 已配置（token 不会回显）。" : (telegram.disabled ? "Telegram 已停用。" : "Telegram 尚未完整配置。");
  document.querySelector("#telegram-chats").replaceChildren();
  renderProducts();
}

async function loadStatus() {
  const response = await fetch("/api/status", { cache: "no-store" });
  if (!response.ok) throw new Error(`读取监控状态失败（HTTP ${response.status}）`);
  lastStatus = await response.json();
  const state = document.querySelector("#state");
  state.textContent = ({ ok: "最近检查成功", blocked: "因网站拦截已停止", error: "检查出错", unknown: "尚无检查记录" })[lastStatus.state] || lastStatus.state || "尚无检查记录";
  state.className = `state ${lastStatus.state || ""}`;
  document.querySelector("#last-checked").textContent = lastStatus.last_checked ? `上次检查：${new Date(lastStatus.last_checked).toLocaleString()}` : "";
  blockedHelpNode.hidden = lastStatus.state !== "blocked";
  document.querySelector("#blocked-message").textContent = lastStatus.message || "检测到拒绝访问或安全验证。请确认厂商允许当前的自动检查方式后再恢复。";
  renderProductList();
  renderOverview();
  if (lastStatus.state === "blocked") setMessage(`监控已停止：${lastStatus.message || "网站拒绝自动访问"}`, "error");
  else if (lastStatus.state === "error") setMessage(`监控错误：${lastStatus.message || "请查看服务器日志"}`, "error");
  else if (lastStatus.state === "ok") setMessage("监控最近一次检查成功。", "success");
}

document.querySelector("#add").addEventListener("click", () => {
  const item = { id: crypto.randomUUID(), adapter: "html", provider: "自定义网站", name: "", url: "", stock_selector: "", stock_mode: "quantity", stock_pattern: "([\\d,]+)", available_pattern: "(?i)in stock|available|有货|现货", unavailable_pattern: "(?i)out of stock|sold out|缺货|售罄", purchase_url: "", enabled: false };
  config.products.push(item);
  renderProducts();
  document.querySelector("#product-editors").closest("details").open = true;
  editorNode.lastElementChild?.scrollIntoView({ behavior: "smooth", block: "center" });
  setMessage("商品已添加，请填写商品信息后保存。", "pending");
});
document.querySelector("#reload").addEventListener("click", () => Promise.all([loadConfig(), loadStatus()]).then(() => setMessage("配置和状态已重新载入。", "success")).catch((error) => setMessage(error.message, "error")));
document.querySelector("#refresh-status").addEventListener("click", () => loadStatus().catch((error) => setMessage(error.message, "error")));
document.querySelector("#fetcher-mode").addEventListener("change", updateFetcherStatus);
document.querySelector("#interval").addEventListener("input", () => { config.interval_seconds = Number(document.querySelector("#interval").value); renderOverview(); });
document.querySelector("#telegram-test").addEventListener("click", async () => {
  const button = document.querySelector("#telegram-test");
  button.disabled = true;
  try {
    const response = await fetch("/api/telegram/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ bot_token: document.querySelector("#telegram-token").value.trim(), chat_id: document.querySelector("#telegram-chat-id").value.trim() }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Telegram 测试失败");
    setMessage("Telegram 测试消息已发送。", "success");
  } catch (error) { setMessage(error.message, "error"); }
  finally { button.disabled = false; }
});
document.querySelector("#telegram-load-chats").addEventListener("click", async () => {
  const button = document.querySelector("#telegram-load-chats");
  const chatsNode = document.querySelector("#telegram-chats");
  button.disabled = true;
  chatsNode.replaceChildren();
  try {
    const response = await fetch("/api/telegram/chats", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "读取聊天失败");
    if (!result.chats.length) { chatsNode.append(text("p", "暂时没有聊天记录；先打开 bot 并发送 /start。", "hint")); return; }
    for (const chat of result.chats) {
      const option = text("button", "", "chat-option");
      option.type = "button";
      option.append(text("span", `${chat.name}（${chat.type}）`), text("span", chat.id));
      option.addEventListener("click", () => { document.querySelector("#telegram-chat-id").value = chat.id; setMessage(`已选择 ${chat.name}，保存后生效。`, "pending"); });
      chatsNode.append(option);
    }
    setMessage(`已读取 ${result.chats.length} 个聊天。`, "success");
  } catch (error) { setMessage(error.message, "error"); }
  finally { button.disabled = false; }
});

async function saveConfig() {
  setMessage("正在保存配置…", "pending");
  const saveButton = document.querySelector("#save");
  saveButton.disabled = true;
  const products = config.products.map((item) => ({ ...item }));
  const next = {
    enabled: document.querySelector("#enabled").checked,
    interval_seconds: Number(document.querySelector("#interval").value),
    affiliate_id: document.querySelector("#affiliate").value.trim(),
    notify_current_stock_on_start: document.querySelector("#notify-current").checked,
    fetcher: { mode: document.querySelector("#fetcher-mode").value, flaresolverr_url: document.querySelector("#flaresolverr-url").value.trim(), timeout_seconds: Number(document.querySelector("#flaresolverr-timeout").value) },
    telegram: { bot_token: document.querySelector("#telegram-token").value.trim(), chat_id: document.querySelector("#telegram-chat-id").value.trim(), disabled: document.querySelector("#telegram-disabled").checked, clear: document.querySelector("#telegram-clear").checked },
    products,
  };
  try {
    const response = await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(next) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "保存失败");
    document.querySelector("#telegram-token").value = "";
    await loadConfig();
    document.querySelector("#enabled").checked = next.enabled;
    setMessage("配置已保存。需要在服务器重启 vmiss-monitor.service 后生效。", "success");
  } catch (error) {
    setMessage(error.message, "error");
  } finally { saveButton.disabled = false; }
}
document.querySelector("#save").addEventListener("click", saveConfig);
loadConfig().then(loadStatus).then(() => setMessage("配置和监控状态已载入。", "success")).catch((error) => setMessage(error.message, "error"));
