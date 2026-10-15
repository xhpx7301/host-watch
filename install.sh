#!/usr/bin/env bash
set -Eeuo pipefail

ARCHIVE_URL="https://codeload.github.com/xhpx7301/host-watch/tar.gz/refs/heads/main"
INSTALL_DIR="/opt/vmiss-monitor"
DATA_DIR="/var/lib/vmiss-monitor"
ENV_FILE="/etc/vmiss-monitor.env"
ADMIN_ENV_FILE="/etc/vmiss-monitor-admin.env"
SERVICE_USER="vmiss-monitor"
SERVICE_GROUP="vmiss-monitor"

log() { printf '[host-watch] %s\n' "$*"; }
fail() { printf '[host-watch] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "${EUID}" -eq 0 ]] || fail "请使用 root 运行，例如：curl -fsSL https://raw.githubusercontent.com/xhpx7301/host-watch/main/install.sh | sudo bash"
[[ -f /etc/debian_version && -f /etc/os-release ]] || fail "此安装脚本仅支持 Debian 12/13。"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
  debian:12|debian:13) ;;
  *) fail "检测到 ${PRETTY_NAME:-未知系统}；此安装脚本仅支持 Debian 12/13。" ;;
esac
command -v systemctl >/dev/null 2>&1 || fail "未找到 systemd/systemctl。"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl python3 python3-venv sudo tar

if ! getent group "$SERVICE_GROUP" >/dev/null; then
  groupadd --system "$SERVICE_GROUP"
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SERVICE_GROUP" --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

TEMP_DIR="$(mktemp -d)"
cleanup() { rm -rf -- "$TEMP_DIR"; }
trap cleanup EXIT

log "正在下载 GitHub main 分支源码。"
curl --fail --location --silent --show-error --connect-timeout 20 --max-time 120 \
  --retry 2 --retry-delay 2 --retry-max-time 300 \
  "$ARCHIVE_URL" --output "$TEMP_DIR/source.tar.gz" \
  || fail "GitHub 源码下载失败，请检查到 codeload.github.com 的网络、DNS 和代理；应用文件尚未替换。"
tar -xzf "$TEMP_DIR/source.tar.gz" -C "$TEMP_DIR"
SOURCE_DIR="$(find "$TEMP_DIR" -mindepth 1 -maxdepth 1 -type d -name 'host-watch-*' -print -quit)"
[[ -n "$SOURCE_DIR" ]] || fail "源码压缩包格式不正确。"

for file in monitor.py admin.py auth.py manage.py stockwatch.sh admin.html admin.js requirements.txt config.example.json .env.example admin.env.example vmiss-monitor.service vmiss-monitor-admin.service host-watch-admin.sudoers install-flaresolverr.sh; do
  [[ -f "$SOURCE_DIR/$file" ]] || fail "源码缺少必需文件：$file"
done

# Deploy before replacing the application, so dependency failures do not leave
# a partially updated web application. Repeated installs reuse existing services.
# shellcheck source=install-flaresolverr.sh
. "$SOURCE_DIR/install-flaresolverr.sh"
deploy_flaresolverr

install -d -o root -g root -m 0755 "$INSTALL_DIR"
[[ ! -L "$DATA_DIR" ]] || fail "$DATA_DIR 不能是符号链接。"
[[ ! -L "$ENV_FILE" ]] || fail "$ENV_FILE 不能是符号链接。"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$DATA_DIR"
[[ ! -L "$DATA_DIR/config.json" ]] || fail "$DATA_DIR/config.json 不能是符号链接。"
install -o root -g root -m 0644 \
  "$SOURCE_DIR/monitor.py" "$SOURCE_DIR/admin.py" "$SOURCE_DIR/auth.py" "$SOURCE_DIR/manage.py" "$SOURCE_DIR/admin.html" \
  "$SOURCE_DIR/admin.js" "$SOURCE_DIR/requirements.txt" "$SOURCE_DIR/config.example.json" \
  "$SOURCE_DIR/.env.example" "$SOURCE_DIR/vmiss-monitor.service" \
  "$SOURCE_DIR/vmiss-monitor-admin.service" "$SOURCE_DIR/admin.env.example" "$SOURCE_DIR/host-watch-admin.sudoers" "$INSTALL_DIR/"
install -d -o root -g root -m 0755 /usr/local/bin
if [[ -f /usr/local/bin/jk && ! -L /usr/local/bin/jk ]] && \
  cmp -s /usr/local/bin/jk <(sed 's/运行 hw/运行 jk/g' "$SOURCE_DIR/stockwatch.sh"); then
  rm -- /usr/local/bin/jk
fi
install -o root -g root -m 0755 "$SOURCE_DIR/stockwatch.sh" /usr/local/bin/host-watch
install -o root -g root -m 0755 "$SOURCE_DIR/stockwatch.sh" /usr/local/bin/stockwatch
install -o root -g root -m 0755 "$SOURCE_DIR/stockwatch.sh" /usr/local/bin/hw
install -o root -g root -m 0440 "$SOURCE_DIR/host-watch-admin.sudoers" /etc/sudoers.d/host-watch-admin
visudo -cf /etc/sudoers.d/host-watch-admin >/dev/null || fail "HostWatch 管理权限配置校验失败。"
install -d -o root -g root -m 0700 /var/backups/stockwatch
[[ ! -L /run/stockwatch-manager.lock ]] || fail "管理锁不能是符号链接。"
if [[ ! -e /run/stockwatch-manager.lock ]]; then
  install -o root -g root -m 0600 /dev/null /run/stockwatch-manager.lock
fi
install -d -o root -g root -m 0755 /etc/tmpfiles.d
printf 'f /run/stockwatch-manager.lock 0600 root root -\n' > /etc/tmpfiles.d/host-watch.conf

if [[ ! -e "$DATA_DIR/config.json" ]]; then
  install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 \
    "$SOURCE_DIR/config.example.json" "$DATA_DIR/config.json"
else
  [[ -f "$DATA_DIR/config.json" && ! -L "$DATA_DIR/config.json" ]] || fail "$DATA_DIR/config.json 必须是普通文件。"
  chown "$SERVICE_USER:$SERVICE_GROUP" "$DATA_DIR/config.json"
  chmod 0600 "$DATA_DIR/config.json"
fi

[[ ! -L "$ENV_FILE" ]] || fail "$ENV_FILE 不能是符号链接。"
if [[ ! -e "$ENV_FILE" ]]; then
  install -o root -g "$SERVICE_GROUP" -m 0640 "$SOURCE_DIR/.env.example" "$ENV_FILE"
  log "已创建兼容用空环境文件 $ENV_FILE；Telegram 凭据可在管理页配置。"
else
  [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "$ENV_FILE 必须是普通文件。"
  chown "root:$SERVICE_GROUP" "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
fi

[[ ! -L "$ADMIN_ENV_FILE" ]] || fail "$ADMIN_ENV_FILE 不能是符号链接。"
if [[ ! -e "$ADMIN_ENV_FILE" ]]; then
  install -o root -g "$SERVICE_GROUP" -m 0640 "$SOURCE_DIR/admin.env.example" "$ADMIN_ENV_FILE"
else
  [[ -f "$ADMIN_ENV_FILE" && ! -L "$ADMIN_ENV_FILE" ]] || fail "$ADMIN_ENV_FILE 必须是普通文件。"
  chown "root:$SERVICE_GROUP" "$ADMIN_ENV_FILE"
  chmod 0640 "$ADMIN_ENV_FILE"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$INSTALL_DIR/.venv"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/.venv"
if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  runuser -u "$SERVICE_USER" -- python3 -m venv "$INSTALL_DIR/.venv"
fi
runuser -u "$SERVICE_USER" -- "$INSTALL_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"

install -o root -g root -m 0644 "$SOURCE_DIR/vmiss-monitor.service" /etc/systemd/system/vmiss-monitor.service
install -o root -g root -m 0644 "$SOURCE_DIR/vmiss-monitor-admin.service" /etc/systemd/system/vmiss-monitor-admin.service
systemctl daemon-reload
systemctl enable --now vmiss-monitor-admin.service
systemctl restart vmiss-monitor-admin.service

log "安装完成。监控总开关和已有配置保持原值；新安装默认关闭，安装程序不会主动启动库存监控。"
log "Debian 管理菜单：hw（更新、服务管理、管理页用户管理；普通用户自动调用 sudo）。"
log "Telegram、厂商、商品和人工购买入口请在管理页配置。"
log "网页请求设置支持普通 HTTPS 和 FlareSolverr；默认普通请求，商品可单独覆盖。服务连接可直接在网页测试。"
log "管理页：在本地电脑运行 ssh -N -L 8765:127.0.0.1:8765 your-user@your-server，然后打开 http://127.0.0.1:8765"
log "若使用反向代理域名，请设置 ADMIN_ALLOWED_HOSTS，并用 hw add-user 创建账号（也兼容原 ADMIN_PASSWORD）。"
log "在管理页配置商品，可先单次测试；点击底部保存配置后，再开始监控。管理员管理区提供账号、日志、备份和更新操作。"
