# VMISS 库存监控

这个工具按配置检查 VMISS 商品库存，并在库存增加时通过 Telegram 发送通知。通知附带一个 `aff.php` 推广链接，可为每个商品选择 `gid`（商品组）或 `pid`（单个商品）。脚本不会下单。

VMISS 曾对自动化访问返回拦截页。部署前请先向 VMISS 确认允许的访问方式；本工具不绕过 Cloudflare。检查间隔至少为 5 分钟。遇到 HTTP 403、429 或 Cloudflare 验证页时会记录状态、尝试发送告警并停止访问，不会自动重试撞站。

## Debian 部署

以下命令以 Debian 12/13 为例。先把本目录中的文件上传到服务器，再执行：

```sh
sudo apt update
sudo apt install -y python3 python3-venv
sudo useradd --system --home /opt/vmiss-monitor --shell /usr/sbin/nologin vmiss-monitor
sudo install -d -o vmiss-monitor -g vmiss-monitor /opt/vmiss-monitor /var/lib/vmiss-monitor
sudo cp monitor.py admin.py admin.html admin.js requirements.txt .env.example /opt/vmiss-monitor/
sudo chown -R vmiss-monitor:vmiss-monitor /opt/vmiss-monitor /var/lib/vmiss-monitor
sudo -u vmiss-monitor python3 -m venv /opt/vmiss-monitor/.venv
sudo -u vmiss-monitor /opt/vmiss-monitor/.venv/bin/pip install -r /opt/vmiss-monitor/requirements.txt
```

创建 Telegram bot，在 Telegram 中打开 bot 并发送 `/start`。准备环境变量文件：

```sh
sudo install -o root -g vmiss-monitor -m 0640 /opt/vmiss-monitor/.env.example /etc/vmiss-monitor.env
sudoedit /etc/vmiss-monitor.env
```

设置 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`。可使用个人/群组数字 ID，也可使用 bot 管理员有权发消息的频道用户名（例如 `@my_channel`）。如需查个人或群组 ID，在 bot 收到 `/start` 后运行：

```sh
sudo -u vmiss-monitor bash -c 'set -a; . /etc/vmiss-monitor.env; set +a; cd /opt/vmiss-monitor; .venv/bin/python monitor.py --show-chat-ids'
```

把服务模板安装到 systemd，并启动本地管理页：

```sh
sudo install -m 0644 /opt/vmiss-monitor/vmiss-monitor.service /etc/systemd/system/
sudo install -m 0644 /opt/vmiss-monitor/vmiss-monitor-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vmiss-monitor-admin.service
```

管理页只监听服务器的 `127.0.0.1:8765`。从自己的电脑建立 SSH 隧道：

```sh
ssh -L 8765:127.0.0.1:8765 your-user@your-server
```

然后打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。页面可配置检查间隔、推广 ID、分类页、商品选择标识和 `gid/pid`。分类页填 `https://app.vmiss.com/store/...`；商品标识可用商品卡片 ID（如 `product44`）、完整名称，或订购链接末段 slug。保存配置不会访问 VMISS。

监控默认关闭。确认获准的访问方式、填写至少一个启用商品及推广 ID 后，在管理页打开总开关并保存；然后在服务器执行：

```sh
sudo systemctl enable vmiss-monitor.service
sudo systemctl restart vmiss-monitor.service
```

之后监控会在开机时启动。首次运行默认只记录库存基线；如需首次检查到已有库存也通知，可勾选“首次启动时，有库存也通知”。改完商品配置后执行 `sudo systemctl restart vmiss-monitor.service` 使当前检查周期立即重新载入配置。管理页关闭总开关后，当前进程会在下一轮检查前退出。

查看日志和管理服务：

```sh
sudo systemctl status vmiss-monitor.service vmiss-monitor-admin.service
sudo journalctl -u vmiss-monitor.service -f
sudo journalctl -u vmiss-monitor-admin.service -f
```

配置和状态保存在 `/var/lib/vmiss-monitor/`；Telegram 凭据单独保存在权限为 `0640` 的 `/etc/vmiss-monitor.env`。不要把真实 token 提交到版本库。

## 本地离线检查

语法检查和离线解析测试不会访问 VMISS：

```sh
python3 -m py_compile monitor.py admin.py
```

手动联网检查仅应在 VMISS 确认允许后进行：

```sh
sudo systemctl restart vmiss-monitor.service
```
