#!/usr/bin/env bash
set -euo pipefail
if [[ "${EUID}" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    exec sudo -- /usr/bin/python3 /opt/vmiss-monitor/manage.py "$@"
  fi
  printf '%s\n' '请切换至 root 后运行 hw，或先配置 sudo。' >&2
  exit 1
fi
exec /usr/bin/python3 /opt/vmiss-monitor/manage.py "$@"
