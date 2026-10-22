#!/bin/sh
# Standalone sub-server installer. The main HostWatch service is not installed.
set -eu

fail() { printf '[HostWatch Egress] %s\n' "$*" >&2; exit 1; }
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || fail '请使用 root 执行安装命令。'
  exec sudo sh "$0" "$@"
fi
[ "$#" -eq 1 ] || fail '请在管理页添加节点，然后复制该节点的安装命令。'
[ -f /etc/os-release ] || fail '无法识别系统，仅支持 Debian 和 Alpine。'
. /etc/os-release
case "${ID:-}" in
  debian)
    command -v systemctl >/dev/null 2>&1 || fail 'Debian 需要 systemd。'
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl python3 python3-requests
    ;;
  alpine)
    apk add --no-cache ca-certificates curl python3 py3-requests openrc coreutils
    command -v rc-service >/dev/null 2>&1 || fail 'Alpine 需要 OpenRC。'
    ;;
  *) fail '仅支持 Debian 和 Alpine。' ;;
esac

task_tmp=$(mktemp -d)
trap 'rm -rf "$task_tmp"' EXIT HUP INT TERM
curl -fsSL --connect-timeout 20 --max-time 120 --retry 2 \
  https://codeload.github.com/xhpx7301/host-watch/tar.gz/refs/heads/main -o "$task_tmp/source.tar.gz"
tar -xzf "$task_tmp/source.tar.gz" -C "$task_tmp"
source_dir="$task_tmp/host-watch-main"
for file in egress_agent.py hostwatch-egress-agent.service hostwatch-egress-agent.openrc uninstall-egress.sh; do
  [ -f "$source_dir/$file" ] || fail "源码缺少 $file，尚未替换已安装的程序。"
done
python3 - "$1" "$task_tmp/config.json" <<'PY'
import base64, json, pathlib, re, sys
from urllib.parse import urlsplit
value = json.loads(base64.b64decode(sys.argv[1], validate=True))
if not isinstance(value, dict):
    raise SystemExit("节点安装配置必须是 JSON 对象")
allowed = {"EGRESS_NODE_ID", "EGRESS_NODE_NAME", "EGRESS_TOKEN", "EGRESS_LISTEN_HOST", "EGRESS_LISTEN_PORT", "EGRESS_REGISTER_URL"}
if set(value) - allowed:
    raise SystemExit("节点安装配置含未知参数")
node_id = value.get("EGRESS_NODE_ID", "egress-node")
if not isinstance(node_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", node_id):
    raise SystemExit("节点 ID 无效")
token = value.get("EGRESS_TOKEN", "")
if not isinstance(token, str) or not token or len(token) > 512:
    raise SystemExit("节点令牌无效")
port = value.get("EGRESS_LISTEN_PORT", 48787)
if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
    raise SystemExit("节点端口无效")
name = value.get("EGRESS_NODE_NAME", node_id)
if not isinstance(name, str) or not name or len(name) > 120:
    raise SystemExit("节点名称无效")
register = value.get("EGRESS_REGISTER_URL", "")
parsed = urlsplit(register)
if register and (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment):
    raise SystemExit("主服务回报地址无效")
if value.get("EGRESS_LISTEN_HOST", "0.0.0.0") != "0.0.0.0":
    raise SystemExit("安装命令的监听地址必须为 0.0.0.0")
value.update(EGRESS_NODE_ID=node_id, EGRESS_NODE_NAME=name, EGRESS_LISTEN_PORT=port, EGRESS_LISTEN_HOST="0.0.0.0", EGRESS_REGISTER_URL=register)
pathlib.Path(sys.argv[2]).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
PY
python3 -c 'import requests; assert __import__("sys").version_info >= (3, 10), "需要 Python 3.10 或更新版本"'
if ! id hostwatch-egress >/dev/null 2>&1; then
  if [ "$ID" = alpine ]; then
    if ! grep -q '^hostwatch-egress:' /etc/group; then addgroup -S hostwatch-egress; fi
    adduser -S -D -H -h /opt/hostwatch-egress -s /sbin/nologin -G hostwatch-egress hostwatch-egress
  else
    getent group hostwatch-egress >/dev/null || groupadd --system hostwatch-egress
    useradd --system --gid hostwatch-egress --home-dir /opt/hostwatch-egress --shell /usr/sbin/nologin hostwatch-egress
  fi
fi
for target in /opt/hostwatch-egress /etc/hostwatch-egress.json /etc/hostwatch-egress.env; do
  [ ! -L "$target" ] || fail "$target 不能是符号链接。"
done
install -d -o root -g root -m 0755 /opt/hostwatch-egress
install -o root -g root -m 0644 "$source_dir/egress_agent.py" /opt/hostwatch-egress/egress_agent.py
install -o root -g root -m 0755 "$source_dir/uninstall-egress.sh" /opt/hostwatch-egress/uninstall.sh
install -o root -g hostwatch-egress -m 0640 "$task_tmp/config.json" /etc/hostwatch-egress.json
printf 'EGRESS_CONFIG=/etc/hostwatch-egress.json\n' > "$task_tmp/agent.env"
install -o root -g hostwatch-egress -m 0640 "$task_tmp/agent.env" /etc/hostwatch-egress.env
if [ "$ID" = alpine ]; then
  install -o hostwatch-egress -g hostwatch-egress -m 0640 /dev/null /var/log/hostwatch-egress.log
  install -o root -g root -m 0755 "$source_dir/hostwatch-egress-agent.openrc" /etc/init.d/hostwatch-egress-agent
  rc-update add hostwatch-egress-agent default
  rc-service hostwatch-egress-agent restart
else
  install -o root -g root -m 0644 "$source_dir/hostwatch-egress-agent.service" /etc/systemd/system/hostwatch-egress-agent.service
  systemctl daemon-reload
  systemctl enable hostwatch-egress-agent.service
  systemctl restart hostwatch-egress-agent.service
fi
python3 - <<'PY'
import json, time
from urllib.request import Request, urlopen
config = json.load(open("/etc/hostwatch-egress.json", encoding="utf-8"))
port = config["EGRESS_LISTEN_PORT"]
for attempt in range(30):
    try:
        request = Request(f"http://127.0.0.1:{port}/health", headers={"Authorization": "Bearer " + config["EGRESS_TOKEN"]})
        with urlopen(request, timeout=20) as response:
            info = json.load(response)
        if info.get("node_id") != config["EGRESS_NODE_ID"]:
            raise ValueError("监听端口已被其他节点占用")
        break
    except Exception:
        if attempt == 29:
            raise SystemExit("节点未就绪，请检查服务日志和端口占用；配置保存在 /etc/hostwatch-egress.json")
        time.sleep(1)
print(f"节点已启动，端口 {port}；配置：/etc/hostwatch-egress.json")
if config.get("EGRESS_REGISTER_URL"):
    print("已启用自动回报，请在主服务管理页点击“刷新节点”。")
else:
    print("主服务使用本地地址，请复制下方 JSON，在管理页“回填安装结果”中粘贴。")
ip = info.get("public_ip", "")
if ip:
    host = f"[{ip}]" if ":" in ip else ip
    payload = {"node_id": config["EGRESS_NODE_ID"], "public_ip": ip, "proxy_url": f"http://{host}:{port}", "health_url": f"http://{host}:{port}/health", "token": config["EGRESS_TOKEN"]}
    print(json.dumps(payload, ensure_ascii=False))
else:
    print("公网 IP 暂未识别，稍后运行 EGRESS_CONFIG=/etc/hostwatch-egress.json python3 /opt/hostwatch-egress/egress_agent.py --connection-info 获取连接信息。")
PY
