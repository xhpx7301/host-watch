# VMISS 库存监控

按配置检查 VMISS 商品库存，并在库存增加时通过 Telegram bot 通知。每条通知带有 `aff.php` 推广链接，可为商品指定 `gid`（商品组）或 `pid`（单品）。程序不会下单。

> VMISS 曾对自动化访问返回拦截页。部署前请先向 VMISS 确认允许的访问方式。本项目不绕过 Cloudflare；遇到 HTTP 403、429 或验证页会记录状态、尝试发送告警并停止访问，不会自动重试。检查间隔不能低于 5 分钟。

## 一键安装

适用于 Debian 12/13，要求 systemd 和 root/sudo 权限。脚本会安装依赖、创建 `vmiss-monitor` 系统用户、安装程序和 systemd 单元，并启动仅监听 `127.0.0.1:8765` 的管理页。库存监控默认关闭，安装脚本不会启用或启动它。

先在 Telegram 创建 bot 并向它发送 `/start`。然后在服务器执行：

```sh
curl -fsSL https://raw.githubusercontent.com/xhpx7301/jiankong/main/install.sh | sudo bash
```

脚本可重复执行用于升级。已有的 `/var/lib/vmiss-monitor/config.json` 和 `/etc/vmiss-monitor.env` 会保留。升级后如监控服务已经在运行，手动重启以载入新程序：

```sh
sudo systemctl restart vmiss-monitor.service
```

安装完成后，按下面步骤填写 Telegram 设置并打开管理页。

## Telegram 设置

Bot token 与 chat ID 只保存在服务器 `/etc/vmiss-monitor.env`，不要放进管理页或提交到 GitHub：

```sh
sudoedit /etc/vmiss-monitor.env
```

设置 `TELEGRAM_BOT_TOKEN`。私聊或群组的 `TELEGRAM_CHAT_ID` 可稍后用以下命令查询；先在目标私聊/群组向 bot 发送 `/start` 或一条消息：

```sh
sudo -u vmiss-monitor bash -c 'set -a; . /etc/vmiss-monitor.env; set +a; cd /opt/vmiss-monitor; .venv/bin/python monitor.py --show-chat-ids'
```

将输出的 ID 写入 `/etc/vmiss-monitor.env` 的 `TELEGRAM_CHAT_ID`。频道可填写频道用户名（如 `@my_channel`），并确保 bot 是该频道管理员。已运行的监控服务需要重启才能读取更新后的凭据：

```sh
sudo systemctl restart vmiss-monitor.service
```

## 打开管理页

管理页不暴露到公网。从自己的电脑建立 SSH 隧道：

```sh
ssh -L 8765:127.0.0.1:8765 your-user@your-server
```

然后访问 [http://127.0.0.1:8765](http://127.0.0.1:8765)。

页面可设置轮询间隔、`aff` 推广 ID、分类页 URL、商品选择标识及 `gid/pid`。分类页需填写 `https://app.vmiss.com/store/...`。商品选择标识可用卡片 ID（例如 `product44`）、完整商品名称，或订购链接路径末段的 slug。保存设置不会访问 VMISS。

## 启用库存监控

确认 VMISS 允许当前访问方式后，在管理页添加并启用至少一个商品，填写推广 ID，打开“启用库存检查”并保存。然后在服务器运行：

```sh
sudo systemctl enable --now vmiss-monitor.service
```

后续配置发生变更时，运行 `sudo systemctl restart vmiss-monitor.service` 立即重新载入。管理页关闭总开关后，监控会在当前检查周期结束时退出。安装时不会启用监控服务。

首次检查默认只记录库存基线，不会因已有库存而通知。如果需要首次检测到有货也通知，请勾选“首次启动时，有库存也通知”。之后库存增加时才会通知。

## 运维

```sh
sudo systemctl status vmiss-monitor-admin.service vmiss-monitor.service
sudo journalctl -u vmiss-monitor.service -f
sudo journalctl -u vmiss-monitor-admin.service -f
```

程序安装在 `/opt/vmiss-monitor/`；商品配置和运行状态在 `/var/lib/vmiss-monitor/`；Telegram 凭据在权限受限的 `/etc/vmiss-monitor.env`。停止服务：

```sh
sudo systemctl disable --now vmiss-monitor.service
sudo systemctl disable --now vmiss-monitor-admin.service
```

## 从源码安装

如不使用一键脚本，可在 Debian 12/13 上手动安装：

```sh
sudo apt update
sudo apt install -y python3 python3-venv
sudo useradd --system --home /opt/vmiss-monitor --shell /usr/sbin/nologin vmiss-monitor
sudo install -d -o vmiss-monitor -g vmiss-monitor /opt/vmiss-monitor /var/lib/vmiss-monitor
sudo cp monitor.py admin.py admin.html admin.js requirements.txt .env.example config.example.json vmiss-monitor.service vmiss-monitor-admin.service /opt/vmiss-monitor/
sudo chown -R vmiss-monitor:vmiss-monitor /opt/vmiss-monitor /var/lib/vmiss-monitor
sudo -u vmiss-monitor python3 -m venv /opt/vmiss-monitor/.venv
sudo -u vmiss-monitor /opt/vmiss-monitor/.venv/bin/pip install -r /opt/vmiss-monitor/requirements.txt
sudo install -o root -g vmiss-monitor -m 0640 /opt/vmiss-monitor/.env.example /etc/vmiss-monitor.env
sudo install -m 0644 /opt/vmiss-monitor/vmiss-monitor.service /etc/systemd/system/
sudo install -m 0644 /opt/vmiss-monitor/vmiss-monitor-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vmiss-monitor-admin.service
```

手动安装也不会自动启用库存监控。离线语法检查命令：

```sh
python3 -m py_compile monitor.py admin.py
```
