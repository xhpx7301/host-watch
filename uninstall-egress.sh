#!/bin/sh
# Remove only the standalone egress node installed by install-egress.sh.
set -eu

fail() { printf '[HostWatch Egress] %s\n' "$*" >&2; exit 1; }
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || fail '请使用 root 执行卸载命令。'
  exec sudo sh "$0" "$@"
fi
[ -f /etc/os-release ] || fail '无法识别系统，仅支持 Debian 和 Alpine。'
. /etc/os-release
case "${ID:-}" in
  debian)
    if [ -e /etc/systemd/system/hostwatch-egress-agent.service ]; then
      systemctl stop hostwatch-egress-agent.service
      systemctl disable hostwatch-egress-agent.service
    fi
    ;;
  alpine)
    if [ -e /etc/init.d/hostwatch-egress-agent ]; then
      rc-service --ifstarted hostwatch-egress-agent stop
      if [ -L /etc/runlevels/default/hostwatch-egress-agent ] || [ -e /etc/runlevels/default/hostwatch-egress-agent ]; then
        rc-update del hostwatch-egress-agent default
      fi
    fi
    ;;
  *) fail '仅支持 Debian 和 Alpine。' ;;
esac

# Do not remove shared packages or use userdel -r / deluser --remove-home.
if id hostwatch-egress >/dev/null 2>&1; then
  if [ "$ID" = alpine ]; then deluser hostwatch-egress; else userdel hostwatch-egress; fi
fi
if grep -q '^hostwatch-egress:' /etc/group; then
  if [ "$ID" = alpine ]; then delgroup hostwatch-egress; else groupdel hostwatch-egress; fi
fi
rm -f /etc/hostwatch-egress.json /etc/hostwatch-egress.env \
  /etc/systemd/system/hostwatch-egress-agent.service /etc/init.d/hostwatch-egress-agent \
  /var/log/hostwatch-egress.log /run/hostwatch-egress-agent.pid
rm -rf /opt/hostwatch-egress
if [ "$ID" = debian ]; then systemctl daemon-reload; fi
printf '[HostWatch Egress] 节点已卸载。请在主服务管理页调整出口路由、移除节点并保存配置。\n'
