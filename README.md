# VMISS 库存监控

按配置检查 VMISS 商品库存，并在库存增加时通过 Telegram bot 通知。每条通知带有 `aff.php` 推广链接，可为商品指定 `gid`（商品组）或 `pid`（单品）。程序不会下单。

> VMISS 曾对自动化访问返回拦截页。部署前请先向 VMISS 确认允许的访问方式。本项目不绕过 Cloudflare；遇到 HTTP 403、429 或验证页会记录状态、尝试发送告警并停止访问，不会自动重试。检查间隔不能低于 5 分钟。

## 一键安装

适用于 Debian 12/13，要求 systemd 和 root/sudo 权限。脚本会安装依赖、创建 `vmiss-monitor` 系统用户、安装程序和 systemd 单元，并启动仅监听 `127.0.0.1:8765` 的管理页。库存监控默认关闭，安装脚本不会启用或启动它。

```sh
curl -fsSL https://raw.githubusercontent.com/xhpx7301/jiankong/main/install.sh | sudo bash
```

脚本可重复执行用于升级，已有的 `/var/lib/vmiss-monitor/config.json`、`/etc/vmiss-monitor.env` 和 `/etc/vmiss-monitor-admin.env` 会保留。

## 管理页访问

### SSH 隧道（推荐）

管理页默认只接受本机 Host，不暴露到公网。从自己的电脑建立隧道：

```sh
ssh -N -L 8765:127.0.0.1:8765 your-user@your-server
```

保持 SSH 窗口运行，然后打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。

### 反向代理域名

如果使用 Nginx、Caddy 或面板把 `jiankong.example.com` 转发到 `http://127.0.0.1:8765`，管理服务会因为 Host 校验返回 403。需要为管理服务设置允许的域名和 Basic Auth 密码：

```sh
sudoedit /etc/vmiss-monitor-admin.env
```

至少填写：

```env
ADMIN_ALLOWED_HOSTS=127.0.0.1,localhost,jiankong.example.com
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

在管理页设置：

- `aff` 推广 ID，例如 `2762`
- 分类页，例如 `https://app.vmiss.com/store/dedicated-server`
- 商品选择标识：商品卡片 ID（如 `product44`）、完整名称，或订购链接路径末段 slug
- `gid` 或 `pid`，只能填写一个
- 检查间隔，最少 300 秒

保存配置不会访问 VMISS。确认 VMISS 允许当前访问方式后，再打开总开关并启用商品。

## 启用监控

```sh
sudo systemctl enable --now vmiss-monitor.service
```

后续修改配置后：

```sh
sudo systemctl restart vmiss-monitor.service
```

首次检查默认只记录库存基线，不会因已有库存而通知。如需首次发现已有库存也通知，勾选页面中的对应选项。

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
python3 -m py_compile monitor.py admin.py
```
