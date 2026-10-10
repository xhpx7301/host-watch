let config = { enabled: false, interval_seconds: 600, notify_current_stock_on_start: false, affiliate_id: "", products: [] };
const productsNode = document.querySelector("#products");
const messageNode = document.querySelector("#message");

function text(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
}

function field(labelText, value, key, placeholder = "") {
  const label = text("label", labelText);
  const input = document.createElement("input");
  input.value = value || "";
  input.placeholder = placeholder;
  input.dataset.key = key;
  label.append(input);
  return label;
}

function productCard(item, index) {
  const article = text("article", "", "product");
  const head = text("div", "", "product-head");
  head.append(text("strong", item.name || `新商品 ${index + 1}`));
  const controls = text("div", "", "actions");
  const enabledLabel = text("label", "啟用", "check");
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.checked = Boolean(item.enabled);
  enabled.dataset.key = "enabled";
  enabledLabel.prepend(enabled);
  const remove = text("button", "移除", "danger");
  remove.type = "button";
  remove.addEventListener("click", () => { config.products.splice(index, 1); renderProducts(); });
  controls.append(enabledLabel, remove);
  head.append(controls);

  const grid = text("div", "", "grid");
  grid.append(
    field("商品显示名称", item.name, "name", "US.LA.TRI.Basic"),
    field("商品选择标识（slug / 完整名称 / product ID）", item.selector, "selector", "basic 或 product44"),
    field("商品分类页 URL", item.category_url, "category_url", "https://app.vmiss.com/store/us-los-angeles-tri"),
  );
  const targetLabel = text("label", "推广跳转类型");
  const select = document.createElement("select");
  select.dataset.key = "target";
  for (const [value, label] of [["pid", "直达单个商品（pid）"], ["gid", "打开商品组（gid）"]]) {
    const option = document.createElement("option"); option.value = value; option.textContent = label;
    select.append(option);
  }
  select.value = item.pid ? "pid" : "gid";
  targetLabel.append(select);
  const targetId = field(select.value === "pid" ? "pid（商品 ID）" : "gid（商品组 ID）", item.pid || item.gid, "target_id", "例如 44");
  select.addEventListener("change", () => {
    targetId.firstChild.textContent = select.value === "pid" ? "pid（商品 ID）" : "gid（商品组 ID）";
    updatePreview(article);
  });
  grid.append(targetLabel, targetId);
  const preview = text("div", "", "preview");
  preview.dataset.preview = "true";
  article.append(head, grid, preview);
  article.querySelectorAll("input,select").forEach((input) => input.addEventListener("input", () => updatePreview(article)));
  updatePreview(article);
  return article;
}

function updatePreview(article) {
  const value = (key) => article.querySelector(`[data-key="${key}"]`)?.value || "";
  const kind = article.querySelector('[data-key="target"]')?.value || "gid";
  const id = value("target_id");
  const aff = document.querySelector("#affiliate").value.trim();
  const link = new URL("https://app.vmiss.com/aff.php");
  if (aff) link.searchParams.set("aff", aff);
  if (id) link.searchParams.set(kind, id);
  article.querySelector("[data-preview]").textContent = `通知按钮预览：${link.toString()}`;
  article.querySelector(".product-head strong").textContent = value("name") || "未命名商品";
  article.querySelectorAll(".grid label")[3].firstChild.textContent = kind === "pid" ? "pid（商品 ID）" : "gid（商品组 ID）";
}

function renderProducts() {
  productsNode.replaceChildren(...config.products.map(productCard));
  productsNode.querySelectorAll("input,select").forEach((input) => {
    const sync = () => {
    const article = input.closest(".product");
    const index = [...productsNode.children].indexOf(article);
    if (index < 0) return;
    const item = config.products[index];
    if (input.dataset.key === "target") {
      const targetInput = article.querySelector('[data-key="target_id"]');
      targetInput.closest("label").firstChild.textContent = input.value === "pid" ? "pid（商品 ID）" : "gid（商品组 ID）";
    } else if (input.dataset.key === "enabled") item.enabled = input.checked;
    else if (input.dataset.key === "target_id") { item.pid = input.closest(".product").querySelector('[data-key="target"]').value === "pid" ? input.value : ""; item.gid = item.pid ? "" : input.value; }
    else item[input.dataset.key] = input.value;
    updatePreview(article);
    };
    input.addEventListener("input", sync);
    input.addEventListener("change", sync);
  });
}

async function loadConfig() {
  const response = await fetch("/api/config", { cache: "no-store" });
  if (!response.ok) throw new Error("读取配置失败");
  config = await response.json();
  document.querySelector("#enabled").checked = Boolean(config.enabled);
  document.querySelector("#interval").value = config.interval_seconds || 600;
  document.querySelector("#affiliate").value = config.affiliate_id || "";
  document.querySelector("#notify-current").checked = Boolean(config.notify_current_stock_on_start);
  const telegram = config.telegram || {};
  document.querySelector("#telegram-token").value = "";
  document.querySelector("#telegram-chat-id").value = telegram.chat_id || "";
  document.querySelector("#telegram-disabled").checked = Boolean(telegram.disabled);
  document.querySelector("#telegram-clear").checked = false;
  document.querySelector("#telegram-status").textContent = telegram.configured
    ? "Telegram 已配置（token 不会回显）。"
    : (telegram.disabled ? "Telegram 已停用。" : "Telegram 尚未完整配置。保存后才会生效。 ");
  document.querySelector("#telegram-chats").replaceChildren();
  renderProducts();
  messageNode.textContent = "配置已载入。保存商品设置不会自动访问 VMISS。";
}

async function loadStatus() {
  const response = await fetch("/api/status", { cache: "no-store" });
  const status = await response.json();
  const state = document.querySelector("#state");
  state.textContent = ({ ok: "最近检查成功", blocked: "因网站拦截已停止", error: "检查出错", unknown: "尚无检查记录" })[status.state] || status.state;
  state.className = `pill ${status.state || ""}`;
  document.querySelector("#last-checked").textContent = status.last_checked ? `上次检查：${status.last_checked}` : "";
}

document.querySelector("#add").addEventListener("click", () => {
  config.products.push({ id: crypto.randomUUID(), name: "", category_url: "", selector: "", gid: "", pid: "", enabled: false });
  renderProducts();
});
document.querySelector("#affiliate").addEventListener("input", () => productsNode.querySelectorAll(".product").forEach(updatePreview));
document.querySelector("#reload").addEventListener("click", () => loadConfig().catch((error) => { messageNode.textContent = error.message; }));
document.querySelector("#refresh-status").addEventListener("click", () => loadStatus().catch((error) => { messageNode.textContent = error.message; }));
document.querySelector("#telegram-test").addEventListener("click", async () => {
  const button = document.querySelector("#telegram-test");
  button.disabled = true;
  try {
    const response = await fetch("/api/telegram/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        bot_token: document.querySelector("#telegram-token").value.trim(),
        chat_id: document.querySelector("#telegram-chat-id").value.trim(),
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Telegram 测试失败");
    messageNode.textContent = "Telegram 测试消息已发送，请检查目标聊天。";
  } catch (error) { messageNode.textContent = error.message; }
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
    if (!result.chats.length) {
      chatsNode.append(text("p", "暂时没有找到聊天。请先打开 bot 并发送 /start 或一条消息，再点击读取；也可以直接填写 Chat ID。", "hint"));
      return;
    }
    for (const chat of result.chats) {
      const option = text("button", "", "chat-option");
      option.type = "button";
      option.append(
        text("span", `${chat.name}（${chat.type}）`),
        text("span", chat.id),
      );
      option.addEventListener("click", () => {
        document.querySelector("#telegram-chat-id").value = chat.id;
        messageNode.textContent = `已选择 ${chat.name}。保存配置后生效。`;
      });
      chatsNode.append(option);
    }
    messageNode.textContent = `已读取 ${result.chats.length} 个聊天，点击一项可填入 Chat ID。`;
  } catch (error) { messageNode.textContent = error.message; }
  finally { button.disabled = false; }
});
document.querySelector("#save").addEventListener("click", async () => {
  const products = [...productsNode.children].map((article) => {
    const get = (key) => article.querySelector(`[data-key="${key}"]`);
    const target = get("target").value;
    const targetId = get("target_id").value.trim();
    return {
      id: config.products[[...productsNode.children].indexOf(article)].id,
      name: get("name").value.trim(), selector: get("selector").value.trim(), category_url: get("category_url").value.trim(),
      gid: target === "gid" ? targetId : "", pid: target === "pid" ? targetId : "", enabled: get("enabled").checked,
    };
  });
  const next = {
    enabled: document.querySelector("#enabled").checked,
    interval_seconds: Number(document.querySelector("#interval").value),
    affiliate_id: document.querySelector("#affiliate").value.trim(),
    notify_current_stock_on_start: document.querySelector("#notify-current").checked,
    telegram: {
      bot_token: document.querySelector("#telegram-token").value.trim(),
      chat_id: document.querySelector("#telegram-chat-id").value.trim(),
      disabled: document.querySelector("#telegram-disabled").checked,
      clear: document.querySelector("#telegram-clear").checked,
    },
    products,
  };
  try {
    const response = await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(next) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "保存失败");
    config = result.config || next;
    document.querySelector("#telegram-token").value = "";
    await loadConfig();
    messageNode.textContent = "配置已保存。若启用监控，请在获准的访问方式下手动启动/重启监控服务。";
  } catch (error) { messageNode.textContent = error.message; }
});
loadConfig().catch((error) => { messageNode.textContent = error.message; });
loadStatus().catch(() => {});
