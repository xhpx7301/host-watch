/* VPS browsing uses cached read-only snapshots; only explicit selection edits config. */
(() => {
  const providers = { vmiss: "VMISS", vps_soy: "VPS.SOY", akile: "Akile", narwhal: "独角鲸云", isvoro: "Isvoro", dmit: "DMIT", bwh: "搬瓦工" };
  const get = (id) => document.getElementById(id);
  let provider = sessionStorage.getItem("hostwatch-catalog-provider") || "akile";
  if (!Object.hasOwn(providers, provider)) provider = "akile";
  if (currentPage === "narwhal") provider = "narwhal";
  const fullDirectory = () => ["akile", "narwhal", "bwh"].includes(provider);
  const hierarchicalDirectory = () => fullDirectory() || provider === "dmit";
  let scope = fullDirectory() ? "all" : "", data = null, task = null, route = null, page = 0, generation = 0, loading = false, metadataOutdated = false;
  let narwhalRegion = "", narwhalMachine = "";
  let akileArea = "", akileNode = "";
  const facetState = { bwh: { region: "", datacenter: "" }, dmit: { network: "", hardware: "" } };
  const networkNames = { PREMIUM: "Premium（Pro）", EYEBALL: "Eyeball（EB）", TIER1: "Tier 1（T1）" };
  function facetValue(row, key) {
    if (key === "region") return row.region || "未标注地区";
    if (key === "datacenter") return row.watch.bwh_datacenter_id || "未标注机房";
    const parts = (row.watch.plan_key || "").split(":");
    if (key === "hardware") return parts[1] || /\b(?:AS|AN)\d+\b/i.exec(`${row.name} ${row.line}`)?.[0].toUpperCase() || "未标注处理器";
    const line = parts[2] || row.line.split("/").at(-1).trim().toUpperCase();
    return ({ PRO: "PREMIUM", PREMIUM: "PREMIUM", EB: "EYEBALL", EYEBALL: "EYEBALL", T1: "TIER1", "TIER 1": "TIER1", TIER1: "TIER1" })[line] || line || "未标注网络";
  }
  function facetLabel(row, key) {
    const value = facetValue(row, key);
    if (key === "datacenter") return `${row.line || "机房名称未提供"} · ${value} · ${row.region}`;
    if (key === "network") return networkNames[value] || value;
    return value;
  }
  function facetRows(kind) {
    return (data?.rows || []).filter(row => Object.entries(facetState[kind]).every(([key, value]) => !value || facetValue(row, key) === value));
  }
  function renderFacetSelect(kind, key, rows, title, disabled = false) {
    const choices = new Map();
    for (const row of rows) {
      const value = facetValue(row, key), choice = choices.get(value) || { label: facetLabel(row, key), count: 0 };
      choice.count++; choices.set(value, choice);
    }
    const state = facetState[kind], control = get(`${kind}-${key}`);
    if (state[key] && !choices.has(state[key])) state[key] = "";
    control.replaceChildren(new Option(`${title}（${choices.size}）`, ""), ...[...choices].sort((a, b) => a[1].label.localeCompare(b[1].label, "zh-CN")).map(([value, choice]) => new Option(`${choice.label} · ${choice.count} 套餐`, value)));
    control.value = state[key]; control.disabled = actionRunning || !rows.length || disabled;
    return state[key] ? rows.filter(row => facetValue(row, key) === state[key]) : rows;
  }
  function resetFacetFilters(kind) {
    for (const key of Object.keys(facetState[kind])) facetState[kind][key] = "";
    get("catalog-search").value = ""; get("catalog-stock").value = "all";
    page = 0; selected.clear();
  }
  function renderBwhFilters() {
    let rows = data?.rows || [];
    rows = renderFacetSelect("bwh", "region", rows, "全部地区");
    rows = renderFacetSelect("bwh", "datacenter", rows, "全部机房");
    get("bwh-reset").disabled = actionRunning;
  }
  function renderDmitFilters() {
    const regions = data?.scopes || knownScopes.get("dmit") || [];
    get("dmit-region").replaceChildren(...(regions.length ? regions.map(row => new Option(row.id === "all" ? "全部地区 / 全部套餐" : row.name, row.id)) : [new Option("等待读取地区", "")]));
    get("dmit-region").value = scope;
    get("dmit-region").disabled = actionRunning || loading || !regions.length;
    let rows = data?.rows || [];
    rows = renderFacetSelect("dmit", "network", rows, "全部网络类型");
    rows = renderFacetSelect("dmit", "hardware", rows, "全部处理器平台");
    get("dmit-reset").disabled = actionRunning;
  }
  const akileNodeKey = (row) => row.key.split(":").slice(0, 3).join(":");
  const regionNames = new Intl.DisplayNames(["zh-CN"], { type: "region" });
  const regionLabel = (code) => {
    if (!code) return "未标注地区";
    try { return /^[A-Z]{2}$/.test(code) ? `${regionNames.of(code)} · ${code}` : code; }
    catch { return code; }
  };
  let pollTimer, autoTimer;
  let routeConfig = null;
  let tableSignature = "";
  let filterGeneration = null;
  const selected = new Set();
  const knownScopes = new Map();
  const pageSize = 30;
  const catalogAutoKey = "hostwatch-catalog-auto-v2";
  const savedCatalogSchedule = config.catalog_schedule || {};
  const autoValue = savedCatalogSchedule.mode === "auto" ? String(savedCatalogSchedule.interval_seconds || 300) : "0";
  get("catalog-auto").value = ["0", "60", "300", "600"].includes(autoValue) ? autoValue : "0";
  const priceSortKey = "hostwatch-catalog-price-sort";
  const defaultCatalogHelp = get("catalog-help").textContent;

  function matches(row, item) {
    const watch = row.watch;
    if (item.adapter !== watch.adapter) return false;
    const equal = (a, b) => String(a || "").trim().toLowerCase() === String(b || "").trim().toLowerCase();
    if (watch.adapter === "dmit") return Boolean(item.plan_key && watch.plan_key && equal(item.plan_key, watch.plan_key) || item.pid && watch.pid && equal(item.pid, watch.pid));
    if (watch.adapter === "bwh") return equal(item.bwh_offer_key || item.selector, watch.bwh_offer_key || watch.selector);
    if (watch.adapter === "akile") return (item.area_mode !== "manual" || equal(item.area_id, watch.area_id)) && equal(item.node_name, watch.node_name) && equal(item.selector, watch.selector);
    if (watch.adapter === "narwhal") return equal(item.machine_id, watch.machine_id) && equal(item.plan_id, watch.plan_id);
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
  function narwhalRows() {
    const query = get("narwhal-machine-search").value.trim().toLowerCase();
    return (data?.rows || []).filter(row => (!narwhalRegion || (row.region || "unknown") === narwhalRegion) &&
      (!query || `${row.line} ${row.watch.machine_id}`.toLowerCase().includes(query)) &&
      (!narwhalMachine || row.watch.machine_id === narwhalMachine));
  }

  function akileRows() {
    const query = get("akile-node-search").value.trim().toLowerCase();
    return (data?.rows || []).filter(row => (!akileArea || row.watch.area_id === akileArea) &&
      (!query || row.line.toLowerCase().includes(query)) && (!akileNode || akileNodeKey(row) === akileNode));
  }

  function renderAkileFilters() {
    const rows = data?.rows || [], areas = new Map(), nodes = new Map();
    for (const row of rows) {
      const id = row.watch.area_id, key = akileNodeKey(row);
      const area = areas.get(id) || { name: row.region || `地区 ${id}`, plans: 0, nodes: new Set() };
      area.plans++; area.nodes.add(key); areas.set(id, area);
      if (akileArea && id !== akileArea) continue;
      const node = nodes.get(key) || { name: row.line, area: area.name, count: 0 };
      node.count++; nodes.set(key, node);
    }
    if (akileArea && !areas.has(akileArea)) { akileArea = ""; akileNode = ""; return renderAkileFilters(); }
    get("akile-region").replaceChildren(new Option(`全部地区（${areas.size}）`, ""), ...[...areas].map(([id, area]) => new Option(`${area.name} · ${area.nodes.size} 节点 / ${area.plans} 套餐`, id)));
    get("akile-region").value = akileArea;
    const query = get("akile-node-search").value.trim().toLowerCase();
    const choices = [...nodes].filter(([, node]) => !query || node.name.toLowerCase().includes(query)).sort((a, b) => a[1].name.localeCompare(b[1].name, "zh-CN"));
    if (akileNode && !choices.some(([key]) => key === akileNode)) akileNode = "";
    get("akile-node").replaceChildren(new Option(`全部节点（${choices.length}）`, ""), ...choices.map(([key, node]) => new Option(`${node.name} · ${node.area} · ${node.count} 套餐（节点 ${key.split(":")[2]}）`, key)));
    get("akile-node").value = akileNode;
    get("akile-region").disabled = actionRunning || !rows.length;
    get("akile-node").disabled = actionRunning || !rows.length;
    get("akile-node-search").disabled = actionRunning;
    get("akile-reset").disabled = actionRunning;
  }

  function resetAkileFilters() {
    akileArea = ""; akileNode = "";
    get("akile-node-search").value = "";
    get("catalog-search").value = ""; get("catalog-stock").value = "all";
    page = 0; selected.clear();
  }

  function renderDirectoryFilters() {
    const active = provider === "narwhal";
    get("narwhal-directory").hidden = !active;
    get("akile-directory").hidden = provider !== "akile";
    get("bwh-directory").hidden = provider !== "bwh";
    get("dmit-directory").hidden = provider !== "dmit";
    get("catalog-scope").closest(".catalog-filters").className = `catalog-filters provider-${["bwh", "dmit", "akile", "narwhal"].includes(provider) ? provider : "standard"}`;
    get("catalog-scope").closest("label").hidden = hierarchicalDirectory();
    const visibleFilters = {
      "bwh-region": provider === "bwh", "bwh-datacenter": provider === "bwh",
      "dmit-region": provider === "dmit", "dmit-network": provider === "dmit", "dmit-hardware": provider === "dmit",
      "akile-region": provider === "akile", "akile-node-search": provider === "akile",
      "narwhal-region": active, "narwhal-machine-search": active
    };
    for (const [id, visible] of Object.entries(visibleFilters)) get(id).closest("label").hidden = !visible;
    document.querySelector(".catalog-table").classList.toggle("narwhal-table", active);
    document.querySelector(".catalog-table th:nth-child(2)").textContent = active ? "实例 / 套餐" : "套餐";
    document.querySelector(".catalog-table th:nth-child(3)").textContent = active ? "地区" : provider === "akile" ? "地区 / 节点" : provider === "bwh" ? "地区 / 机房" : provider === "dmit" ? "地区 / 网络 / 处理器" : "地区 / 线路";
    document.querySelector(".catalog-merchants").parentElement.hidden = currentPage === "narwhal";
    document.querySelector(".catalog-intro h2").textContent = active ? "发现 NAT 实例，关注套餐补货" : "发现套餐，关注补货";
    document.querySelector(".catalog-intro p").textContent = active ? "全部地区、实例与套餐统一检索，按指定实例和套餐加入监控或 Telegram 通知。" : "商家公开目录直接同步，分别选择库存监控或 Telegram 通知。无需先配置每一款商品。";
    get("catalog-search").placeholder = active ? "实例、套餐、地区或配置，例如 HK、香港、SoftBank" : provider === "akile" ? "套餐、节点、地区或配置，例如 HKL、香港" : "名称、地区、线路或配置";
    get("catalog-help").textContent = active ? "独角鲸云一次读取全部公开套餐，地区和实例筛选、套餐搜索使用同一份库存快照；支持全目录自动刷新。大盘自动刷新仅在此栏目打开且页面可见时运行。加入库存通知并开启通知自动刷新后，服务器会在关闭网页后继续检查指定套餐，通过 Telegram 提醒补货；大盘刷新本身不发送通知。" : defaultCatalogHelp;
    if (provider === "akile") {
      get("catalog-help").textContent = "Akile 一次读取全部地区、节点和套餐，地区、节点筛选与套餐搜索使用同一份库存快照；支持全目录自动刷新。大盘自动刷新仅在此栏目打开且页面可见时运行。将指定节点的套餐加入库存通知并开启通知自动刷新后，服务器会在关闭网页后继续检查，通过 Telegram 提醒补货；大盘刷新本身不发送通知。外币价格折算仅供参考。";
      renderAkileFilters();
    }
    if (provider === "bwh") {
      get("catalog-search").placeholder = "机房名称、机房 ID、地区或配置，例如 USCA_9、洛杉矶";
      get("catalog-help").textContent = "搬瓦工一次读取全部公开报价，按地区和机房筛选，配置套餐与计费周期在下方列表查看；同名机房使用机房 ID 区分。支持全目录自动刷新，筛选不重复请求商家。大盘刷新仅在页面可见时运行；已保存的库存通知按服务器的通知刷新设置检查补货。";
      renderBwhFilters();
    }
    if (provider === "dmit") {
      get("catalog-search").placeholder = "地区、网络、处理器平台或配置，例如 Eyeball、AN5、TINY";
      get("catalog-help").textContent = "DMIT 按地区、网络类型和处理器平台筛选，完整配置套餐显示在下方列表；AS3、AN4、AN5 等标识沿用官网命名，未提供的平台单独标注。筛选使用当前地区快照。全地区仅手动刷新，选择单地区后可自动刷新；有货仍须校验购买页，校验失败显示未知。已保存的库存通知按服务器的通知刷新设置检查补货。";
      renderDmitFilters();
    }
    if (!active) return;
    const rows = data?.rows || [], regions = new Map(), machines = new Map();
    for (const row of rows) {
      const code = row.region || "unknown";
      const region = regions.get(code) || { plans: 0, machines: new Set() };
      region.plans++; region.machines.add(row.watch.machine_id); regions.set(code, region);
      if (narwhalRegion && code !== narwhalRegion) continue;
      const machine = machines.get(row.watch.machine_id) || { name: row.line, count: 0 };
      machine.count++; machines.set(row.watch.machine_id, machine);
    }
    if (narwhalRegion && !regions.has(narwhalRegion)) { narwhalRegion = ""; narwhalMachine = ""; return renderDirectoryFilters(); }
    get("narwhal-region").replaceChildren(new Option(`全部地区（${regions.size}）`, ""), ...[...regions].sort((a, b) => b[1].plans - a[1].plans).map(([code, stats]) => new Option(`${regionLabel(code === "unknown" ? "" : code)} · ${stats.machines.size} 实例 / ${stats.plans} 套餐`, code)));
    get("narwhal-region").value = narwhalRegion;
    const query = get("narwhal-machine-search").value.trim().toLowerCase();
    const choices = [...machines].filter(([id, machine]) => !query || `${machine.name} ${id}`.toLowerCase().includes(query)).sort((a, b) => a[1].name.localeCompare(b[1].name, "zh-CN"));
    if (narwhalMachine && !choices.some(([id]) => id === narwhalMachine)) narwhalMachine = "";
    get("narwhal-machine").replaceChildren(new Option(`全部实例（${choices.length}）`, ""), ...choices.map(([id, machine]) => new Option(`${machine.name} · ${machine.count} 套餐（${id.slice(0, 8)}）`, id)));
    get("narwhal-machine").value = narwhalMachine;
    get("narwhal-region").disabled = actionRunning || !rows.length;
    get("narwhal-machine").disabled = actionRunning || !rows.length;
    get("narwhal-machine-search").disabled = actionRunning;
  }

  function resetNarwhalFilters() {
    narwhalRegion = ""; narwhalMachine = "";
    get("narwhal-machine-search").value = "";
    get("catalog-search").value = ""; get("catalog-stock").value = "all";
    page = 0; selected.clear();
  }

  function filtered() {
    const search = get("catalog-search").value.trim().toLowerCase(), stock = get("catalog-stock").value;
    const rows = (provider === "narwhal" ? narwhalRows() : provider === "akile" ? akileRows() : ["bwh", "dmit"].includes(provider) ? facetRows(provider) : data?.rows || []).filter((row) => (!search || [row.name, row.region, row.line, row.specs, row.traffic, row.price, ...(provider === "narwhal" ? [regionLabel(row.region), row.watch.machine_id, row.watch.plan_id] : provider === "bwh" ? [row.watch.bwh_datacenter_id] : provider === "dmit" ? [facetLabel(row, "network"), facetValue(row, "hardware")] : [])].join(" ").toLowerCase().includes(search)) &&
      (stock === "all" || stock === "in" && row.available > 0 || stock === "out" && row.available === 0 || stock === "unknown" && row.available === null || stock === "watched" && existing(row) && membership(existing(row), "watch") || stock === "notified" && existing(row) && membership(existing(row), "notify")));
    return sortByMonthlyPrice(rows, readPriceSort(priceSortKey), row => row.price);
  }

  function renderPriceHeading() {
    const heading = document.querySelector("#catalog-price-heading");
    if (!heading) return;
    heading.replaceChildren(createPriceSortButton("价格 / 周期", priceSortKey, () => { page = 0; renderTable(true); }));
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
    let note = dirty ? "选择已修改，正在自动保存。" : "选择已保存。";
    if (!config.enabled || monitorService?.active !== "active") note += "后台自动刷新尚未运行，请检查监控 / 通知的独立刷新设置。修改后会自动保存。";
    else note += "HostWatch 服务已按保存的自动刷新设置运行，关闭网页后继续检查。";
    if (notifications && (!config.telegram?.configured || config.telegram?.disabled)) note += "请在 Telegram 通知中配置并启用机器人后保存。";
    if (config.enabled && !config.products.some(p => p.enabled)) note += "已取消全部商品，保存后自动刷新服务会停止。";
    get("catalog-reminder-note").textContent = note;
  }

  function renderTable(animate = false) {
    if (animate) animateFilterResult(get("catalog-results"));
    renderPriceHeading();
    const rows = filtered();
    if (provider === "bwh") get("bwh-filter-note").textContent = `全目录 ${new Set((data?.rows || []).map(row => facetValue(row, "datacenter"))).size} 个机房 / ${data?.rows.length || 0} 个套餐报价 · 当前筛选 ${new Set(rows.map(row => facetValue(row, "datacenter"))).size} 个机房 / ${rows.length} 个套餐报价。不同计费周期分别计数。`;
    if (provider === "dmit") get("dmit-filter-note").textContent = `当前${scope === "all" ? "全部地区" : "地区 / 分类"}快照 ${data?.rows.length || 0} 个套餐 · 筛选后 ${new Set(rows.map(row => facetValue(row, "network"))).size} 种网络 / ${new Set(rows.map(row => facetValue(row, "hardware"))).size} 种处理器平台 / ${rows.length} 个套餐。`;
    if (provider === "akile") get("akile-filter-note").textContent = `全目录 ${new Set((data?.rows || []).map(akileNodeKey)).size} 个节点 / ${data?.rows.length || 0} 个套餐 · 当前筛选 ${new Set(rows.map(akileNodeKey)).size} 个节点 / ${rows.length} 个套餐。筛选在已读取的全目录中完成。`;
    if (provider === "narwhal") get("narwhal-filter-note").textContent = `全目录 ${new Set((data?.rows || []).map(row => row.watch.machine_id)).size} 个实例 / ${data?.rows.length || 0} 个套餐 · 当前筛选 ${new Set(rows.map(row => row.watch.machine_id)).size} 个实例 / ${rows.length} 个套餐。筛选在已读取的全目录中完成。`;
    page = Math.max(0, Math.min(page, Math.ceil(rows.length / pageSize) - 1));
    const visible = rows.slice(page * pageSize, (page + 1) * pageSize);
    const hasSnapshot = Boolean(data?.fetched_at);
    get("catalog-empty").hidden = rows.length > 0;
    get("catalog-empty").textContent = !hasSnapshot ? (loading || task?.provider === provider && task.pending ? "正在读取目录，尚未取得库存结果…" : task?.provider === provider && task.error ? "目录读取失败，请根据上方错误检查出口或请求方式后重试。" : "尚无目录数据，点击“刷新目录库存”读取。") : data.rows.length ? "没有符合筛选条件的商品。" : "该分类暂无公开套餐。";
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
      const identity = cell();
      identity.append(text("strong", provider === "narwhal" ? row.line : row.name), text("small", provider === "narwhal" ? `套餐：${row.name}` : row.provider));
      const region = cell(); region.append(text("span", provider === "narwhal" ? regionLabel(row.region) : row.region || "—"));
      if (provider === "bwh") region.append(text("small", `${row.line} · ${facetValue(row, "datacenter")}`));
      else if (provider === "dmit") region.append(text("small", `${facetLabel(row, "network")} · ${facetValue(row, "hardware")}`));
      else if (provider !== "narwhal" && row.line && row.line !== row.region) region.append(text("small", row.line));
      const hardware = cell();
      const metadata = productSnapshot({ adapter: row.watch.adapter, snapshot: row });
      if (metadata.specs) renderSpecs(hardware, metadata.specs);
      if (metadata.traffic) { const network = text("div", "", "catalog-network"); renderSpecs(network, metadata.traffic, true); hardware.append(network); }
      if (!metadata.specs && !metadata.traffic) hardware.textContent = "商家未提供配置";
      renderPrice(cell(), row.price || "以商家页面为准", expanded.has(row.key));
      const stock = cell();
      stock.append(text("span", row.available === null ? "未知" : row.available > 0 ? (row.stock_mode === "availability" ? "有货" : `有货 ${row.available}`) : "缺货", `state ${row.available === null ? "pending" : row.available > 0 ? "ok" : "error"}`));
      if (row.available > 0 && row.stock_mode === "availability") stock.append(text("small", "数量未公开"));
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
        if (url.protocol === "https:" && ["app.vmiss.com", "www.vps.soy", "akile.ai", "dash.fuckip.me", "isvoro.com", "www.dmit.io", "bwh81.net", "www.bwh81.net", "bandwagonhost.com", "www.bandwagonhost.com"].includes(url.hostname) && !url.username && !url.password) {
          const link = text("a", "直接购买", "buy-link"); link.href = affiliatePurchaseURL(url.href, row.watch.adapter, row.watch); link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link);
        }
      }
      cell().append(actions);
      return tr;
    }));
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
    renderDirectoryFilters();
    document.querySelectorAll("[data-catalog-provider]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.catalogProvider === provider)));
    const scopes = data?.scopes || knownScopes.get(provider) || [];
    get("catalog-scope").replaceChildren(...(scopes.length ? scopes.map((row) => new Option(row.id === "all" ? "全部公开套餐（手动）" : row.name, row.id)) : [new Option("等待目录", "")]));
    get("catalog-scope").value = scope;
    get("catalog-scope").disabled = actionRunning || loading || !scopes.length;
    const fullCatalog = scope === "all" && !fullDirectory();
    get("catalog-auto").disabled = actionRunning || fullCatalog;
    get("catalog-auto").title = fullCatalog ? "全部公开套餐只支持手动刷新，避免重复读取多个分类" : fullDirectory() ? "全目录可按设定间隔自动刷新，筛选共用一次读取" : "分类目录可按设定间隔自动刷新";
    const busy = Boolean(task?.pending);
    get("catalog-refresh").disabled = actionRunning || busy || loading;
    get("catalog-stop").hidden = !busy;
    get("catalog-stop").disabled = actionRunning || Boolean(task?.cancelled);
    get("catalog-stop").textContent = task?.cancelled ? "停止中…" : "停止刷新";
    get("catalog-summary").textContent = data?.fetched_at ? `${providers[provider]} · ${data.rows.length} 个套餐 · ${data.rows.filter((row) => row.available > 0).length} 个有货` : `${providers[provider]} · 尚未取得库存结果`;
    get("catalog-updated").textContent = data?.fetched_at ? `数据更新时间：${new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", dateStyle: "medium", timeStyle: "medium" }).format(new Date(data.fetched_at))}` : "尚无库存快照";
    const status = get("catalog-status");
    const savedRoute = merchantRequestPolicy(savedConfig ? JSON.parse(savedConfig) : config, provider);
    const effectiveRoute = route && routeConfig === savedConfig ? route : savedRoute;
    const changed = merchantRequestChanged(provider);
    const routeHint = get("catalog-route");
    const hints = [`${providers[provider]} 已保存的读取设置：${requestRouteLabel(effectiveRoute)}。`];
    if (provider === "dmit") hints.push("有货须通过购买页校验；价格页可购买但下单页缺货时统一显示缺货，验证失败显示未知。不同时间的快照可能变化。");
    if (changed) hints.push("此商家的出口或请求设置尚未保存；当前刷新仍使用上面的已保存设置，修改后会自动保存。");
    if (effectiveRoute.error) hints.push(effectiveRoute.error);
    if (busy && task.provider === provider && task.route) hints.push(`本次刷新使用：${requestRouteLabel(task.route)}。执行期间修改设置将在下次刷新生效。`);
    if (data?.fetched_at) {
      const sameRoute = data.route && ["mode", "node_id", "public_ip", ...(effectiveRoute.mode === "flaresolverr" ? ["timeout_seconds", "wait_seconds", "disable_media"] : [])].every(key => data.route[key] === effectiveRoute[key]);
      if (!data.route) hints.push("此历史快照未记录出口，请刷新以核对当前线路。");
      else if (!sameRoute) hints.push(`当前展示的是历史快照，采集设置：${requestRouteLabel(data.route)}。请刷新后核对当前读取设置的结果。`);
    }
    routeHint.textContent = hints.join(" ");
    routeHint.className = `hint${effectiveRoute.error ? " error" : changed ? " stale" : ""}`;
    const stale = data && Date.now() - Date.parse(data.fetched_at) > 300000;
    status.className = "hint";
    if (busy) { status.classList.add("pending"); status.textContent = task.cancelled ? "正在终止目录刷新，保留上次快照…" : `正在刷新 ${providers[task.provider] || "商家"}${task.provider === "narwhal" ? "全部实例与套餐" : task.provider === "akile" ? "全部地区、节点与套餐" : task.provider === "bwh" ? "全部地区、机房与套餐" : task.scope === "all" ? "全部公开套餐（会读取多个分类）" : "目录"}，页面仍可操作；可点击“停止刷新”。`; }
    else if (task?.provider === provider && task.error) { status.classList.add("error"); status.textContent = task.error; }
    else if (task?.provider === provider && task.cancelled) status.textContent = "刷新已停止，保留上次快照。";
    else if (loading) status.textContent = "正在读取目录缓存…";
    else if (metadataOutdated) { status.classList.add("stale"); status.textContent = provider === "narwhal" ? "此目录为旧版快照，请刷新以读取全部地区、实例与套餐。" : provider === "dmit" ? "此分类为旧版快照，有货记录尚未校验购买页，请刷新目录库存。" : "此分类为旧版快照，带宽信息尚未更新，请刷新分类库存。"; }
    else { status.textContent = data ? `已读取${provider === "narwhal" ? "全部实例与套餐" : provider === "akile" ? "全部地区、节点与套餐" : provider === "bwh" ? "全部地区、机房与套餐" : scope === "all" ? "全部公开套餐（仅手动刷新）" : "此分类"}。${data.unknown ? `${data.unknown} 个套餐库存未知，未当作缺货或现货。` : ""}${stale ? "当前为历史快照，请刷新后核对。" : "库存以此快照的检查时间为准。"}` : "选择商家后刷新公开套餐目录，无需先开启自动监控。"; if (stale) status.classList.add("stale"); }
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
      if (row.watch.adapter === "dmit") item.snapshot.stock_version = data?.stock_version || 0;
      if (membership(item, type) !== enabled) { setMembership(item, type, enabled); changed++; }
    }
    renderProducts(); updateDraftState(); renderTable();
    if (changed) {
      scheduleAutoSave(type === "watch" ? "monitor" : "notifications");
      setMessage(`已${enabled ? "加入" : "移出"} ${changed} 个商品的${type === "watch" ? "库存监控（不通知）" : "库存通知"}，正在自动保存。`, "pending", true, false);
    } else setMessage("选择无需更改。", "success");
  }

  function schedule() {
    clearTimeout(pollTimer); clearTimeout(autoTimer);
    if (!catalogPage() || document.hidden) return;
    if (task?.pending) pollTimer = setTimeout(() => loadSnapshot(false), 1200);
    else if ((scope !== "all" || fullDirectory()) && !task?.error && !task?.cancelled && Number(get("catalog-auto").value)) autoTimer = setTimeout(() => refresh(true), Number(get("catalog-auto").value) * 1000);
  }

  async function loadSnapshot(refreshMissing = true) {
    const version = generation, requestedProvider = provider, requestedScope = scope;
    loading = true; render();
    try {
      const result = await requestJSON(`/api/catalog?${new URLSearchParams({ provider: requestedProvider, scope: requestedScope })}`, { timeoutMs: 10000 });
      if (version !== generation || !catalogPage()) return;
      data = result.data; task = result.task; route = result.route; routeConfig = savedConfig; metadataOutdated = Boolean(result.needs_refresh);
      if (data) {
        scope = data.scope;
        knownScopes.set(provider, data.scopes);
        const keys = new Set(data.rows.map((row) => row.key));
        for (const key of selected) if (!keys.has(key)) selected.delete(key);
      }
      if ((!data || metadataOutdated) && !task?.pending && refreshMissing && !(task?.provider === provider && (!task.scope || !scope || task.scope === scope) && (task.error || task.cancelled))) {
        loading = false; await refresh(true); return;
      }
    } catch (error) {
      if (version !== generation) return;
      task = { provider, pending: false, error: `读取目录失败：${error.message} 可点击刷新重试。` };
    } finally {
      if (version === generation) { loading = false; render(); schedule(); }
    }
  }

  async function refresh(auto = false) {
    if (loading || task?.pending || !catalogPage() || document.hidden || actionRunning) { schedule(); return; }
    const version = generation;
    loading = true; render();
    try {
      const result = await requestJSON("/api/catalog/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider, scope, trigger: auto ? "auto" : "manual" }), timeoutMs: 10000 });
      if (version === generation) task = result;
    } catch (error) { if (version === generation) task = { provider, pending: false, error: error.message }; }
    finally { if (version === generation) { loading = false; render(); schedule(); } }
  }

  document.querySelectorAll("[data-catalog-provider]").forEach((button) => button.addEventListener("click", () => {
    if (provider === button.dataset.catalogProvider) return;
    generation++; provider = button.dataset.catalogProvider; scope = fullDirectory() ? "all" : ""; data = null; route = null; metadataOutdated = false; page = 0; selected.clear();
    resetNarwhalFilters();
    resetAkileFilters();
    resetFacetFilters("bwh"); resetFacetFilters("dmit");
    filterGeneration = generation;
    sessionStorage.setItem("hostwatch-catalog-provider", provider);
    get("catalog-search").value = ""; get("catalog-stock").value = "all";
    void loadSnapshot();
    animateFilterResult(get("catalog-results"));
  }));
  function catalogPage() { return currentPage === "catalog" || currentPage === "narwhal"; }
  function changeScope(value) {
    generation++; scope = value; page = 0; selected.clear();
    filterGeneration = generation;
    const scopes = data?.scopes || knownScopes.get(provider) || []; data = { provider, scope, scopes, rows: [] }; metadataOutdated = false;
    void loadSnapshot();
    animateFilterResult(get("catalog-results"));
  }
  get("catalog-scope").addEventListener("change", () => changeScope(get("catalog-scope").value));
  get("dmit-region").addEventListener("change", () => { resetFacetFilters("dmit"); changeScope(get("dmit-region").value); });
  for (const kind of ["bwh", "dmit"]) {
    const keys = Object.keys(facetState[kind]);
    keys.forEach((key, index) => get(`${kind}-${key}`).addEventListener("change", () => {
      facetState[kind][key] = get(`${kind}-${key}`).value;
      keys.slice(index + 1).forEach(child => { facetState[kind][child] = ""; });
      page = 0; selected.clear(); renderDirectoryFilters(); renderTable(true);
    }));
    get(`${kind}-reset`).addEventListener("click", () => {
      resetFacetFilters(kind);
      if (kind === "dmit" && scope !== "all" && (data?.scopes || []).some(row => row.id === "all")) changeScope("all");
      else render();
    });
  }
  for (const id of ["catalog-search", "catalog-stock"]) get(id).addEventListener(id === "catalog-search" ? "input" : "change", () => { page = 0; if (hierarchicalDirectory()) selected.clear(); renderTable(true); });
  for (const id of ["akile-region", "akile-node", "akile-node-search"]) get(id).addEventListener(id.endsWith("search") ? "input" : "change", () => {
    if (id === "akile-region") { akileArea = get(id).value; akileNode = ""; get("akile-node-search").value = ""; }
    if (id === "akile-node") akileNode = get(id).value;
    page = 0; selected.clear(); renderDirectoryFilters(); renderTable(true);
  });
  get("akile-reset").addEventListener("click", () => { resetAkileFilters(); render(); });
  for (const id of ["narwhal-region", "narwhal-machine", "narwhal-machine-search"]) get(id).addEventListener(id.endsWith("search") ? "input" : "change", () => {
    if (id === "narwhal-region") { narwhalRegion = get(id).value; narwhalMachine = ""; get("narwhal-machine-search").value = ""; }
    if (id === "narwhal-machine") narwhalMachine = get(id).value;
    page = 0; selected.clear(); renderDirectoryFilters(); renderTable(true);
  });
  get("narwhal-reset").addEventListener("click", () => { resetNarwhalFilters(); render(); });
  get("catalog-auto").addEventListener("change", () => {
    const value = Number(get("catalog-auto").value || 0);
    config.catalog_schedule = { mode: value ? "auto" : "manual", interval_seconds: value || 300 };
    sessionStorage.setItem(catalogAutoKey, get("catalog-auto").value);
    schedule();
    if (typeof updateDraftState === "function") updateDraftState();
    if (typeof scheduleAutoSave === "function") scheduleAutoSave("catalog");
  });
  get("catalog-refresh").addEventListener("click", () => { task = task?.pending ? task : null; void refresh(); });
  get("catalog-stop").addEventListener("click", async () => {
    get("catalog-auto").value = "0"; sessionStorage.setItem(catalogAutoKey, "0"); clearTimeout(autoTimer);
    if (typeof updateDraftState === "function") updateDraftState();
    if (typeof scheduleAutoSave === "function") scheduleAutoSave("catalog");
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
    if (event.detail === "narwhal") {
      provider = "narwhal"; scope = "all"; data = null; route = null; metadataOutdated = false; page = 0; selected.clear();
      resetNarwhalFilters();
      sessionStorage.setItem("hostwatch-catalog-provider", provider);
      document.querySelectorAll("[data-catalog-provider]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.catalogProvider === provider)));
      void loadSnapshot();
    } else if (event.detail === "catalog") void loadSnapshot();
  });
  for (const event of ["hostwatch-draft-change", "hostwatch-service-change"]) window.addEventListener(event, () => { if (catalogPage()) render(); });
  window.addEventListener("hostwatch-action-change", () => {
    if (!catalogPage()) return;
    render();
    if (!data && !loading && !task?.pending && !task?.error && !task?.cancelled) void loadSnapshot();
  });
  document.addEventListener("visibilitychange", () => { if (catalogPage() && !document.hidden) void loadSnapshot(false); else schedule(); });
  render();
  if (catalogPage()) void loadSnapshot();
})();
