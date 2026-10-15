# HostWatch 服务器库存监控

[GitHub 仓库](https://github.com/xhpx7301/host-watch)

自托管的多厂商库存监控脚本。支持 VMISS、VPS.SOY 专用套餐解析和通用公开 HTTPS 商品页；发现库存增加或由缺货转为有货时，可通过 Telegram 发送提醒与人工购买入口。程序不会登录厂商账户、绕过验证或自动下单。

VMISS 曾对自动化访问返回拦截页。请先确认目标厂商允许自动检查及访问频率。遇到 HTTP 403、429 或安全验证页时，程序会记录状态、尝试告警并停止，不会自动重试。FlareSolverr 仅适用于你拥有或明确获准测试的站点，默认关闭。检查间隔可自定义到秒，0 表示本轮检查完成后立即检查下一轮。

## 支持的监控方式

- **VMISS WHMCS**：解析商品卡片和库存数量，可生成 `aff.php` 推广入口。
- **VPS.SOY**：按完整套餐名称匹配商品卡片，识别库存及购买入口，支持深港IX-MINI 等套餐；负库存按缺货处理。
- **通用 HTTPS 网页**：配置 CSS 库存元素选择器，支持提取数量或匹配有货/缺货文本；购买入口可单独设置，留空时使用商品页。

所有适配器均支持普通 HTTPS 和 FlareSolverr 浏览器请求。普通请求解析服务器返回的 HTML；浏览器请求可执行 JavaScript，并配置渲染等待读取动态库存。两者均不登录厂商账户；复杂交互和私有 API 仍需专用适配器。所有购买动作由用户在收到通知后手动完成。

## 一键安装

适用于 Debian 12/13，要求 systemd 和 root/sudo 权限。脚本会安装依赖、创建 `vmiss-monitor` 系统用户、自动部署 Docker 和官方 FlareSolverr 容器、安装程序和 systemd 单元，并启动仅监听 `127.0.0.1:8765` 的管理页。FlareSolverr 仅监听本机 `127.0.0.1:8191`，设置自动重启，启动后检测就绪状态。库存监控默认关闭，安装脚本不会启用或启动它。首次部署需要下载浏览器镜像，请预留内存、磁盘和到 ghcr.io 的网络连接；浏览器请求按商品顺序执行。

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

首次启动监控时，在管理页配置并启用商品，先点击底部“保存配置”，再点击“开始监控”。停止服务会取消监控开机自启，商品配置保持原样。管理菜单同一时间只允许一个终端运行。

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

频道可填写频道用户名（如 `@my_channel`），并确保 bot 是该频道管理员。保存 Telegram 配置后会自动应用到监控服务。

旧版本仍支持 `/etc/vmiss-monitor.env` 中的 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`，但推荐改用管理页配置。真实 token 不要提交到 GitHub。

## 配置商品

在管理页可添加 VMISS、VPS.SOY 商品，或选择“通用 HTTPS 网页”适配器。通用网页示例：

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
- 检查间隔，可填写 0–86400 秒的整数；0 表示不额外等待

VMISS 的推广 ID 是全局配置；通用网页可各自设置购买链接，VPS.SOY 从套餐卡片读取购买链接。三类商品可以同时监控。

### VPS.SOY：深港IX-MINI

点击商品监控区的“＋ VPS.SOY 商品”，会展开已填好以下信息的商品配置：

- 适配器：`VPS.SOY 商品页`
- 商品分类页：`https://www.vps.soy/cart`（可带分类查询参数）
- 套餐完整名称：`深港IX-MINI`，精确匹配，避免误选 PRO 或其他变体
- 此商品的请求方式：普通 HTTPS 请求；该公开页面目前直接返回库存，无需浏览器服务

先点击“测试此商品”，结果直接显示在商品配置内，无需启用监控。确认结果后勾选“参与自动监控”，保存配置，再点击“开始监控”。添加的商品默认不参与自动监控，不会因点击快捷添加就启动请求。

监控其他套餐时可再次添加，修改商品名称和“套餐完整名称”，例如 `深港IX-PRO`、`深港IX-Ultra`；若套餐在其他分类下，同时修改分类页地址。同一轮内，相同地址和请求方式的商品共用一次页面读取。无需填写 CSS 选择器、VMISS 推广 ID 或猜测商品 ID。

页面显示 `库存：-3` 等负数时按缺货处理，不会误报为 3 件。名称未匹配、库存无法解析，或正库存与售罄标记、禁用购买按钮冲突时会报错；不会将未知状态当作缺货。有货时读取本站真实购买链接，用于提醒中的人工下单入口；缺货时没有可用订购链接则使用商品分类页。

确认厂商允许当前访问方式后，勾选需要参与自动监控的商品并点击底部“保存配置”，再点击“开始监控”；暂停时点击“暂停监控”。

## 网页请求与 FlareSolverr

首次安装或更新时自动部署 FlareSolverr，无需另行手动安装。若 `127.0.0.1:8191` 已有就绪服务，安装器直接复用，不改动既有容器。HostWatch 创建的容器名称为 `host-watch-flaresolverr`；重复安装会启动已有容器，保留其镜像和设置，不会强制拉取新镜像或删除其他容器。镜像拉取、端口冲突或启动检查失败会报错，并停止安装流程，不会报告依赖部署成功。

使用已有远端 FlareSolverr 或内存较小、仅需普通 HTTPS 的机器，可显式跳过自动部署：

```sh
curl -fsSL https://raw.githubusercontent.com/xhpx7301/host-watch/main/install.sh -o /tmp/host-watch-install.sh
sudo env HOSTWATCH_INSTALL_FLARESOLVERR=0 bash /tmp/host-watch-install.sh
```

在管理页“网页请求设置”中选择全局请求方式，VMISS、VPS.SOY 和通用网页均生效；每个商品还可选择“跟随全局设置”“普通 HTTPS 请求”或“FlareSolverr 浏览器请求”。旧配置默认跟随全局设置；原来全局已选 FlareSolverr 的通用商品升级后也会使用浏览器。VPS.SOY 快捷添加默认单独选择普通 HTTPS，可按需修改。默认仍使用普通 HTTPS，不会遇到拦截就自动切换或反复重试。

FlareSolverr API 默认 `http://127.0.0.1:8191/v1`，超时 10–180 秒。渲染等待支持 0–30 秒，传递官方 `waitInSeconds` 参数，用于页面验证结束后等待动态内容。默认 0，按需要设置 2–5 秒；普通 HTTPS 不使用此等待。每次浏览器请求使用独立临时会话，由 FlareSolverr 负责关闭，避免跨厂商会话混用和常驻浏览器泄漏；不把浏览器 Cookie 或 User-Agent 写入共享 HTTP 会话。

点击“测试 FlareSolverr 连接”可测试当前未保存的 API 地址并显示版本；此操作不会访问商品网站，也不保证商品页能正常通过验证。实际库存可展开“商品配置”中的商品，点击“测试此商品”：只检查当前商品一次，直接使用当前填写的商品及请求配置，即使未保存、未勾选参与监控或全局监控暂停也能测试。结果直接显示在商品内，包含库存、检查时间、耗时或错误原因；不保存配置、不写入监控历史、不发送 Telegram，也不改变监控开关。修改该商品字段后会清除旧测试提示，避免误认为新配置已经验证。反向代理应为浏览器测试预留足够响应时间，例如 Nginx `proxy_read_timeout 300s`。可在管理员管理区查看监控日志，或在服务器执行：

```sh
sudo docker logs --tail 80 host-watch-flaresolverr
sudo journalctl -u vmiss-monitor.service -n 50 --no-pager
```

请求错误区分 HTTP 403、HTTP 429、Cloudflare 验证页和 FlareSolverr 服务连接/超时/格式错误；Cloudflare 提供 Ray ID 时会展示，便于查询安全事件。使用 `cf-mitigated: challenge` 和验证页特征识别拦截，不再因正常 HTML 包含 `cf-chl-` 脚本就直接误判。服务故障记录为检查失败，网站拒绝访问记录为被拦截，两者均停止等待处理；不会泄露原始浏览器错误中的 Cookie 或 URL 参数。页面最终域名仍需匹配配置的商品域名。

小黄云不等于一定需要浏览器。对于自己的测试站点，建议在 Cloudflare 为监控流量配置合适的规则，FlareSolverr 不能保证通过所有验证。保持 `8191` 仅本机可访问，并仅用于你拥有或获准访问的页面。

管理页通过底部“保存配置”统一保存全部配置；“开始监控”和“暂停监控”只操作已保存的监控开关。保存、刷新状态和测试等操作会显示处理中并暂时禁用控件，避免重复请求；结果通过固定浮动提示显示，商品库存测试另有商品内结果。成功提示约 6 秒后收起，失败提示保留到手动关闭。添加、移除商品及选择聊天会提示尚需保存。保存配置会自动应用，无需再登录服务器重启；刷新状态只读取最近记录。“重新载入”和额外的网页重启监控按钮已移除；需要重新读取配置时可刷新浏览器。

## 网页管理员管理

点击页面右上角“管理员管理”进入管理区，可查看服务状态、重启管理页、管理管理员账号，查看最近 100 条服务日志，备份程序和配置，以及从 GitHub 更新 HostWatch。修改密码和删除账号立即生效；不能删除最后一个管理员。添加首个账号后会接管旧认证，后续请求使用新账号登录。网页管理员均具备完整管理权限。

监控设置使用“开始监控”和“暂停监控”。开始只使用已保存的配置，页面有未保存修改时会提示先保存；暂停可随时执行并保留页面草稿，避免填写不完整的商品阻止暂停。开始后立即检查一次，再按间隔循环检查。底部“保存配置”保持服务器已保存的监控开关；监控开启时保存会重启应用，暂停时保存保持停止。每个商品配置均为独立折叠卡片，标题显示名称、厂商和是否参与自动监控；默认收起，新增商品自动展开，列表的“配置”按钮会展开对应商品。保存及增删其他商品时保留现有展开状态。与当前适配器不相关的字段隐藏。

页面分别显示实际服务运行状态、上次检查结果和未保存修改；历史拦截记录会保留至下次检查，不能据此判断服务现在是否运行。商品状态结合已保存的商品配置与实际服务状态显示：服务停止时显示“监控已暂停”或“监控服务已停止”，服务运行且全局监控开启时才显示“监控运行中”；未参与监控、商品修改待保存、服务异常和状态未知也会单独标明。如果暂停配置已保存但服务仍运行，会提示暂停尚未生效。库存数量保留上次检查结果，暂停后不会更新。常用检查间隔支持秒级和分钟级选项，也可填写 0–86400 秒的整数。间隔是每轮检查完成后的等待时间；0 表示顺序连续检查，不额外等待，也不并发启动多轮。首次有货提醒仅在该商品没有库存历史时适用，重启不会清除历史。

从旧版本首次升级仍需在服务器执行一次 `hw update`，以安装网页管理所需的 sudoers 和 systemd 配置；此后可在网页更新。更新先备份，再由独立 systemd 任务执行，管理页短暂中断时页面会等待恢复。备份保存在 `/var/backups/stockwatch/`，包含凭据，仅 root 可访问。

管理服务仍以普通用户运行，sudoers 仅允许固定的 `/usr/bin/python3 /opt/vmiss-monitor/manage.py web-action` 命令；该入口校验固定动作、用户名和参数，不接受任意 shell 命令。管理文件由 root 持有，网页与终端操作共用互斥锁。自定义 `ADMIN_USERS_FILE` 的安装继续通过终端管理账号。

首次检查默认只记录库存基线，不会因已有库存而通知。如需首次发现已有库存也通知，勾选页面中的对应选项。

## 遇到 Cloudflare 或 HTTP 403

如果 Telegram 收到“VMISS 监控已停止”，或管理页状态显示“因网站拦截已停止”，说明目标网站返回了 Cloudflare 安全验证、HTTP 403 或 HTTP 429。程序会停止当前监控周期，不会连续重试或绕过验证。

处理步骤：

1. 暂停监控，不要反复重启服务或更换 IP 撞站。
2. 把服务器公网 IP、访问时间和页面截图发给 VMISS，确认允许的监控方式、访问频率和服务器 IP。
3. 获得明确许可后，可先点击商品内“测试此商品”；确认正常后保存配置并“开始监控”，再在管理员管理区查看日志。

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

在网页管理员管理区点击“更新 HostWatch”，或在服务器运行 `hw update`。升级不会覆盖商品、Telegram 或管理员配置；更新前自动备份，成功后恢复原来正在运行的监控。旧版本首次升级请先运行一次 `hw update`。

## 离线语法检查

```sh
python3 -m py_compile monitor.py admin.py auth.py manage.py
python3 -m unittest -v
```
