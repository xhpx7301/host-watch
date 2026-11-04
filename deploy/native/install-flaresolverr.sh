#!/usr/bin/env bash
# Sourced by install.sh after downloading the release. No application data changes.
deploy_flaresolverr() {
  local container="host-watch-flaresolverr"
  local image="ghcr.io/flaresolverr/flaresolverr:latest"
  local health_file="$TEMP_DIR/flaresolverr-health.json"
  flaresolverr_ready() {
    curl --noproxy '*' --fail --silent --max-time 3 http://127.0.0.1:8191/ -o "$health_file" &&
      python3 - "$health_file" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        value = json.load(stream)
    assert isinstance(value, dict) and "flaresolverr" in str(value.get("msg", "")).lower()
except (OSError, ValueError, AssertionError):
    sys.exit(1)
PY
  }

  if [[ "${HOSTWATCH_INSTALL_FLARESOLVERR:-1}" == "0" ]]; then
    log "跳过本机 FlareSolverr 部署（HOSTWATCH_INSTALL_FLARESOLVERR=0）；可使用已有远端服务或普通 HTTPS。"
    return
  fi
  if flaresolverr_ready; then
    log "已有本机 FlareSolverr 服务就绪，复用原服务，不更改已有容器和镜像。"
    return
  fi
  if ! command -v docker >/dev/null 2>&1; then
    log "正在安装 Docker 以运行 FlareSolverr。"
    DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
  fi
  systemctl enable --now docker.service
  docker info >/dev/null || fail "Docker 未就绪，无法自动部署 FlareSolverr。"
  if docker container inspect "$container" >/dev/null 2>&1; then
    [[ "$(docker inspect --format '{{index .Config.Labels "io.hostwatch.managed"}}' "$container")" == "true" ]] ||
      fail "容器 $container 已存在但不属于 HostWatch，未修改，请处理命名冲突。"
    # Keep the installed image and container settings on repeated installs.
    docker start "$container" >/dev/null || fail "FlareSolverr 容器启动失败，请检查 8191 端口占用和 Docker 日志。"
  else
    log "正在下载官方 FlareSolverr 镜像（首次安装可能需要几分钟）。"
    docker pull "$image" || fail "FlareSolverr 镜像下载失败，请检查服务器到 ghcr.io 的网络后重新安装。"
    docker run -d --name "$container" --label io.hostwatch.managed=true \
      --restart unless-stopped --shm-size=256m \
      -p 127.0.0.1:8191:8191 -e LOG_LEVEL=info -e TZ=Asia/Shanghai \
      -e TEST_URL=https://example.com "$image" >/dev/null ||
      fail "FlareSolverr 部署失败，请检查 8191 端口和 Docker 日志。"
  fi
  log "正在等待 FlareSolverr 浏览器初始化。"
  local attempt
  for attempt in {1..60}; do
    if flaresolverr_ready; then
      log "FlareSolverr 已就绪：http://127.0.0.1:8191/v1（仅本机可访问，已设置容器自动重启）。"
      return
    fi
    sleep 2
  done
  fail "FlareSolverr 初始化超时。查看：docker logs --tail 80 $container；修复后重新安装。"
}
