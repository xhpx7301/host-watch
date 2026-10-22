#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"

compose() {
    docker compose -f "$PROJECT_DIR/docker-compose.yml" "$@"
}

update_project() {
    git -C "$PROJECT_DIR" pull --ff-only origin main
    compose pull flaresolverr
    compose build --pull
    # The management service was renamed from ``admin`` to ``hostwatch``;
    # remove orphaned containers before rebinding port 25678.
    compose down --remove-orphans
    compose up -d
    echo "HostWatch Docker 项目已更新。"
}

show_logs() {
    compose logs --tail 100 hostwatch monitor backup
}

run_menu() {
    while :; do
        printf '\n──────── HostWatch Docker 管理菜单 ────────\n'
        printf ' 1. 查看容器状态\n'
        printf ' 2. 更新项目（拉取 GitHub、重建并启动）\n'
        printf ' 3. 重启 HostWatch\n'
        printf ' 4. 查看 HostWatch 日志\n'
        printf ' 5. 执行数据备份\n'
        printf ' 6. 停止项目\n'
        printf ' 0. 退出\n'
        printf '请选择：'
        read -r choice
        case "$choice" in
            1) compose ps ;;
            2) update_project ;;
            3) compose restart ;;
            4) show_logs ;;
            5) compose exec -T hostwatch python manage.py backup ;;
            6) compose stop ;;
            0) return 0 ;;
            *) echo "请输入菜单中的编号。" ;;
        esac
    done
}

case "${1:-menu}" in
    menu) run_menu ;;
    status) compose ps ;;
    update) update_project ;;
    restart) compose restart ;;
    logs) show_logs ;;
    backup) compose exec -T hostwatch python manage.py backup ;;
    stop) compose stop ;;
    start) compose up -d ;;
    *) echo "用法：hw [status|update|restart|logs|backup|stop|start]" >&2; exit 2 ;;
esac
