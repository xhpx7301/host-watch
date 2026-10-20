let config = {
  enabled: false,
  interval_seconds: 600,
  monitor_schedule: { mode: "auto", interval_seconds: 600 },
  notify_schedule: { mode: "auto", interval_seconds: 600 },
  catalog_schedule: { mode: "manual", interval_seconds: 300 },
  seckill_schedule: { mode: "manual", interval_seconds: 5 },
  provider_schedules: {},
  notify_current_stock_on_start: false,
  isvoro_seckill: false,
  affiliate_id: "",
  affiliates: {},
  merchant_order: ["vmiss", "vps_soy", "akile", "isvoro", "dmit", "bwh"],
  watch_order: [],
  notify_order: [],
  fetcher: { mode: "requests", flaresolverr_url: "http://127.0.0.1:8191/v1", timeout_seconds: 60 },
  telegram: {},
  egress: { nodes: [], routes: {} },
  akile_market: { enabled: false, interval_seconds: 60, account_id: "", items: [] },
  isvoro_market: { enabled: false, interval_seconds: 600, account_id: "", items: [] },
  vps_seckill: { enabled: false, mode: "manual", interval_seconds: 5, items: [], accounts: [] },
  products: [],
};
const editorNode = document.querySelector("#product-editors");
const listNode = document.querySelector("#product-list");
const messageNode = document.querySelector("#message");
const blockedHelpNode = document.querySelector("#blocked-help");
let lastStatus = null;
const toastNode = document.querySelector("#toast");
let toastTimer;
const TOAST_TIMEOUT_MS = 30_000;
let actionRunning = false;
let actionDepth = 0;
const autoSaveTimers = new Map();
let updateUnit = sessionStorage.getItem("hostwatch-update-unit") || "";
const UPDATE_RELOAD_KEY = "hostwatch-update-reload-at";
let updateReloadTimer;
let managementUsers = [];
let backupScheduleDraft = null;
let backupScheduleSaved = null;
let backupScheduleRuntime = null;
let monitorService = null;
let savedConfig = null;
const providerFlareDraft = new Map();
let serviceRefreshTimer, serviceRefreshPending = false;
let serviceTransitionStartedAt = 0;
let hostwatchService = null;
let egressHealthTimer;
let egressHealthRunning = false;
let updatePollTimer;
let updatePollTimeout;
let exchangeRateData = null;
let exchangeClockTimer;
let exchangeRateRefreshTimer;
const SERVICE_TRANSITION_TIMEOUT_MS = 60_000;
const compareSelection = new Set();
const openProducts = new Set();
const productTests = new Map();
let activeTest = null;
let productDrag = null;
let merchantOrderEditing = false;
const seckillTestId = "isvoro-seckill";
const cloudscraperTestId = "cloudscraper-probe";
const BEIJING_TIME_ZONE = "Asia/Shanghai";
const EGRESS_HEALTH_INTERVAL_MS = 60_000;

function formatBeijingDate(value, options = {}) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.valueOf())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: BEIJING_TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", ...options }).format(date);
}

function beijingISOString(value = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: BEIJING_TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).formatToParts(value).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}+08:00`;
}

const pages = {
  overview: { title: "运行概览", description: "每分钟查看大盘、监控、通知、抢购的状态与最近刷新记录。" },
  catalog: { title: "VPS 库存大盘", description: "浏览商家全部公开套餐或地区分类，选择你关心的 VPS 加入补货提醒。" },
  seckill: { title: "VPS 补货抢购", description: "从库存监控或库存通知选择商品，配置优惠码和账户后执行补货抢购。" },
  market: { title: "Akile 交易市场", description: "查看 Akile 交易市场快照，按续费价格和到期时间计算折价/溢价金额与比例，设置指定商品的 Telegram 提醒。" },
  accounts: { title: "网站账户", description: "集中管理 VPS 补货抢购使用的网站登录账户，检查账户状态并导入商家认证信息。" },
  products: { title: "自定义商品", description: "新增大盘未收录的商品，编辑手动配置，或恢复已移出的商品。" },
  monitor: { title: "VPS 库存监控", description: "集中查看你关注的 VPS，查看库存与配置、编辑备注和线路，直接购买，不发送通知。" },
  notifications: { title: "VPS 库存通知", description: "选择需要 Telegram 库存提醒的商品，管理通知和测试。" },
  affiliates: { title: "AFF 管理", description: "统一管理各商家推广码，保存后应用到新的购买入口。" },
  requests: { title: "网页请求", description: "设置各商家请求方式，配置轻量请求与浏览器请求，测试访问效果。" },
  settings: { title: "设置", description: "集中管理服务、通知、备份和管理员账号。" },
};
let currentPage = null;
const mobileNavToggle = document.querySelector("#mobile-nav-toggle");
const mobileNavClose = document.querySelector("#mobile-nav-close");
const mobileNavScrim = document.querySelector("#mobile-nav-scrim");

function setMobileNav(open, restoreFocus = true) {
  if (!mobileNavToggle || !mobileNavClose || !mobileNavScrim) return;
  document.body.classList.toggle("mobile-nav-open", open);
  mobileNavToggle.setAttribute("aria-expanded", String(open));
  mobileNavToggle.setAttribute("aria-label", open ? "导航已打开" : "打开导航");
  mobileNavToggle.title = open ? "导航已打开" : "打开导航";
  mobileNavScrim.hidden = !open;
  if (open) mobileNavClose.focus({ preventScroll: true });
  else if (restoreFocus) mobileNavToggle.focus({ preventScroll: true });
}

mobileNavToggle?.addEventListener("click", () => setMobileNav(true));
mobileNavClose?.addEventListener("click", () => setMobileNav(false));
mobileNavScrim?.addEventListener("click", () => setMobileNav(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && document.body.classList.contains("mobile-nav-open")) setMobileNav(false);
});
window.addEventListener("resize", () => {
  if (window.innerWidth > 780 && document.body.classList.contains("mobile-nav-open")) setMobileNav(false, false);
});

function navigatePage(name, { history = true, focus = true } = {}) {
  if (name === "narwhal") name = "catalog";
  if (name === "telegram" || name === "management") name = "settings";
  if (!Object.hasOwn(pages, name)) name = "overview";
  if (history && location.hash !== `#${name}`) window.history.pushState(null, "", `#${name}`);
  sessionStorage.setItem("hostwatch-current-page", name);
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
    if (document.body.classList.contains("mobile-nav-open")) setMobileNav(false);
  });
});
window.addEventListener("hashchange", () => navigatePage(location.hash.slice(1), { history: false }));
const initialPage = location.hash.slice(1) || sessionStorage.getItem("hostwatch-current-page") || "overview";
navigatePage(initialPage, { history: false, focus: false });

let marketSnapshot = { items: [], results: [], updated_at: "", error: "" };
let marketBusy = false;
let marketRuleTestBusy = "";
const marketRuleTestMessages = new Map();
let marketNotificationTestBusy = false;
let marketPage = 0;
function activeMarketProvider() { return document.querySelector("#market-provider")?.value || "akile"; }
function marketConfig() { const isvoro = activeMarketProvider() === "isvoro", key = isvoro ? "isvoro_market" : "akile_market"; return config[key] ||= { enabled: false, interval_seconds: isvoro ? 600 : 60, account_id: "", items: [] }; }
function marketCurrency(value, currency = "CNY") { return Number.isFinite(value) ? `${currency === "CNY" ? "¥" : `${currency} `}${value.toFixed(2)}` : "—"; }
function marketLink(id) { return activeMarketProvider() === "isvoro" ? "https://isvoro.com/marketplace" : `https://akile.ai/console/pushshop?highlight=${encodeURIComponent(id)}`; }
function marketUnit(value, unit) {
  if (value === null || value === undefined || String(value).trim() === "") return "";
  const display = typeof value === "number" && Number.isFinite(value) ? String(value) : String(value).trim();
  return /^\d+(?:\.\d+)?$/.test(display) ? `${display} ${unit}` : display;
}
function marketCapacity(value, defaultUnit) {
  if (value === null || value === undefined || String(value).trim() === "") return "";
  const display = String(value).trim();
  const match = display.match(/^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(MiB|GiB|TiB|PiB|MB|GB|TB|PB)?$/i);
  if (!match) return display;
  let amount = Number(match[1]);
  const unit = (match[2] || defaultUnit).toUpperCase().replace("IB", "B");
  const units = ["MB", "GB", "TB", "PB"];
  let index = units.indexOf(unit);
  if (!Number.isFinite(amount) || index < 0) return display;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index++;
  }
  return `${Number(amount.toFixed(3))}${units[index]}`;
}
function marketSpecText(item) {
  return [marketUnit(item.cpu, "核"), marketCapacity(item.memory, "MB"), marketCapacity(item.disk, "GB"),
    item.bandwidth ? `带宽 ${marketUnit(item.bandwidth, "Mbps")}` : "",
    item.flow ? `流量 ${marketCapacity(item.flow, "GB")}` : ""].filter(Boolean).join(" · ");
}
function marketRenewalText(item) {
  if (!Number.isFinite(item.renewal)) return "";
  const cycle = Number(item.cycle), cycleLabel = Number.isFinite(cycle) && cycle > 0 ? `${cycle}个月` : "周期未知";
  const monthly = Number.isFinite(cycle) && cycle > 0 ? ` · 折合 ${marketCurrency(item.renewal / cycle)}/月` : "";
  return `续费 ${marketCurrency(item.renewal)}/${cycleLabel}${monthly}`;
}
function marketRuleInfo(item) {
  const info = {};
  for (const key of ["cpu", "memory", "disk", "bandwidth", "flow"]) {
    if (item[key] !== null && item[key] !== undefined && String(item[key]).trim() !== "") info[key] = String(item[key]).trim();
  }
  if (Number.isFinite(item.renewal)) info.renewal = item.renewal;
  if (item.cycle !== null && item.cycle !== undefined && String(item.cycle).trim() !== "") info.cycle = String(item.cycle).trim();
  return Object.keys(info).length ? info : null;
}
function renderMarketSettings() {
  const value = marketConfig(), account = document.querySelector("#market-account");
  const provider = activeMarketProvider(), providerName = provider === "isvoro" ? "Isvoro" : "Akile";
  document.querySelector("#market-account-label").textContent = `${providerName} 网站账户`;
  const source = document.querySelector("#market-source-link");
  source.href = provider === "isvoro" ? "https://isvoro.com/marketplace" : "https://akile.ai/console/pushshop";
  source.textContent = `打开 ${providerName} 市场 ↗`;
  account.replaceChildren(new Option(`请选择已保存的 ${providerName} 账户`, ""));
  (config.vps_seckill?.accounts || []).filter(row => row.provider === provider).forEach(row => account.append(new Option(row.name || row.username || row.id, row.id)));
  if (value.account_id && ![...account.options].some(option => option.value === value.account_id)) account.append(new Option("已绑定账户已删除，请重新选择", value.account_id));
  account.value = value.account_id || "";
  document.querySelector("#market-enabled").value = value.enabled ? "on" : "off";
  document.querySelector("#market-interval").value = value.interval_seconds ?? 60;
  renderMarketRules();
  renderMarket();
}
function collectMarket() {
  return { ...marketConfig(), account_id: document.querySelector("#market-account").value,
    enabled: document.querySelector("#market-enabled").value === "on",
    interval_seconds: Number(document.querySelector("#market-interval").value), items: marketConfig().items.map(row => ({ ...row })) };
}
function addMarketRule(item) {
  const marketInfo = marketRuleInfo(item);
  const isvoro = activeMarketProvider() === "isvoro";
  const rule = { id: `market-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, product_id: item.id,
    name: item.name, plan_name: item.plan_name, region: item.region, node: item.node,
    target: isvoro ? "renewal" : "listing", renewal_price: isvoro ? item.renewal : null,
    direction: "premium", rate_threshold: 10, amount_threshold: null,
    match: "any", action: "notify", enabled: true, ...(marketInfo ? { market_info: marketInfo } : {}) };
  marketConfig().items.push(rule);
  renderMarketRules(); renderMarket(); updateDraftState();
  document.querySelector("#market-rule-items").lastElementChild?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  setMessage("已添加商品规则，请设置阈值并保存。", "pending", true, false);
}
function renderMarketRules() {
  const target = document.querySelector("#market-rule-items"); target.replaceChildren();
  for (const rule of marketConfig().items) {
    const card = text("article", "", "market-rule-item"), head = text("div", "", "section-head");
    card.dataset.marketRuleId = rule.id;
    const isvoro = activeMarketProvider() === "isvoro";
    const ruleTitle = rule.target === "renewal" ? `${rule.region || "—"} · 续费 ${marketCurrency(rule.renewal_price)}` : `${rule.name || rule.product_id} · ${rule.region} · ${rule.node}`;
    head.append(text("strong", ruleTitle));
    const remove = text("button", "移除", "danger"); remove.type = "button";
    remove.addEventListener("click", () => { marketConfig().items = marketConfig().items.filter(row => row.id !== rule.id); renderMarketRules(); updateDraftState(); });
    const test = text("button", marketRuleTestBusy === rule.id ? "检查中…" : "测试", "secondary"); test.type = "button";
    test.disabled = Boolean(marketRuleTestBusy) || marketBusy || !rule.enabled;
    test.title = rule.enabled ? "仅检查这条已保存的规则" : "启用规则后才能测试";
    test.addEventListener("click", () => testMarketRule(rule.id));
    head.append(test, remove); card.append(head, text("div", "", "market-rule-meta"));
    const testState = text("p", marketRuleTestMessages.get(rule.id) || "", "hint market-rule-test-state");
    testState.setAttribute("aria-live", "polite"); card.append(testState);
    const grid = text("div", "", "form-grid");
    for (const [key, label, options] of [
      ["target", "监控范围", isvoro
        ? [["renewal", "同地区、同续费价格商品"], ["listing", `此挂单 #${rule.product_id}`]]
        : [["listing", `此挂单 #${rule.product_id}`], ["plan", "同地区、同节点、同套餐新挂单"]]],
      ["direction", "阈值方向", [["discount", "折价达到最低值（≥）"], ["premium", "溢价低于最高值（≤）"]]],
      ["match", "条件关系", [["any", "比例或金额任一满足"], ["all", "比例与金额全部满足"]]],
      ["action", "达到条件后", activeMarketProvider() === "isvoro" ? [["notify", "Telegram 提醒手动购买"]] : [["notify", "Telegram 提醒手动购买"], ["unpaid_order", "待付款下单（官网验证受限）"]]],
      ["enabled", "本条规则", [["on", "启用"], ["off", "暂停"]]],
    ]) {
      const field = text("label", label), select = document.createElement("select");
      options.forEach(([value, label]) => select.append(new Option(label, value)));
      select.value = key === "enabled" ? (rule.enabled ? "on" : "off") : rule[key];
      select.addEventListener("change", () => { rule[key] = key === "enabled" ? select.value === "on" : select.value; updateDraftState(); });
      field.append(select); grid.append(field);
    }
    if (isvoro && rule.target === "renewal") {
      const field = text("label", "续费价格（元）"), input = document.createElement("input");
      input.type = "number"; input.min = "0"; input.max = "1000000"; input.step = "0.01";
      input.value = rule.renewal_price ?? "";
      input.addEventListener("input", () => { rule.renewal_price = input.value === "" ? null : Number(input.value); updateDraftState(); });
      field.append(input); grid.append(field);
    }
    for (const [key, label] of [["rate_threshold", "比例阈值（%）"], ["amount_threshold", "金额阈值（人民币元）"]]) {
      const field = text("label", label), input = document.createElement("input");
      input.type = "number"; input.min = "0"; input.max = "1000000"; input.step = "0.01";
      input.value = rule[key] ?? ""; input.placeholder = "留空不使用";
      input.addEventListener("input", () => { rule[key] = input.value === "" ? null : Number(input.value); updateDraftState(); });
      field.append(input); grid.append(field);
    }
    card.append(grid, text("p", isvoro ? (rule.target === "renewal" ? `匹配条件：${rule.region || "—"} · 续费 ${marketCurrency(rule.renewal_price)}；同地区同续费价的新挂单都会参与检查。这里只发送提醒，不会购买或付款。` : `套餐匹配：${rule.plan_name || "—"}；这里只发送提醒，不会购买或付款。`) : `套餐匹配：${rule.plan_name || "—"}；金额按剩余价值差额计算，比例以续费月付价为基准。待付款模式受 Akile Passkey 购买验证限制，当前只通知需要手动操作，未创建订单。`, "hint"));
    target.append(card);
  }
  document.querySelector("#market-rule-empty").hidden = marketConfig().items.length > 0;
  renderMarketRuleDetails();
}
function renderMarketRuleDetails() {
  const accountId = document.querySelector("#market-account")?.value || "";
  const snapshotMatches = marketSnapshot.account_id === accountId && Boolean(marketSnapshot.updated_at);
  for (const card of document.querySelectorAll(".market-rule-item")) {
    const rule = marketConfig().items.find(row => row.id === card.dataset.marketRuleId);
    const meta = card.querySelector(".market-rule-meta");
    if (!rule || !meta) continue;
    meta.replaceChildren();
    const current = snapshotMatches ? (marketSnapshot.items || []).filter(item => rule.target === "listing"
      ? String(item.id) === String(rule.product_id)
      : rule.target === "renewal" ? item.region === rule.region && Math.abs(Number(item.renewal) - Number(rule.renewal_price)) <= 0.01
      : item.plan_name === rule.plan_name && item.region === rule.region && item.node === rule.node) : [];
    const groups = new Map();
    for (const item of current) {
      const spec = marketSpecText(item), renewal = marketRenewalText(item);
      if (!spec && !renewal) continue;
      const key = `${spec}\u0000${renewal}`;
      if (!groups.has(key)) groups.set(key, { spec, renewal, ids: [] });
      groups.get(key).ids.push(item.id);
    }
    if (groups.size) {
      meta.append(text("strong", `最新市场信息（${current.length} 条匹配挂单）`));
      for (const group of [...groups.values()].slice(0, 4)) {
        const parts = [group.spec && `配置：${group.spec}`, group.renewal].filter(Boolean);
        const ids = group.ids.slice(0, 3).map(id => `#${id}`).join("、");
        if (ids) parts.push(ids + (group.ids.length > 3 ? ` 等 ${group.ids.length} 条` : group.ids.length > 1 ? `（${group.ids.length} 条）` : ""));
        meta.append(text("p", parts.join(" · ")));
      }
      if (groups.size > 4) meta.append(text("p", `另有 ${groups.size - 4} 种配置或续费价格。`, "hint"));
    } else if (snapshotMatches && current.length) {
      meta.append(text("p", "最新挂单没有返回可用的配置或续费信息。", "hint"));
    } else {
      meta.append(text("p", snapshotMatches ? "当前快照中未找到符合此规则的挂单。" : "刷新市场后显示最新挂单信息。", "hint"));
    }
    const saved = rule.market_info;
    if (saved && typeof saved === "object") {
      const spec = marketSpecText(saved), renewal = marketRenewalText(saved);
      const parts = [spec && `配置：${spec}`, renewal].filter(Boolean);
      if (parts.length) meta.append(text("p", `添加时记录：${parts.join(" · ")}`, "hint"));
    }
  }
}
function renderMarket() {
  const rowsNode = document.querySelector("#market-rows"); if (!rowsNode) return;
  const accountId = document.querySelector("#market-account")?.value || "";
  const all = marketSnapshot.account_id === accountId ? (marketSnapshot.items || []) : [];
  const search = document.querySelector("#market-search").value.trim().toLowerCase();
  const isvoro = activeMarketProvider() === "isvoro";
  document.querySelector("#market-product-label").textContent = isvoro ? "续费价格" : "商品";
  const regionNode = document.querySelector("#market-region"), region = regionNode.value;
  const regions = [...new Set(all.map(row => row.region).filter(Boolean))];
  regionNode.replaceChildren(new Option("全部地区", "all"), ...regions.map(value => new Option(value, value)));
  regionNode.value = regions.includes(region) ? region : "all";
  const productNode = document.querySelector("#market-product"), product = productNode.value;
  const regionProducts = regionNode.value === "all" ? all : all.filter(row => row.region === regionNode.value);
  const products = [...new Set(regionProducts.map(row => isvoro ? row.renewal : (row.plan_name || row.name)).filter(value => value !== null && value !== undefined && value !== ""))]
    .sort((a, b) => Number(a) - Number(b));
  productNode.replaceChildren(new Option(isvoro ? "全部续费价格" : "全部商品", "all"), ...products.map(value => new Option(isvoro ? marketCurrency(Number(value)) : value, String(value))));
  productNode.value = products.map(String).includes(product) ? product : "all";
  const visible = all.filter(row => {
    if (search && !`${row.id} ${row.name} ${row.region} ${row.node} ${row.renewal ?? ""}`.toLowerCase().includes(search)) return false;
    if (regionNode.value !== "all" && row.region !== regionNode.value) return false;
    if (productNode.value !== "all" && String(isvoro ? row.renewal : (row.plan_name || row.name)) !== productNode.value) return false;
    return true;
  });
  const columnSort = readMarketColumnSort();
  const compareNumeric = (a, b, key) => {
    const left = Number(a[key]), right = Number(b[key]);
    if (!Number.isFinite(left) && !Number.isFinite(right)) return 0;
    if (!Number.isFinite(left)) return 1;
    if (!Number.isFinite(right)) return -1;
    return (left - right) * (columnSort.direction === "desc" ? -1 : 1);
  };
  if (columnSort.key) visible.sort((a, b) => compareNumeric(a, b, columnSort.key));
  else visible.sort((a, b) => (b.rate ?? -Infinity) - (a.rate ?? -Infinity));
  const pageSize = 15, pageCount = Math.max(1, Math.ceil(visible.length / pageSize));
  marketPage = Math.min(marketPage, pageCount - 1);
  const headRow = document.querySelector(".market-table thead tr");
  if (headRow) {
    headRow.replaceChildren();
    [["商品 / 配置"], ["地区 / 节点"], ["续费价格", "renewal"], ["到期 / 剩余时间", "due_time"], ["售价", "price"], ["剩余价值", "fair_value"], ["折价 / 溢价率（月付基准）", "rate"], ["折价 / 溢价金额", "delta"], ["操作"]].forEach(([label, key]) => {
      const th = document.createElement("th"); th.scope = "col";
      th.append(key ? createMarketSortButton(label, key) : text("span", label));
      headRow.append(th);
    });
  }
  rowsNode.replaceChildren();
  for (const row of visible.slice(marketPage * pageSize, (marketPage + 1) * pageSize)) {
    const tr = document.createElement("tr");
    const cell = (...nodes) => { const td = document.createElement("td"); td.append(...nodes); return td; };
    const priceKind = row.delta >= 0 ? "折价" : "溢价", tone = row.delta >= 0 ? "discount" : "premium";
    const actions = text("div", "", "actions"), add = text("button", "添加监控"); add.type = "button";
    add.disabled = Boolean(row.valuation_error); add.addEventListener("click", () => addMarketRule(row));
    const buy = text("a", "官网查看", "market-buy"); buy.href = marketLink(row.id); buy.target = "_blank"; buy.rel = "noopener noreferrer";
    actions.append(add,buy);
    const hardware = `${marketUnit(row.cpu, "C") || "—"}/${marketCapacity(row.memory, "MB") || "—"}/${marketCapacity(row.disk, "GB") || "—"}`;
    const network = `带宽 ${marketUnit(row.bandwidth, "Mbps") || "—"} · 流量 ${marketCapacity(row.flow, "GB") || "—"}`;
    tr.append(cell(text("strong", row.name), text("small", `#${row.id} · ${hardware}`), text("small", network, "market-network"), text("small", row.is_bid ? "拍卖 · 不触发规则" : row.detail || "")),
      cell(text("strong", row.region || "—"), text("small", row.node || "—")),
      cell(text("span", `${marketCurrency(row.renewal, row.currency_code)}/${row.cycle ?? "?"}${row.cycle_unit === "year" ? "年" : "个月"}`, "market-number")),
      cell(text("span", row.due_time ? formatBeijingDate(new Date(row.due_time * 1000)) : "—"), text("small", row.remaining_days > 0 ? `剩余 ${row.remaining_days.toFixed(2)} 天` : "已到期")),
      cell(text("strong", marketCurrency(row.price, row.currency_code))), cell(text("span", marketCurrency(row.fair_value, row.currency_code))),
      cell(text("span", Number.isFinite(row.rate) ? `${priceKind} ${Math.abs(row.rate).toFixed(2)}%` : row.valuation_error || "无法估值", `market-deal ${Number.isFinite(row.rate) ? tone : "neutral"}`)),
      cell(text("span", Number.isFinite(row.delta) ? `${priceKind} ${marketCurrency(Math.abs(row.delta), row.currency_code)}` : "—")),cell(actions));
    rowsNode.append(tr);
  }
  document.querySelector("#market-empty").hidden = visible.length > 0;
  document.querySelector("#market-empty").textContent = all.length ? "没有符合条件的商品。" : `选择已保存的 ${activeMarketProvider() === "isvoro" ? "Isvoro" : "Akile"} 账户，点击刷新获取市场商品。`;
  document.querySelector("#market-page-label").textContent = `共 ${visible.length} 条 · 第 ${marketPage + 1}/${pageCount} 页`;
  document.querySelector("#market-prev").disabled = marketPage === 0;
  document.querySelector("#market-next").disabled = marketPage + 1 >= pageCount;
  document.querySelector("#market-count").textContent = all.length;
  document.querySelector("#market-discount-count").textContent = all.filter(row=>row.delta > 0).length;
  document.querySelector("#market-expiring-count").textContent = all.filter(row=>row.remaining_days > 0 && row.remaining_days <= 7).length;
  document.querySelector("#market-updated").textContent = marketSnapshot.updated_at && all.length ? formatBeijingDate(marketSnapshot.updated_at) : "—";
  const state = document.querySelector("#market-snapshot-state"), enabled = marketConfig().enabled;
  state.textContent = marketBusy ? `正在读取 ${activeMarketProvider() === "isvoro" ? "Isvoro" : "Akile"} 市场…` : marketSnapshot.error || marketSnapshot.warning || `${enabled ? "后台监控已启用" : "后台监控已暂停"} · ${all.length ? "真实市场快照" : "尚无快照"}`;
  state.className = `state ${marketSnapshot.error ? "error" : marketBusy ? "pending" : all.length ? "ok" : ""}`;
  document.querySelector("#market-refresh").disabled = marketBusy || !accountId;
  document.querySelector("#market-check").disabled = marketBusy || !marketConfig().items.some(row=>row.enabled);
  document.querySelector("#market-test-notification").disabled = marketBusy || marketNotificationTestBusy;
  const result = document.querySelector("#market-rule-results"); result.replaceChildren();
  for (const entry of marketSnapshot.results || []) {
    const rule = marketConfig().items.find(row=>row.id === entry.rule_id); if (!rule) continue;
    result.append(text("p", `${rule.name || rule.product_id}${entry.product_id ? ` #${entry.product_id}` : ""}：${entry.message} ${entry.notification || ""}`));
  }
}
async function loadMarketSnapshot() {
  if (marketBusy) return;
  try { marketSnapshot = await requestJSON(`/api/market?provider=${activeMarketProvider()}`); renderMarketRuleDetails(); renderMarket(); }
  catch (error) { document.querySelector("#market-snapshot-state").textContent = error.message; }
}
async function refreshMarket(evaluate) {
  if (marketBusy) return;
  if (evaluate && scopeIsDirty("market")) { setMessage("请先保存交易市场规则后再检查。", "error"); return; }
  marketBusy = true; renderMarket();
  try {
    marketSnapshot = await requestJSON(evaluate ? "/api/market/check" : "/api/market/refresh", {
      method:"POST", headers:{"Content-Type":"application/json"}, timeoutMs:120000,
      body: JSON.stringify({ provider: activeMarketProvider(), account_id:document.querySelector("#market-account").value }),
    });
    setMessage(evaluate ? "已检查保存的规则，触发结果见下方。" : `${activeMarketProvider() === "isvoro" ? "Isvoro" : "Akile"} 市场商品已刷新。`, "success", true, false);
  } catch(error) { marketSnapshot.error = error.message; setMessage(error.message, "error"); }
  finally { marketBusy = false; renderMarketRuleDetails(); renderMarket(); }
}
async function testMarketRule(ruleId) {
  if (marketBusy || marketRuleTestBusy) return;
  if (scopeIsDirty("market")) { setMessage("请先保存交易市场规则后再测试。", "error"); return; }
  const rule = marketConfig().items.find(row => row.id === ruleId);
  if (!rule?.enabled) return;
  marketRuleTestBusy = ruleId; marketRuleTestMessages.set(ruleId, "正在检查此规则…"); renderMarketRules(); renderMarket();
  try {
    const result = await requestJSON("/api/market/check", {
      method: "POST", headers: { "Content-Type": "application/json" }, timeoutMs: 120000,
      body: JSON.stringify({ provider: activeMarketProvider(), account_id: document.querySelector("#market-account").value, rule_id: ruleId }),
    });
    marketSnapshot = result;
    const entries = (result.results || []).filter(row => row.rule_id === ruleId);
    marketRuleTestMessages.set(ruleId, entries.length
      ? entries.map(row => `${row.product_id ? `#${row.product_id}：` : ""}${row.message}${row.notification ? ` · ${row.notification}` : ""}`).join("\n")
      : "本次检查没有返回该规则的结果。");
  } catch (error) {
    marketRuleTestMessages.set(ruleId, `检查失败：${error.message}`);
  } finally {
    marketRuleTestBusy = ""; renderMarketRules(); renderMarketRuleDetails(); renderMarket();
  }
}
document.querySelector("#market-refresh").addEventListener("click", () => refreshMarket(false));
document.querySelector("#market-check").addEventListener("click", () => refreshMarket(true));
document.querySelector("#market-test-notification").addEventListener("click", async event => {
  if (marketNotificationTestBusy) return;
  marketNotificationTestBusy = true; renderMarket();
  try {
    await runAction(event.currentTarget, "发送中…", `正在发送 ${activeMarketProvider() === "isvoro" ? "Isvoro" : "Akile"} 市场测试通知…`, async () => {
      const result = await requestJSON("/api/market/test-notification", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider: activeMarketProvider() }) });
      setMessage(result.message, "success");
    });
  } finally { marketNotificationTestBusy = false; renderMarket(); }
});
for (const id of ["market-account", "market-enabled", "market-interval"]) document.getElementById(id).addEventListener("input", () => {
  Object.assign(marketConfig(), collectMarket()); updateDraftState();
  if (id === "market-account") renderMarketRuleDetails();
  renderMarket();
});
document.querySelector("#market-provider").addEventListener("change", () => { marketPage = 0; renderMarketSettings(); void loadMarketSnapshot(); });
for (const id of ["market-search", "market-region", "market-product"]) document.getElementById(id).addEventListener("input", () => { marketPage = 0; renderMarket(); });
document.querySelector("#market-prev").addEventListener("click", () => { marketPage--; renderMarket(); });
document.querySelector("#market-next").addEventListener("click", () => { marketPage++; renderMarket(); });
window.addEventListener("hostwatch-page-change", event => { if (event.detail === "market") void loadMarketSnapshot(); });
window.addEventListener("hostwatch-page-change", event => { if (["seckill", "accounts"].includes(event.detail)) scheduleAccountHealthCheck(500); else clearTimeout(accountHealthTimer); });
setInterval(() => { if (!document.hidden && currentPage === "market" && savedConfig) void loadMarketSnapshot(); }, 10000);

function setMessage(message, kind = "", notify = true, inline = true) {
  if (inline) {
    messageNode.textContent = message;
    messageNode.className = kind;
  } else if (messageNode.className === "pending") {
    messageNode.textContent = "";
    messageNode.className = "";
  }
  if (!notify) return;
  clearTimeout(toastTimer);
  toastNode.className = `toast ${kind}`;
  toastNode.hidden = false;
  document.querySelector("#toast-label").textContent = ({ pending: "操作提示", success: "操作成功", error: "操作失败" })[kind] || "操作提示";
  document.querySelector("#toast-message").textContent = message;
  // Keep every operation result visible long enough to read, then dismiss it
  // automatically so a stale error cannot remain over the page indefinitely.
  toastTimer = setTimeout(() => { toastNode.hidden = true; }, TOAST_TIMEOUT_MS);
}

document.querySelector("#toast-close").addEventListener("click", () => {
  clearTimeout(toastTimer);
  toastNode.hidden = true;
});

document.querySelector("#logout-button")?.addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "退出中…";
  try {
    await requestJSON("/api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}", timeoutMs: 10_000 });
  } catch {
    // The session may already have expired; returning to the login page is
    // still the safest and clearest outcome.
  }
  window.location.replace("/");
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
    if (response.status === 401 && !url.startsWith("/api/auth/")) {
      window.location.replace("/");
      throw Object.assign(new Error("登录状态已失效，正在返回登录页"), { status: response.status });
    }
    if (!response.ok) throw Object.assign(new Error(result.error || `请求失败（HTTP ${response.status}）`), { status: response.status });
    return result;
  } catch (error) {
    if (controller.signal.aborted) throw new Error("请求超时，请核对结果及反向代理超时设置后重试。");
    if (error instanceof TypeError) throw new Error("网络连接失败，请检查连接并刷新状态核对结果。");
    throw error;
  } finally { clearTimeout(timer); signal?.removeEventListener("abort", cancel); }
}

async function runAction(button, label, message, action, notify = true, inline = true) {
  cancelProductDrag();
  actionDepth += 1;
  actionRunning = true;
  const previousLabel = button?.textContent;
  if (button) {
    button.textContent = label;
    button.setAttribute("aria-busy", "true");
  }
  setMessage(message, "pending", notify, inline);
  try { await action(); }
  catch (error) { setMessage(error.message, "error", true, inline); }
  finally {
    actionDepth = Math.max(0, actionDepth - 1);
    actionRunning = actionDepth > 0;
    if (button) {
      button.textContent = previousLabel;
      button.removeAttribute("aria-busy");
    }
    syncUpdateControl();
    renderMerchantOrder();
    renderProductList();
    syncTestControls();
    updateCardSaveStates();
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

function setFlareModeButtons(buttons, light) {
  buttons.forEach(button => button.setAttribute("aria-pressed", String(button.dataset.flareMode === (light ? "light" : "full"))));
}

function createFlareModeButtons(label, light, onChange) {
  const group = text("div", "", "mode-buttons");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", `${label}加载模式`);
  const buttons = [
    ["light", "轻量加载"],
    ["full", "完整加载"],
  ].map(([mode, name]) => {
    const button = text("button", name, "option-button mode-button");
    button.type = "button";
    button.dataset.flareMode = mode;
    button.setAttribute("aria-pressed", String(Boolean(light) === (mode === "light")));
    button.disabled = actionRunning;
    button.addEventListener("click", () => {
      setFlareModeButtons(buttons, mode === "light");
      onChange(mode === "light");
      updateDraftState();
    });
    group.append(button);
    return button;
  });
  return group;
}

function settingButton(label, pressed, onChange, states = ["未允许", "已允许"]) {
  const button = text("button", "", "option-button"); button.type = "button";
  const state = text("span", ""); state.dataset.optionState = "";
  button.append(text("span", label), state);
  const update = value => { button.setAttribute("aria-pressed", String(value)); state.textContent = states[Number(value)]; };
  update(Boolean(pressed)); button.disabled = actionRunning;
  button.addEventListener("click", () => {
    const next = button.getAttribute("aria-pressed") !== "true";
    update(next); onChange(next); updateDraftState();
  });
  return button;
}
document.querySelectorAll("[data-config-option]").forEach(button => button.addEventListener("click", () => {
  setOptionPressed(button.id, !optionPressed(button.id));
  updateDraftState();
  if (button.id === "isvoro-seckill") renderSeckill();
  if (["notify-current", "isvoro-seckill"].includes(button.id)) scheduleAutoSave("notifications");
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
    hostwatchService = null;
    renderMonitorState();
    scheduleServiceRefresh();
    throw error;
  }
  const previousActive = monitorService?.active;
  monitorService = state.monitor;
  // The top-right badge represents the actual HostWatch monitor service.
  // The admin HTTP service is shown separately in the management panel.
  hostwatchService = state.monitor;
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
  document.querySelector("#manage-backup-storage").textContent = `自动备份存储位置：${state.backup_dir || "服务器备份目录未返回"}`;
  const backupDirty = backupScheduleDraft && JSON.stringify(backupScheduleDraft) !== JSON.stringify(backupScheduleSaved);
  backupScheduleRuntime = state.backup_schedule || null;
  backupScheduleSaved = backupScheduleRuntime ? { enabled: backupScheduleRuntime.enabled, time: backupScheduleRuntime.time } : null;
  if (!backupDirty) backupScheduleDraft = backupScheduleSaved ? { ...backupScheduleSaved } : null;
  renderBackupSchedule();
  const backups = document.querySelector("#manage-backups");
  const recentBackups = (state.backups || []).slice(0, 5);
  backups.replaceChildren(...(recentBackups.length ? recentBackups.map((item) => text("span", `${item.name} · ${formatBytes(item.size)}`)) : [text("span", "暂无备份")]));
  if (updateUnit) {
    const update = await refreshUpdateStatus();
    if (update.finished || update.LoadState === "not-found") {
      const reloadAfterUpdate = sessionStorage.getItem(UPDATE_RELOAD_KEY) === "pending" && update.Result === "success";
      updateUnit = "";
      sessionStorage.removeItem("hostwatch-update-unit");
      sessionStorage.removeItem(UPDATE_RELOAD_KEY);
      clearInterval(updatePollTimer); updatePollTimer = null;
      clearTimeout(updatePollTimeout); updatePollTimeout = null;
      syncUpdateControl();
      if (reloadAfterUpdate) {
        document.querySelector("#manage-task-status").textContent = "HostWatch 更新完成，管理页将在 2 秒后刷新。";
        scheduleUpdateReload(2_000);
      }
    }
  }
  return state;
}

function renderBackupSchedule() {
  const value = backupScheduleDraft || { enabled: false, time: "03:00" };
  setOptionPressed("manage-auto-backup", value.enabled);
  document.querySelector("#manage-auto-backup").disabled = actionRunning || !backupScheduleRuntime;
  document.querySelector("#manage-backup-time").value = value.time;
  document.querySelector("#manage-backup-time").disabled = actionRunning || !backupScheduleRuntime;
  document.querySelector("#manage-backup-schedule-save").disabled = actionRunning || !backupScheduleRuntime || JSON.stringify(backupScheduleDraft) === JSON.stringify(backupScheduleSaved);
  document.querySelector("#manage-backup-timezone").textContent = `调度时区：${backupScheduleRuntime?.timezone || "尚未返回，请更新安装后刷新"}`;
  const state = backupScheduleRuntime;
  let note = "服务器尚未提供备份计划，请先更新 HostWatch 安装后刷新。";
  if (state) {
    note = state.error || (state.enabled ? (state.timer_active === "active" && state.timer_enabled === "enabled" ? `已启用，每天北京时间 ${state.time}；下次执行：${state.next_run && state.next_run !== "n/a" ? state.next_run : "等待定时器安排"}` : "计划已保存为开启，但定时器未正常运行，请检查服务日志。") : "每日自动备份已关闭。");
    if (state.last_run && state.last_run !== "n/a") note += ` 上次触发：${state.last_run}；${state.backup_active === "activating" ? "正在备份" : state.last_result === "success" ? "执行成功" : state.last_result ? `执行异常（${state.last_result}），请载入日志检查` : "结果未知"}。`;
  }
  if (backupScheduleDraft && JSON.stringify(backupScheduleDraft) !== JSON.stringify(backupScheduleSaved)) note += " 有未保存的备份计划，请点击“保存备份计划”。";
  document.querySelector("#manage-backup-schedule-status").textContent = note;
}
window.addEventListener("hostwatch-action-change", renderBackupSchedule);

function serviceTone(active) {
  if (monitorService?.transition_timeout && ["activating", "deactivating"].includes(active)) return "error";
  return active === "active" ? "ok" : ["inactive", "failed"].includes(active) ? "error" : ["activating", "deactivating"].includes(active) ? "pending" : "";
}
function scheduleServiceRefresh() {
  clearTimeout(serviceRefreshTimer);
  const transition = ["activating", "deactivating"].includes(monitorService?.active);
  const delay = transition && !monitorService?.transition_timeout ? 2000 : 60000;
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

async function refreshUpdateStatus(includeLogs = true) {
  const result = await managementRequest({ action: "update-status", unit: updateUnit, include_logs: includeLogs });
  const finished = result.ActiveState === "failed" || (result.ActiveState === "active" && result.SubState === "exited");
  const label = finished ? (result.Result === "success" ? "更新完成。" : `更新失败：${result.Result}`) : `更新任务：${result.ActiveState || result.LoadState || "等待中"}`;
  document.querySelector("#manage-task-status").textContent = label;
  if (result.logs) document.querySelector("#manage-logs").textContent = result.logs;
  return { ...result, finished };
}

function syncUpdateControl() {
  const button = document.querySelector("#manage-update");
  if (!button) return;
  button.disabled = actionRunning || Boolean(updateUnit);
  button.title = updateUnit ? "更新任务正在执行，请等待完成" : "从 GitHub 更新 HostWatch";
}

function scheduleUpdateReload(delayMs = 2_000) {
  clearTimeout(updateReloadTimer);
  sessionStorage.removeItem(UPDATE_RELOAD_KEY);
  const reload = () => {
    sessionStorage.setItem("hostwatch-current-page", "overview");
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#overview`);
    window.location.reload();
  };
  updateReloadTimer = setTimeout(reload, Math.max(0, Number(delayMs) || 0));
}

function resumeUpdateReload() {
  const reloadState = sessionStorage.getItem(UPDATE_RELOAD_KEY);
  if (updateUnit && reloadState) {
    sessionStorage.setItem(UPDATE_RELOAD_KEY, "pending");
    return;
  }
  sessionStorage.removeItem(UPDATE_RELOAD_KEY);
}

async function performManagementAction(action, extra = {}) {
  const result = await managementRequest({ action, ...extra });
  if (result.update_unit) {
    updateUnit = result.update_unit;
    sessionStorage.setItem("hostwatch-update-unit", updateUnit);
    sessionStorage.setItem(UPDATE_RELOAD_KEY, "pending");
    syncUpdateControl();
    document.querySelector("#manage-task-status").textContent = result.message;
    setMessage(result.message, "success");
    pollUpdateStatus();
    if (action === "update") {
      document.querySelector("#manage-task-status").textContent = `${result.message} 更新完成后管理页会自动刷新。`;
    }
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
    if (!['admin-restart', 'monitor-restart'].includes(action)) {
      setMessage(`${result.message}后续请求如出现登录提示，请使用新的管理员凭据登录。`, "success");
      return;
    }
  }
  if (action === "hostwatch-restart") {
    document.querySelector("#manage-task-status").textContent = `${result.message} 页面将在服务恢复后自动刷新。`;
    setTimeout(() => window.location.reload(), 5000);
  }
  if (!result.update_unit && !['admin-restart', 'monitor-restart', 'hostwatch-restart'].includes(action)) {
    try { await refreshManagement(); }
    catch (error) { setMessage(`${result.message || "操作已完成。"}状态刷新失败：${error.message}`, "error"); }
  }
}

async function exportManagementBackup() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 210_000);
  try {
    const response = await fetch("/api/manage", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "backup-download" }),
      signal: controller.signal,
    });
    if (!response.ok) {
      let detail = `请求失败（HTTP ${response.status}）`;
      try { detail = (await response.json()).error || detail; } catch {}
      throw new Error(detail);
    }
    const blob = await response.blob();
    if (response.status !== 200 || blob.size === 0) throw new Error("服务器未返回有效备份文件，请刷新状态后重试。");
    const signature = new Uint8Array(await blob.slice(0, 2).arrayBuffer());
    if (signature[0] !== 0x1f || signature[1] !== 0x8b) throw new Error("备份响应不是有效的 gzip 文件，请检查服务器或反向代理。");
    const disposition = response.headers.get("Content-Disposition") || "";
    const filename = disposition.match(/filename="([^"]+)"/)?.[1] || "host-watch-backup.tar.gz";
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    setMessage("备份已导出，通过当前浏览器保存到本地。", "success");
  } catch (error) {
    if (controller.signal.aborted) throw new Error("备份导出超时，请刷新状态后重试。");
    throw error;
  } finally { clearTimeout(timer); }
}

async function importManagementBackup(file) {
  if (!file.name.toLowerCase().endsWith(".tar.gz") || !file.size || file.size > 20 * 1024 * 1024) {
    setMessage("请选择不超过 20 MB 的 HostWatch .tar.gz 备份文件。", "error"); return;
  }
  const signature = new Uint8Array(await file.slice(0, 2).arrayBuffer());
  if (signature[0] !== 0x1f || signature[1] !== 0x8b) { setMessage("文件不是有效的 gzip 备份。", "error"); return; }
  const draftNote = hasUnsavedChanges() ? "当前页面未保存的修改将被替换。\n" : "";
  if (!window.confirm(`确认导入备份“${file.name}”？\n${draftNote}将恢复商品配置、库存记录和备份计划，保留当前管理员账号、访问设置及程序版本。\n导入前自动备份当前数据，恢复后按保存的自动刷新设置运行。`)) return;
  await runAction(document.querySelector("#manage-import-backup"), "导入中…", "正在校验备份并恢复数据，请勿关闭页面…", async () => {
    const result = await requestJSON("/api/manage/backup-import", {
      method: "POST", headers: { "Content-Type": "application/gzip" }, body: file, timeoutMs: 150_000,
    });
    document.querySelector("#manage-task-status").textContent = `${result.message} 恢复前备份：${result.rollback_path || "请查看最近备份"}`;
    // Reload imported data so a stale browser draft cannot overwrite the restore.
    backupScheduleDraft = null;
    try { await loadConfig(); await refreshManagement(); await loadStatus(); }
    catch (error) { setMessage(`备份已导入，但页面刷新失败：${error.message} 请重新载入页面。`, "error"); return; }
    setMessage(result.message, "success");
  });
}

function pollUpdateStatus() {
  if (updatePollTimer || !updateUnit) return;
  let polling = false;
  updatePollTimer = setInterval(async () => {
    if (polling || actionRunning) return;
    polling = true;
    try {
      const result = await refreshUpdateStatus(false);
      if (result.finished || result.LoadState === "not-found") {
        clearInterval(updatePollTimer);
        updatePollTimer = null;
        clearTimeout(updatePollTimeout);
        updatePollTimeout = null;
        const reloadAfterUpdate = sessionStorage.getItem(UPDATE_RELOAD_KEY) === "pending" && result.Result === "success";
        setMessage(result.Result === "success" ? "HostWatch 更新完成，管理页将在服务恢复后刷新。" : `HostWatch 更新结束：${result.Result || result.LoadState}`, result.Result === "success" ? "success" : "error");
        updateUnit = "";
        sessionStorage.removeItem("hostwatch-update-unit");
        sessionStorage.removeItem(UPDATE_RELOAD_KEY);
        syncUpdateControl();
        await refreshManagement();
        if (reloadAfterUpdate) scheduleUpdateReload(2_000);
      }
    } catch (error) {
      document.querySelector("#manage-task-status").textContent = `更新可能正在重启服务，等待管理页恢复：${error.message}`;
    } finally { polling = false; }
  }, 4000);
  updatePollTimeout = setTimeout(() => {
    clearInterval(updatePollTimer);
    updatePollTimer = null;
    updatePollTimeout = null;
    if (updateUnit) document.querySelector("#manage-task-status").textContent = "更新仍未返回最终结果，请点击刷新状态核对，或查看服务器更新任务日志。";
  }, 10 * 60 * 1000);
}

function bindManagementActions() {
  const importFile = document.querySelector("#manage-import-backup-file");
  document.querySelector("#manage-import-backup").addEventListener("click", () => { importFile.value = ""; importFile.click(); });
  importFile.addEventListener("change", async () => {
    const file = importFile.files?.[0];
    if (file) {
      try { await importManagementBackup(file); }
      catch (error) { setMessage(error.message, "error"); }
    }
    importFile.value = "";
  });
  document.querySelector("#manage-auto-backup").addEventListener("click", () => { if (!backupScheduleDraft) return; backupScheduleDraft.enabled = !backupScheduleDraft.enabled; renderBackupSchedule(); });
  document.querySelector("#manage-backup-time").addEventListener("input", event => { if (!backupScheduleDraft) return; backupScheduleDraft.time = event.target.value; renderBackupSchedule(); });
  document.querySelector("#manage-backup-schedule-save").addEventListener("click", event => {
    if (!backupScheduleDraft || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(backupScheduleDraft.time)) { setMessage("请选择有效的每日备份时间。", "error"); return; }
    runAction(event.currentTarget, "保存中…", "正在保存并应用服务器备份计划…", async () => {
      const result = await managementRequest({ action: "backup-schedule", ...backupScheduleDraft });
      const schedule = result.backup_schedule;
      backupScheduleRuntime = schedule;
      backupScheduleSaved = { enabled: schedule.enabled, time: schedule.time };
      backupScheduleDraft = { ...backupScheduleSaved };
      renderBackupSchedule();
      setMessage(result.message || "备份计划已保存并生效。", "success");
    });
  });
  document.querySelector("#manage-refresh").addEventListener("click", (event) => runAction(event.currentTarget, "刷新中…", "正在读取服务器服务和账号状态…", async () => {
    await refreshManagement();
    setMessage("服务器服务、管理员账号和备份列表已刷新。", "success");
  }));
  for (const [id, action, label, message] of [
    ["manage-restart-admin", "admin-restart", "安排中…", "正在安排管理页服务重启…"],
    ["manage-restart-monitor", "hostwatch-restart", "重启中…", "正在安排 HostWatch 服务组重启…"],
    ["manage-backup", "backup-download", "导出中…", "正在打包并导出备份…"],
    ["manage-update", "update", "启动中…", "正在启动 HostWatch 更新任务…"],
  ]) {
    document.querySelector(`#${id}`).addEventListener("click", (event) => {
      if (action === "hostwatch-restart" && !window.confirm("确认重启 HostWatch 服务组？监控服务和管理页服务会短暂重启，当前页面将在服务恢复后自动刷新。")) return;
      const operation = action === "backup-download" ? exportManagementBackup : () => performManagementAction(action);
      runAction(event.currentTarget, label, message, operation);
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
  const toggle = text("span", "", "section-toggle");
  toggle.setAttribute("aria-hidden", "true");
  const body = text("div", "", "section-body");
  const actions = text("div", "", "actions");
  // Controls live in the summary row; keep their clicks from toggling the
  // surrounding details element.
  actions.addEventListener("click", event => event.stopPropagation());
  actions.addEventListener("keydown", event => event.stopPropagation());
  for (const [type, label] of [["watch", "库存监控"], ["notify", "库存通知"]]) {
    const button = text("button", "", `membership-toggle membership-${type}`); button.type = "button"; button.dataset.membership = type;
    const syncButton = () => {
      const selected = membership(item, type);
      button.textContent = selected ? `✓ 已加入${label}` : `＋ 加入${label}`;
      button.setAttribute("aria-pressed", String(selected));
      button.title = selected ? `点击移出${label}，修改会自动保存` : `点击加入${label}，修改会自动保存`;
    };
    syncButton(); button.disabled = actionRunning;
    button.addEventListener("click", () => {
      if (actionRunning) return;
      setMembership(item, type, !membership(item, type)); syncButton();
      updateProductEditorTitle(article, item); renderOverview(); updateDraftState();
      scheduleAutoSave(type === "watch" ? "monitor" : "notifications");
      setMessage(`已${membership(item, type) ? "加入" : "移出"}${label}，正在自动保存。`, "pending", true, false);
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
    scheduleAutoSave(`product:${item.id}`);
    setMessage("商品已移除，正在自动保存。", "pending");
  });
  actions.append(remove);
  head.append(identity, toggle, actions);

  const grid = text("div", "", "form-grid");
  grid.append(
    selectField("监控适配器", item.adapter || (item.category_url ? "vmiss" : "html"), "adapter", orderedMerchantEntries().map(([key, label]) => [key, key === "html" ? "通用 HTTPS 网页" : `${label} 商品库存`]).map(([key, label]) => [key, key === "vmiss" ? "VMISS WHMCS" : label])),
    inputField("厂商名称", item.provider || (item.adapter === "vmiss" || item.category_url ? "VMISS" : "自定义网站"), "provider", "text", "例如 ExampleHost"),
    inputField("商品名称", item.name, "name", "text", "例如 US VPS 2 GB"),
    inputField("通知优惠码（留空跟随 AFF 默认）", item.notification_coupon_code || "", "notification_coupon_code", "text", "例如 10%OFF"),
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
    inputField("套餐完整名称（精确匹配）", item.adapter === "akile" ? item.selector : "TWLite-Mini", "akile_selector", "text", "TWLite-Mini"),
    text("p", "默认按线路＋套餐完整名称在实际商品目录中自动识别地区；官网切换地区后地址栏可能仍保留旧 areaId，请勿据此判断。HKL-TW 属于台湾（6），MOLite 属于澳门（14）。自动模式忽略旧地区 ID，重名时需指定地区；推荐普通 HTTPS 请求。", "hint span-2"),
  );
  const narwhalFields = text("div", "", "form-grid span-2");
  narwhalFields.dataset.adapterFields = "narwhal";
  narwhalFields.append(
    inputField("实例 ID", item.adapter === "narwhal" ? item.machine_id : "", "narwhal_machine_id", "text", "从独角鲸云目录选择实例"),
    inputField("套餐 ID", item.adapter === "narwhal" ? item.plan_id : "", "narwhal_plan_id", "text", "从独角鲸云目录选择套餐"),
    text("p", "实例和套餐 ID 用于精确匹配补货状态。建议从独角鲸云库存大盘添加；手动修改后保存前请确认两个 ID 对应同一套餐。", "hint span-2"),
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
  modeWrap.append(vmissFields, htmlFields, soyFields, akileFields, narwhalFields, isvoroFields, dmitFields, bwhFields);
  const result = text("div", "", "product-test-result");
  result.setAttribute("role", "status");
  result.setAttribute("aria-live", "polite");
  body.append(text("p", "单次检查使用当前商品配置，无需保存商品或开启监控；结果通过已保存的 Telegram 机器人发送（含缺货和错误），不受自动提醒开关影响。请先保存机器人 Token 和 Chat ID；停止检查不发送结果。", "hint"), result, grid, modeWrap);
  article.append(head, body);
  renderProductTest(article, item.id);
  updateProductEditorTitle(article, item);

  article.addEventListener("input", (event) => { syncSoyCategory(article, event.target.dataset.key); syncEditor(article); updateDraftState(); scheduleAutoSave(`product:${item.id}`); });
  article.addEventListener("change", (event) => {
    if (event.target.dataset.key === "adapter") article.isvoroController?.abort();
    syncSoyCategory(article, event.target.dataset.key);
    if (["isvoro_type", "isvoro_line"].includes(event.target.dataset.key)) {
      article.isvoroController?.abort();
      renderIsvoroFields(article, event.target.dataset.key === "isvoro_type" ? "type" : "line", true);
      void loadIsvoroDirectory(article);
    }
    syncEditor(article);
    updateDraftState();
    scheduleAutoSave(`product:${item.id}`);
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
  item.notification_coupon_code = get("notification_coupon_code")?.value.trim() || "";
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
  } else if (item.adapter === "narwhal") {
    item.machine_id = get("narwhal_machine_id").value.trim();
    item.plan_id = get("narwhal_plan_id").value.trim();
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
    ? `测试成功：${stockLabel(result)}。${formatBeijingDate(result.checked_at)} · 耗时 ${result.duration_seconds} 秒。自动监控状态保持不变。`
    : `测试失败：${result.error}${result.checked_at ? `（${formatBeijingDate(result.checked_at)}）` : ""}`;
  if (result.warning) node.textContent += ` ${result.warning}`;
  if (result.route) node.append(text("div", `本次读取：${requestRouteLabel(result.route)}。出口使用已保存配置。`, "test-notification"));
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
    if (task.id === seckillTestId) result.checked_at ||= beijingISOString();
    if (task.probeDraft && task.probeDraft !== JSON.stringify(collectConfig().fetcher.cloudscraper)) result.warning = "此结果使用测试开始时的 cloudscraper 参数；当前参数已修改，请按需重新测试。";
    productTests.set(task.id, result);
    const notificationIssue = result.notification && !["sent", "skipped"].includes(result.notification.status);
    const summary = result.cancelled ? "测试已停止。" : result.ok ? (task.id === cloudscraperTestId ? "cloudscraper 访问测试完成，结果已显示在网页请求中。" : task.id === seckillTestId ? "秒杀单次测试完成，结果已显示在秒杀区域。" : "单次库存测试完成，结果已显示在商品下方。") : `${task.id === cloudscraperTestId ? "cloudscraper 访问" : "单次库存"}测试失败：${result.error}`;
    setMessage([summary, result.notification?.message, result.warning].filter(Boolean).join(" "), result.warning || notificationIssue || (!result.ok && !result.cancelled) ? "error" : "success");
  } catch (error) {
    productTests.set(task.id, { ok: false, error: error.message, ...(task.id === seckillTestId ? { checked_at: beijingISOString() } : {}) });
    setMessage(error.message, "error");
  } finally {
    clearTimeout(task.retryStopTimer);
    task.pollController?.abort();
    task.startController?.abort();
    if (activeTest === task) activeTest = null;
    sessionStorage.removeItem("hostwatch-stock-test");
    renderProductList();
    renderOverview();
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
  if (result.route) node.textContent += `\n本次读取：${requestRouteLabel(result.route)}。出口使用已保存配置。`;
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
  let message = !result ? "尚无秒杀检查记录。" : result.pending ? (result.notifying ? "秒杀检查已完成，正在发送 Telegram 结果…" : "正在读取秒杀活动，可停止测试。") : result.cancelled ? "秒杀测试已停止。" : !result.ok ? `秒杀检查失败：${result.error}` : `${manual ? "单次测试" : "最近自动检查"}：${result.activities.length ? `${result.activities.length} 个活动` : "暂无秒杀活动"}。${result.checked_at ? formatBeijingDate(result.checked_at) : ""}`;
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
    card.append(text("div", `${formatBeijingDate(campaign.starts_at)} 至 ${formatBeijingDate(campaign.ends_at)}`, "subtle"));
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
  if (active === "active") return "HostWatch 服务运行中";
  if (active === "inactive") return "HostWatch 服务已停止";
  return ({ failed: "监控服务运行失败", activating: "监控服务启动中", deactivating: "监控服务停止中" })[active] || "监控服务状态未知";
}

const merchantNames = { vmiss: "VMISS", vps_soy: "VPS.SOY", akile: "Akile", narwhal: "独角鲸云", isvoro: "Isvoro", dmit: "DMIT", bwh: "搬瓦工" };
const customMerchantEntry = ["html", "其他商家 / 自定义网页"];
const providerScheduleModules = { monitor: "monitor", notifications: "notify", "seckill-settings": "seckill" };
function scheduleProviderOptions() { return [...Object.entries(merchantNames), customMerchantEntry]; }
function providerSchedule(module, provider) {
  if (config.provider_schedules?.[module]?.[provider]) return config.provider_schedules[module][provider];
  if (module === "seckill") return { mode: config.vps_seckill?.mode || "manual", interval_seconds: config.vps_seckill?.interval_seconds || 5 };
  return config[`${module}_schedule`] || { mode: "manual", interval_seconds: 300 };
}
function renderProviderScheduleSummary() {
  for (const module of ["monitor", "notify", "seckill"]) {
    const target = document.getElementById(module + "-provider-summary");
    if (!target) continue;
    target.replaceChildren(...orderedMerchantEntries().map(([provider, name]) => {
      const schedule = providerSchedule(module, provider);
      const modeLabel = module === "seckill"
        ? ({ rapid: "极速抢购（自动）", precise: "精准抢购（自动）", manual: "手动抢购" })[schedule.mode] || schedule.mode
        : schedule.mode === "auto" ? "自动刷新" : "仅手动刷新";
      const row = text("span", "");
      row.append(text("strong", name), text("span", modeLabel + " · " + schedule.interval_seconds + " 秒"));
      return row;
    }));
  }
}
function renderProviderSchedule(scope) {
  const module = providerScheduleModules[scope];
  if (!module) return;
  const providerId = scope === "monitor" ? "monitor-schedule-provider" : scope === "notifications" ? "notify-schedule-provider" : "vps-seckill-schedule-provider";
  const providerSelect = document.querySelector(`#${providerId}`);
  if (!providerSelect) return;
  if (!providerSelect.options.length) providerSelect.replaceChildren(...scheduleProviderOptions().map(([value, label]) => new Option(label, value)));
  const provider = providerSelect.value || "vmiss", schedule = providerSchedule(module, provider);
  const modeId = scope === "monitor" ? "monitor-schedule-mode" : scope === "notifications" ? "notify-schedule-mode" : "vps-seckill-mode";
  const intervalId = scope === "monitor" ? "interval" : scope === "notifications" ? "notify-interval" : "vps-seckill-interval";
  document.querySelector(`#${modeId}`).value = schedule.mode;
  document.querySelector(`#${intervalId}`).value = schedule.interval_seconds;
  renderProviderScheduleSummary();
}
function updateProviderSchedule(scope) {
  const module = providerScheduleModules[scope];
  if (!module) return;
  const providerId = scope === "monitor" ? "monitor-schedule-provider" : scope === "notifications" ? "notify-schedule-provider" : "vps-seckill-schedule-provider";
  const provider = document.querySelector(`#${providerId}`).value || "vmiss";
  const modeId = scope === "monitor" ? "monitor-schedule-mode" : scope === "notifications" ? "notify-schedule-mode" : "vps-seckill-mode";
  const intervalId = scope === "monitor" ? "interval" : scope === "notifications" ? "notify-interval" : "vps-seckill-interval";
  const mode = document.querySelector(`#${modeId}`).value, interval = Number(document.querySelector(`#${intervalId}`).value || 0);
  config.provider_schedules ||= {};
  config.provider_schedules[module] ||= {};
  config.provider_schedules[module][provider] = { mode, interval_seconds: interval };
  if (module !== "seckill") config[`${module}_schedule`] = config.provider_schedules[module][provider];
  if (module === "seckill") { config.vps_seckill.mode = mode; config.vps_seckill.interval_seconds = interval; }
  renderProviderScheduleSummary(); updateDraftState(); renderOverview(); scheduleAutoSave(scope, INTERVAL_AUTOSAVE_DELAY_MS);
}
function orderedMerchantEntries({ includeCustom = true } = {}) {
  const entries = merchantOrder().map(key => [key, merchantNames[key]]);
  if (includeCustom) entries.push(customMerchantEntry);
  return entries;
}
function requestRouteLabel(route) {
  if (!route) return "出口信息尚未确认";
  const method = { requests: "HTTP", cloudscraper: "cloudscraper", flaresolverr: "FlareSolverr 浏览器" }[route.mode] || route.mode;
  const parameters = route.mode === "flaresolverr" && Number.isInteger(route.timeout_seconds) ? ` · 超时 ${route.timeout_seconds} 秒 / 等待 ${route.wait_seconds} 秒 / ${route.disable_media ? "轻量加载" : "完整加载"}` : "";
  return `${method} · ${route.node_id ? `${route.node_name || route.node_id}（${route.public_ip || "出口 IP 未检查"}）` : "主服务器直连"}${parameters}`;
}
function merchantRequestPolicy(value, provider) {
  const fetcher = value?.fetcher || {}, routes = value?.egress || {};
  const id = routes.routes?.[provider] || "", node = routes.nodes?.find(row => row.id === id);
  const mode = fetcher.provider_modes?.[provider];
  const effectiveMode = mode && mode !== "inherit" ? mode : fetcher.mode || "requests";
  const parameters = effectiveMode === "flaresolverr" ? Object.fromEntries(["timeout_seconds", "wait_seconds", "disable_media"].map(key => [key, fetcher.provider_flaresolverr?.[provider]?.[key] ?? fetcher[key]])) : {};
  return { mode: effectiveMode, ...parameters, node_id: id,
    node_name: node?.name || id, public_ip: node?.public_ip || "",
    error: id && node?.enabled === false ? "绑定的出口节点已停用，请启用并保存后再刷新。" : id && !node?.proxy_url ? "绑定的出口节点尚未完成安装回报。" : "" };
}
function merchantRequestChanged(provider) {
  if (!savedConfig) return false;
  const relevant = value => {
    const policy = merchantRequestPolicy(value, provider), fetcher = value.fetcher || {};
    const node = value.egress?.nodes?.find(row => row.id === policy.node_id);
    return [policy, node?.proxy_url, node?.enabled, node?.token, node?.token_configured,
      policy.mode === "flaresolverr" ? [fetcher.flaresolverr_url, ...["timeout_seconds", "wait_seconds", "disable_media"].map(key => fetcher.provider_flaresolverr?.[provider]?.[key] ?? fetcher[key])] : policy.mode === "cloudscraper" ? fetcher.cloudscraper : null];
  };
  return JSON.stringify(relevant(collectConfig())) !== JSON.stringify(relevant(JSON.parse(savedConfig)));
}
const merchantHosts = { vmiss: "app.vmiss.com", vps_soy: "www.vps.soy", akile: "akile.ai", narwhal: "dash.fuckip.me", isvoro: "isvoro.com", dmit: "www.dmit.io", bwh: "bwh81.net" };
function merchantOrder() {
  return [...new Set([...(config.merchant_order || []), ...Object.keys(merchantNames)])].filter((key) => Object.hasOwn(merchantNames, key));
}
function affiliatePurchaseURL(raw, provider, item = {}, affiliates = config.affiliates || {}) {
  if (provider === "vps_soy") return raw;
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
  return config.affiliate_notes || {};
}
function collectAffiliateAccounts() {
  return Object.fromEntries([...document.querySelectorAll("[data-affiliate-account]")].map((input) => [input.dataset.affiliateAccount, input.value.trim()]));
}
function collectAffiliateRates() {
  return Object.fromEntries([...document.querySelectorAll("[data-affiliate-rate]")].map((input) => [input.dataset.affiliateRate, input.value.trim()]));
}
function collectAffiliateCoupons() {
  return Object.fromEntries([...document.querySelectorAll("[data-affiliate-coupon]")].map((input) => [input.dataset.affiliateCoupon, input.value.trim()]));
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
function renderMerchantOrder() {
  const order = merchantOrder();
  const tabs = document.querySelector(".catalog-merchants");
  if (!tabs.dataset.orderDragBound) {
    tabs.dataset.orderDragBound = "true";
    tabs.addEventListener("dragstart", event => {
      if (!merchantOrderEditing) return;
      const button = event.target.closest("[data-catalog-provider]");
      if (!button?.draggable) return;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", button.dataset.catalogProvider);
      button.setAttribute("aria-grabbed", "true");
      button.classList.add("dragging");
    });
    tabs.addEventListener("dragend", event => {
      const button = event.target.closest("[data-catalog-provider]");
      button?.classList.remove("dragging");
      button?.setAttribute("aria-grabbed", "false");
    });
    tabs.addEventListener("dragover", event => {
      if (!merchantOrderEditing || !event.target.closest("[data-catalog-provider]")) return;
      event.preventDefault();
      event.target.closest("[data-catalog-provider]").classList.add("drag-over");
    });
    tabs.addEventListener("dragleave", event => event.target.closest("[data-catalog-provider]")?.classList.remove("drag-over"));
    tabs.addEventListener("drop", event => {
      if (!merchantOrderEditing) return;
      const button = event.target.closest("[data-catalog-provider]");
      if (!button) return;
      event.preventDefault(); button.classList.remove("drag-over");
      const source = event.dataTransfer.getData("text/plain"), next = merchantOrder();
      const from = next.indexOf(source), to = next.indexOf(button.dataset.catalogProvider);
      if (from < 0 || to < 0 || from === to) return;
      next.splice(from, 1); next.splice(to, 0, source); config.merchant_order = next;
      renderMerchantOrder(); renderProducts(); updateDraftState(); scheduleAutoSave("monitor");
      setMessage("商家顺序已调整，正在自动保存。", "pending", true, false);
    });
    tabs.addEventListener("click", event => {
      if (merchantOrderEditing && event.target.closest("[data-catalog-provider]")) {
        event.preventDefault(); event.stopImmediatePropagation();
      }
    }, true);
  }
  tabs.classList.toggle("merchant-order-editing", merchantOrderEditing);
  const toggle = document.querySelector("#merchant-order-toggle");
  toggle.disabled = actionRunning;
  toggle.classList.toggle("merchant-order-editing", merchantOrderEditing);
  toggle.textContent = merchantOrderEditing ? "保存商家顺序" : "调整商家顺序";
  toggle.setAttribute("aria-expanded", String(merchantOrderEditing));
  for (const key of order) {
    const button = tabs.querySelector(`[data-catalog-provider="${key}"]`);
    if (!button) continue;
    button.draggable = merchantOrderEditing && !actionRunning;
    if (merchantOrderEditing) button.setAttribute("aria-grabbed", "false");
    else button.removeAttribute("aria-grabbed");
    button.title = merchantOrderEditing ? "拖动调整商家顺序" : `切换到${merchantNames[key]}`;
    tabs.append(button);
  }
  const filter = document.querySelector("#watchlist-provider"), selected = filter.value || "all";
  filter.replaceChildren(new Option("全部商家", "all"), ...order.map((key) => new Option(merchantNames[key], key)), new Option("其他商家", "other")); filter.value = selected;
  const notificationFilter = document.querySelector("#notification-provider"), notificationSelected = notificationFilter?.value || "all";
  if (notificationFilter) {
    notificationFilter.replaceChildren(new Option("全部商家", "all"), ...order.map((key) => new Option(merchantNames[key], key)), new Option("其他商家", "other"));
    notificationFilter.value = notificationSelected;
  }
  const add = document.querySelector("#add-merchant"), chosen = add.value;
  for (const key of [...order, "html"]) { const option = [...add.options].find((option) => option.value === key); if (option) add.append(option); } add.value = chosen;
  renderProviderModeOrder();
  renderProviderScheduleSummary();
  renderAffiliateOrder();
  renderCloudscraperTargetOrder();
  renderEgress();
  renderProviderFlareSettings();
  renderVpsSeckill();
}

function renderProviderModeOrder() {
  const target = document.querySelector("#provider-request-modes");
  if (!target) return;
  const labels = new Map([...target.querySelectorAll("label")].map(label => [label.querySelector("[data-provider-mode]")?.dataset.providerMode, label]));
  orderedMerchantEntries().forEach(([key]) => { const label = labels.get(key); if (label) target.append(label); });
}

function renderAffiliateOrder() {
  const target = document.querySelector(".affiliate-grid");
  if (!target) return;
  const cards = new Map([...target.querySelectorAll("[data-affiliate-card]")].map(card => [card.dataset.affiliateCard, card]));
  merchantOrder().forEach(key => { const card = cards.get(key); if (card) target.append(card); });
}

function renderCloudscraperTargetOrder() {
  const target = document.querySelector("#cloudscraper-test-target");
  if (!target) return;
  const selected = target.value;
  const options = new Map([...target.options].map(option => [option.value, option]));
  const ordered = [...merchantOrder().map(key => options.get(key)).filter(Boolean), options.get("baseline")].filter(Boolean);
  target.replaceChildren(...ordered);
  target.value = selected;
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
function seckillMembership(item) {
  return Boolean(item && seckillItems().some(row => row.product_id === item.id));
}
function productSnapshot(item, status) {
  const data = { ...(item.snapshot || {}), ...status, region: status?.region || item.snapshot?.region || item.category_name || "—" };
  if (item.adapter === "dmit") {
    const stamp = value => Date.parse(value || "") || 0;
    // All stock views use the latest completed observation, including failures.
    // Keep old successful values in storage, never present them as a new check.
    if (data.available > 0 && (status ? status.stock_version : data.stock_version) !== 1) {
      data.available = null; data.error = "旧版库存尚未校验购买页，请刷新目录或测试商品";
    }
    if (!status && ["blocked", "error"].includes(lastStatus?.state) && stamp(lastStatus.last_checked) > stamp(data.checked_at)) {
      data.available = null; data.error = `最近检查未取得此商品结果：${lastStatus.message || "读取失败"}`; data.checked_at = lastStatus.last_checked;
    }
    const test = productTests.get(item.id);
    if (test && !test.pending && !test.cancelled && !test.warning && stamp(test.checked_at) >= stamp(data.checked_at) && stamp(test.checked_at)) {
      if (test.ok && Number.isInteger(test.available) && (test.available === 0 || test.stock_version === 1)) {
        Object.assign(data, test, { error: "", url: test.url || test.purchase_url || data.url, _fromTest: true });
      } else if (!test.ok && test.error) {
        Object.assign(data, { available: null, error: `本次库存未确认：${test.error}`, checked_at: test.checked_at, _fromTest: true });
      }
    }
  }
  // VMISS's older snapshots may repeat the same inline resources in both fields.
  if (item.adapter === "vmiss") {
    const fields = [...new Set([data.specs, data.traffic].filter(Boolean).join(" · ").split(/\s*·\s*|\s+(?=\d+(?:\.\d+)?\s*(?:v?CPU|cores?|[KMGT]i?B|[KMGT]?bps|IPv[46])\b)/i).map(value => value.trim()).filter(Boolean))];
    const network = value => /traffic|bandwidth|[KMGT]?bps|流量|带宽/i.test(value);
    data.specs = fields.filter(value => !network(value)).join(" · ");
    data.traffic = fields.filter(network).join(" · ");
  }
  // Older directory caches combined hardware and network text in one field.
  if (item.adapter !== "vmiss" && !data.traffic && data.specs) {
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
  return formatBeijingDate(value, { year: undefined, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: undefined });
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
function renderCurrentTime() {
  const timeNode = document.querySelector("#exchange-time");
  if (timeNode) timeNode.textContent = `当前时间（北京时间）${formatBeijingDate(new Date())}`;
}
function renderExchangeRates() {
  const timeNode = document.querySelector("#exchange-time"), noteNode = document.querySelector("#exchange-rate-note"), listNode = document.querySelector("#exchange-rate-list");
  if (!timeNode || !noteNode || !listNode) return;
  renderCurrentTime();
  const currencies = usedCurrencies();
  listNode.replaceChildren(...currencies.map(currency => {
    const rate = cnyRates[currency];
    return rate ? text("span", `1 ${currency} ≈ ¥${Number(rate).toFixed(rate < 0.1 ? 4 : 2)}`, "exchange-rate-item") : null;
  }).filter(Boolean));
  if (!currencies.length) noteNode.textContent = "当前商品未使用外币价格";
  else if (exchangeRateData?.error) noteNode.textContent = exchangeRateData.error;
  else {
    const updated = exchangeRateData?.fetched_at ? formatBeijingDate(Number(exchangeRateData.fetched_at) * 1000) : "内置参考值";
    noteNode.textContent = `汇率更新时间 ${updated}${exchangeRateData?.stale ? " · 使用缓存" : ""}`;
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
const priceSortModes = ["default", "asc", "desc"];
function readPriceSort(key) {
  const value = sessionStorage.getItem(key);
  return priceSortModes.includes(value) ? value : "default";
}
function cyclePriceSort(value) {
  return priceSortModes[(priceSortModes.indexOf(value) + 1) % priceSortModes.length];
}
function createPriceSortButton(label, key, onChange) {
  const button = text("button", "", "price-sort-button");
  button.type = "button";
  button.setAttribute("aria-label", `${label}排序：默认排序`);
  button.append(text("span", label, "price-sort-label"));
  const icon = text("span", "", "price-sort-icon");
  icon.append(text("span", "", "price-sort-arrow price-sort-up"), text("span", "", "price-sort-arrow price-sort-down"));
  button.append(icon);
  const update = (mode) => {
    button.dataset.sort = mode;
    button.setAttribute("aria-pressed", String(mode !== "default"));
    button.setAttribute("aria-label", `${label}排序：${mode === "asc" ? "月付人民币升序" : mode === "desc" ? "月付人民币降序" : "默认排序"}`);
    button.title = button.getAttribute("aria-label");
  };
  update(readPriceSort(key));
  button.addEventListener("click", () => {
    const mode = cyclePriceSort(readPriceSort(key));
    sessionStorage.setItem(key, mode);
    update(mode);
    onChange(mode);
  });
  return button;
}
function readMarketColumnSort() {
  const provider = activeMarketProvider();
  const key = sessionStorage.getItem(`hostwatch-${provider}-market-sort-key`) || "";
  const direction = sessionStorage.getItem(`hostwatch-${provider}-market-sort-direction`) || "asc";
  return { key, direction: direction === "desc" ? "desc" : "asc" };
}
function createMarketSortButton(label, key) {
  const button = text("button", "", "price-sort-button");
  button.type = "button";
  const update = () => {
    const state = readMarketColumnSort(), active = state.key === key;
    button.dataset.sort = active ? state.direction : "default";
    const icon = text("span", "", "price-sort-icon");
    icon.append(text("span", "", "price-sort-arrow price-sort-up"), text("span", "", "price-sort-arrow price-sort-down"));
    button.replaceChildren(text("span", label, "price-sort-label"), icon);
    button.setAttribute("aria-label", `${label}：${active ? state.direction === "asc" ? "升序" : "降序" : "默认排序"}`);
    button.title = button.getAttribute("aria-label");
  };
  update();
  button.addEventListener("click", () => {
    const state = readMarketColumnSort();
    const same = state.key === key;
    const direction = same && state.direction === "asc" ? "desc" : "asc";
    sessionStorage.setItem(`hostwatch-${activeMarketProvider()}-market-sort-key`, same && state.direction === "desc" ? "" : key);
    sessionStorage.setItem(`hostwatch-${activeMarketProvider()}-market-sort-direction`, direction);
    marketPage = 0; renderMarket();
  });
  return button;
}
function priceTextWithConversion(raw) {
  return String(raw || "—").split(/[；;]/).map(value => value.trim()).filter(Boolean).map(value => {
    const original = formatBillingCycle(formatPrice(value));
    const converted = priceConversion(value);
    return `${original}${converted ? `\n${converted}` : ""}`;
  }).join("；") || "—";
}
function renderLineEditor(cell, item) {
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
    input.addEventListener("change", () => scheduleAutoSave("monitor"));
    value.append(measure, input); wrap.append(value); lines.append(wrap);
  }
  if (item.line_label) {
    const legacy = text("div", "", "legacy-line"), input = document.createElement("input");
    legacy.append(text("span", "原线路：")); input.className = "manual-field"; input.value = item.line_label; input.maxLength = 500;
    input.setAttribute("aria-label", `${item.name} 原线路`); input.disabled = actionRunning;
    input.addEventListener("input", () => { item.line_label = input.value; updateDraftState(); }); input.addEventListener("change", () => scheduleAutoSave("monitor")); legacy.append(input); lines.append(legacy);
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
  scheduleAutoSave(type === "watch" ? "monitor" : "notifications");
  const target = type === "watch" ? listNode : document.querySelector("#notification-list");
  restoreProductSortFocus(target, focusId);
  document.querySelector("#product-sort-status").textContent = type === "watch" ? "监控商品顺序已调整，正在自动保存。" : "通知商品顺序已调整，正在自动保存。";
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

function resizeRemarkEditor(editor) {
  if (!editor.offsetWidth) return;
  editor.style.height = "auto";
  editor.style.height = `${editor.scrollHeight + 2}px`;
}
function resizeRemarkEditors() {
  listNode.querySelectorAll(".remarks-field").forEach(resizeRemarkEditor);
}
window.addEventListener("resize", resizeRemarkEditors);
window.addEventListener("hostwatch-page-change", resizeRemarkEditors);

function renderProductList() {
  if (productDrag) return;
  renderNotificationList();
  // Keep a focused manual editor alive during status polling.
  if (listNode.querySelector(".action-popover:popover-open") || listNode.contains(document.activeElement) && document.activeElement.matches("input, textarea")) return;
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
  }), readPriceSort("hostwatch-watch-price-sort"), item => productSnapshot(item, latest.get(item.id)).price);
  document.querySelector("#watchlist-count").textContent = `共 ${watched.length} 个 · 显示 ${visible.length} 个`;
  document.querySelector("#watchlist-note").textContent = "此列表仅检查和展示库存，不发送消息。加入库存通知后才会推送。后台检查更新商品数据，暂停后保留历史快照。";
  renderCompareBar();
  if (!visible.length) { listNode.replaceChildren(text("div", watched.length ? "没有符合条件的商品。" : "从库存大盘点击“加入库存监控”，添加关心的商品。", "empty")); return; }
  const wrap = text("div", "", "catalog-table-wrap"); wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "关注商品库存表");
  const table = text("table", "", "watch-table"), head = document.createElement("thead"), tr = document.createElement("tr");
  for (const label of ["对比", "商家", "产品名称", "地区", "备注", "线路", "硬件配置", "流量 / 带宽", "价格 / 周期", "状态", "操作"]) {
    const th = document.createElement("th"); th.scope = "col";
    if (label.startsWith("价格")) th.append(createPriceSortButton("价格 / 周期", "hostwatch-watch-price-sort", () => { renderProductList(); animateFilterResult(listNode); }));
    else th.append(text("span", label));
    tr.append(th);
  }
  head.append(tr); const body = document.createElement("tbody");
  for (const item of visible) {
    const runtime = latest.get(item.id), p = productSnapshot(item, runtime), row = document.createElement("tr"); row.dataset.id = item.id;
    const cell = (value = "", cls = "") => { const td = text("td", value, cls); row.append(td); return td; };
    const compareCell = cell("", "compare-cell");
    const compare = document.createElement("input"); compare.type = "checkbox"; compare.className = "compare-check"; compare.checked = compareSelection.has(item.id); compare.disabled = actionRunning; compare.setAttribute("aria-label", `选择 ${item.name} 进行对比`);
    compare.addEventListener("change", () => updateCompareSelection(item.id, compare.checked)); compareCell.append(compare);
    const merchant = text("div", "", "merchant-sort"); merchant.append(productSortHandle(item, "watch", row, visible.length), text("span", item.provider)); cell().append(merchant);
    const identity = cell(); identity.append(text("strong", p.name || item.name));
    const time = item.adapter === "dmit" ? p.checked_at : runtime ? runtime.checked_at || lastStatus?.last_checked : p.checked_at;
    const updated = text("small", time ? `${p._fromTest ? "测试" : "读取"} ${compactDate(time)}` : "等待首次读取"); updated.title = time ? formatBeijingDate(time) : "尚无商品快照"; identity.append(updated); cell(p.region || "—");
    for (const [key, label] of [["remarks", "备注"]]) {
      const input = document.createElement("textarea"); input.className = "manual-field remarks-field"; input.rows = 1; input.value = item[key] || ""; input.maxLength = 500; input.placeholder = "管理员填写";
      input.setAttribute("aria-label", `${item.name} ${label}`); input.disabled = actionRunning;
      input.addEventListener("input", () => { item[key] = input.value; resizeRemarkEditor(input); updateDraftState(); }); input.addEventListener("change", () => scheduleAutoSave("monitor")); cell().append(input);
    }
    renderLineEditor(cell(), item);
    renderSpecs(cell("", "watch-specs"), p.specs); renderSpecs(cell("", "watch-specs"), p.traffic, true); renderPrice(cell("", "watch-price"), p.price, expanded.has(item.id));
    const stockCell = cell(); stockCell.append(text("span", Number.isInteger(p.available) ? stockLabel(p) : "未知", `state ${!Number.isInteger(p.available) ? "pending" : p.available > 0 ? "ok" : "error"}`));
    if (item.adapter === "dmit" && p.error) stockCell.append(text("small", p.error, "error"));
    const actions = text("div", "", "watch-operations");
    const raw = p.url || item.purchase_url;
    if (raw && /^https:\/\//i.test(raw)) { const link = text("a", "直接购买", "buy-link"); link.href = affiliatePurchaseURL(raw, item.adapter, item); link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link); }
    else actions.append(text("small", "等待购买链接"));
    const notifyActive = membership(item, "notify");
    const notify = text("button", notifyActive ? "✓ 已加入库存通知" : "＋ 加入库存通知", "membership-toggle membership-notify"); notify.type = "button"; notify.disabled = actionRunning; notify.setAttribute("aria-pressed", String(notifyActive)); notify.title = notifyActive ? "点击移出库存通知，修改会自动保存" : "点击加入库存通知，修改会自动保存";
    notify.addEventListener("click", () => { const enabled = !membership(item, "notify"); setMembership(item, "notify", enabled); renderProducts(); updateDraftState(); scheduleAutoSave("notifications"); setMessage(`已${enabled ? "加入" : "移出"}库存通知，正在自动保存。`, "pending", true, false); });
    const seckillActive = seckillMembership(item);
    const seckill = text("button", seckillActive ? "✓ 已加入 VPS 补货抢购" : "＋ 加入 VPS 补货抢购", "membership-toggle membership-seckill"); seckill.type = "button"; seckill.disabled = actionRunning; seckill.setAttribute("aria-pressed", String(seckillActive)); seckill.title = seckillActive ? "点击移出 VPS 补货抢购" : "点击加入 VPS 补货抢购"; seckill.addEventListener("click", () => toggleVpsSeckill(item));
    const remove = text("button", "移出监控", "danger"); remove.type = "button"; remove.disabled = actionRunning;
    remove.addEventListener("click", () => { setMembership(item, "watch", false); renderProducts(); updateDraftState(); scheduleAutoSave("monitor"); setMessage("已移出库存监控，正在自动保存。", "pending"); });
    actions.append(moreActions(`watch-${item.id}`, item.name, [notify, seckill, remove])); cell().append(actions); body.append(row);
  }
  table.append(head, body); wrap.append(table); listNode.replaceChildren(wrap);
  resizeRemarkEditors();
  renderCompareBar();
  if (document.querySelector("#watch-compare-dialog")?.open) renderCompareContent();
  restoreProductSortFocus(listNode, focusedSortId);
}

window.addEventListener("hostwatch-page-change", event => {
  if (["monitor", "notifications"].includes(event.detail)) renderProductList();
});

function renderNotificationList() {
  if (productDrag) return;
  syncProductEditorOrder();
  if (document.querySelector("#notification-list .action-popover:popover-open")) return;
  const focusedSortId = document.querySelector("#notification-list .sort-handle:focus")?.dataset.sortId;
  const expanded = expandedPrices(document.querySelector("#notification-list"));
  const statusProducts = new Map((lastStatus?.products || []).map(p => [p.id, p]));
  const savedProducts = new Map((savedConfig ? JSON.parse(savedConfig).products : []).map(p => [p.id, p]));
  const search = document.querySelector("#notification-search")?.value.trim().toLowerCase() || "";
  const provider = document.querySelector("#notification-provider")?.value || "all";
  const stock = document.querySelector("#notification-stock")?.value || "all";
  const selected = sortByMonthlyPrice(orderedProducts("notify").filter(item => {
    const p = productSnapshot(item, statusProducts.get(item.id));
    return (!search || [item.name, item.provider, p.region, item.remarks, item.line_label, item.line_telecom, item.line_unicom, item.line_mobile].join(" ").toLowerCase().includes(search)) &&
      (provider === "all" || provider === item.adapter || provider === "other" && !merchantNames[item.adapter]) &&
      (stock === "all" || stock === "in" && p.available > 0 || stock === "out" && p.available === 0 || stock === "unknown" && !Number.isInteger(p.available));
  }), readPriceSort("hostwatch-notification-price-sort"), item => {
    const runtime = statusProducts.get(item.id), testResult = productTests.get(item.id);
    const source = testResult?.ok && !testResult.pending && !testResult.cancelled && !testResult.warning && (!runtime || Date.parse(testResult.checked_at) > Date.parse(runtime.checked_at || lastStatus?.last_checked || 0)) ? { ...runtime, ...testResult, error: "", url: testResult.url || testResult.purchase_url } : runtime;
    return productSnapshot(item, source).price;
  });
  const total = orderedProducts("notify").length;
  document.querySelector("#notification-count").textContent = `共 ${total} 个 · 显示 ${selected.length} 个`;
  document.querySelector("#notification-note").textContent = "商品信息来自商家快照、后台检查或最近一次成功测试。测试通知可在暂停时执行；下架或未匹配商品保留配置并显示未知，可在高级中手动添加及修改。";
  const target = document.querySelector("#notification-list");
  if (!selected.length) { target.replaceChildren(text("div", total ? "没有符合条件的商品。" : "尚未选择通知商品，可从大盘、库存监控或下方高级管理添加。", "empty")); return; }
  const wrap = text("div", "", "catalog-table-wrap"); wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "通知商品库存表");
  const table = text("table", "", "watch-table notification-table"), head = document.createElement("thead"), headings = document.createElement("tr");
  for (const label of ["商家", "产品名称", "地区", "硬件配置", "流量 / 带宽", "价格 / 周期", "状态", "操作"]) {
    const th = document.createElement("th"); th.scope = "col";
    if (label.startsWith("价格")) th.append(createPriceSortButton("价格 / 周期", "hostwatch-notification-price-sort", () => { renderNotificationList(); animateFilterResult(document.querySelector("#notification-list")); }));
    else th.append(text("span", label));
    headings.append(th);
  }
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
    const time = item.adapter === "dmit" ? p.checked_at : testIsLatest ? testResult.checked_at : runtime ? runtime.checked_at || lastStatus?.last_checked : p.checked_at;
    const updated = text("small", time ? `${testIsLatest || p._fromTest ? "测试" : "读取"} ${compactDate(time)}` : "等待首次读取"); updated.title = time ? formatBeijingDate(time) : "尚无商品快照"; identity.append(updated);
    cell(p.region || "—"); renderSpecs(cell("", "watch-specs"), p.specs); renderSpecs(cell("", "watch-specs"), p.traffic, true); renderPrice(cell("", "watch-price"), p.price, expanded.has(item.id));
    const stock = cell();
    stock.append(text("span", Number.isInteger(p.available) ? stockLabel(p) : "未知", `state ${!Number.isInteger(p.available) ? "pending" : p.available > 0 ? "ok" : "error"}`));
    if (p.error) stock.append(text("small", p.error, "error"));
    const actions = text("div", "", "watch-operations");
    const raw = p.url || item.purchase_url;
    if (raw && /^https:\/\//i.test(raw)) { const link = text("a", "直接购买", "buy-link"); link.href = affiliatePurchaseURL(raw, item.adapter, item); link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link); }
    const test = text("button", "测试通知", "product-test"); test.type = "button"; test.addEventListener("click", () => testProduct(item.id));
    const seckillActive = seckillMembership(item);
    const seckill = text("button", seckillActive ? "✓ 已加入 VPS 补货抢购" : "＋ 加入 VPS 补货抢购", "membership-toggle membership-seckill"); seckill.type = "button"; seckill.disabled = actionRunning; seckill.setAttribute("aria-pressed", String(seckillActive)); seckill.title = seckillActive ? "点击移出 VPS 补货抢购" : "点击加入 VPS 补货抢购"; seckill.addEventListener("click", () => toggleVpsSeckill(item));
    const stop = text("button", "停止测试", "product-test-stop danger"); stop.type = "button"; stop.addEventListener("click", stopProductTest);
    const edit = text("button", "编辑商品"); edit.type = "button"; edit.disabled = actionRunning;
    edit.addEventListener("click", () => { openProducts.add(item.id); renderProducts(); openAdvancedProducts(); editorNode.querySelector(`[data-id="${CSS.escape(item.id)}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" }); });
    const remove = text("button", "移出通知", "danger product-remove"); remove.type = "button";
    remove.addEventListener("click", () => { if (actionRunning) return; setMembership(item, "notify", false); renderProducts(); updateDraftState(); scheduleAutoSave("notifications"); setMessage("已移出库存通知，库存监控选择保留，正在自动保存。", "pending", true, false); });
    actions.append(test, stop, moreActions(`notify-${item.id}`, item.name, [seckill, edit, remove])); cell().append(actions); body.append(row);
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
  const inStock = enabled.filter((item) => ((item.adapter === "dmit" ? productSnapshot(item, latest.get(item.id)) : latest.get(item.id))?.available || 0) > 0).length;
  const modules = lastStatus?.modules || {};
  const moduleNames = { catalog: "大盘", monitor: "监控", notify: "通知", seckill: "抢购" };
  for (const key of Object.keys(moduleNames)) {
    const state = modules[key] || {};
    const node = document.querySelector(`#overview-${key}-state`), note = document.querySelector(`#overview-${key}-note`), log = document.querySelector(`#overview-${key}-log`);
    if (!node) continue;
    const result = state.last_result || (key === "seckill" && vpsSeckillTask ? (vpsSeckillTask.pending ? "pending" : vpsSeckillTask.error ? "error" : "success") : "unknown");
    node.textContent = result === "success" ? "运行正常" : result === "pending" ? "刷新中" : result === "skipped" ? "未启用" : result === "error" ? "运行异常" : "未刷新";
    const tone = result === "success" ? "ok" : result === "pending" ? "pending" : result === "error" ? "error" : "";
    node.className = `overview-value state ${tone}`;
    const card = node.closest(".module-overview");
    if (card) card.dataset.state = tone || "unknown";
    const scheduleModule = ({ monitor: "monitor", notify: "notify", catalog: "catalog", seckill: "seckill" })[key];
    const scheduleProvider = key === "catalog"
      ? document.querySelector('[data-catalog-provider][aria-pressed="true"]')?.dataset.catalogProvider || "vmiss"
      : document.querySelector(`#${key === "monitor" ? "monitor-schedule-provider" : key === "notify" ? "notify-schedule-provider" : "vps-seckill-schedule-provider"}`)?.value || "vmiss";
    const schedule = key === "seckill" && vpsSeckillTask?.pending
      ? { ...providerSchedule(scheduleModule, scheduleProvider), mode: vpsSeckillTask.mode }
      : providerSchedule(scheduleModule, scheduleProvider);
    const mode = key === "seckill"
      ? ({ rapid: "极速抢购（自动）", precise: "精准抢购（自动）", manual: "手动抢购" }[schedule.mode] || "手动抢购")
      : schedule.mode === "auto" ? "自动" : "仅手动";
    const scheduleProviderName = merchantNames[scheduleProvider] || "其他商家";
    const at = state.last_checked ? formatBeijingDate(state.last_checked) : "暂无";
    const manualAt = state.last_manual_refresh ? formatBeijingDate(state.last_manual_refresh) : "暂无";
    const autoAt = state.last_auto_refresh ? formatBeijingDate(state.last_auto_refresh) : "暂无";
    const serviceState = key === "monitor" || key === "notify" ? ({ active: "运行中", inactive: "已停止", failed: "运行失败", activating: "启动中", deactivating: "停止中" }[monitorService?.active] || "状态未知") : "";
    note.replaceChildren(
      text("span", `${scheduleProviderName} · ${mode} · 间隔 ${intervalLabel(Number(schedule.interval_seconds ?? 0))}${serviceState ? ` · 服务${serviceState}` : ""} · 最近 ${at}`, "overview-note-main"),
    );
    if (log) {
      const sourceLogs = key === "seckill" && vpsSeckillTask?.logs?.length ? vpsSeckillTask.logs : state.logs || [];
      const lines = sourceLogs.slice(-5).map(item => {
        const trigger = item.trigger || "manual";
        const label = key === "seckill" ? ({ rapid: "极速抢购", precise: "精准抢购", manual: "手动抢购" }[trigger] || "抢购") : trigger === "manual" ? "手动" : "自动";
        const provider = item.provider ? `${merchantNames[item.provider] || item.provider} · ` : "";
        return `[${formatBeijingDate(item.at)}] ${provider}${label} · ${item.message || item.result || ""}`;
      });
      log.textContent = lines.length ? lines.join("\n") : "暂无日志";
    }
  }
  const combinedLogs = ["monitor", "notify"].flatMap(key => (modules[key]?.logs || []).map(item => ({ ...item, module: key === "monitor" ? "监控" : "通知" }))).sort((a, b) => String(a.at).localeCompare(String(b.at))).slice(-10);
  const combinedLogNode = document.querySelector("#watch-notify-logs");
  if (combinedLogNode) combinedLogNode.textContent = combinedLogs.length ? combinedLogs.map(item => `[${formatBeijingDate(item.at)}] ${item.module} · ${item.trigger === "manual" ? "手动" : "自动"} · ${item.message || item.result || ""}`).join("\n") : "暂无日志";
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
  ["商品", "地区", "价格 / 周期", "库存", "关注方式"].forEach(label => { const th = text("th", label); th.scope = "col"; heading.append(th); }); head.append(heading); table.append(head);
  const body = document.createElement("tbody");
  products.forEach(item => {
    const p = productSnapshot(item, latest.get(item.id)), row = document.createElement("tr"); row.dataset.id = item.id;
    const cell = (value = "", cls = "") => { const td = text("td", value, cls); row.append(td); return td; };
    const identity = cell(); identity.append(text("strong", p.name || item.name), text("small", item.provider));
    cell().textContent = p.region || "—";
    const priceCell = cell("", "overview-price");
    if (!String(p.price || "").trim() || p.price === "—") {
      const pending = text("span", "—", "price-pending");
      pending.title = "监控服务完成首次成功检查后显示价格";
      priceCell.append(pending);
    } else renderPrice(priceCell, p.price, expanded.has(item.id));
    cell().append(text("span", Number.isInteger(p.available) ? stockLabel(p) : "未知", `state ${!Number.isInteger(p.available) ? "pending" : p.available > 0 ? "ok" : "error"}`));
    const tags = cell(); if (membership(item, "watch")) tags.append(text("span", "库存监控", "membership-tag")); if (membership(item, "notify")) tags.append(text("span", "TG 通知", "membership-tag notify"));
    body.append(row);
  }); table.append(body); wrap.append(table); target.replaceChildren(wrap);
}

const egressInstallCommands = new Map();
const expandedEgressNodes = new Set();
// The command is identical for every node. Prefer the installed offline script;
// existing installations can download it without requiring a node ID or token.
const egressUninstallCommand = `sh -c 'set -eu; if [ -f /opt/hostwatch-egress/uninstall.sh ]; then exec sh /opt/hostwatch-egress/uninstall.sh; fi; script=$(mktemp); trap "rm -f \\"$script\\"" EXIT HUP INT TERM; url=https://raw.githubusercontent.com/xhpx7301/host-watch/main/uninstall-egress.sh; if command -v curl >/dev/null 2>&1; then curl -fsSL --connect-timeout 20 --max-time 120 "$url" -o "$script"; elif command -v wget >/dev/null 2>&1; then wget -q -T 120 -O "$script" "$url"; else echo "请安装 curl 或 wget 后重试。" >&2; exit 1; fi; sh "$script"'`;
function egressNodes() { return config.egress?.nodes || (config.egress = { nodes: [], routes: {} }).nodes; }
function rememberSavedEgressNode(node) {
  if (!savedConfig) return;
  const saved = JSON.parse(savedConfig);
  saved.egress ||= { nodes: [], routes: {} };
  const index = saved.egress.nodes.findIndex(item => item.id === node.id);
  if (index < 0) saved.egress.nodes.push({ ...node });
  else saved.egress.nodes[index] = { ...node };
  savedConfig = JSON.stringify(saved);
}
function mergeEgressStatus(fresh) {
  for (const remote of fresh.nodes || []) {
    const local = egressNodes().find(node => node.id === remote.id);
    if (!local) continue;
    const previous = savedConfig ? JSON.parse(savedConfig).egress?.nodes.find(node => node.id === remote.id) : null;
    const status = Object.fromEntries(["proxy_url", "health_url", "public_ip", "last_seen", "last_error", "pending_registration"].map(key => [key, remote[key]]));
    for (const [key, value] of Object.entries(status)) {
      if (!["proxy_url", "health_url"].includes(key) || local.pending_registration || local[key] === previous?.[key]) local[key] = value;
    }
    if (savedConfig) {
      const saved = JSON.parse(savedConfig).egress?.nodes.find(node => node.id === remote.id);
      if (saved) rememberSavedEgressNode({ ...saved, ...status });
    }
  }
}
function egressHealth(node) {
  if (node._egressHealthChecking) return { label: "检查中", tone: "checking", detail: "正在检查健康地址和 HTTPS 代理" };
  if (node.enabled === false) return { label: "已停用", tone: "disabled", detail: "节点已停用，不执行自动检查" };
  if (node.pending_registration || !node.proxy_url || !node.health_url || !node.token_configured) return { label: "待连接", tone: "pending", detail: "等待安装回报、代理地址或令牌" };
  if (node.last_error) return { label: "出口异常", tone: "error", detail: node.last_error };
  if (node.last_seen) return { label: "出口正常", tone: "ok", detail: `最近检查：${formatBeijingDate(node.last_seen)}` };
  return { label: "待检查", tone: "unknown", detail: "尚未检查出口连接" };
}
async function probeEgressNode(node, automatic = false) {
  node._egressHealthChecking = true;
  renderEgress();
  try {
    const result = await requestJSON("/api/egress/probe", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node_id: node.id }), timeoutMs: 65_000 });
    node.public_ip = result.public_ip;
    node.last_seen = result.last_seen || new Date((result.checked_at || Date.now() / 1000) * 1000).toISOString();
    node.last_error = "";
    if (!automatic) setMessage(`${node.name} ${result.proxy_verified ? "HTTPS 代理验证成功，实际出口 IP" : "出口检查成功"}：${result.public_ip}`, "success");
  } catch (error) {
    node.last_error = error.message;
    if (!automatic) setMessage(error.message, "error");
  } finally {
    node._egressHealthChecking = false;
    renderEgress();
  }
}
function scheduleEgressHealthCheck(delay = EGRESS_HEALTH_INTERVAL_MS) {
  clearTimeout(egressHealthTimer);
  egressHealthTimer = setTimeout(() => { void runEgressHealthChecks(); }, delay);
}
async function runEgressHealthChecks() {
  if (egressHealthRunning || actionRunning) { scheduleEgressHealthCheck(); return; }
  const nodes = egressNodes().filter(node => node.enabled !== false && !node.pending_registration && node.proxy_url && node.health_url && node.token_configured);
  if (!nodes.length) { renderEgress(); scheduleEgressHealthCheck(); return; }
  egressHealthRunning = true;
  nodes.forEach(node => { node._egressHealthChecking = true; });
  renderEgress();
  try {
    await Promise.all(nodes.map(node => probeEgressNode(node, true)));
    try { mergeEgressStatus(await requestJSON("/api/egress")); } catch { /* retain the per-node result when status refresh is unavailable */ }
  } finally {
    egressHealthRunning = false;
    nodes.forEach(node => { node._egressHealthChecking = false; });
    renderEgress();
    scheduleEgressHealthCheck();
  }
}
async function copyEgressCommand(node) {
  let result;
  await runAction(null, "", "正在生成安装命令并保存此节点的配置…", async () => {
    result = await requestJSON("/api/egress/install-command", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node, register_url: location.origin }) });
    Object.assign(node, result.node); rememberSavedEgressNode(result.node);
    egressInstallCommands.set(node.id, result); renderEgress(); updateDraftState();
  }, false);
  if (!result) return;
  try { await navigator.clipboard.writeText(result.command); }
  catch {
    const input = document.querySelector(`[data-egress-command="${CSS.escape(node.id)}"]`);
    input.focus(); input.select();
    if (!document.execCommand("copy")) { setMessage("安装命令已生成，请在命令框中手动复制。", "pending"); return; }
  }
  setMessage(result.auto_register ? "安装命令已复制。在子服务器执行后，点击“刷新节点”获取地址。" : "安装命令已复制。当前通过本地地址访问，请执行后回填终端输出的 JSON。", "success");
}
async function copyEgressUninstallCommand(input) {
  input.hidden = false; input.value = egressUninstallCommand;
  try { await navigator.clipboard.writeText(egressUninstallCommand); }
  catch {
    input.focus(); input.select();
    if (!document.execCommand("copy")) { setMessage("卸载命令已显示，请在命令框中手动复制。", "pending"); return; }
  }
  setMessage("卸载命令已复制，请在要卸载的子服务器执行；卸载后在管理页移除节点，修改后自动保存。", "success");
}
function renderEgress() {
  const nodes = egressNodes(), target = document.querySelector("#egress-nodes"); target.replaceChildren();
  if (!nodes.length) target.append(text("p", "点击“添加节点”生成默认配置，再一键复制命令到 Debian / Alpine 子服务器安装。", "empty"));
  nodes.forEach(node => {
    const card = text("article", "", "egress-node"), head = text("div", "", "egress-node-head"); card.dataset.nodeId = node.id;
    const body = text("div", "", "egress-node-body"); body.id = `egress-details-${node.id}`;
    const toggle = text("button", "", "egress-node-toggle"); toggle.type = "button"; toggle.disabled = actionRunning; toggle.setAttribute("aria-controls", body.id);
    const title = text("strong", node.name || node.id || "未命名节点"), identity = text("span", "", "egress-node-identity");
    identity.append(title, text("small", node.public_ip || (node.pending_registration ? "等待安装回报" : "公网 IP 未检查"), "subtle egress-node-ip")); toggle.append(identity);
    const syncExpanded = () => {
      const open = expandedEgressNodes.has(node.id); body.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", `${title.textContent}，${open ? "收起" : "展开"}节点配置`);
      toggle.title = open ? "收起节点配置" : "展开节点配置";
    };
    toggle.addEventListener("click", () => { if (expandedEgressNodes.has(node.id)) expandedEgressNodes.delete(node.id); else expandedEgressNodes.add(node.id); syncExpanded(); });
    syncExpanded(); head.append(toggle); card.append(head, body);
    const setup = text("div", "", "egress-setup"), install = text("button", "一键复制安装命令", "primary egress-install-copy"); install.type = "button"; install.disabled = actionRunning;
    install.addEventListener("click", () => copyEgressCommand(node));
    const uninstall = text("button", "一键复制卸载命令", "egress-uninstall-copy"); uninstall.type = "button"; uninstall.disabled = actionRunning;
    const uninstallCommand = document.createElement("textarea"); uninstallCommand.className = "egress-command"; uninstallCommand.rows = 3; uninstallCommand.readOnly = true; uninstallCommand.hidden = true; uninstallCommand.setAttribute("aria-label", `${node.name} 卸载命令`);
    uninstall.addEventListener("click", () => copyEgressUninstallCommand(uninstallCommand));
    const commandActions = text("div", "", "actions"); commandActions.append(install, uninstall);
    setup.append(commandActions, text("p", "支持 Debian / Alpine，默认端口 8787，节点令牌自动生成。复制安装命令时仅保存此节点，其他栏目草稿保留；在子服务器以 root 执行。", "hint"));
    const command = document.createElement("textarea"); command.className = "egress-command"; command.dataset.egressCommand = node.id; command.rows = 3; command.readOnly = true; command.setAttribute("aria-label", `${node.name} 安装命令`); command.value = egressInstallCommands.get(node.id)?.command || ""; command.placeholder = "点击上方按钮，自动生成并复制安装命令，无需修改下方默认配置。"; setup.append(command);
    const registration = egressInstallCommands.get(node.id);
    setup.append(text("p", "卸载命令所有节点通用，仅在目标子服务器执行，停止并删除节点程序、配置和专用账户。卸载后需在管理页调整出口路由、移除节点，修改后自动保存。", "hint"), uninstallCommand);
    if (registration) setup.append(text("p", registration.auto_register ? `自动回报至 ${registration.register_url}；安装后点击“刷新节点”。` : "当前主服务地址仅本机可访问。安装后将终端输出的 JSON 粘贴到“回填安装结果”，即可自动填入 IP 和连接地址。", "hint"));
    const fallback = document.createElement("details"); fallback.className = "egress-import"; fallback.append(text("summary", "回填安装结果（SSH 隧道或自动回报不可用时）"));
    const info = document.createElement("textarea"); info.rows = 3; info.placeholder = "粘贴子服务器安装完成后输出的 JSON"; info.setAttribute("aria-label", `${node.name} 安装结果`);
    const importButton = text("button", "回填安装结果"); importButton.type = "button";
    importButton.addEventListener("click", () => runAction(importButton, "回填中…", "正在回填节点安装结果…", async () => {
      let payload; try { payload = JSON.parse(info.value); } catch { throw new Error("请粘贴完整的安装结果 JSON。"); }
      if (payload?.node_id !== node.id) throw new Error("安装结果的节点 ID 与此节点不一致。");
      const result = await requestJSON("/api/egress/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      mergeEgressStatus(result.egress); renderEgress(); updateDraftState(); setMessage("节点连接信息已回填并保存，可检查出口 IP 后绑定商家。", "success");
    }));
    fallback.append(info, importButton); setup.append(fallback); body.append(setup);
    const grid = text("div", "", "form-grid");
    [["节点 ID", "id", "自动生成"], ["节点名称", "name", "出口节点"], ["代理地址", "proxy_url", "安装后自动回填"], ["健康检查地址", "health_url", "安装后自动回填"]].forEach(([label, key, placeholder]) => { const input = document.createElement("input"); input.value = node[key] || ""; input.placeholder = placeholder; input.disabled = key === "id" && Boolean(node.id); input.addEventListener("input", () => { node[key] = input.value.trim(); if (key === "name") { title.textContent = node.name || node.id || "未命名节点"; syncExpanded(); } updateDraftState(); scheduleAutoSave("egress"); }); const labelNode = document.createElement("label"); labelNode.append(text("span", label), input); grid.append(labelNode); });
    const port = document.createElement("input"); port.type = "number"; port.min = "1"; port.max = "65535"; port.value = node.listen_port || 8787; port.addEventListener("input", () => { node.listen_port = Number(port.value); updateDraftState(); scheduleAutoSave("egress"); }); const portLabel = document.createElement("label"); portLabel.append(text("span", "节点监听端口"), port); grid.append(portLabel);
    const token = document.createElement("input"); token.type = "password"; token.autocomplete = "new-password"; token.placeholder = node.token_configured ? "已配置，留空保持不变" : "复制安装命令时自动生成"; token.addEventListener("input", () => { node.token = token.value; updateDraftState(); scheduleAutoSave("egress"); }); const tokenLabel = document.createElement("label"); tokenLabel.append(text("span", "代理令牌"), token); grid.append(tokenLabel);
    const health = egressHealth(node), healthLabel = text("span", health.label, `egress-health ${health.tone}`); healthLabel.title = health.detail;
    const enabledLabel = settingButton("出口节点", node.enabled !== false, value => { node.enabled = value; renderEgress(); scheduleAutoSave("egress"); }, ["已停用", "已启用"]);
    const probe = text("button", "检查出口 IP"); probe.type = "button";
    probe.addEventListener("click", () => { if (!egressHealthRunning) void probeEgressNode(node); });
    const remove = text("button", "移除节点", "danger"); remove.type = "button"; remove.addEventListener("click", () => { const index = egressNodes().indexOf(node); if (index >= 0) egressNodes().splice(index, 1); expandedEgressNodes.delete(node.id); Object.keys(config.egress.routes || {}).forEach(provider => { if (config.egress.routes[provider] === node.id) delete config.egress.routes[provider]; }); renderEgress(); updateDraftState(); scheduleAutoSave("egress"); });
    const actions = text("div", "", "actions egress-node-actions"); actions.append(healthLabel, enabledLabel, probe, remove); probe.disabled = remove.disabled = actionRunning || egressHealthRunning || Boolean(node._egressHealthChecking); head.append(actions); body.append(grid); if (node.last_error) card.append(text("p", node.last_error, "error")); target.append(card);
  });
  const routes = document.querySelector("#egress-routes"); routes.replaceChildren(); const options = [new Option("主服务器直连", "")]; nodes.forEach(node => options.push(new Option(`${node.name} · ${node.public_ip || "未检查"}`, node.id)));
  orderedMerchantEntries().forEach(([id, label]) => {
    const select = document.createElement("select"); select.replaceChildren(...options.map(option => option.cloneNode(true)));
    select.value = config.egress?.routes?.[id] || "";
    const hint = text("small", "", "hint");
  const updateHint = () => { const node = nodes.find(row => row.id === select.value); hint.textContent = !node ? "仅此商家使用主服务器直连" : node.enabled === false ? "此节点已停用，绑定商家将无法读取；请启用，修改后自动保存。" : !node.proxy_url ? "此节点尚未完成安装回报，暂时无法读取。" : `仅此商家使用 ${node.name}；修改后修改后会自动保存。`; hint.className = node && (node.enabled === false || !node.proxy_url) ? "error" : "hint"; };
    select.addEventListener("change", () => { config.egress.routes[id] = select.value; if (!select.value) delete config.egress.routes[id]; updateHint(); updateDraftState(); scheduleAutoSave("egress"); });
    updateHint(); const node = document.createElement("label"); node.append(text("span", label), select, hint); routes.append(node);
  });
  document.querySelector("#egress-status").textContent = `${nodes.length} 个出口节点；已绑定 ${Object.keys(config.egress?.routes || {}).length} 个商家。已安装并启用的节点每 1 分钟自动检查健康地址和 HTTPS 代理，也可手动检查出口 IP。出口按商家生效，未绑定商家使用主服务器直连。修改后修改后会自动保存。大盘、库存监控/通知、分类读取和网页测试沿用对应商家出口；VPS 补货抢购可单独指定出口。`;
}
function collectEgress() { return { nodes: egressNodes().map(node => ({ ...node })), routes: { ...(config.egress?.routes || {}) } }; }
document.querySelector("#egress-add-node").addEventListener("click", () => { const node = { id: `egress-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, name: `出口节点 ${egressNodes().length + 1}`, proxy_url: "", health_url: "", token: "", listen_port: 8787, pending_registration: true, enabled: true }; egressNodes().push(node); expandedEgressNodes.add(node.id); renderEgress(); updateDraftState(); scheduleAutoSave("egress"); document.querySelector("#egress-nodes").lastElementChild.scrollIntoView({ behavior: "smooth", block: "nearest" }); });
document.querySelector("#egress-refresh").addEventListener("click", async () => { try { mergeEgressStatus(await requestJSON("/api/egress")); renderEgress(); updateDraftState(); setMessage("出口节点状态已刷新，其他未保存修改已保留。", "success"); } catch (error) { setMessage(error.message, "error"); } });

let vpsSeckillTask = null;
let vpsSeckillTestTask = null;
let vpsSeckillTestPoll = null;
const seckillAccountChecks = new Map();
const standaloneAccountChecks = new Map();
const accountHealthChecks = new Map();
const accountCheckGenerations = new Map();
const accountSaveFeedback = new Map();
let accountHealthTimer;
const ACCOUNT_HEALTH_INTERVAL_MS = 3_600_000;
function seckillAccountNote(item) {
  if (!item.account_id) return "未绑定账户：发现有货后提供链接，由你到官网下单和付款。";
  const checks = [seckillAccountChecks.get(item.id), vpsSeckillTask?.accounts?.[item.id]]
    .filter(result => result?.account_id === item.account_id).sort((a, b) => Date.parse(b.checked_at) - Date.parse(a.checked_at));
  return checks.length ? `上次检查：${checks[0].message} · ${formatBeijingDate(checks[0].checked_at)}` : "账户待检查：自动模式会通过此商品的出口验证账户。";
}
function standaloneAccountNote(account) {
  const result = standaloneAccountChecks.get(account.id);
  if (!result) return "账户待检查：此检查只验证登录，不创建订单、不付款。";
  const cache = result.session_reused ? " · 已复用认证缓存" : result.ok ? " · 已建立认证缓存" : "";
  return `上次检查：${result.message}${cache} · ${formatBeijingDate(result.checked_at)}`;
}
function accountHealth(account) {
  const result = accountHealthChecks.get(account.id);
  if (!result) return { label: "待检查", tone: "unknown", detail: "尚未检查认证缓存" };
  if (result.ok) return { label: "账号正常", tone: "ok", detail: `最近检查：${formatBeijingDate(result.checked_at)}` };
  if (result.status === "expired") return { label: "登录已失效", tone: "error", detail: result.message || "请重新检查账户" };
  return { label: "待认证", tone: "pending", detail: result.message || "请点击检查账户" };
}
function storeAccountCheck(target, result) {
  const accountId = String(result?.account_id || "");
  if (!accountId) return;
  const previous = target.get(accountId);
  const nextAt = Date.parse(result.checked_at || "") || 0;
  const previousAt = Date.parse(previous?.checked_at || "") || 0;
  if (previous?.ok && result.status === "unknown") return;
  if (previous && previousAt > nextAt) return;
  target.set(accountId, result);
}
function invalidateAccountCheck(accountId) {
  const id = String(accountId || "");
  accountCheckGenerations.set(id, (accountCheckGenerations.get(id) || 0) + 1);
}
async function refreshAccountHealth() {
  if (actionRunning || document.hidden || !["seckill", "accounts"].includes(currentPage)) {
    accountHealthTimer = setTimeout(refreshAccountHealth, ACCOUNT_HEALTH_INTERVAL_MS);
    return;
  }
  const accounts = seckillAccounts();
  if (!accounts.length) {
    accountHealthTimer = setTimeout(refreshAccountHealth, ACCOUNT_HEALTH_INTERVAL_MS);
    return;
  }
  await Promise.allSettled(accounts.map(async account => {
    const generation = accountCheckGenerations.get(account.id) || 0;
    try {
      const result = await requestJSON("/api/seckill/account-status", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ account_id: account.id }), timeoutMs: 30_000 });
      if (generation !== (accountCheckGenerations.get(account.id) || 0)) return;
      storeAccountCheck(accountHealthChecks, result);
    } catch (error) {
      if (generation !== (accountCheckGenerations.get(account.id) || 0)) return;
      storeAccountCheck(accountHealthChecks, { account_id: account.id, ok: false, status: "unknown", message: error.message, checked_at: new Date().toISOString() });
    }
  }));
  renderVpsSeckill();
  accountHealthTimer = setTimeout(refreshAccountHealth, ACCOUNT_HEALTH_INTERVAL_MS);
}
function scheduleAccountHealthCheck(delay = 1_000) {
  clearTimeout(accountHealthTimer);
  accountHealthTimer = setTimeout(refreshAccountHealth, delay);
}
let vpsSeckillPoll = null;
function seckillItemId() { return `seckill-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`; }
function seckillItems() { return config.vps_seckill?.items || (config.vps_seckill = { enabled: false, mode: "manual", interval_seconds: 5, items: [], accounts: [] }).items; }
function seckillAccounts() { return config.vps_seckill?.accounts || (config.vps_seckill.accounts = []); }
function seckillProviderKey(value) {
  const aliases = { vmiss: "vmiss", "vps.soy": "vps_soy", vps_soy: "vps_soy", "搬瓦工": "bwh", bwh: "bwh", bandwagonhost: "bwh", akile: "akile", narwhal: "narwhal", "独角鲸云": "narwhal", isvoro: "isvoro", dmit: "dmit" };
  const raw = String(value || "").trim();
  return aliases[raw.toLowerCase()] || (Object.prototype.hasOwnProperty.call(merchantNames, raw) ? raw : "");
}
function seckillAccountMerchant(account) {
  const provider = String(account?.provider || "").trim();
  const key = seckillProviderKey(provider) || provider;
  return merchantNames[key] || provider || (account?.login_url ? "自定义商家" : "未选择商家");
}
function seckillAccountLabel(account) {
  const name = account?.name || account?.username || account?.id || "未命名账户";
  return `${name} · ${seckillAccountMerchant(account)}${account?.password_configured ? " · 已保存密码" : ""}`;
}
function orderedSeckillAccounts() {
  const ranks = new Map(merchantOrder().map((key, index) => [merchantNames[key], index]));
  return seckillAccounts().map((account, index) => ({ account, index })).sort((a, b) => (ranks.get(seckillAccountMerchant(a.account)) ?? 999) - (ranks.get(seckillAccountMerchant(b.account)) ?? 999) || a.index - b.index).map(entry => entry.account);
}
function seckillAddDefaults(product) {
  const provider = seckillProviderKey(product?.adapter) || seckillProviderKey(product?.provider);
  const account = provider ? seckillAccounts().find(row => seckillProviderKey(row.provider) === provider) : null;
  const coupon = provider ? String(product?.notification_coupon_code || config.affiliate_coupons?.[provider] || "").trim() : "";
  const vmissElite = provider === "vmiss" && String(product?.name || "").trim().toLowerCase() === "us.la.cmin2.elite";
  const vmiss = provider === "vmiss";
  const akile = provider === "akile";
  return {
    account_id: account?.id || "",
    egress_node_id: provider ? config.egress?.routes?.[provider] || "" : "",
    coupon_code: vmissElite ? "10%OFF" : coupon,
    operating_system: vmiss || akile ? "Debian 12" : "",
    billing_cycle: vmiss || akile ? "monthly" : "",
    payment_method: vmiss ? "alipay" : "",
    akile_protocol_agreed: akile,
    akile_broadcast_ip_ack: akile,
  };
}
function addToVpsSeckill(product) {
  if (!product || !config.products.some(item => item.id === product.id && (membership(item, "watch") || membership(item, "notify")))) return;
  if (scopeIsDirty("monitor")) scheduleAutoSave("monitor");
  const items = seckillItems();
  let addedDefaults = null;
  if (!items.some(item => item.product_id === product.id)) {
    const defaults = seckillAddDefaults(product);
    items.push({ id: product.id, product_id: product.id, label: product.name, quantity: 1, ...defaults, target_url: product.purchase_url || product.snapshot?.url || "", enabled: true });
    const accountLabel = defaults.account_id ? "已绑定默认账户" : "未找到对应账户";
    const egressLabel = defaults.egress_node_id ? "已绑定默认出口节点" : "跟随商家出口";
    const couponLabel = defaults.coupon_code ? "已填入默认优惠码" : "暂无默认优惠码";
    addedDefaults = `${accountLabel}、${egressLabel}、${couponLabel}`;
  }
  renderVpsSeckill(); updateDraftState(); scheduleAutoSave("seckill"); setMessage(`${product.name} 已加入 VPS 补货抢购${addedDefaults ? `：${addedDefaults}` : ""}，正在自动保存。`, "pending", true, false);
}
function toggleVpsSeckill(product) {
  if (!product || actionRunning) return;
  const existing = seckillItems().find(item => item.product_id === product.id);
  if (existing) {
    const index = seckillItems().indexOf(existing);
    if (index >= 0) seckillItems().splice(index, 1);
    renderVpsSeckill(); updateDraftState(); scheduleAutoSave("seckill");
    setMessage(`${product.name} 已移出 VPS 补货抢购，正在自动保存。`, "pending", true, false);
    return;
  }
  addToVpsSeckill(product);
}
function renderVpsSeckill() {
  const value = config.vps_seckill || { enabled: false, mode: "manual", interval_seconds: 5, items: [], accounts: [] };
  renderProviderSchedule("seckill-settings");
  const accountOptions = [new Option("不绑定账户", "")];
  const accounts = orderedSeckillAccounts();
  accounts.forEach(account => accountOptions.push(new Option(seckillAccountLabel(account), account.id)));
  const itemTarget = document.querySelector("#vps-seckill-items"); itemTarget.replaceChildren();
  const products = new Map(config.products.map(item => [item.id, item]));
  if (!seckillItems().length) itemTarget.append(text("p", "还没有补货抢购商品，请从 VPS 库存监控或 VPS 库存通知的“更多”中逐个加入。", "empty"));
  seckillItems().forEach(item => {
    const product = products.get(item.product_id), card = text("article", "", "seckill-item"); card.dataset.id = item.id;
    const title = text("strong", product?.name || item.label || item.product_id || "已下架商品");
    const head = text("div", "", "seckill-item-head"); head.append(title, text("small", product ? `${product.provider} · ${item.product_id}` : "商品已下架，保留补货抢购配置", "subtle")); card.append(head);
    const grid = text("div", "", "form-grid");
    const quantity = document.createElement("input"); quantity.type = "number"; quantity.min = "1"; quantity.max = "100"; quantity.step = "1"; quantity.value = item.quantity ?? 1;
    quantity.addEventListener("input", () => { item.quantity = Number(quantity.value); updateDraftState(); scheduleAutoSave("seckill"); });
    const coupon = document.createElement("input"); coupon.value = item.coupon_code || ""; coupon.maxLength = 256; coupon.placeholder = "可选专属优惠码"; coupon.addEventListener("input", () => { item.coupon_code = coupon.value; updateDraftState(); scheduleAutoSave("seckill"); });
    const operatingSystem = document.createElement("select");
    [["", "跟随商家默认系统"], ["AlmaLinux 9", "AlmaLinux 9"], ["AlmaLinux 10", "AlmaLinux 10"], ["Debian 11", "Debian 11"], ["Debian 12", "Debian 12"], ["Debian 13", "Debian 13"], ["Ubuntu 22", "Ubuntu 22"], ["Ubuntu 24", "Ubuntu 24"]].forEach(([value, label]) => operatingSystem.add(new Option(label, value)));
    operatingSystem.value = item.operating_system || "";
    operatingSystem.addEventListener("change", () => { item.operating_system = operatingSystem.value; updateDraftState(); scheduleAutoSave("seckill"); });
    const billingCycle = document.createElement("select");
    [["", "跟随商家默认周期"], ["monthly", "月付"], ["quarterly", "季付"], ["annually", "年付"]].forEach(([value, label]) => billingCycle.add(new Option(label, value)));
    billingCycle.value = item.billing_cycle || "";
    billingCycle.addEventListener("change", () => { item.billing_cycle = billingCycle.value; updateDraftState(); scheduleAutoSave("seckill"); });
    const paymentMethod = document.createElement("select");
    [["", "跟随商家默认付款方式"], ["alipay", "Alipay"], ["credit_card", "Credit Card"], ["usdt", "USDT"]].forEach(([value, label]) => paymentMethod.add(new Option(label, value)));
    paymentMethod.value = item.payment_method || "";
    paymentMethod.addEventListener("change", () => { item.payment_method = paymentMethod.value; updateDraftState(); scheduleAutoSave("seckill"); });
    const account = document.createElement("select"); account.replaceChildren(...accountOptions.map(option => option.cloneNode(true))); account.value = item.account_id || ""; account.addEventListener("change", () => { item.account_id = account.value; seckillAccountChecks.delete(item.id); renderVpsSeckill(); updateDraftState(); scheduleAutoSave("seckill"); });
    const egressSelect = document.createElement("select"); const egressOptions = [new Option("跟随商家出口（未分配则直连）", "")]; egressNodes().forEach(node => egressOptions.push(new Option(node.name || node.id, node.id))); egressSelect.replaceChildren(...egressOptions); egressSelect.value = item.egress_node_id || ""; egressSelect.addEventListener("change", () => { item.egress_node_id = egressSelect.value; seckillAccountChecks.delete(item.id); renderVpsSeckill(); updateDraftState(); scheduleAutoSave("seckill"); });
    const target = document.createElement("input"); target.type = "url"; target.value = item.target_url || product?.purchase_url || ""; target.placeholder = "下架商品目标购买链接（可选）"; target.addEventListener("input", () => { item.target_url = target.value; updateDraftState(); scheduleAutoSave("seckill"); });
    const providerKey = seckillProviderKey(product?.adapter) || seckillProviderKey(product?.provider);
    if (providerKey === "akile" || providerKey === "vmiss") {
      item.operating_system = "Debian 12";
      item.billing_cycle = "monthly";
      if (providerKey === "akile") {
        item.akile_protocol_agreed = true;
        item.akile_broadcast_ip_ack = true;
      }
    }
    const submitHint = text("p", providerKey === "vmiss"
      ? "VMISS 使用内置官网结算适配：密码登录时自动启用官网 Remember Me，请求更持久的登录 Cookie；认证会话在内存中缓存最长 30 天，验证仍有效时会续延本地缓存。自动模式会预填商品配置并创建待付款发票；不会自动付款。"
      : providerKey === "akile"
        ? "Akile 使用内置待付款适配：固定 Debian 12 和 1 个月周期创建订单；不会自动付款。"
        : providerKey === "bwh"
          ? "搬瓦工使用内置官网结算适配：密码登录时自动启用官网 Remember Me，以请求更持久的登录 Cookie；认证会话在内存中缓存最长 30 天，验证仍有效时会续延本地缓存。官网未提供可用的 refresh Cookie 接口，手动导入的普通 Cookie 过期后仍需重新登录。"
        : "自动模式仅对已配置商家待付款适配的商品创建订单；未适配商家只提供购买链接。", "hint span-2");
    [["下单数量（台，1–100）", quantity], ["优惠码", coupon], ["系统", operatingSystem], ["付款周期", billingCycle], ["付款方式", paymentMethod], ["账户", account], ["出口节点", egressSelect], ["目标购买链接", target]].forEach(([label, control]) => { const labelNode = document.createElement("label"); labelNode.append(text("span", label), control); grid.append(labelNode); });
    const enabled = settingButton("商品状态", item.enabled !== false, value => { item.enabled = value; renderVpsSeckillTask(); updateDraftState(); scheduleAutoSave("seckill"); }, ["已停用", "已启用"]);
    const flags = text("div", "", "actions"); flags.append(enabled);
    const status = text("p", seckillAccountNote(item), "hint seckill-account-status"); status.setAttribute("role", "status");
    card.append(status);
    const remove = text("button", "删除商品", "danger"); remove.type = "button"; remove.disabled = actionRunning;
    remove.setAttribute("aria-label", `删除补货抢购商品 ${title.textContent}`);
    remove.addEventListener("click", () => { const index = seckillItems().indexOf(item); if (index >= 0) seckillItems().splice(index, 1); renderVpsSeckill(); updateDraftState(); scheduleAutoSave("seckill"); setMessage("已删除该补货抢购商品，正在自动保存。", "pending"); }); flags.append(remove);
    card.append(grid, submitHint, flags); itemTarget.append(card);
  });
  const accountTarget = document.querySelector("#vps-seckill-accounts"); accountTarget.replaceChildren();
  const accountPanel = accountTarget.closest(".seckill-panel");
  let authDesign = accountPanel?.querySelector(".merchant-auth-design");
  if (accountPanel && !authDesign) {
    authDesign = document.createElement("div"); authDesign.className = "merchant-auth-design";
    const title = text("h4", "商家登录方式设计");
    const note = text("p", "认证会话按商家、账户和出口节点隔离，最长缓存 30 天。缓存失效后只尝试正常登录或官方 refresh；遇到验证码、Passkey 或 hCaptcha 时需要人工完成。", "hint");
    const table = document.createElement("table"); table.className = "merchant-auth-table";
    table.innerHTML = `<thead><tr><th>商家</th><th>主方案</th><th>Cookie 备用</th><th>浏览器增强</th></tr></thead><tbody>
      <tr><th>Akile</th><td>官方 API + 令牌校验</td><td>不作为常规方案</td><td>API 异常时人工恢复</td></tr>
      <tr><th>独角鲸云</th><td>邮箱密码登录 API + Bearer 令牌，核对账户邮箱</td><td>暂不支持导入，令牌最长缓存 30 天</td><td>API 异常或安全验证时到官网人工处理</td></tr>
      <tr><th>Isvoro</th><td>Cookie 主方案 + refresh Cookie</td><td>主方案</td><td>refresh 失败后人工 hCaptcha 恢复</td></tr>
      <tr><th>VMISS</th><td>账号密码 + Remember Me</td><td>支持导入后验证</td><td>Cloudflare 或安全验证时使用</td></tr>
      <tr><th>DMIT</th><td>账号密码 + Remember Me</td><td>支持 Cookie + User-Agent</td><td>浏览器/IP 绑定时人工恢复</td></tr>
      <tr><th>搬瓦工</th><td>账号密码 + Remember Me</td><td>支持 Cookie，减少邮箱验证</td><td>邮箱验证或会话失效时使用</td></tr>
      <tr><th>VPS.SOY / 其他</th><td>按官网实际流程</td><td>仅在商家会话可确认时启用</td><td>逐商家适配</td></tr>
    </tbody>`;
    authDesign.append(title, note, table); accountPanel.insertBefore(authDesign, accountTarget);
  }
  if (!seckillAccounts().length) accountTarget.append(text("p", "尚未配置网站账户。手动抢购不需要账户。", "empty"));
  accounts.forEach(account => {
    const card = text("article", "", "seckill-account"), grid = text("div", "", "form-grid"); card.dataset.accountId = account.id;
    const merchantIds = { vmiss: "vmiss", "vps.soy": "vps_soy", vps_soy: "vps_soy", "搬瓦工": "bwh", bwh: "bwh", bandwagonhost: "bwh", akile: "akile", isvoro: "isvoro", dmit: "dmit", narwhal: "narwhal", "独角鲸云": "narwhal" };
    const providerValue = () => account.provider ? (merchantIds[String(account.provider).trim().toLowerCase()] || "custom") : (account.login_url ? "custom" : "");
    const field = (label, key, placeholder, type = "text") => {
      const input = document.createElement("input"); input.type = type; input.value = account[key] || ""; input.placeholder = placeholder;
      input.addEventListener("input", () => { account[key] = input.value; clearAccountSaveFeedback(account.id); updateDraftState(); updateSeckillAccountControls(account.id); });
      const node = document.createElement("label"); node.append(text("span", label), input); grid.append(node);
    };
    field("账户名称", "name", "账户名称");
    const provider = document.createElement("select");
    provider.replaceChildren(new Option("选择商家", ""), ...merchantOrder().map(key => new Option(merchantNames[key], key)), new Option("其他商家 · JSON 适配接口", "custom"));
    provider.value = providerValue();
    const providerLabel = document.createElement("label"); providerLabel.append(text("span", "商家"), provider); grid.append(providerLabel);
    const adapterNote = text("p", "", "hint span-2"); adapterNote.setAttribute("role", "status");
    const providerExtra = text("div", "", "form-grid span-2");
    const setProviderExtra = () => {
      providerExtra.replaceChildren();
      const selected = provider.value;
      const ids = { vmiss: "VMISS", vps_soy: "VPS.SOY", akile: "Akile", narwhal: "独角鲸云", isvoro: "Isvoro", dmit: "DMIT", bwh: "搬瓦工" };
      if (selected === "custom") {
        adapterNote.textContent = "其他商家需要自行提供实现 hostwatch-unpaid-v1 的账户检查 JSON 接口；普通登录页不能直接使用。";
        const name = document.createElement("input"); name.value = account.provider && !merchantIds[String(account.provider).trim().toLowerCase()] ? account.provider : ""; name.placeholder = "商家名称"; name.addEventListener("input", () => { account.provider = name.value; clearAccountSaveFeedback(account.id); updateDraftState(); updateSeckillAccountControls(account.id); });
        const nameLabel = document.createElement("label"); nameLabel.append(text("span", "商家名称"), name); providerExtra.append(nameLabel);
        const login = document.createElement("input"); login.type = "url"; login.value = account.login_url || ""; login.placeholder = "https://…（HostWatch JSON 接口）"; login.addEventListener("input", () => { account.login_url = login.value; clearAccountSaveFeedback(account.id); updateDraftState(); updateSeckillAccountControls(account.id); });
        const loginLabel = document.createElement("label"); loginLabel.append(text("span", "账户检查接口"), login); providerExtra.append(loginLabel);
      } else if (selected === "vmiss" || selected === "vps_soy" || selected === "bwh" || selected === "akile" || selected === "narwhal" || selected === "isvoro" || selected === "dmit") {
        const urls = { vmiss: "https://app.vmiss.com/login", vps_soy: "https://www.vps.soy/login", bwh: "https://bwh81.net/clientarea.php", akile: "https://api.akile.ai/api/v1/user/login", narwhal: "https://api.fuckip.me/api/v1/auth/login", isvoro: "https://isvoro.com/api/v1/auth/login", dmit: "https://www.dmit.io/clientarea.php" };
        account.provider = selected; account.login_url = urls[selected];
        adapterNote.textContent = selected === "akile" ? "Akile 已内置 API 登录检查；填写邮箱和密码。若要求邮箱验证、二步验证、Passkey 或验证码，请到官网完成。检查不下单、不付款。" : selected === "narwhal" ? "独角鲸云已内置 API 登录检查；填写注册邮箱和密码。服务会核对返回账户邮箱，并在内存中缓存登录令牌最长 30 天。检查不下单、不付款。" : selected === "isvoro" ? "Isvoro 已内置 API 登录检查；首次在官网完成 hCaptcha 后导入 Cookie。服务会在内存中缓存最长 30 天，并在 access Cookie 过期时自动使用 refresh Cookie 续期；服务重启或 refresh Cookie 失效后需重新导入。" : selected === "vmiss" ? "VMISS 已内置登录与官网结算适配；填写官网登录邮箱和密码。自动抢购会预填商品配置并创建待付款发票，不会自动付款。" : selected === "bwh" ? "搬瓦工已内置登录与官网结算适配；可在官网完成邮箱验证后导入 Cookie，服务在内存中缓存最长 30 天。Cookie 不会写入配置或备份；过期或服务重启后需重新导入，密码登录仍可能要求邮箱验证码。" : selected === "dmit" ? "DMIT 已内置账号密码登录检查。也可在官网用账号密码登录并完成安全验证后导入 Cookie；验证成功后只保存在服务器内存，最长 30 天。Cookie 不会写入配置或备份；若 DMIT 将会话绑定浏览器或出口 IP，服务器可能无法验证。" : `${ids[selected]} 已内置登录检查；只需填写官网登录名和密码。检查仅登录，不下单、不付款。遇到安全验证时，请在网页请求中为该商家选择 FlareSolverr 或 cloudscraper。`;
        const destinations = { ...urls, akile: "https://akile.ai/login", narwhal: "https://dash.fuckip.me/login", isvoro: "https://isvoro.com/login" };
        const link = text("a", `打开 ${ids[selected]} 官网登录页`); link.href = destinations[selected]; link.target = "_blank"; link.rel = "noopener noreferrer"; link.className = "provider-login-link"; providerExtra.append(link);
        if (selected === "isvoro" || selected === "bwh" || selected === "dmit") {
          const cookie = document.createElement("input"); cookie.type = "password"; cookie.autocomplete = "off"; cookie.value = account.session_cookie || ""; cookie.placeholder = "官网手动登录后粘贴 Cookie: name=value; …";
          cookie.addEventListener("input", () => { account.session_cookie = cookie.value; updateDraftState(); });
          const cookieLabel = document.createElement("label"); cookieLabel.append(text("span", `${ids[selected]} 会话 Cookie（仅本次检查）`), cookie); providerExtra.append(cookieLabel);
          if (selected === "dmit") {
            const userAgent = document.createElement("input"); userAgent.type = "text"; userAgent.autocomplete = "off"; userAgent.value = account.session_user_agent || ""; userAgent.placeholder = "可选：Network → Headers → User-Agent";
            userAgent.addEventListener("input", () => { account.session_user_agent = userAgent.value; updateDraftState(); });
            const userAgentLabel = document.createElement("label"); userAgentLabel.append(text("span", "DMIT 浏览器 User-Agent（可选）"), userAgent); providerExtra.append(userAgentLabel);
          }
          const instructions = selected === "isvoro"
            ? "导入步骤：打开登录页 → 完成 hCaptcha 并登录 → F12 打开 Network → 刷新 → 找到 /api/v1/auth/me → Headers → Request Headers → Cookie，复制整行粘贴到上方。验证通过后只保存在服务器内存，最长 30 天；access Cookie 过期时会尝试使用 refresh Cookie 续期。"
            : selected === "bwh"
              ? "导入步骤：打开搬瓦工官网登录页并完成邮箱验证码 → F12 打开 Network → 刷新 → 找到 clientarea.php 请求 → Headers → Request Headers → Cookie，复制整行粘贴到上方。验证通过后只保存在服务器内存，最长 30 天；Cookie 本身过期、被撤销或服务重启后需重新导入。密码登录时 HostWatch 会自动启用官网 Remember Me；它请求商家签发更持久的 Cookie，但不是 refresh Cookie 自动续期。"
              : "导入步骤：打开 DMIT 官网，用账户邮箱和密码正常登录（如果官网要求安全验证，请先完成）→ 登录后打开 F12 的 Network → 刷新账户页面 → 找到 clientarea.php 请求 → Headers → Request Headers → Cookie，复制整行粘贴到上方。若仍失败，再复制该请求的 User-Agent 填入下方。验证通过后只保存在服务器内存，最长 30 天；过期、被撤销或服务重启后需重新登录并导入。Cookie 可能受浏览器/IP 绑定，服务器出口验证不一定成功。";
          providerExtra.append(text("p", instructions, "merchant-cookie-help"));
        }
      } else if (selected) {
        account.provider = selected; account.login_url = "";
        adapterNote.textContent = `${ids[selected]} 登录含验证码、API 验证或二次验证，目前需到官网手动登录；检查会明确提示无法自动确认。`;
        const urls = { isvoro: "https://isvoro.com/", dmit: "https://www.dmit.io/login" };
        const link = text("a", `打开 ${ids[selected]} 官网`); link.href = urls[selected]; link.target = "_blank"; link.rel = "noopener noreferrer"; providerExtra.append(link);
      } else {
        account.provider = ""; account.login_url = ""; adapterNote.textContent = "先选择账户所属商家。";
      }
      updateDraftState();
    };
    provider.addEventListener("change", () => {
      clearAccountSaveFeedback(account.id);
      if (provider.value === "custom" && account.provider && merchantIds[String(account.provider).trim().toLowerCase()]) {
        account.provider = ""; account.login_url = "";
      }
      setProviderExtra(); updateSeckillAccountControls(account.id);
    });
    setProviderExtra();
    field("用户名 / 邮箱", "username", "官网登录名或邮箱");
    const accountEgress = document.createElement("select");
    accountEgress.add(new Option("跟随商家出口（未分配则直连）", ""));
    egressNodes().forEach(node => accountEgress.add(new Option(node.name || node.id, node.id)));
    accountEgress.value = account.egress_node_id || "";
    accountEgress.addEventListener("change", () => { account.egress_node_id = accountEgress.value; clearAccountSaveFeedback(account.id); standaloneAccountChecks.delete(account.id); accountHealthChecks.delete(account.id); updateDraftState(); updateSeckillAccountControls(account.id); });
    const accountEgressLabel = document.createElement("label"); accountEgressLabel.append(text("span", "Cookie / 账户检查出口"), accountEgress); grid.append(accountEgressLabel);
    const password = document.createElement("input"); password.type = "password"; password.autocomplete = "new-password"; password.placeholder = account.password_configured ? "已保存，留空保持不变" : "登录密码"; password.addEventListener("input", () => { account.password = password.value; account.password_configured = Boolean(password.value) || account.password_configured; clearAccountSaveFeedback(account.id); updateDraftState(); updateSeckillAccountControls(account.id); }); const passwordLabel = document.createElement("label"); passwordLabel.append(text("span", "密码"), password); grid.append(passwordLabel);
    const check = text("button", "检查账户", "seckill-check-account"); check.type = "button"; check.dataset.accountId = account.id; check.disabled = actionRunning;
    check.setAttribute("aria-label", `检查网站账户 ${account.name || account.username || account.id}`);
    check.addEventListener("click", () => {
      runAction(check, "检查中…", "正在检查账户，不创建订单、不付款…", async () => {
        invalidateAccountCheck(account.id);
        let result;
        const transientCredentials = Object.fromEntries(["session_cookie", "session_user_agent", "captcha_token", "captcha_provider"].filter(key => account[key]).map(key => [key, account[key]]));
        try {
          await saveConfig(`seckill-account:${account.id}`);
          const savedAccount = seckillAccounts().find(row => row.id === account.id) || account;
          result = await requestJSON("/api/seckill/account-check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ config: collectConfig(), account_id: savedAccount.id, account: { ...savedAccount, ...transientCredentials } }) });
        } finally {
          // Manually imported merchant cookies are transient credentials; never
          // leave them in the browser draft after the check request completes.
          delete account.session_cookie;
          delete account.session_user_agent;
          delete account.captcha_token;
          delete account.captcha_provider;
        }
        invalidateAccountCheck(account.id);
        storeAccountCheck(standaloneAccountChecks, result); storeAccountCheck(accountHealthChecks, result); renderVpsSeckill(); setMessage(result.message, result.ok ? "success" : result.verification_required ? "pending" : "error");
      });
    });
    const remove = text("button", "删除账户", "danger"); remove.type = "button"; remove.disabled = actionRunning;
    remove.setAttribute("aria-label", `删除补货抢购账户 ${account.name || account.id}`);
    remove.addEventListener("click", () => deleteSeckillAccounts([account.id]));
    const clearCache = text("button", "清除登录缓存"); clearCache.type = "button"; clearCache.disabled = actionRunning || hasUnsavedSeckillAccount(account.id);
    clearCache.setAttribute("aria-label", `清除网站账户 ${account.name || account.id} 的登录缓存`);
    clearCache.addEventListener("click", () => {
      runAction(clearCache, "清除中…", "正在清除该账户的认证缓存…", async () => {
        invalidateAccountCheck(account.id);
        const result = await requestJSON("/api/seckill/account-cache-clear", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ account_id: account.id }) });
        standaloneAccountChecks.delete(account.id); accountHealthChecks.delete(account.id); renderVpsSeckill(); setMessage(result.message, "success");
      });
    });
    const verification = standaloneAccountChecks.get(account.id);
    const verificationBox = text("div", "", "form-grid span-2");
    if (verification?.verification_required && verification.challenge_id) {
      const code = document.createElement("input"); code.type = "text"; code.inputMode = "numeric"; code.autocomplete = "one-time-code"; code.maxLength = 12; code.placeholder = "输入搬瓦工邮箱中的最新验证码";
      const codeLabel = document.createElement("label"); codeLabel.append(text("span", "邮箱验证码"), code); verificationBox.append(codeLabel);
      const submitCode = text("button", "提交验证码", "primary"); submitCode.type = "button"; submitCode.disabled = actionRunning;
      submitCode.addEventListener("click", () => {
        runAction(submitCode, "提交中…", "正在提交搬瓦工邮箱验证码…", async () => {
          const result = await requestJSON("/api/seckill/account-verification", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ account_id: account.id, account, challenge_id: verification.challenge_id, verification_code: code.value }) });
          storeAccountCheck(standaloneAccountChecks, result); storeAccountCheck(accountHealthChecks, result); renderVpsSeckill(); setMessage(result.message, result.ok ? "success" : result.verification_required ? "pending" : "error");
        });
      });
      const action = text("div", "", "actions"); action.append(submitCode); verificationBox.append(action);
    }
    const accountHealthState = accountHealth(account);
    const healthLabel = text("span", accountHealthState.label, `egress-health ${accountHealthState.tone}`); healthLabel.title = accountHealthState.detail;
    const accountParts = [grid, adapterNote, providerExtra];
    if (verificationBox.childElementCount) accountParts.push(verificationBox);
    accountParts.push(text("p", standaloneAccountNote(account), "hint seckill-account-status"), text("div", "", "actions"));
    card.append(...accountParts);
    const actions = card.lastChild;
    const save = text("button", "保存账户"); save.type = "button"; save.className = "primary account-save-button"; save.dataset.accountSave = account.id; save.disabled = actionRunning || !hasUnsavedSeckillAccount(account.id); save.title = save.disabled ? "当前账户没有未保存修改" : "保存当前账户修改";
    save.addEventListener("click", () => saveSeckillAccount(account, save));
    actions.append(healthLabel, save, check, clearCache, remove);
    const feedback = accountSaveFeedback.get(account.id);
    if (feedback) {
      const status = text("p", feedback.message, "seckill-account-save-status " + feedback.kind);
      status.setAttribute("role", "status");
      card.append(status);
    }
    accountTarget.append(card);
  });
  renderVpsSeckillTask();
}

async function saveSeckillAccount(account, button) {
  if (!account || !hasUnsavedSeckillAccount(account.id)) return;
  await runAction(button, "保存中…", "正在保存当前网站账户…", async () => {
    try {
      invalidateAccountCheck(account.id);
      const result = await saveConfig("seckill-account:" + account.id);
      const name = account.name || account.username || account.id;
      const ok = result.service?.ok;
      const message = ok ? name + " 已保存，可点击“检查账户”验证登录。" : name + " 已保存，但服务应用失败：" + (result.service?.error || "请在设置中重试");
      accountSaveFeedback.set(account.id, { message, kind: ok ? "success" : "error" });
      renderVpsSeckill();
      setMessage(message, ok ? "success" : "error");
    } catch (error) {
      accountSaveFeedback.set(account.id, { message: "保存失败：" + error.message, kind: "error" });
      renderVpsSeckill();
      throw error;
    }
  });
}

function clearAccountSaveFeedback(accountId) {
  invalidateAccountCheck(accountId);
  accountSaveFeedback.delete(accountId);
  document.querySelectorAll(".seckill-account").forEach(card => {
    if (card.dataset.accountId === accountId) card.querySelector(".seckill-account-save-status")?.remove();
  });
}

function deleteSeckillAccounts(ids) {
  const deleted = new Set(ids);
  config.vps_seckill.accounts = seckillAccounts().filter(account => !deleted.has(account.id));
  seckillItems().forEach(item => { if (deleted.has(item.account_id)) item.account_id = ""; });
  renderVpsSeckill(); updateDraftState();
  scheduleAutoSave("seckill");
  setMessage("已删除补货抢购账户并解除对应商品绑定，正在自动保存。", "pending");
}
function collectVpsSeckill() {
  const items = seckillItems().map(({ submit_url, auto_submit, ...item }) => item);
  const accounts = seckillAccounts().map(({ auto_submit, session_cookie, session_user_agent, captcha_token, captcha_provider, ...account }) => account);
  return { enabled: items.some(item => item.enabled !== false), mode: document.querySelector("#vps-seckill-mode")?.value || "manual", interval_seconds: Number(document.querySelector("#vps-seckill-interval")?.value || 5), items, accounts };
}
function savedConfigSnapshot() {
  if (!savedConfig) return null;
  try { return JSON.parse(savedConfig); } catch { return null; }
}
function comparableSeckillAccount(account) {
  if (!account) return null;
  // The API intentionally never returns the saved password. An empty password
  // therefore means “keep the saved one”; compare that state by its configured
  // flag while still detecting a newly typed password.
  return {
    id: String(account.id || ""),
    name: String(account.name || ""),
    provider: String(account.provider || ""),
    username: String(account.username || ""),
    login_url: String(account.login_url || ""),
    egress_node_id: String(account.egress_node_id || ""),
    password: String(account.password || ""),
    password_configured: Boolean(account.password_configured || account.password),
  };
}
function hasUnsavedSeckillAccount(accountId) {
  const saved = savedConfigSnapshot();
  const current = seckillAccounts().find(account => account.id === accountId);
  const previous = saved?.vps_seckill?.accounts?.find(account => account.id === accountId);
  if (!saved || !current || !previous) return true;
  return JSON.stringify(comparableSeckillAccount(current)) !== JSON.stringify(comparableSeckillAccount(previous));
}
function updateSeckillAccountControls(accountId) {
  const card = document.querySelector(`.seckill-account[data-account-id="${CSS.escape(accountId)}"]`);
  if (!card) return;
  const dirty = hasUnsavedSeckillAccount(accountId);
  const save = [...card.querySelectorAll("button")].find(button => button.dataset.accountSave === accountId);
  if (save) { save.disabled = actionRunning || !dirty; save.textContent = "保存账户"; save.title = dirty ? "保存当前账户修改" : "当前账户没有未保存修改"; }
  const check = card.querySelector(".seckill-check-account"), clear = [...card.querySelectorAll("button")].find(button => button.textContent === "清除登录缓存");
  if (check) check.disabled = actionRunning || dirty;
  if (clear) clear.disabled = actionRunning || dirty;
}
function comparableSeckillItem(item) {
  if (!item) return null;
  return {
    id: String(item.id || ""),
    product_id: String(item.product_id || ""),
    coupon_code: String(item.coupon_code || ""),
    operating_system: String(item.operating_system || ""),
    billing_cycle: String(item.billing_cycle || ""),
    payment_method: String(item.payment_method || ""),
    target_url: String(item.target_url || ""),
    account_id: String(item.account_id || ""),
    egress_node_id: String(item.egress_node_id || ""),
    quantity: Number(item.quantity || 0),
    enabled: item.enabled !== false,
    label: String(item.label || ""),
  };
}
function hasUnsavedSeckillItem(itemId) {
  const current = seckillItems().find(item => item.id === itemId);
  const saved = savedConfigSnapshot();
  const previous = saved?.vps_seckill?.items?.find(item => item.id === itemId);
  if (!saved || !current || !previous || hasUnsavedSeckillAccount(current.account_id)) return true;
  return JSON.stringify(comparableSeckillItem(current)) !== JSON.stringify(comparableSeckillItem(previous));
}
function renderVpsSeckillTask() {
  document.querySelectorAll("#page-seckill .option-button, .seckill-item .danger, .seckill-account .danger, .seckill-account [data-account-save], #egress-nodes .egress-node-head button, #egress-nodes .egress-install-copy, #egress-nodes .egress-uninstall-copy").forEach(button => { button.disabled = actionRunning; });
  document.querySelectorAll(".seckill-check-account").forEach(button => { const itemCard = button.closest(".seckill-item"); const item = itemCard ? seckillItems().find(row => row.id === itemCard.dataset.id) : null; button.disabled = actionRunning || (itemCard ? !item?.account_id : !button.dataset.accountId); });
  seckillAccounts().forEach(account => updateSeckillAccountControls(account.id));
  document.querySelectorAll(".seckill-account-status").forEach(node => { const itemCard = node.closest(".seckill-item"); if (itemCard) { const item = seckillItems().find(row => row.id === itemCard.dataset.id); if (item) node.textContent = seckillAccountNote(item); } else { const account = seckillAccounts().find(row => row.id === node.closest(".seckill-account")?.dataset.accountId); if (account) node.textContent = standaloneAccountNote(account); } });
  document.querySelector("#vps-seckill-delete-all").disabled = actionRunning || !seckillItems().length;
  document.querySelector("#vps-seckill-delete-all-accounts").disabled = actionRunning || !seckillAccounts().length;
  const state = document.querySelector("#seckill-task-state"), start = document.querySelector("#vps-seckill-start"), stop = document.querySelector("#vps-seckill-stop");
  const running = vpsSeckillTask && vpsSeckillTask.pending !== false; state.textContent = running ? "执行中" : vpsSeckillTask ? (vpsSeckillTask.cancelled ? "已暂停" : "已完成") : "未运行"; state.className = `state ${running ? "pending" : vpsSeckillTask?.cancelled ? "error" : vpsSeckillTask ? "ok" : ""}`; start.disabled = Boolean(running) || actionRunning || !seckillItems().some(item => item.enabled !== false); stop.hidden = !running; stop.disabled = !running;
  const results = document.querySelector("#vps-seckill-results"); results.replaceChildren();
  const labels = { submitted: "已创建待付款订单", ready: "请手动下单", uncertain: "结果待核对，请勿重复下单", blocked: "预检查未通过", failed_before_submit: "配置失败，未提交账单", cancelled: "已停止" };
  (vpsSeckillTask?.items || []).forEach(result => {
    const item = seckillItems().find(row => row.id === result.item_id);
    const row = text("div", "", "seckill-result");
    row.append(text("strong", `${item?.label || result.item_id} · ${labels[result.status] || result.status}${result.quantity ? ` · ${result.quantity} 台` : ""}`));
    if (result.order_id) row.append(text("p", `订单号：${result.order_id}。请登录官网手动支付。`, "hint"));
    try { const url = new URL(result.url); if (url.protocol === "https:" && !url.username && !url.password) { const link = text("a", "前往官网核对 / 手动支付"); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; row.append(link); } } catch {}
    results.append(row);
  });
  const logs = vpsSeckillTask?.logs || []; document.querySelector("#vps-seckill-logs").textContent = logs.length ? logs.map(log => `[${formatBeijingDate(log.at)}] ${log.message}`).join("\n") : "暂无日志"; document.querySelector("#vps-seckill-log-note").textContent = vpsSeckillTask ? `${vpsSeckillTask.mode || ""} · ${logs.length} 条日志` : "尚未启动补货抢购任务。"; renderOverview();
}
function renderVpsSeckillTest(result) {
  const status = document.querySelector("#vps-seckill-test-status"), target = document.querySelector("#vps-seckill-test-results");
  if (!status || !target || !result) return;
  const stop = document.querySelector("#vps-seckill-test-stop"), start = document.querySelector("#vps-seckill-test");
  const pending = result.pending !== false;
  status.textContent = pending ? `正在测试抢购配置${result.phase === "checking" ? " · 检查商品中" : "…"}` : `${result.cancelled ? "抢购配置测试已停止" : result.ok ? "抢购配置测试通过" : "抢购配置测试发现问题"} · ${result.checked_at ? formatBeijingDate(result.checked_at) : ""}`;
  if (start) start.disabled = pending || actionRunning;
  if (stop) { stop.hidden = !pending; stop.disabled = !pending; }
  target.replaceChildren(...(result.items || []).map(item => {
    const row = text("article", "", `seckill-result ${item.ok ? "success" : "error"}`);
    row.append(text("strong", item.label || item.item_id));
    row.append(text("p", `${item.site_access?.ok ? "✓" : "✗"} 网站访问：${item.site_access?.message || "未检查"}`, "hint"));
    row.append(text("p", `${item.page_recognition?.ok ? "✓" : "✗"} 页面识别：${item.page_recognition?.message || "未检查"}`, "hint"));
    const submission = item.order_submission || { ok: !item.required_account, message: item.required_account ? "尚未检查" : "手动模式只准备购买链接，不创建订单" };
    row.append(text("p", `${submission.ok ? "✓" : "✗"} 下单准备：${submission.message || "未检查"}`, submission.ok ? "hint" : "error"));
    const account = item.account || {};
    row.append(text("p", `${account.required ? (account.ok ? "✓" : "✗") : "－"} 账户状态：${account.message || "未绑定账户（手动模式可用）"}`, "hint"));
    return row;
  }));
}
window.addEventListener("hostwatch-action-change", renderVpsSeckillTask);
async function pollVpsSeckill(taskId) { clearInterval(vpsSeckillPoll); vpsSeckillPoll = setInterval(async () => { try { vpsSeckillTask = await requestJSON(`/api/seckill/status/${encodeURIComponent(taskId)}`); renderVpsSeckillTask(); if (!vpsSeckillTask.pending) clearInterval(vpsSeckillPoll); } catch { clearInterval(vpsSeckillPoll); } }, 1000); }
document.querySelector("#vps-seckill-start").addEventListener("click", async () => { try { const taskId = `seckill-${Date.now()}-${Math.random().toString(36).slice(2, 18)}`; vpsSeckillTask = await requestJSON("/api/seckill/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ config: collectConfig(), task_id: taskId, mode: document.querySelector("#vps-seckill-mode").value }) }); renderVpsSeckillTask(); pollVpsSeckill(taskId); setMessage("VPS 补货抢购任务已启动，使用当前页面配置（未保存的配置仅用于本次任务）。", "success"); } catch (error) { setMessage(error.message, "error"); } });
document.querySelector("#vps-seckill-test").addEventListener("click", async event => {
  if (vpsSeckillTestTask?.pending) return;
  const taskId = `seckill-test-${Date.now()}-${Math.random().toString(36).slice(2, 18)}`;
  try {
    vpsSeckillTestTask = await requestJSON("/api/seckill/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ config: collectConfig(), task_id: taskId, mode: document.querySelector("#vps-seckill-mode").value }), timeoutMs: 15000 });
    renderVpsSeckillTest(vpsSeckillTestTask);
    clearInterval(vpsSeckillTestPoll);
    vpsSeckillTestPoll = setInterval(async () => {
      try {
        vpsSeckillTestTask = await requestJSON(`/api/seckill/test/status/${encodeURIComponent(taskId)}`, { timeoutMs: 10000 });
        renderVpsSeckillTest(vpsSeckillTestTask);
        if (!vpsSeckillTestTask.pending) {
          clearInterval(vpsSeckillTestPoll);
          setMessage(vpsSeckillTestTask.cancelled ? "抢购配置测试已停止。" : vpsSeckillTestTask.ok ? "VPS 抢购配置测试通过（使用当前页面配置，未保存的内容不会写入服务器配置）。" : "VPS 抢购配置测试发现问题，请查看测试结果。", vpsSeckillTestTask.cancelled || vpsSeckillTestTask.ok ? "success" : "error");
        }
      } catch (error) { clearInterval(vpsSeckillTestPoll); setMessage(error.message, "error"); }
    }, 500);
  } catch (error) { setMessage(error.message, "error"); }
});
document.querySelector("#vps-seckill-test-stop").addEventListener("click", async () => {
  if (!vpsSeckillTestTask?.task_id) return;
  try {
    vpsSeckillTestTask = await requestJSON("/api/seckill/test/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task_id: vpsSeckillTestTask.task_id }), timeoutMs: 10000 });
    renderVpsSeckillTest(vpsSeckillTestTask);
  } catch (error) { setMessage(error.message, "error"); }
});
document.querySelector("#vps-seckill-stop").addEventListener("click", async () => { if (!vpsSeckillTask?.task_id) return; try { vpsSeckillTask = await requestJSON("/api/seckill/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task_id: vpsSeckillTask.task_id }) }); renderVpsSeckillTask(); } catch (error) { setMessage(error.message, "error"); } });
function openSeckillPicker(type) {
  const dialog = document.querySelector("#seckill-picker-dialog"), content = document.querySelector("#seckill-picker-content");
  const rows = config.products.filter(item => membership(item, type));
  content.replaceChildren();
  if (!rows.length) content.append(text("p", `暂无${type === "watch" ? "库存监控" : "库存通知"}商品，请先添加商品。`, "empty"));
  const selected = new Set();
  rows.forEach(item => {
    const label = document.createElement("label"); label.className = "picker-row";
    const input = document.createElement("input"); input.type = "checkbox"; input.value = item.id; input.disabled = seckillItems().some(row => row.product_id === item.id); input.addEventListener("change", () => input.checked ? selected.add(item.id) : selected.delete(item.id));
    label.append(input, text("span", `${item.name} · ${item.provider}${input.disabled ? "（已添加）" : ""}`)); content.append(label);
  });
  const actions = text("div", "", "actions"); const add = text("button", "添加所选", "primary"); add.type = "button"; add.disabled = !rows.some(item => selected.has(item.id));
  content.addEventListener("change", () => { add.disabled = !selected.size; }, { once: false });
  add.addEventListener("click", () => { rows.filter(item => selected.has(item.id)).forEach(addToVpsSeckill); dialog.close(); renderVpsSeckill(); }); actions.append(add); content.append(actions);
  dialog.showModal();
}
document.querySelector("#vps-seckill-open-monitor").addEventListener("click", () => openSeckillPicker("watch"));
document.querySelector("#vps-seckill-open-notifications").addEventListener("click", () => openSeckillPicker("notify"));
document.querySelector("#seckill-picker-close").addEventListener("click", () => document.querySelector("#seckill-picker-dialog").close());
document.querySelector("#vps-seckill-delete-all").addEventListener("click", () => { seckillItems().splice(0); renderVpsSeckill(); updateDraftState(); scheduleAutoSave("seckill"); setMessage("已删除全部补货抢购商品，正在自动保存。", "pending"); });
document.querySelector("#vps-seckill-delete-all-accounts").addEventListener("click", () => deleteSeckillAccounts(seckillAccounts().map(account => account.id)));
document.querySelector("#vps-seckill-add-account").addEventListener("click", () => { seckillAccounts().push({ id: `account-${Date.now()}`, name: "", provider: "", username: "", password: "", login_url: "" }); renderVpsSeckill(); updateDraftState(); setMessage("已添加账户草稿，请填写后点击该账户的“保存账户”。", "pending"); });
document.querySelector("#vps-seckill-clear-log").addEventListener("click", () => { if (vpsSeckillTask) { vpsSeckillTask.logs = []; renderVpsSeckillTask(); } });
document.querySelector("#vps-seckill-mode").addEventListener("change", () => updateProviderSchedule("seckill-settings"));
document.querySelector("#vps-seckill-interval").addEventListener("input", () => updateProviderSchedule("seckill-settings"));

function renderMonitorState() {
  const active = monitorService?.active;
  const transitionTimedOut = monitorService?.transition_timeout;
  const labels = { active: "HostWatch 服务运行中", inactive: "HostWatch 服务已停止", failed: "HostWatch 服务运行失败", activating: transitionTimedOut ? "HostWatch 服务启动超时" : "HostWatch 服务启动中", deactivating: transitionTimedOut ? "HostWatch 服务停止超时" : "HostWatch 服务停止中" };
  const label = labels[active] || "自动刷新服务状态未知";
  const schedules = config.provider_schedules || {};
  const automaticModules = [
    Object.values(schedules.monitor || {}).some(row => row.mode === "auto") ? "库存监控" : "",
    Object.values(schedules.notify || {}).some(row => row.mode === "auto") ? "库存通知" : "",
  ].filter(Boolean);
  let note = automaticModules.length ? `已保存：${automaticModules.join("、")}开启自动刷新。` : "已保存：库存监控和库存通知均为仅手动刷新。";
  if (!savedConfig) note = "正在读取配置。";
  if (!monitorService) note += "请刷新状态核对服务器实际运行情况。";
  else if (active === "active") note += "服务已启动；自动任务按库存监控和库存通知各自设置执行，手动模式不会停止服务。";
  else if (active === "activating" && transitionTimedOut) note += "超过 60 秒仍未确认启动；请刷新状态并查看服务日志。";
  else if (active === "activating") note += "服务尚未确认启动成功，正在自动刷新；超过 60 秒会提示启动超时。";
  else if (active === "deactivating" && transitionTimedOut) note += "超过 60 秒仍未确认停止；请刷新状态并查看服务日志。";
  else if (active === "deactivating") note += "服务正在停止，正在自动刷新最终状态。";
  else if (automaticModules.length) note += "自动任务尚未运行，请查看最近检查错误及服务日志。";
  else note += "当前没有启用自动刷新；HostWatch 服务仍保持运行，可在对应栏目手动刷新。";
  const runtime = document.querySelector("#monitor-runtime");
  const tone = serviceTone(active);
  runtime.textContent = label;
  runtime.className = `state ${tone}`;
  document.querySelector("#monitor-runtime-note").textContent = note;
  const notifyRuntime = document.querySelector("#notify-runtime");
  if (notifyRuntime) {
    notifyRuntime.textContent = label;
    notifyRuntime.className = `state ${tone}`;
    document.querySelector("#notify-runtime-note").textContent = note;
  }
  const overview = document.querySelector("#overview-state");
  if (overview) { overview.textContent = label; overview.className = `overview-value state ${tone}`; }
  const header = document.querySelector("#header-monitor-state");
  const hostActive = hostwatchService?.active;
  const hostLabels = { active: "HostWatch 服务运行中", inactive: "HostWatch 服务已停止", failed: "HostWatch 服务运行失败", activating: "HostWatch 服务启动中", deactivating: "HostWatch 服务停止中" };
  header.textContent = hostLabels[hostActive] || "HostWatch 服务状态未知";
  header.className = `state ${serviceTone(hostActive)}`;
  const overviewNote = document.querySelector("#overview-state-note");
  if (overviewNote) overviewNote.textContent = active === "active" ? "服务持续运行，自动任务按各模块设置执行" : active === "activating" && transitionTimedOut ? "启动超过 60 秒未完成，请查看服务日志" : active === "activating" ? "正在确认启动状态，页面自动刷新" : active === "failed" ? "请检查配置或查看服务日志" : active === "deactivating" && transitionTimedOut ? "停止超过 60 秒未完成，请查看服务日志" : active === "deactivating" ? "正在停止，等待最终状态" : active === "inactive" ? "服务已停止，请检查服务日志" : "等待服务器返回服务状态";
  window.dispatchEvent(new Event("hostwatch-service-change"));
}

function collectConfig(enabled = config.enabled) {
  return {
    enabled,
    interval_seconds: Number(document.querySelector("#interval").value || NaN),
    monitor_schedule: { mode: document.querySelector("#monitor-schedule-mode").value, interval_seconds: Number(document.querySelector("#interval").value || NaN) },
    notify_schedule: { mode: document.querySelector("#notify-schedule-mode").value, interval_seconds: Number(document.querySelector("#notify-interval").value || NaN) },
    catalog_schedule: config.catalog_schedule || { mode: "manual", interval_seconds: 300 },
    provider_schedules: cloneConfig(config.provider_schedules || {}),
    // VPS 抢购由页面上的启动/暂停按钮独立控制；执行模式（极速/精准/手动）
    // 只决定任务内行为，不把一次性任务误报成后台自动刷新。
    seckill_schedule: { mode: "manual", interval_seconds: Number(document.querySelector("#vps-seckill-interval")?.value || 5) },
    affiliate_id: config.affiliate_id || "",
    affiliates: collectAffiliates(),
    affiliate_notes: collectAffiliateNotes(),
    affiliate_accounts: collectAffiliateAccounts(),
    affiliate_rates: collectAffiliateRates(),
    affiliate_coupons: collectAffiliateCoupons(),
    merchant_order: merchantOrder(),
    watch_order: config.watch_order || [],
    notify_order: config.notify_order || [],
    notify_current_stock_on_start: optionPressed("notify-current"),
    isvoro_seckill: optionPressed("isvoro-seckill"),
    fetcher: { provider_modes: collectProviderModes(), provider_flaresolverr: Object.fromEntries([...providerFlareDraft].map(([provider, options]) => [provider, { ...options }])), mode: document.querySelector("#fetcher-mode").value, flaresolverr_url: document.querySelector("#flaresolverr-url").value.trim(), timeout_seconds: Number(document.querySelector("#flaresolverr-timeout").value), wait_seconds: Number(document.querySelector("#fetcher-wait").value), disable_media: optionPressed("flaresolverr-light"), cloudscraper: { connect_timeout_seconds: Number(document.querySelector("#cloudscraper-connect-timeout").value), read_timeout_seconds: Number(document.querySelector("#cloudscraper-read-timeout").value), challenge_delay_seconds: Number(document.querySelector("#cloudscraper-challenge-delay").value) } },
    telegram: { bot_token: document.querySelector("#telegram-token").value.trim(), chat_id: document.querySelector("#telegram-chat-id").value.trim(), disabled: optionPressed("telegram-disabled"), clear: optionPressed("telegram-clear") },
    egress: collectEgress(),
    vps_seckill: collectVpsSeckill(),
    akile_market: activeMarketProvider() === "akile" ? collectMarket() : config.akile_market,
    isvoro_market: activeMarketProvider() === "isvoro" ? collectMarket() : config.isvoro_market,
    products: config.products.map((item) => ({ ...item })),
  };
}

function hasUnsavedChanges() {
  return savedConfig !== null && JSON.stringify(collectConfig()) !== savedConfig;
}

function cloneConfig(value) {
  return JSON.parse(JSON.stringify(value));
}

function mergeProductMemberships(baseProducts, draftProducts, type) {
  const baseRows = Array.isArray(baseProducts) ? baseProducts : [];
  const draftRows = Array.isArray(draftProducts) ? draftProducts : [];
  const baseById = new Map(baseRows.map(item => [String(item.id), item]));
  const enabledKey = `${type}_enabled`;
  const otherType = type === "watch" ? "notify" : "watch";
  const draftIds = new Set(draftRows.map(item => String(item.id || "")));
  return draftRows.reduce((rows, draftItem) => {
    const id = String(draftItem.id || "");
    const savedItem = baseById.get(id);
    if (!savedItem) {
      // A newly selected item belongs to this card only when this card's
      // membership is enabled; leave notification-only drafts for the
      // notification card and vice versa.
      if (membership(draftItem, type)) {
        const item = cloneConfig(draftItem);
        item[enabledKey] = true;
        item[`${otherType}_enabled`] = false;
        item.enabled = true;
        rows.push(item);
      }
      return rows;
    }
    const item = cloneConfig(savedItem);
    item[enabledKey] = membership(draftItem, type);
    item.enabled = item[enabledKey] || membership(savedItem, otherType);
    if (Object.hasOwn(draftItem, "snapshot")) item.snapshot = cloneConfig(draftItem.snapshot);
    if (type === "watch") {
      for (const key of ["remarks", "line_label", "line_telecom", "line_unicom", "line_mobile"]) {
        if (Object.hasOwn(draftItem, key)) item[key] = draftItem[key];
      }
    }
    rows.push(item);
    return rows;
  }, baseRows.filter(item => !draftIds.has(String(item.id))).map(cloneConfig));
}

function mergeConfigScope(baseValue, draftValue, scope) {
  const base = cloneConfig(baseValue || draftValue);
  const draft = cloneConfig(draftValue);
  if (!scope || scope === "all") return draft;
  if (scope === "affiliates") {
    for (const key of ["affiliate_id", "affiliates", "affiliate_notes", "affiliate_accounts", "affiliate_rates", "affiliate_coupons"]) base[key] = draft[key];
  } else if (scope === "catalog") { base.catalog_schedule = draft.catalog_schedule; base.provider_schedules = { ...base.provider_schedules, catalog: draft.provider_schedules.catalog }; }
  else if (scope === "fetcher") {
    const keys = ["mode", "flaresolverr_url", "timeout_seconds", "wait_seconds", "disable_media", "provider_flaresolverr"];
    base.fetcher = { ...base.fetcher, ...Object.fromEntries(keys.map(key => [key, draft.fetcher[key]])) };
  } else if (scope === "cloudscraper") base.fetcher = { ...base.fetcher, cloudscraper: draft.fetcher.cloudscraper };
  else if (scope === "provider-modes") base.fetcher = { ...base.fetcher, provider_modes: draft.fetcher.provider_modes };
  else if (scope === "egress") { base.egress = draft.egress; base.fetcher = { ...base.fetcher, egress: draft.egress }; }
  else if (scope.startsWith("seckill-account:")) {
    const accountId = scope.slice("seckill-account:".length);
    const draftAccount = draft.vps_seckill?.accounts?.find(account => account.id === accountId);
    if (draftAccount) {
      const accounts = base.vps_seckill?.accounts || [];
      base.vps_seckill = { ...base.vps_seckill, accounts: [...accounts.filter(account => account.id !== accountId), cloneConfig(draftAccount)] };
    }
  } else if (scope === "market") { base.akile_market = draft.akile_market; base.isvoro_market = draft.isvoro_market; }
  else if (scope === "telegram") base.telegram = draft.telegram;
  else if (scope === "monitor") {
    for (const key of ["interval_seconds", "monitor_schedule", "watch_order", "merchant_order"]) base[key] = draft[key];
    base.provider_schedules = { ...base.provider_schedules, monitor: draft.provider_schedules.monitor };
    base.products = mergeProductMemberships(base.products, draft.products, "watch");
  } else if (scope === "notifications") {
    for (const key of ["notify_schedule", "notify_current_stock_on_start", "isvoro_seckill", "notify_order"]) base[key] = draft[key];
    base.provider_schedules = { ...base.provider_schedules, notify: draft.provider_schedules.notify };
    base.products = mergeProductMemberships(base.products, draft.products, "notify");
  } else if (scope === "seckill") {
    const savedAccounts = base.vps_seckill?.accounts || [];
    const draftAccountIds = new Set((draft.vps_seckill?.accounts || []).map(account => account.id));
    // Product auto-save may not persist account field drafts. Deletions remain
    // effective; account edits and new accounts require their own save button.
    base.vps_seckill = { ...draft.vps_seckill, accounts: savedAccounts.filter(account => draftAccountIds.has(account.id)) };
  }
  else if (scope === "seckill-settings") {
    base.vps_seckill = { ...base.vps_seckill, mode: draft.vps_seckill.mode, interval_seconds: draft.vps_seckill.interval_seconds };
    base.provider_schedules = { ...base.provider_schedules, seckill: draft.provider_schedules.seckill };
  }
  else if (scope.startsWith("product:")) {
    const id = scope.slice("product:".length), item = draft.products.find(row => row.id === id);
    const index = base.products.findIndex(row => row.id === id);
    if (index >= 0) {
      if (item) base.products[index] = item;
      else base.products.splice(index, 1);
    } else if (item) base.products.push(item);
  } else if (scope.startsWith("seckill-item:") || scope.startsWith("seckill-account:")) {
    const [kind, id] = scope.split(":", 2), key = kind === "seckill-item" ? "items" : "accounts";
    const draftRows = draft.vps_seckill[key] || [], baseRows = base.vps_seckill[key] || [];
    const row = draftRows.find(entry => entry.id === id);
    const index = baseRows.findIndex(entry => entry.id === id);
    if (index >= 0) {
      if (row) baseRows[index] = row;
      else baseRows.splice(index, 1);
    } else if (row) baseRows.push(row);
    base.vps_seckill = { ...base.vps_seckill, [key]: baseRows };
  }
  return base;
}

function scopeIsDirty(scope) {
  if (!savedConfig) return false;
  const saved = savedConfigSnapshot();
  return JSON.stringify(mergeConfigScope(saved, collectConfig(), scope)) !== JSON.stringify(saved);
}

function updateCardSaveStates() {
  document.querySelectorAll("[data-save-card]").forEach(button => {
    const dirty = scopeIsDirty(button.dataset.saveCard);
    button.disabled = actionRunning;
    button.classList.toggle("is-dirty", dirty);
    button.setAttribute("aria-label", dirty ? `${button.textContent.trim()}（有未保存修改）` : button.textContent.trim());
  });
}

function intervalLabel(seconds) {
  if (!Number.isInteger(seconds) || seconds < 0) return "未设置";
  if (seconds === 0) return "连续检查";
  return seconds % 60 === 0 ? `${seconds / 60} 分钟` : `${seconds} 秒`;
}

function updateDraftState() {
  if (savedConfig === null) return;
  syncProviderFlareSettings();
  const dirty = hasUnsavedChanges();
  const node = document.querySelector("#monitor-draft");
  if (node) {
    const monitorDirty = scopeIsDirty("monitor");
    node.textContent = monitorDirty ? "监控设置有修改，正在自动保存；单次测试可直接使用当前填写的商品配置。" : "监控设置已保存。";
    node.className = monitorDirty ? "hint draft-note" : "hint";
  }
  const notifyNode = document.querySelector("#notify-draft");
  if (notifyNode) {
    const notifyDirty = scopeIsDirty("notifications");
    notifyNode.textContent = notifyDirty ? "通知设置有修改，正在自动保存。" : "通知设置已保存。";
    notifyNode.className = notifyDirty ? "hint draft-note" : "hint";
  }
  const seconds = Number(document.querySelector("#interval").value || NaN);
  const valid = Number.isInteger(seconds) && seconds >= 0 && seconds <= 86400;
  const description = document.querySelector("#interval-description");
  if (description) description.textContent = valid ? (seconds === 0 ? "本轮检查完成后立即检查下一轮，不额外等待；各轮顺序执行。修改后自动保存。" : `每轮检查完成后等待 ${intervalLabel(seconds)}，再检查下一轮；修改后自动保存。`) : "检查间隔必须是 0–86400 秒的整数。";
  updateCardSaveStates();
  window.dispatchEvent(new Event("hostwatch-draft-change"));
}

const autoSaveScopeNames = { catalog: "大盘设置", monitor: "监控设置", notifications: "通知设置", seckill: "抢购设置", "seckill-settings": "抢购执行设置", fetcher: "全局请求方式", "provider-modes": "商家请求方式", cloudscraper: "cloudscraper 参数", egress: "出口节点" };
// 时间输入需要给用户留出连续输入多位数字的时间，避免输入首位数字时就触发保存。
const AUTOSAVE_DELAY_MS = 350;
const INTERVAL_AUTOSAVE_DELAY_MS = 1000;
const AUTOSAVE_RETRY_DELAY_MS = 300;
let automaticSaveRunning = false;
const automaticSavePending = new Set();
function scheduleAutoSave(scope, delay = AUTOSAVE_DELAY_MS) {
  if (!savedConfig || !scope) return;
  clearTimeout(autoSaveTimers.get(scope));
  const timer = setTimeout(() => {
    autoSaveTimers.delete(scope);
    if (!savedConfig || !scopeIsDirty(scope)) return;
    if (scope === "monitor") {
      const seconds = Number(document.querySelector("#interval")?.value || NaN);
      if (!Number.isInteger(seconds) || seconds < 0 || seconds > 86400) return;
    }
    if (scope === "notifications") {
      const seconds = Number(document.querySelector("#notify-interval")?.value || NaN);
      if (!Number.isInteger(seconds) || seconds < 0 || seconds > 86400) return;
    }
    if (actionRunning) { scheduleAutoSave(scope, AUTOSAVE_RETRY_DELAY_MS); return; }
    if (automaticSaveRunning) { automaticSavePending.add(scope); return; }
    runAutomaticSave(scope);
  }, delay);
  autoSaveTimers.set(scope, timer);
}

async function runAutomaticSave(scope) {
  if (automaticSaveRunning) { automaticSavePending.add(scope); return; }
  automaticSaveRunning = true;
  setMessage(`正在自动保存${autoSaveScopeNames[scope] || "配置"}…`, "pending", true, false);
  try { await saveConfig(scope, true); }
  catch (error) { setMessage(error.message, "error", true, false); }
  finally {
    automaticSaveRunning = false;
    const pending = [...automaticSavePending];
    automaticSavePending.clear();
    pending.forEach(item => scheduleAutoSave(item, 0));
  }
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
function flareDefaults() {
  return { timeout_seconds: Number(document.querySelector("#flaresolverr-timeout").value), wait_seconds: Number(document.querySelector("#fetcher-wait").value), disable_media: optionPressed("flaresolverr-light") };
}
function syncProviderFlareSettings() {
  const modes = collectProviderModes(), defaults = flareDefaults();
  for (const card of document.querySelectorAll("[data-provider-flare]")) {
    card.querySelectorAll("input, button").forEach(control => { control.disabled = actionRunning; });
    const provider = card.dataset.providerFlare, options = providerFlareDraft.get(provider) || defaults;
    let mode = modes[provider];
    if (mode === "inherit") mode = document.querySelector("#fetcher-mode").value;
    const method = { requests: "普通 HTTPS", cloudscraper: "cloudscraper", flaresolverr: "FlareSolverr", legacy: "已有商品的请求方式" }[mode] || "全局请求方式";
    card.querySelector(".provider-flare-status").textContent = `${providerFlareDraft.has(provider) ? "独立参数" : "跟随默认"}：超时 ${options.timeout_seconds} 秒 · 渲染等待 ${options.wait_seconds} 秒 · ${options.disable_media ? "轻量加载" : "完整加载"}。${mode === "flaresolverr" ? "当前商家使用 FlareSolverr。" : `当前使用${method}，上述参数在选择 FlareSolverr 后生效。`}`;
  }
}
function renderProviderFlareSettings() {
  const target = document.querySelector("#provider-flaresolverr-settings");
  target.replaceChildren(...orderedMerchantEntries().map(([provider, label]) => {
    const card = text("article", "", "provider-flare-card"); card.dataset.providerFlare = provider;
    const head = text("div", "", "provider-flare-head");
    const independent = providerFlareDraft.has(provider);
    const toggle = settingButton(`${label} 参数来源`, independent, enabled => {
      if (enabled) providerFlareDraft.set(provider, flareDefaults());
      else providerFlareDraft.delete(provider);
      renderProviderFlareSettings();
      scheduleAutoSave("fetcher");
      target.querySelector(`[data-provider-flare="${provider}"] .provider-flare-source`).focus({ preventScroll: true });
    }, ["跟随默认", "独立设置"]);
    toggle.classList.add("provider-flare-source");
    head.append(text("strong", label), toggle); card.append(head, text("p", "", "hint provider-flare-status"));
    if (independent) {
      const options = providerFlareDraft.get(provider), grid = text("div", "", "form-grid");
      for (const [key, name, min, max] of [["timeout_seconds", "请求超时（秒）", 10, 180], ["wait_seconds", "渲染等待（秒）", 0, 30]]) {
        const input = document.createElement("input"); input.type = "number"; input.min = min; input.max = max; input.step = "1"; input.value = options[key]; input.dataset.flareOption = key; input.disabled = actionRunning;
        input.setAttribute("aria-label", `${label} ${name}`);
        input.addEventListener("input", () => { options[key] = input.value === "" ? NaN : Number(input.value); updateDraftState(); scheduleAutoSave("fetcher"); });
        const field = text("label", name); field.append(input); grid.append(field);
      }
      grid.append(createFlareModeButtons(label, options.disable_media, enabled => { options.disable_media = enabled; scheduleAutoSave("fetcher"); }));
      card.append(grid);
    }
    return card;
  }));
  syncProviderFlareSettings();
}
function updateFetcherStatus() {
  const mode = document.querySelector("#fetcher-mode").value;
  const names = { requests: "普通 HTTPS 请求", flaresolverr: "FlareSolverr 浏览器请求", cloudscraper: "cloudscraper 请求" };
  document.querySelector("#fetcher-status").textContent = `全局默认使用 ${names[mode] || "未知请求方式"}；商家选择优先于全局设置，修改后自动应用。${mode === "cloudscraper" ? "cloudscraper 无需浏览器服务，不能完整执行网页 JavaScript，也不保证通过所有 Cloudflare 校验。" : ""}`;
  const legacy = [...document.querySelectorAll("[data-provider-mode]")].filter((input) => input.value === "legacy");
  document.querySelector("#provider-request-note").textContent = legacy.length ? "部分商家原有商品使用不同请求方式，目前保留。为该商家选择统一方式后，会自动覆盖这些旧选择。" : "新增商品自动使用对应商家设置，测试使用当前页面填写的请求方式；后台自动监控使用已保存的设置。";
}

function openAdvancedProducts() {
  navigatePage("products");
}

async function loadConfig() {
  applyConfig(await requestJSON("/api/config"));
}

function applyConfig(value) {
  config = value;
  config.provider_schedules ||= {};
  config.products.forEach(item => { setMembership(item, "watch", membership(item, "watch")); });
  document.querySelector("#interval").value = config.interval_seconds ?? 600;
  const monitorSchedule = config.monitor_schedule || { mode: "auto", interval_seconds: config.interval_seconds ?? 600 };
  const notifySchedule = config.notify_schedule || { mode: "auto", interval_seconds: config.interval_seconds ?? 600 };
  document.querySelector("#monitor-schedule-mode").value = monitorSchedule.mode || "auto";
  document.querySelector("#interval").value = monitorSchedule.interval_seconds ?? config.interval_seconds ?? 600;
  document.querySelector("#notify-schedule-mode").value = notifySchedule.mode || "auto";
  document.querySelector("#notify-interval").value = notifySchedule.interval_seconds ?? config.interval_seconds ?? 600;
  renderProviderSchedule("monitor");
  renderProviderSchedule("notifications");
  renderProviderSchedule("seckill-settings");
  for (const input of document.querySelectorAll("[data-affiliate]")) input.value = config.affiliates?.[input.dataset.affiliate] ?? (input.dataset.affiliate === "vmiss" ? config.affiliate_id || "" : "");
  for (const input of document.querySelectorAll("[data-affiliate-account]")) input.value = config.affiliate_accounts?.[input.dataset.affiliateAccount] ?? "";
  for (const input of document.querySelectorAll("[data-affiliate-rate]")) input.value = config.affiliate_rates?.[input.dataset.affiliateRate] ?? "";
  for (const input of document.querySelectorAll("[data-affiliate-coupon]")) input.value = config.affiliate_coupons?.[input.dataset.affiliateCoupon] ?? "";
  renderAffiliatePreviews(); renderMerchantOrder();
  setOptionPressed("notify-current", config.notify_current_stock_on_start);
  setOptionPressed("isvoro-seckill", config.isvoro_seckill);
  const fetcher = config.fetcher || {};
  document.querySelector("#fetcher-mode").value = fetcher.mode || "requests";
  document.querySelector("#flaresolverr-url").value = fetcher.flaresolverr_url || "http://127.0.0.1:8191/v1";
  document.querySelector("#flaresolverr-timeout").value = fetcher.timeout_seconds || 60;
  document.querySelector("#fetcher-wait").value = fetcher.wait_seconds || 0;
  setFlareModeButtons([document.querySelector("#flaresolverr-light"), document.querySelector("#flaresolverr-full")], Boolean(fetcher.disable_media));
  providerFlareDraft.clear();
  Object.entries(fetcher.provider_flaresolverr || {}).forEach(([provider, options]) => providerFlareDraft.set(provider, { ...options }));
  const cloud = fetcher.cloudscraper || {};
  document.querySelector("#cloudscraper-connect-timeout").value = cloud.connect_timeout_seconds ?? 10;
  document.querySelector("#cloudscraper-read-timeout").value = cloud.read_timeout_seconds ?? 25;
  document.querySelector("#cloudscraper-challenge-delay").value = cloud.challenge_delay_seconds ?? 5;
  applyProviderModes(fetcher);
  renderProviderFlareSettings();
  updateFetcherStatus();
  const telegram = config.telegram || {};
  document.querySelector("#telegram-token").value = "";
  document.querySelector("#telegram-chat-id").value = telegram.chat_id || "";
  setOptionPressed("telegram-disabled", telegram.disabled);
  setOptionPressed("telegram-clear", false);
  document.querySelector("#telegram-status").textContent = telegram.configured ? "Telegram 已配置（token 不会回显）。" : (telegram.disabled ? "Telegram 已停用。" : "Telegram 尚未完整配置。");
  document.querySelector("#telegram-chats").replaceChildren();
  renderProducts();
  renderEgress();
  scheduleEgressHealthCheck(1_000);
  renderVpsSeckill();
  if (["seckill", "accounts"].includes(currentPage)) scheduleAccountHealthCheck(1_000);
  renderMarketSettings();
  if (currentPage === "market") void loadMarketSnapshot();
  savedConfig = JSON.stringify(collectConfig());
  updateDraftState();
  renderMonitorState();
  renderExchangeRates();
}

async function loadStatus() {
  lastStatus = await requestJSON("/api/status");
  for (const [accountId, result] of Object.entries(lastStatus.account_checks || {})) {
    storeAccountCheck(accountHealthChecks, result);
    storeAccountCheck(standaloneAccountChecks, result);
  }
  const state = document.querySelector("#state");
  state.textContent = ({ partial: "部分库存待核对", ok: "最近库存检查成功", blocked: "最近库存检查被拦截", error: "最近库存检查失败", unknown: "尚无库存检查记录" })[lastStatus.state] || lastStatus.state || "尚无库存检查记录";
  state.className = `state ${lastStatus.state || ""}`;
  document.querySelector("#last-checked").textContent = lastStatus.last_checked ? `最近库存检查：${formatBeijingDate(lastStatus.last_checked)}` : "";
  blockedHelpNode.hidden = lastStatus.state !== "blocked";
  document.querySelector("#blocked-message").textContent = lastStatus.message || "检测到拒绝访问或安全验证。请确认厂商允许当前的自动检查方式后再恢复。";
  const errorNode = document.querySelector("#request-error");
  errorNode.hidden = !["error", "partial"].includes(lastStatus.state);
  errorNode.textContent = ["error", "partial"].includes(lastStatus.state) ? `最近库存检查提示：${lastStatus.message || "请查看服务日志"}` : "";
  if (["monitor", "notifications"].includes(currentPage)) renderProductList();
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
  navigatePage("products");
  editorNode.querySelector(`[data-id="${CSS.escape(items[0].id)}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  setMessage(items.length === 1 ? "商品已添加，填写内容后会自动保存。" : `已添加 ${items.length} 个商品，可先测试，填写内容后会自动保存。`, "pending");
  updateDraftState();
}
// Merchant defaults live in one registry so future presets share the same entry.
const merchantPresets = {
  html: { label: "其他商家 · 通用 HTTPS 网页", hint: "填写商品网址及库存规则，适用于其他公开商品网页。", product: { adapter: "html", provider: "自定义网站", name: "", url: "", stock_selector: "", stock_mode: "quantity", stock_pattern: "([\\d,]+)", available_pattern: "(?i)in stock|available|有货|现货", unavailable_pattern: "(?i)out of stock|sold out|缺货|售罄", purchase_url: "" } },
  vmiss: { label: "VMISS", hint: "填写分类页、商品选择器和分组 ID，使用 VMISS 专用解析。", product: { adapter: "vmiss", provider: "VMISS", name: "", category_url: "", selector: "", gid: "" } },
  vps_soy: { label: "VPS.SOY", hint: "默认深港IX-MINI（FID=1、GID=8），支持切换地区/线路分组。", product: { adapter: "vps_soy", provider: "VPS.SOY", name: "深港IX-MINI", url: "https://www.vps.soy/cart?fid=1&gid=8", selector: "深港IX-MINI", request_mode: "inherit" } },
  akile: { label: "Akile", hint: "默认添加中国台湾 TWLite-Mini，自动模式固定 Debian 12、1 个月并停在待付款订单。", product: { adapter: "akile", provider: "Akile", name: "TWLite-Mini", area_mode: "manual", area_id: "6", node_name: "TWLite", selector: "TWLite-Mini", request_mode: "inherit" } },
  narwhal: { label: "独角鲸云", hint: "从库存大盘选择实例及套餐，支持库存监控和 Telegram 补货通知。", product: { adapter: "narwhal", provider: "独角鲸云", name: "", machine_id: "", plan_id: "" } },
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
document.querySelector("#catalog-open-seckill").addEventListener("click", () => navigatePage("seckill"));
document.querySelector("#requests-open-seckill").addEventListener("click", () => navigatePage("seckill"));
document.querySelector("#notification-browse").addEventListener("click", () => navigatePage("catalog"));
document.querySelector("#watchlist-browse").addEventListener("click", () => navigatePage("catalog"));
for (const id of ["watchlist-search", "watchlist-provider", "watchlist-stock", "notification-search", "notification-provider", "notification-stock"]) document.getElementById(id).addEventListener(id.endsWith("search") ? "input" : "change", () => {
  renderProductList();
  animateFilterResult(id.startsWith("notification-") ? document.querySelector("#notification-list") : listNode);
});
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
for (const input of document.querySelectorAll("[data-provider-mode]")) input.addEventListener("change", () => { updateFetcherStatus(); scheduleAutoSave("provider-modes"); });
document.querySelector("#merchant-order-toggle").addEventListener("click", (event) => {
  if (actionRunning) return;
  merchantOrderEditing = !merchantOrderEditing;
  event.currentTarget.textContent = merchantOrderEditing ? "完成调整" : "调整商家顺序";
  event.currentTarget.classList.toggle("merchant-order-editing", merchantOrderEditing);
  event.currentTarget.setAttribute("aria-expanded", String(merchantOrderEditing));
  renderMerchantOrder();
  if (!merchantOrderEditing) { scheduleAutoSave("monitor"); setMessage("商家顺序编辑已完成，正在自动保存。", "pending", true, false); }
});
document.querySelector("#add").addEventListener("click", () => {
  const item = { ...structuredClone(merchantPresets[merchantSelect.value].product), id: crypto.randomUUID(), enabled: false };
  const existing = item.adapter === "akile" && config.products.find((product) => product.adapter === "akile" && (product.area_mode !== "manual" || String(product.area_id) === item.area_id) && product.node_name?.trim().toLowerCase() === item.node_name.toLowerCase() && product.selector?.trim().toLowerCase() === item.selector.toLowerCase());
  if (existing) {
    openProducts.add(existing.id);
    renderProducts();
    navigatePage("products");
    editorNode.querySelector(`[data-id="${CSS.escape(existing.id)}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    setMessage("TWLite-Mini 已存在，已展开配置；监控其他套餐可修改此商品的套餐完整名称后再添加。", "success");
    return;
  }
  addProduct(item);
});
document.querySelector("#refresh-status").addEventListener("click", (event) => runAction(event.currentTarget, "刷新中…", "正在刷新服务状态与最近检查记录…", async () => {
  await Promise.all([loadStatus(), refreshManagement()]);
  setMessage("服务状态与最近检查记录已刷新；此次操作不会触发库存检查。", "success");
}));
async function triggerManualRefresh(role, button) {
  await runAction(button, "刷新中…", `正在手动刷新${role === "watch" ? "库存监控" : "库存通知"}…`, async () => {
    const task = await requestJSON("/api/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role }) });
    let result = task;
    for (let attempt = 0; attempt < 120 && result.pending; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      result = await requestJSON(`/api/refresh/status/${encodeURIComponent(task.task_id)}`);
    }
    if (result.pending) throw new Error("刷新仍在执行，请稍后查看状态");
    if (!result.ok) throw new Error(result.error || "手动刷新失败");
    await loadStatus();
    setMessage(`${role === "watch" ? "库存监控" : "库存通知"}手动刷新完成。`, "success");
  });
}
document.querySelector("#monitor-refresh-manual").addEventListener("click", event => triggerManualRefresh("watch", event.currentTarget));
document.querySelector("#notify-refresh-manual").addEventListener("click", event => triggerManualRefresh("notify", event.currentTarget));
const overviewLogDialog = document.querySelector("#overview-log-dialog");
const overviewLogDetail = document.querySelector("#overview-log-detail-content");
const overviewLogTitle = document.querySelector("#overview-log-title");
const overviewModuleNames = { catalog: "大盘", monitor: "监控", notify: "通知", seckill: "抢购" };
function moduleLogItems(key) {
  const state = lastStatus?.modules?.[key] || {};
  return key === "seckill" && vpsSeckillTask?.logs?.length ? vpsSeckillTask.logs : state.logs || [];
}
function fullModuleLogItems(key) {
  const stateLogs = lastStatus?.modules?.[key]?.logs || [];
  const currentLogs = key === "seckill" ? vpsSeckillTask?.logs || [] : [];
  const seen = new Set(stateLogs.map(item => `${item.at || ""}|${item.message || item.result || ""}`));
  return [...stateLogs, ...currentLogs.filter(item => !seen.has(`${item.at || ""}|${item.message || item.result || ""}`))].sort((a, b) => Date.parse(a.at || 0) - Date.parse(b.at || 0));
}
function formatModuleLogs(key, limit = 0, includeHistory = false) {
  const logs = includeHistory ? fullModuleLogItems(key) : moduleLogItems(key), items = limit ? logs.slice(-limit) : logs;
  return items.length ? items.map(item => `[${formatBeijingDate(item.at)}] ${item.provider ? `${merchantNames[item.provider] || item.provider} · ` : ""}${(item.trigger || "manual") === "manual" ? "手动" : "自动"} · ${item.message || item.result || ""}`).join("\n") : "暂无日志";
}
document.querySelectorAll("[data-overview-log-toggle]").forEach(button => button.addEventListener("click", () => {
  const log = document.querySelector(`#overview-${button.dataset.overviewLogToggle}-log`);
  if (!log) return;
  log.hidden = !log.hidden;
  button.textContent = log.hidden ? "显示日志" : "隐藏日志";
}));
document.querySelectorAll("[data-overview-log-detail]").forEach(button => button.addEventListener("click", () => {
  const key = button.dataset.overviewLogDetail;
  overviewLogTitle.textContent = `${overviewModuleNames[key] || "模块"}详细日志`;
  overviewLogDetail.textContent = formatModuleLogs(key);
  overviewLogDialog.showModal();
}));
document.querySelector("#vps-seckill-log-detail").addEventListener("click", () => {
  overviewLogTitle.textContent = "抢购详细日志";
  overviewLogDetail.textContent = formatModuleLogs("seckill", 0, true);
  overviewLogDialog.showModal();
});
document.querySelector("#overview-log-close").addEventListener("click", () => overviewLogDialog.close());
overviewLogDialog.addEventListener("click", event => { if (event.target === overviewLogDialog) overviewLogDialog.close(); });
document.querySelector("#fetcher-mode").addEventListener("change", () => { updateFetcherStatus(); scheduleAutoSave("fetcher"); });
window.addEventListener("hostwatch-action-change", syncProviderFlareSettings);
document.querySelectorAll("#flaresolverr-light, #flaresolverr-full").forEach(button => button.addEventListener("click", () => {
  if (actionRunning) return;
  setFlareModeButtons([...document.querySelectorAll("#flaresolverr-light, #flaresolverr-full")], button.id === "flaresolverr-light");
  updateDraftState();
  scheduleAutoSave("fetcher");
}));
document.querySelector("#fetcher-test").addEventListener("click", (event) => runAction(event.currentTarget, "检测中…", "正在测试 FlareSolverr 服务连接，不访问商品网站…", async () => {
  const result = await requestJSON("/api/fetcher/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(collectConfig().fetcher) });
  document.querySelector("#fetcher-health").textContent = `版本 ${result.version}：${result.message}`;
  setMessage(`FlareSolverr ${result.version} 服务连接成功；实际库存请在 VPS 库存通知中点击“测试通知”，无需开启监控。`, "success");
}));
document.querySelector("#monitor-schedule-provider").addEventListener("change", () => renderProviderSchedule("monitor"));
document.querySelector("#notify-schedule-provider").addEventListener("change", () => renderProviderSchedule("notifications"));
document.querySelector("#vps-seckill-schedule-provider").addEventListener("change", () => renderProviderSchedule("seckill-settings"));
document.querySelector("#interval").addEventListener("input", () => updateProviderSchedule("monitor"));
document.querySelector("#notify-interval").addEventListener("input", () => updateProviderSchedule("notifications"));
document.querySelector("#monitor-schedule-mode").addEventListener("change", () => updateProviderSchedule("monitor"));
document.querySelector("#notify-schedule-mode").addEventListener("change", () => updateProviderSchedule("notifications"));
document.querySelector(".shell").addEventListener("input", (event) => {
  if (!event.target.closest("#page-catalog, .watchlist-filters")) updateDraftState();
});
document.querySelector(".shell").addEventListener("change", (event) => {
  if (!event.target.closest("#page-catalog, .watchlist-filters")) updateDraftState();
});
document.querySelectorAll("#flaresolverr-url, #flaresolverr-timeout, #fetcher-wait").forEach(input => input.addEventListener("input", () => scheduleAutoSave("fetcher")));
document.querySelectorAll("#cloudscraper-connect-timeout, #cloudscraper-read-timeout, #cloudscraper-challenge-delay").forEach(input => input.addEventListener("input", () => scheduleAutoSave("cloudscraper")));
document.querySelector("#telegram-test").addEventListener("click", (event) => runAction(event.currentTarget, "发送中…", "正在发送 Telegram 测试消息…", async () => {
  await requestJSON("/api/telegram/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ bot_token: document.querySelector("#telegram-token").value.trim(), chat_id: document.querySelector("#telegram-chat-id").value.trim() }) });
  setMessage("Telegram 测试消息已发送。", "success");
}));
document.querySelector("#telegram-load-chats").addEventListener("click", (event) => runAction(event.currentTarget, "读取中…", "正在读取 Telegram 最近聊天…", async () => {
  const chatsNode = document.querySelector("#telegram-chats");
  const token = document.querySelector("#telegram-token").value.trim();
  const result = token
    ? await requestJSON("/api/telegram/chats", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ bot_token: token }) })
    : await requestJSON("/api/telegram/chats");
  chatsNode.replaceChildren();
  if (!result.chats.length) {
    const message = "读取完成，暂无聊天记录；请先打开 bot 并发送 /start 后重试。";
    chatsNode.append(text("p", message, "hint"));
    setMessage(message, "success");
    return;
  }
  for (const chat of result.chats) {
    const option = text("button", "", "chat-option");
    option.type = "button";
    option.append(text("span", `${chat.name}（${chat.type}）`), text("span", `${chat.id}${chat.username ? ` · @${chat.username}` : ""}`));
    option.addEventListener("click", () => { document.querySelector("#telegram-chat-id").value = chat.id; updateDraftState(); setMessage(`已选择 ${chat.name}，保存后生效。`, "pending"); });
    chatsNode.append(option);
  }
  setMessage(`已读取 ${result.chats.length} 个聊天。`, "success");
}));

async function saveConfig(scope = "all", automatic = false) {
  const draft = collectConfig();
  const saved = savedConfigSnapshot() || draft;
  const next = mergeConfigScope(saved, draft, scope);
  const result = await requestJSON("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(automatic ? { "X-HostWatch-Auto-Save": "1" } : {}) },
    body: JSON.stringify(next),
  });
  document.querySelector("#telegram-token").value = "";
  // Keep unrelated cards' drafts visible after saving this card. Their values
  // remain local until their own save button is pressed.
  // Automatic saves do not lock the page. Preserve edits made while the
  // request was in flight so a later change can be saved by the next cycle.
  const visibleDraftSource = automatic ? collectConfig() : draft;
  const visibleDraft = scope === "all" ? result.config : mergeConfigScope(result.config, visibleDraftSource, "all");
  if (scope !== "all") {
    if (scope === "affiliates") Object.assign(visibleDraft, { affiliate_id: result.config.affiliate_id, affiliates: result.config.affiliates, affiliate_notes: result.config.affiliate_notes, affiliate_accounts: result.config.affiliate_accounts, affiliate_rates: result.config.affiliate_rates, affiliate_coupons: result.config.affiliate_coupons, merchant_order: result.config.merchant_order });
    else if (scope === "fetcher" || scope === "provider-modes" || scope === "egress") visibleDraft.fetcher = draft.fetcher;
    else if (scope === "cloudscraper") visibleDraft.fetcher = { ...visibleDraft.fetcher, cloudscraper: draft.fetcher.cloudscraper };
    else if (scope === "market") { visibleDraft.akile_market = draft.akile_market; visibleDraft.isvoro_market = draft.isvoro_market; }
    else if (scope === "telegram") visibleDraft.telegram = draft.telegram;
    else if (scope === "catalog") visibleDraft.catalog_schedule = draft.catalog_schedule;
    else if (scope === "monitor" || scope === "notifications") visibleDraft.products = draft.products;
    else if (scope === "seckill") visibleDraft.vps_seckill = draft.vps_seckill;
    else if (scope === "seckill-settings") visibleDraft.vps_seckill = { ...visibleDraft.vps_seckill, mode: draft.vps_seckill.mode, interval_seconds: draft.vps_seckill.interval_seconds };
    else if (scope.startsWith("product:")) visibleDraft.products = draft.products;
    else if (scope.startsWith("seckill-")) visibleDraft.vps_seckill = draft.vps_seckill;
  }
  // Password fields are write-only. Do not restore a submitted password into
  // the local draft after saving, or the account card remains falsely dirty.
  if (scope === "seckill") {
    for (const account of visibleDraft.vps_seckill?.accounts || []) account.password = "";
  } else if (scope.startsWith("seckill-account:")) {
    const accountId = scope.slice("seckill-account:".length);
    const accountIndex = visibleDraft.vps_seckill?.accounts?.findIndex(row => row.id === accountId) ?? -1;
    const savedAccount = result.config.vps_seckill?.accounts?.find(row => row.id === accountId);
    if (accountIndex >= 0 && savedAccount) visibleDraft.vps_seckill.accounts[accountIndex] = { ...savedAccount, password: "" };
  }
  const telegramTokenDraft = draft.telegram?.bot_token || "";
  // Normalize the server response into the same public shape used by
  // collectConfig(), then restore unrelated local drafts for card isolation.
  if (automatic) {
    // The page already contains the current draft. Updating the saved
    // snapshot directly avoids rebuilding every product table while the user
    // is working, which is especially noticeable from the catalog page.
    const savedBaseline = savedConfigSnapshot() || collectConfig();
    savedConfig = JSON.stringify(mergeConfigScope(savedBaseline, result.config, scope));
    updateDraftState();
    if (result.service?.ok) {
      setMessage(`${autoSaveScopeNames[scope] || "配置"}已自动保存，服务正在后台应用。`, "success", true, false);
    } else {
      setMessage(`配置已保存，但自动应用服务失败：${result.service?.error || "请在设置中重试"}`, "error", true, false);
    }
    return;
  }
  applyConfig(result.config);
  savedConfig = JSON.stringify(collectConfig());
  if (scope !== "all") {
    applyConfig(visibleDraft);
    if (scope !== "telegram" && telegramTokenDraft) {
      document.querySelector("#telegram-token").value = telegramTokenDraft;
    }
  }
  updateDraftState();
  if (result.service?.ok) {
    setMessage(automatic ? `${autoSaveScopeNames[scope] || "配置"}已自动保存，服务正在后台应用。` : scope === "all" ? "配置已保存并应用。" : "本卡片配置已保存并应用。", "success", true, automatic ? false : true);
  } else {
    setMessage(`配置已保存，但自动应用服务失败：${result.service?.error || "请在设置中重试"}`, "error", true, automatic ? false : true);
  }
  // Show account feedback immediately; regular polling refreshes runtime state.
  if (scope.startsWith("seckill-account:")) return result;
  // Automatic saves should release the page as soon as the configuration API
  // has completed. The regular live-state refresh will pick up service status
  // shortly; waiting for two extra management requests here made every small
  // edit keep the whole page locked for too long.
  if (automatic) return;
  try { await Promise.all([refreshManagement(), loadStatus()]); }
  catch (error) { document.querySelector("#manage-task-status").textContent = `配置已保存，管理状态刷新失败：${error.message}`; }
  return result;
}
document.addEventListener("click", event => {
  const button = event.target.closest("[data-save-card]");
  if (!button || button.getAttribute("aria-busy") === "true") return;
  event.preventDefault();
  runAction(button, "保存中…", "正在保存本卡片配置…", () => saveConfig(button.dataset.saveCard));
}, true);
renderExchangeRates();
exchangeClockTimer = setInterval(renderCurrentTime, 1_000);
exchangeRateRefreshTimer = setInterval(() => { if (!document.hidden) void loadExchangeRates(); }, 60 * 60 * 1000);
runAction(null, "", "正在载入配置和监控状态…", () => reloadConfig(false), false);
bindManagementActions();
if (updateUnit) pollUpdateStatus();
resumeUpdateReload();
