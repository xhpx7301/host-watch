/* VPS browsing uses cached read-only snapshots; only explicit selection edits config. */
(() => {
  const providers = { vmiss: "VMISS", vps_soy: "VPS.SOY", akile: "Akile", isvoro: "Isvoro" };
  const get = (id) => document.getElementById(id);
  let provider = sessionStorage.getItem("hostwatch-catalog-provider") || "akile";
  if (!Object.hasOwn(providers, provider)) provider = "akile";
  let scope = "", data = null, task = null, page = 0, generation = 0, loading = false;
  let pollTimer, autoTimer;
  let tableSignature = "";
  const selected = new Set();
  const knownScopes = new Map();
  const pageSize = 30;
  const autoValue = sessionStorage.getItem("hostwatch-catalog-auto") || "300";
  get("catalog-auto").value = ["0", "60", "300", "600"].includes(autoValue) ? autoValue : "300";

  function matches(row, item) {
    const watch = row.watch;
    if (item.adapter !== watch.adapter) return false;
    const equal = (a, b) => String(a || "").trim().toLowerCase() === String(b || "").trim().toLowerCase();
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
    return (data?.rows || []).filter((row) => (!search || [row.name, row.region, row.line, row.specs, row.price].join(" ").toLowerCase().includes(search)) &&
      (stock === "all" || stock === "in" && row.available > 0 || stock === "out" && row.available === 0 || stock === "unknown" && row.available === null || stock === "watched" && existing(row)?.enabled));
  }

  function reminderLabel(row) {
    const item = existing(row);
    if (!item?.enabled) return "未选提醒";
    const saved = savedConfig ? JSON.parse(savedConfig).products.find((p) => p.id === item.id) : null;
    if (!saved?.enabled || JSON.stringify(saved) !== JSON.stringify(item)) return "待保存";
    return config.enabled && monitorService?.active === "active" ? "提醒监控中" : "已保存 · 监控未运行";
  }

  function renderReminders() {
    const count = config.products.filter((p) => p.enabled).length;
    get("catalog-watch-count").textContent = count;
    const dirty = savedConfig !== null && hasUnsavedChanges();
    let note = dirty ? "选择已修改，点击底部“保存配置”应用全部栏目修改。" : "选择已保存。";
    if (!config.enabled || monitorService?.active !== "active") note += "自动监控尚未运行，保存后点击“开始监控”。";
    else note += "服务器自动检查已开启，关闭网页后继续运行。";
    if (!config.telegram?.configured || config.telegram?.disabled) note += "请在 Telegram 通知中配置并启用机器人后保存。";
    if (config.enabled && !count) note += "已取消全部商品，请先暂停监控再保存。";
    get("catalog-reminder-note").textContent = note;
  }

  function renderTable() {
    const rows = filtered();
    page = Math.max(0, Math.min(page, Math.ceil(rows.length / pageSize) - 1));
    const visible = rows.slice(page * pageSize, (page + 1) * pageSize);
    const signature = JSON.stringify([visible, rows.length, page, [...selected], config.products, savedConfig, config.enabled, monitorService?.active, actionRunning]);
    if (signature === tableSignature) { renderSelection(visible); renderReminders(); return; }
    tableSignature = signature;
    const body = get("catalog-rows");
    body.replaceChildren(...visible.map((row) => {
      const tr = document.createElement("tr");
      const cell = () => { const td = document.createElement("td"); tr.append(td); return td; };
      const choice = document.createElement("input");
      choice.type = "checkbox"; choice.checked = selected.has(row.key);
      choice.setAttribute("aria-label", `选择 ${row.name} ${row.line}`);
      choice.disabled = actionRunning || row.available === null && !existing(row)?.enabled;
      choice.addEventListener("change", () => { choice.checked ? selected.add(row.key) : selected.delete(row.key); renderSelection(visible); });
      const label = text("label", "", "check"); label.append(choice); cell().append(label);
      const identity = cell(); identity.append(text("strong", row.name), text("small", row.provider));
      const region = cell(); region.append(text("span", row.region || "—"));
      if (row.line && row.line !== row.region) region.append(text("small", row.line));
      cell().textContent = row.specs || "商家未提供配置";
      cell().textContent = row.price || "以商家页面为准";
      const stock = cell();
      stock.append(text("span", row.available === null ? "未知" : row.available > 0 ? (row.stock_mode === "availability" ? "有货" : `有货 ${row.available}`) : "缺货", `state ${row.available === null ? "pending" : row.available > 0 ? "ok" : "error"}`));
      if (row.error) { const error = text("small", row.error); stock.append(error); stock.style.whiteSpace = "normal"; }
      const actions = text("div", "", "catalog-row-actions");
      const watched = Boolean(existing(row)?.enabled);
      const button = text("button", watched ? "取消提醒" : "加入补货提醒"); button.type = "button";
      button.disabled = actionRunning || row.available === null && !watched;
      button.setAttribute("aria-pressed", String(watched));
      button.addEventListener("click", () => setReminders([row], !watched));
      actions.append(button, text("small", reminderLabel(row)));
      if (row.purchase_url) {
        const url = new URL(row.purchase_url);
        if (url.protocol === "https:" && ["app.vmiss.com", "www.vps.soy", "akile.ai", "isvoro.com"].includes(url.hostname) && !url.username && !url.password) {
          const link = text("a", "查看 / 手动购买"); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link);
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
    const eligible = visible.filter((row) => row.available !== null || existing(row)?.enabled);
    const all = get("catalog-select-all");
    all.checked = eligible.length > 0 && eligible.every((row) => selected.has(row.key));
    all.indeterminate = !all.checked && eligible.some((row) => selected.has(row.key));
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
    else { status.textContent = data ? `已读取此分类。${data.unknown ? `${data.unknown} 个套餐库存未知，未当作缺货或现货。` : ""}${stale ? "当前为历史快照，请刷新后核对。" : "库存以此快照的检查时间为准。"}` : "选择商家后刷新分类库存，无需先开启自动监控。"; if (stale) status.classList.add("stale"); }
    renderTable();
  }

  function setReminders(rows, enabled) {
    if (actionRunning) return;
    const eligible = rows.filter((row) => !enabled || row.available !== null);
    const needed = eligible.filter((row) => !existing(row));
    if (enabled && config.products.length + needed.length > 100) { setMessage("最多保存 100 个监控商品，请先移除不需要的商品。此次选择未改动。", "error"); return; }
    let changed = 0;
    for (const row of eligible) {
      const item = existing(row);
      if (item) { if (item.enabled !== enabled) { item.enabled = enabled; changed++; } }
      else if (enabled) { config.products.push({ ...structuredClone(row.watch), id: crypto.randomUUID(), enabled: true }); changed++; }
    }
    renderProducts(); updateDraftState(); renderTable();
    setMessage(changed ? `${enabled ? "已选择" : "已取消"} ${changed} 个商品的补货提醒，请点击底部“保存配置”${config.enabled ? "应用。" : "，然后开始监控。"}` : "所选商品的提醒状态无需更改。", changed ? "pending" : "success");
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
      data = result.data; task = result.task;
      if (data) {
        scope = data.scope;
        knownScopes.set(provider, data.scopes);
        const keys = new Set(data.rows.map((row) => row.key));
        for (const key of selected) if (!keys.has(key)) selected.delete(key);
      }
      if (!data && !task?.pending && refreshMissing && !(task?.provider === provider && (!task.scope || !scope || task.scope === scope) && (task.error || task.cancelled))) {
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
    generation++; provider = button.dataset.catalogProvider; scope = ""; data = null; page = 0; selected.clear();
    sessionStorage.setItem("hostwatch-catalog-provider", provider);
    get("catalog-search").value = ""; get("catalog-stock").value = "all";
    void loadSnapshot();
  }));
  get("catalog-scope").addEventListener("change", () => {
    generation++; scope = get("catalog-scope").value; page = 0; selected.clear();
    const scopes = data?.scopes || knownScopes.get(provider) || []; data = { provider, scope, scopes, rows: [] };
    void loadSnapshot();
  });
  for (const id of ["catalog-search", "catalog-stock"]) get(id).addEventListener(id === "catalog-search" ? "input" : "change", () => { page = 0; renderTable(); });
  get("catalog-auto").addEventListener("change", () => { sessionStorage.setItem("hostwatch-catalog-auto", get("catalog-auto").value); schedule(); });
  get("catalog-refresh").addEventListener("click", () => { task = task?.pending ? task : null; void refresh(); });
  get("catalog-stop").addEventListener("click", async () => {
    get("catalog-auto").value = "0"; sessionStorage.setItem("hostwatch-catalog-auto", "0"); clearTimeout(autoTimer);
    try { task = await requestJSON("/api/catalog/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}", timeoutMs: 10000 }); }
    catch (error) { setMessage(`停止刷新尚未确认：${error.message}，请重试。`, "error"); }
    render(); schedule();
  });
  get("catalog-select-all").addEventListener("change", (event) => {
    filtered().slice(page * pageSize, (page + 1) * pageSize).filter((row) => row.available !== null || existing(row)?.enabled).forEach((row) => event.target.checked ? selected.add(row.key) : selected.delete(row.key));
    renderTable();
  });
  get("catalog-watch-selected").addEventListener("click", () => setReminders((data?.rows || []).filter((row) => selected.has(row.key)), true));
  get("catalog-unwatch-selected").addEventListener("click", () => setReminders((data?.rows || []).filter((row) => selected.has(row.key)), false));
  get("catalog-prev").addEventListener("click", () => { page--; renderTable(); });
  get("catalog-next").addEventListener("click", () => { page++; renderTable(); });
  get("catalog-open-products").addEventListener("click", () => navigatePage("products"));
  window.addEventListener("hostwatch-page-change", (event) => {
    generation++; clearTimeout(autoTimer); clearTimeout(pollTimer); loading = false;
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
