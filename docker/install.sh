#!/bin/sh
set -eu

REPOSITORY="${HOSTWATCH_REPOSITORY:-https://github.com/xhpx7301/host-watch.git}"
PROJECT_DIR="${HOSTWATCH_DIR:-/opt/host-watch}"
LAUNCHER="/usr/local/bin/hw"

if [ "$(id -u)" -ne 0 ]; then
    echo "请使用 root 或 sudo 执行此命令。" >&2
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "未找到 Docker，请先安装 Docker Engine。" >&2
    exit 1
fi
if ! command -v git >/dev/null 2>&1; then
    echo "未找到 Git，请先安装 git。" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "未找到 Docker Compose 插件，请安装 docker compose plugin。" >&2
    exit 1
fi

if [ -e "$PROJECT_DIR" ] && [ ! -d "$PROJECT_DIR/.git" ]; then
    echo "目标目录已存在但不是 HostWatch Git 仓库：$PROJECT_DIR" >&2
    exit 1
fi
if [ -d "$PROJECT_DIR/.git" ]; then
    git -C "$PROJECT_DIR" pull --ff-only origin main
else
    mkdir -p "$(dirname "$PROJECT_DIR")"
    git clone --depth 1 --branch main "$REPOSITORY" "$PROJECT_DIR"
fi

install -d -m 0755 /usr/local/bin
cat > "$LAUNCHER" <<EOF
#!/bin/sh
exec "$PROJECT_DIR/docker/hw-menu.sh" "\$@"
EOF
chmod 0755 "$LAUNCHER"

cd "$PROJECT_DIR"
docker compose pull flaresolverr
docker compose down --remove-orphans
docker compose up -d --build

echo "HostWatch Docker 部署完成。"
echo "管理页：http://服务器公网地址:25678"
echo "快捷菜单：hw"
echo "查看初始管理员密码：docker compose logs --no-log-prefix hostwatch | grep '初始密码'"
