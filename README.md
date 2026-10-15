# HostWatch 服务器库存监控

[GitHub 仓库](https://github.com/xhpx7301/host-watch)

自托管的多厂商库存监控脚本。支持 VMISS 专用套餐解析和通用公开 HTTPS 商品页；发现库存增加或由缺货转为有货时，可通过 Telegram 发送提醒与人工购买入口。程序不会登录厂商账户、绕过验证或自动下单。

VMISS 曾对自动化访问返回拦截页。请先确认目标厂商允许自动检查及访问频率。遇到 HTTP 403、429 或安全验证页时，程序会记录状态、尝试告警并停止，不会自动重试。FlareSolverr 仅适用于你拥有或明确获准测试的站点，默认关闭。检查间隔不能低于 5 分钟。

## 支持的监控方式

- **VMISS WHMCS**：解析商品卡片和库存数量，可生成 `aff.php` 推广入口。
- **通用 HTTPS 网页**：配置 CSS 库存元素选择器，支持提取数量或匹配有货/缺货文本；购买入口可单独设置，留空时使用商品页。

通用网页适配器适合服务器返回 HTML 的公开商品页面，不执行 JavaScript，也不登录账户。页面结构若由前端脚本动态生成，或库存来自私有 API，需要后续为该厂商添加专用适配器。所有购买动作由用户在收到通知后手动完成。

## 一键安装

适用于 Debian 12/13，要求 systemd 和 root/sudo 权限。脚本会安装依赖、创建 `vmiss-monitor` 系统用户、安装程序和 systemd 单元，并启动仅监听 `127.0.0.1:8765` 的管理页。库存监控默认关闭，安装脚本不会启用或启动它。

```sh
curl -fsSL https://raw.githubusercontent.com/xhpx7301/host-watch/main/install.sh | sudo bash
```

脚本可重复执行用于升级，已有的 `/var/lib/vmiss-monitor/config.json`、`/etc/vmiss-monitor.env` 和 `/etc/vmiss-monitor-admin.env` 会保留。

项目已更名为 HostWatch，仓库名为 `host-watch`。为兼容已部署版本，继续沿用 `vmiss-monitor` 安装目录、系统用户和 systemd 服务名，以及 `/etc/stockwatch-users.json`、`/var/backups/stockwatch/` 等数据路径；菜单快捷命令改为 `hw`。

## Debian 管理菜单

安装或升级完成后，在 Debian 12/13 终端运行：

```sh
hw
```

直接输入 `hw` 即可调出菜单；普通用户会自动通过 sudo 请求权限，root 无需 sudo。`host-watch` 命令也可调用同一菜单，原 `stockwatch` 命令继续兼容。菜单支持查看状态、更新脚本、启动/停止/重启库存监控、重启管理页、查看最近 100 条日志、管理管理页用户和备份。也可以直接执行：

旧版使用 `jk` 的服务器，重新执行一键安装后改用 `hw`；安装器会移除内容与本项目旧启动脚本完全一致的 `jk` 文件，不会删除其他同名脚本。

```sh
hw update       # 下载更新，先备份，再安装
hw backup       # 备份程序、配置和账号
hw status       # 查看服务状态
hw users        # 列出管理页用户
hw add-user     # 交互式添加用户
hw passwd       # 交互式修改密码
hw delete-user  # 交互式删除用户
```

这里的用户是**管理页管理员**，都有完整配置权限，不是 Debian/SSH 系统账号。密码在终端中隐藏输入，不支持通过命令行参数传递。账号保存于 `/etc/stockwatch-users.json`，使用独立随机盐和 PBKDF2-SHA256 哈希，权限为 `root:vmiss-monitor 0640`。不能删除最后一个管理员。

首次创建账号后，菜单会将旧 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 配置替换为 `ADMIN_USERS_FILE`，保留域名等其他设置，并重启管理页。以后修改密码或删除账号后，旧凭据在下一次请求即失效。仅本机 SSH 隧道访问的旧安装可继续沿用原认证方式；域名反向代理必须配置账号或原有密码。

更新使用 GitHub `main` 的安装程序，保留商品、Telegram 和账号配置；成功后仅恢复更新前正在运行的监控服务。更新失败不会自动恢复监控，终端会输出备份路径。备份位于 `/var/backups/stockwatch/`，仅 root 可访问；其中包含配置凭据，应按敏感文件保管。备份不包含虚拟环境，恢复程序后可重新安装依赖。菜单不提供自动恢复操作。

首次启动监控前，仍需在管理页勾选“启用库存检查”并保存。停止服务会取消监控开机自启，商品配置保持原样。管理菜单同一时间只允许一个终端运行。

## 管理页访问

### SSH 隧道（推荐）

管理页默认只接受本机 Host，不暴露到公网。从自己的电脑建立隧道：

```sh
ssh -N -L 8765:127.0.0.1:8765 your-user@your-server
```

保持 SSH 窗口运行，然后打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。

### 反向代理域名

如果使用 Nginx、Caddy 或面板把 `host-watch.example.com` 转发到 `http://127.0.0.1:8765`，管理服务会因为 Host 校验返回 403。需要为管理服务设置允许的域名和 Basic Auth 密码：

```sh
sudoedit /etc/vmiss-monitor-admin.env
```

可以先执行 `hw add-user` 创建账号，再只设置允许的域名；如果继续使用旧单账号方式，至少填写：

```env
ADMIN_ALLOWED_HOSTS=127.0.0.1,localhost,host-watch.example.com
ADMIN_USERNAME=admin
ADMIN_PASSWORD=替换为足够长的随机密码
```

同时确认反向代理传递原始 `Host`，然后重载并重启管理服务：

```sh
sudo systemctl daemon-reload
sudo systemctl restart vmiss-monitor-admin.service
```

浏览器打开域名后会弹出 Basic Auth，用户名和密码就是上面设置的值。不要把管理页直接绑定到公网 IP，也不要使用 `ADMIN_ALLOWED_HOSTS=*`。

## 在管理页配置 Telegram

管理页的“Telegram 通知”区域可以填写 bot token、chat ID，并临时停用通知。已保存的 token 不会回显；输入框留空表示保留原 token，勾选“清除已保存的 Telegram 凭据”才会删除它。配置保存在 `/var/lib/vmiss-monitor/config.json`，权限为 `0600`。

保存前可以点击“测试 Telegram”发送一条测试消息。点击“读取最近聊天”会读取 bot 收到的消息，从列表选择 chat ID；如果 bot 使用 webhook，Telegram 不允许读取 updates，此时请直接填写 chat ID。

先在 Telegram 中打开 bot 并发送 `/start`。私聊或群组的 chat ID 可以从 Bot API 查询，也可以先临时在服务器执行：

```sh
sudo -u vmiss-monitor bash -c 'set -a; . /etc/vmiss-monitor.env; set +a; cd /opt/vmiss-monitor; .venv/bin/python monitor.py --show-chat-ids'
```

频道可填写频道用户名（如 `@my_channel`），并确保 bot 是该频道管理员。保存 Telegram 配置后，重启监控服务即可载入：

```sh
sudo systemctl restart vmiss-monitor.service
```

旧版本仍支持 `/etc/vmiss-monitor.env` 中的 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`，但推荐改用管理页配置。真实 token 不要提交到 GitHub。

## 配置商品

在管理页可添加 VMISS 商品，或选择“通用 HTTPS 网页”适配器。通用网页示例：

```json
{
  "id": "provider-vps-2g",
  "adapter": "html",
  "provider": "ExampleHost",
  "name": "VPS 2 GB",
  "enabled": true,
  "url": "https://shop.example.com/vps-2g",
  "stock_selector": ".stock-status",
  "stock_mode": "availability",
  "available_pattern": "(?i)in stock|available|有货",
  "unavailable_pattern": "(?i)out of stock|sold out|缺货|售罄",
  "purchase_url": "https://shop.example.com/order/vps-2g"
}
```

如果网页显示精确数量，设置 `stock_mode` 为 `quantity`，并将 `stock_pattern` 配置为带捕获组的正则，例如默认的 `([\\d,]+)` 可匹配 `库存 1,203 台` 中的 `1,203`。CSS 选择器必须只匹配一个非空元素；解析不确定时会报错停止，不会把不确定状态当成有货或缺货。

VMISS 商品在管理页设置：

- `aff` 推广 ID，例如 `2762`
- 分类页，例如 `https://app.vmiss.com/store/dedicated-server`
- 商品选择标识：商品卡片 ID（如 `product44`）、完整名称，或订购链接路径末段 slug
- `gid` 或 `pid`，只能填写一个
- 检查间隔，最少 300 秒

VMISS 的推广 ID 是全局配置；通用网页可各自设置购买链接。两类商品可以同时监控。

保存配置不会访问 VMISS。确认 VMISS 允许当前访问方式后，再打开总开关并启用商品。

## FlareSolverr（可选）

如果这是你自己搭建的测试站点，并且测试环境确实需要通过 FlareSolverr 取得页面，可以在 Debian 服务器上只绑定本机地址运行 FlareSolverr。下面示例使用 Docker，API 不暴露到公网：

以下命令假设服务器已经安装 Docker；FlareSolverr 不会由本项目的一键安装脚本自动安装。

```sh
docker run -d --name flaresolverr --restart unless-stopped \
  -e LOG_LEVEL=info \
  -p 127.0.0.1:8191:8191 \
  ghcr.io/flaresolverr/flaresolverr:latest
```

在管理页“网页请求方式”中选择“FlareSolverr”，填写 `http://127.0.0.1:8191/v1` 和超时（10 至 180 秒），保存后重启监控服务：

```sh
sudo systemctl restart vmiss-monitor.service
sudo journalctl -u vmiss-monitor.service -n 50 --no-pager
```

程序向 FlareSolverr 发送 `request.get`，复用返回的页面、Cookie 和 User-Agent，再使用现有商品解析逻辑。FlareSolverr API 请求失败、响应格式异常、最终 URL 离开 `app.vmiss.com`，或返回仍是 Cloudflare 拦截页时，监控会停止并告警；不会无限重试，也不会把 API 地址或响应内容发送到 Telegram。需要停用时，在管理页切回“普通 HTTP 请求”并保存，再重启服务。

不要将 FlareSolverr 的 `8191` 端口映射到公网，也不要把 FlareSolverr 用于没有授权的第三方站点。

管理页勾选或取消“启用库存检查”后，点击“保存配置”保存监控总开关。保存、重新载入、刷新状态、Telegram 测试和读取聊天时，按钮会显示处理中并暂时禁用，避免重复请求；结果通过固定浮动提示显示，滚动到页面下方也能看到。成功提示约 6 秒后收起，失败提示保留到手动关闭，页面顶部也保留最近的操作结果。添加、移除商品及选择聊天会提示尚需保存。保存配置后仍需通过 `hw` 菜单启动或重启监控服务；刷新状态只读取最近记录。首次安装后需要在服务器启动一次监控服务：

## 启用监控

```sh
sudo systemctl enable --now vmiss-monitor.service
```

后续修改配置后：

```sh
sudo systemctl restart vmiss-monitor.service
```

首次检查默认只记录库存基线，不会因已有库存而通知。如需首次发现已有库存也通知，勾选页面中的对应选项。

## 遇到 Cloudflare 或 HTTP 403

如果 Telegram 收到“VMISS 监控已停止”，或管理页状态显示“因网站拦截已停止”，说明目标网站返回了 Cloudflare 安全验证、HTTP 403 或 HTTP 429。程序会停止当前监控周期，不会连续重试或绕过验证。

处理步骤：

1. 暂停监控，不要反复重启服务或更换 IP 撞站。
2. 把服务器公网 IP、访问时间和页面截图发给 VMISS，确认允许的监控方式、访问频率和服务器 IP。
3. 获得明确许可后，在管理页点击“启用监控并保存”，再执行：

```sh
sudo systemctl restart vmiss-monitor.service
sudo journalctl -u vmiss-monitor.service -n 50 --no-pager
```

如果 VMISS 不允许服务器自动检查，应保持监控关闭，不要尝试绕过 Cloudflare 验证。

## 运维

```sh
sudo systemctl status vmiss-monitor-admin.service vmiss-monitor.service
sudo journalctl -u vmiss-monitor.service -f
sudo journalctl -u vmiss-monitor-admin.service -f
```

程序安装在 `/opt/vmiss-monitor/`；配置和状态在 `/var/lib/vmiss-monitor/`；兼容用 Telegram 环境文件在 `/etc/vmiss-monitor.env`；反向代理访问设置在 `/etc/vmiss-monitor-admin.env`。

停止服务：

```sh
sudo systemctl disable --now vmiss-monitor.service
sudo systemctl disable --now vmiss-monitor-admin.service
```

## 升级

重新执行一键安装命令即可。升级不会覆盖商品配置、Telegram 配置或管理页环境文件。升级后如果监控正在运行，执行：

```sh
sudo systemctl daemon-reload
sudo systemctl restart vmiss-monitor.service
```

## 离线语法检查

```sh
python3 -m py_compile monitor.py admin.py auth.py manage.py
python3 -m unittest -v
```
