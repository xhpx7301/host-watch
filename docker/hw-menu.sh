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

uninstall_project() {
    case "$PROJECT_DIR" in
        ""|"/"|"/opt"|"/usr"|"/var"|"/home")
            echo "拒绝删除异常项目目录：$PROJECT_DIR" >&2
            return 1
            ;;
    esac
    echo "此操作将删除 HostWatch 容器、镜像、命名卷、配置、账号、备份和项目目录："
    echo "  $PROJECT_DIR"
    echo "其他 Docker 项目不会受到影响。"
    printf '请输入 DELETE HOSTWATCH 继续：'
    read -r confirmation
    if [ "$confirmation" != "DELETE HOSTWATCH" ]; then
        echo "已取消卸载。"
        return 0
    fi
    compose down --volumes --remove-orphans --rmi all
    rm -f -- /usr/local/bin/hw
    cd /tmp
    rm -rf -- "$PROJECT_DIR"
    echo "HostWatch Docker 已完全卸载。"
}

run_menu() {
    while :; do
        printf '\n──────── HostWatch Docker 管理菜单 ────────\n'
        printf ' 1. 查看容器状态\n'
        printf ' 2. 更新项目（拉取 GitHub、重建并启动）\n'
        printf ' 3. 重启 HostWatch\n'
        printf ' 4. 查看 HostWatch 日志\n'
        printf ' 5. 执行数据备份\n'
        printf ' 6. 重置 Docker 管理员为 pingzi / pingzi\n'
        printf ' 7. 停止项目\n'
        printf ' 8. 完全卸载 HostWatch\n'
        printf ' 0. 退出\n'
        printf '请选择：'
        read -r choice
        case "$choice" in
            1) compose ps ;;
            2) update_project ;;
            3) compose restart ;;
            4) show_logs ;;
            5) compose exec -T hostwatch python manage.py backup ;;
            6) compose exec -T hostwatch python docker/reset_admin.py ;;
            7) compose stop ;;
            8) uninstall_project; return 0 ;;
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
    reset-admin) compose exec -T hostwatch python docker/reset_admin.py ;;
    stop) compose stop ;;
    start) compose up -d ;;
    uninstall) uninstall_project ;;
    *) echo "用法：hw [status|update|restart|logs|backup|reset-admin|stop|start|uninstall]" >&2; exit 2 ;;
esac
