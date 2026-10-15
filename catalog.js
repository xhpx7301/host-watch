/* VPS browsing uses cached read-only snapshots; only explicit selection edits config. */
(() => {
  const providers = { vmiss: "VMISS", vps_soy: "VPS.SOY", akile: "Akile", isvoro: "Isvoro", dmit: "DMIT" };
  const get = (id) => document.getElementById(id);
  let provider = sessionStorage.getItem("hostwatch-catalog-provider") || "akile";
  if (!Object.hasOwn(providers, provider)) provider = "akile";
  let scope = "", data = null, task = null, page = 0, generation = 0, loading = false, metadataOutdated = false;
  let pollTimer, autoTimer;
  let tableSignature = "";
  let filterGeneration = null;
  const selected = new Set();
  const knownScopes = new Map();
  const pageSize = 30;
  const autoValue = sessionStorage.getItem("hostwatch-catalog-auto") || "300";
  get("catalog-auto").value = ["0", "60", "300", "600"].includes(autoValue) ? autoValue : "300";

  function matches(row, item) {
    const watch = row.watch;
    if (item.adapter !== watch.adapter) return false;
    const equal = (a, b) => String(a || "").trim().toLowerCase() === String(b || "").trim().toLowerCase();
    if (watch.adapter === "dmit") return Boolean(item.plan_key && watch.plan_key && equal(item.plan_key, watch.plan_key) || item.pid && watch.pid && equal(item.pid, watch.pid));
    if (watch.adapter === "akile") return (item.area_mode !== "manual" || equal(item.area_id, watch.area_id)) && equal(item.node_name, watch.node_name) && equal(item.selector, watch.selector);
    if (watch.adapter === "isvoro") return ["type_id", "category_id", "product_id"].every((key) => equal(item[key], watch[key]));
    if (watch.adapter === "vps_soy") {
      try {
        const left = new URL(item.url), right = new URL(watch.url);
        const category = (url, key) => url.searchParams.get(key) || (key === "fid" ? "1" : "8");
        return equal(item.selector, watch.selector) && ["fid", "gid"].every((key) => equal(category(left, key), category(right, key)));
      } catch { return false; }
    }
    const selector = String(item.selector || "").trim().toLowerCase();
    let slug = "";
    try { slug = new URL(row.purchase_url).pathname.split("/").at(-1); } catch { /* Unknown stock has no purchase link. */ }
    return equal(String(item.category_url || "").replace(/\/$/, ""), watch.category_url) && [watch.selector, watch.name, row.purchase_url ? new URL(row.purchase_url).pathname : "", slug].filter(Boolean).some((value) => equal(selector, value));
  }

  function existing(row) { return config.products.find((item) => matches(row, item)); }
  function filtered() {
    const search = get("catalog-search").value.trim().toLowerCase(), stock = get("catalog-stock").value;
    return (data?.rows || []).filter((row) => (!search || [row.name, row.region, row.line, row.specs, row.traffic, row.price].join(" ").toLowerCase().includes(search)) &&
      (stock === "all" || stock === "in" && row.available > 0 || stock === "out" && row.available === 0 || stock === "unknown" && row.available === null || stock === "watched" && existing(row) && membership(existing(row), "watch") || stock === "notified" && existing(row) && membership(existing(row), "notify")));
  }

  function reminderLabel(row) {
    const item = existing(row);
    if (!item || !membership(item, "watch") && !membership(item, "notify")) return "尚未关注";
    const saved = savedConfig ? JSON.parse(savedConfig).products.find((p) => p.id === item.id) : null;
    if (!saved?.enabled || JSON.stringify(saved) !== JSON.stringify(item)) return "待保存";
    return config.enabled && monitorService?.active === "active" ? "自动检查中" : "已保存 · 检查未运行";
  }

  function renderReminders() {
    const count = config.products.filter(p => membership(p, "watch")).length;
    const notifications = config.products.filter(p => membership(p, "notify")).length;
    get("catalog-watch-count").textContent = `${count} / ${notifications}`;
    const dirty = savedConfig !== null && hasUnsavedChanges();
    let note = dirty ? "选择已修改，点击底部“保存配置”应用全部栏目修改。" : "选择已保存。";
    if (!config.enabled || monitorService?.active !== "active") note += "自动监控尚未运行，保存后点击“开始监控”。";
    else note += "服务器自动检查已开启，关闭网页后继续运行。";
    if (notifications && (!config.telegram?.configured || config.telegram?.disabled)) note += "请在 Telegram 通知中配置并启用机器人后保存。";
    if (config.enabled && !config.products.some(p => p.enabled)) note += "已取消全部商品，请先暂停监控再保存。";
    get("catalog-reminder-note").textContent = note;
  }

  function renderTable(animate = false) {
    if (animate) animateFilterResult(get("catalog-results"));
    const rows = filtered();
    page = Math.max(0, Math.min(page, Math.ceil(rows.length / pageSize) - 1));
    const visible = rows.slice(page * pageSize, (page + 1) * pageSize);
    const signature = JSON.stringify([visible, rows.length, page, [...selected], config.products, config.affiliates, savedConfig, config.enabled, monitorService?.active, actionRunning]);
    if (signature === tableSignature) { renderSelection(visible); renderReminders(); return; }
    tableSignature = signature;
    const body = get("catalog-rows");
    const expanded = expandedPrices(body);
    body.replaceChildren(...visible.map((row) => {
      const tr = document.createElement("tr"); tr.dataset.id = row.key;
      const cell = () => { const td = document.createElement("td"); tr.append(td); return td; };
      const choice = text("button", selected.has(row.key) ? "✓ 已选" : "选择", "option-button catalog-choice");
      choice.type = "button"; choice.dataset.catalogSelect = row.key;
      choice.setAttribute("aria-pressed", String(selected.has(row.key)));
      choice.setAttribute("aria-label", `选择 ${row.name} ${row.line}`);
      choice.disabled = actionRunning;
      choice.addEventListener("click", () => { selected.has(row.key) ? selected.delete(row.key) : selected.add(row.key); renderSelection(visible); });
      cell().append(choice);
      const identity = cell(); identity.append(text("strong", row.name), text("small", row.provider));
      const region = cell(); region.append(text("span", row.region || "—"));
      if (row.line && row.line !== row.region) region.append(text("small", row.line));
      const hardware = cell();
      const metadata = productSnapshot({ adapter: row.watch.adapter, snapshot: row });
      if (metadata.specs) renderSpecs(hardware, metadata.specs);
      if (metadata.traffic) { const network = text("div", "", "catalog-network"); renderSpecs(network, metadata.traffic, true); hardware.append(network); }
      if (!metadata.specs && !metadata.traffic) hardware.textContent = "商家未提供配置";
      renderPrice(cell(), row.price || "以商家页面为准", expanded.has(row.key));
      const stock = cell();
      stock.append(text("span", row.available === null ? "未知" : row.available > 0 ? (row.stock_mode === "availability" ? "有货" : `有货 ${row.available}`) : "缺货", `state ${row.available === null ? "pending" : row.available > 0 ? "ok" : "error"}`));
      if (row.error) { const error = text("small", row.error); stock.append(error); stock.style.whiteSpace = "normal"; }
      const actions = text("div", "", "catalog-row-actions");
      const item = existing(row);
      for (const [type, label] of [["watch", "库存监控"], ["notify", "库存通知"]]) {
        const active = item && membership(item, type);
        const button = text("button", active ? `✓ 已加入${label}` : `＋ 加入${label}`, `reminder-action reminder-${type}`); button.type = "button";
        button.disabled = actionRunning;
        button.setAttribute("aria-pressed", String(Boolean(active)));
        button.addEventListener("click", () => setReminders([row], !active, type));
        actions.append(button);
      }
      actions.append(text("small", reminderLabel(row)));
      if (row.purchase_url) {
        const url = new URL(row.purchase_url);
        if (url.protocol === "https:" && ["app.vmiss.com", "www.vps.soy", "akile.ai", "isvoro.com", "www.dmit.io"].includes(url.hostname) && !url.username && !url.password) {
          const link = text("a", "直接购买", "buy-link"); link.href = affiliatePurchaseURL(url.href, row.watch.adapter, row.watch); link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link);
        }
      }
      cell().append(actions);
      return tr;
    }));
    get("catalog-empty").hidden = rows.length > 0;
    get("catalog-empty").textContent = data ? (data.rows.length ? "没有符合筛选条件的商品。" : "该分类暂无公开套餐。") : "尚无目录数据，点击“刷新分类库存”读取。";
    get("catalog-page-label").textContent = `${rows.length} 个结果 · 第 ${page + 1} / ${Math.max(1, Math.ceil(rows.length / pageSize))} 页`;
    get("catalog-prev").disabled = actionRunning || page === 0;
    get("catalog-next").disabled = actionRunning || (page + 1) * pageSize >= rows.length;
    renderSelection(visible);
    renderReminders();
  }

  function renderSelection(visible = filtered().slice(page * pageSize, (page + 1) * pageSize)) {
    const eligible = visible;
    const all = get("catalog-select-all");
    const complete = eligible.length > 0 && eligible.every((row) => selected.has(row.key));
    const partial = !complete && eligible.some((row) => selected.has(row.key));
    all.setAttribute("aria-pressed", complete ? "true" : partial ? "mixed" : "false");
    all.querySelector("[data-option-state]").textContent = complete ? "已全选" : partial ? "部分选中" : "未全选";
    for (const choice of get("catalog-rows").querySelectorAll("[data-catalog-select]")) {
      const active = selected.has(choice.dataset.catalogSelect);
      choice.setAttribute("aria-pressed", String(active));
      choice.textContent = active ? "已选" : "选择";
      choice.disabled = actionRunning;
    }
    all.disabled = actionRunning || !eligible.length;
    get("catalog-selected").textContent = `已选 ${selected.size} 项`;
    get("catalog-watch-selected").disabled = actionRunning || !selected.size;
    get("catalog-unwatch-selected").disabled = actionRunning || !selected.size;
  }

  function render() {
    document.querySelectorAll("[data-catalog-provider]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.catalogProvider === provider)));
    const scopes = data?.scopes || knownScopes.get(provider) || [];
    get("catalog-scope").replaceChildren(...(scopes.length ? scopes.map((row) => new Option(row.name, row.id)) : [new Option("等待目录", "")]));
    get("catalog-scope").value = scope;
    get("catalog-scope").disabled = actionRunning || loading || !scopes.length;
    const busy = Boolean(task?.pending);
    get("catalog-refresh").disabled = actionRunning || busy || loading;
    get("catalog-stop").hidden = !busy;
    get("catalog-stop").disabled = actionRunning || Boolean(task?.cancelled);
    get("catalog-stop").textContent = task?.cancelled ? "停止中…" : "停止刷新";
    get("catalog-summary").textContent = `${providers[provider]} · ${data?.rows.length || 0} 个套餐 · ${data?.rows.filter((row) => row.available > 0).length || 0} 个有货`;
    get("catalog-updated").textContent = data?.fetched_at ? `数据更新时间：${new Date(data.fetched_at).toLocaleString()}` : "尚无库存快照";
    const status = get("catalog-status");
    const stale = data && Date.now() - Date.parse(data.fetched_at) > 300000;
    status.className = "hint";
    if (busy) { status.classList.add("pending"); status.textContent = task.cancelled ? "正在终止目录刷新，保留上次快照…" : `正在刷新 ${providers[task.provider] || "商家"} 目录，页面仍可操作；可点击“停止刷新”。`; }
    else if (task?.provider === provider && task.error) { status.classList.add("error"); status.textContent = task.error; }
    else if (task?.provider === provider && task.cancelled) status.textContent = "刷新已停止，保留上次快照。";
    else if (loading) status.textContent = "正在读取目录缓存…";
    else if (metadataOutdated) { status.classList.add("stale"); status.textContent = "此分类为旧版快照，带宽信息尚未更新，请刷新分类库存。"; }
    else { status.textContent = data ? `已读取此分类。${data.unknown ? `${data.unknown} 个套餐库存未知，未当作缺货或现货。` : ""}${stale ? "当前为历史快照，请刷新后核对。" : "库存以此快照的检查时间为准。"}` : "选择商家后刷新分类库存，无需先开启自动监控。"; if (stale) status.classList.add("stale"); }
    const filterArrived = filterGeneration === generation && !loading && (data?.fetched_at || !task?.pending);
    renderTable(Boolean(filterArrived));
    if (filterArrived) filterGeneration = null;
  }

  function setReminders(rows, enabled, type = "watch") {
    if (actionRunning) return;
    const needed = rows.filter(row => !existing(row));
    if (enabled && config.products.length + needed.length > 100) { setMessage("最多保存 100 个商品，请先移除不需要的配置。", "error"); return; }
    let changed = 0;
    for (const row of rows) {
      let item = existing(row);
      if (!item && enabled) {
        item = { ...structuredClone(row.watch), id: crypto.randomUUID(), enabled: false, watch_enabled: false, notify_enabled: false };
        config.products.push(item);
      }
      if (!item) continue;
      item.snapshot = { name: row.name, region: row.region, specs: row.specs, traffic: row.traffic || "", price: row.price, available: row.available, stock_mode: row.stock_mode, url: row.purchase_url, checked_at: data?.fetched_at || "" };
      if (membership(item, type) !== enabled) { setMembership(item, type, enabled); changed++; }
    }
    renderProducts(); updateDraftState(); renderTable();
    setMessage(changed ? `已${enabled ? "加入" : "移出"} ${changed} 个商品的${type === "watch" ? "库存监控（不通知）" : "库存通知"}，点击底部“保存配置”生效。` : "选择无需更改。", changed ? "pending" : "success");
  }

  function schedule() {
    clearTimeout(pollTimer); clearTimeout(autoTimer);
    if (currentPage !== "catalog" || document.hidden) return;
    if (task?.pending) pollTimer = setTimeout(() => loadSnapshot(false), 1200);
    else if (!task?.error && !task?.cancelled && Number(get("catalog-auto").value)) autoTimer = setTimeout(refresh, Number(get("catalog-auto").value) * 1000);
  }

  async function loadSnapshot(refreshMissing = true) {
    const version = generation, requestedProvider = provider, requestedScope = scope;
    loading = true; render();
    try {
      const result = await requestJSON(`/api/catalog?${new URLSearchParams({ provider: requestedProvider, scope: requestedScope })}`, { timeoutMs: 10000 });
      if (version !== generation || currentPage !== "catalog") return;
      data = result.data; task = result.task; metadataOutdated = Boolean(result.needs_refresh);
      if (data) {
        scope = data.scope;
        knownScopes.set(provider, data.scopes);
        const keys = new Set(data.rows.map((row) => row.key));
        for (const key of selected) if (!keys.has(key)) selected.delete(key);
      }
      if ((!data || metadataOutdated) && !task?.pending && refreshMissing && !(task?.provider === provider && (!task.scope || !scope || task.scope === scope) && (task.error || task.cancelled))) {
        loading = false; await refresh(); return;
      }
    } catch (error) {
      if (version !== generation) return;
      task = { provider, pending: false, error: `读取目录失败：${error.message} 可点击刷新重试。` };
    } finally {
      if (version === generation) { loading = false; render(); schedule(); }
    }
  }

  async function refresh() {
    if (loading || task?.pending || currentPage !== "catalog" || document.hidden || actionRunning) { schedule(); return; }
    const version = generation;
    loading = true; render();
    try {
      const result = await requestJSON("/api/catalog/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider, scope }), timeoutMs: 10000 });
      if (version === generation) task = result;
    } catch (error) { if (version === generation) task = { provider, pending: false, error: error.message }; }
    finally { if (version === generation) { loading = false; render(); schedule(); } }
  }

  document.querySelectorAll("[data-catalog-provider]").forEach((button) => button.addEventListener("click", () => {
    if (provider === button.dataset.catalogProvider) return;
    generation++; provider = button.dataset.catalogProvider; scope = ""; data = null; metadataOutdated = false; page = 0; selected.clear();
    filterGeneration = generation;
    sessionStorage.setItem("hostwatch-catalog-provider", provider);
    get("catalog-search").value = ""; get("catalog-stock").value = "all";
    void loadSnapshot();
    animateFilterResult(get("catalog-results"));
  }));
  get("catalog-scope").addEventListener("change", () => {
    generation++; scope = get("catalog-scope").value; page = 0; selected.clear();
    filterGeneration = generation;
    const scopes = data?.scopes || knownScopes.get(provider) || []; data = { provider, scope, scopes, rows: [] }; metadataOutdated = false;
    void loadSnapshot();
    animateFilterResult(get("catalog-results"));
  });
  for (const id of ["catalog-search", "catalog-stock"]) get(id).addEventListener(id === "catalog-search" ? "input" : "change", () => { page = 0; renderTable(true); });
  get("catalog-auto").addEventListener("change", () => { sessionStorage.setItem("hostwatch-catalog-auto", get("catalog-auto").value); schedule(); });
  get("catalog-refresh").addEventListener("click", () => { task = task?.pending ? task : null; void refresh(); });
  get("catalog-stop").addEventListener("click", async () => {
    get("catalog-auto").value = "0"; sessionStorage.setItem("hostwatch-catalog-auto", "0"); clearTimeout(autoTimer);
    try { task = await requestJSON("/api/catalog/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}", timeoutMs: 10000 }); }
    catch (error) { setMessage(`停止刷新尚未确认：${error.message}，请重试。`, "error"); }
    render(); schedule();
  });
  get("catalog-select-all").addEventListener("click", () => {
    const select = get("catalog-select-all").getAttribute("aria-pressed") !== "true";
    filtered().slice(page * pageSize, (page + 1) * pageSize).forEach((row) => select ? selected.add(row.key) : selected.delete(row.key));
    renderSelection();
  });
  get("catalog-watch-selected").addEventListener("click", () => setReminders((data?.rows || []).filter((row) => selected.has(row.key)), true));
  get("catalog-unwatch-selected").addEventListener("click", () => setReminders((data?.rows || []).filter((row) => selected.has(row.key)), true, "notify"));
  get("catalog-prev").addEventListener("click", () => { page--; renderTable(true); });
  get("catalog-next").addEventListener("click", () => { page++; renderTable(true); });
  get("catalog-open-products").addEventListener("click", () => navigatePage("monitor"));
  window.addEventListener("hostwatch-page-change", (event) => {
    generation++; filterGeneration = null; clearTimeout(autoTimer); clearTimeout(pollTimer); loading = false;
    if (event.detail === "catalog") void loadSnapshot();
  });
  for (const event of ["hostwatch-draft-change", "hostwatch-service-change"]) window.addEventListener(event, () => { if (currentPage === "catalog") renderTable(); });
  window.addEventListener("hostwatch-action-change", () => {
    if (currentPage !== "catalog") return;
    render();
    if (!data && !loading && !task?.pending && !task?.error && !task?.cancelled) void loadSnapshot();
  });
  document.addEventListener("visibilitychange", () => { if (currentPage === "catalog" && !document.hidden) void loadSnapshot(false); else schedule(); });
  render();
  if (currentPage === "catalog") void loadSnapshot();
})();
