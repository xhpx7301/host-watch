# 项目结构

HostWatch 的运行入口仍保留在仓库根目录，以兼容已经安装的 `/opt/vmiss-monitor`、systemd 单元和升级脚本。

| 目录/文件 | 作用 |
| --- | --- |
| `admin.py`、`monitor.py`、`catalog.py` | 管理页、后台监控和库存大盘入口 |
| `dmit.py`、`bwh.py`、`egress.py` 等 | 商家适配器与出口节点组件 |
| `admin.html`、`admin.js`、`catalog.js` | 管理页静态资源 |
| `docs/` | 部署、配置和维护说明 |
| `test_*.py` | 与根入口同目录运行的回归测试 |
| `*.service`、`*.openrc`、`*.sh` | 安装、升级、服务和节点部署入口 |

根目录的 Python、静态资源和安装脚本不是临时文件：安装器会将它们作为一个版本原子更新，服务单元也按这些路径启动。新增说明和诊断工具放入 `docs/`，避免移动运行入口导致旧服务器更新后管理页无法启动。

## Docker Compose 运行方式

`docker/install.sh` 是 Docker 一键部署入口，会将项目放到 `/opt/host-watch`，安装主机侧 `hw` 快捷菜单，并执行 Compose 首次构建。`Dockerfile` 构建同一份应用镜像，`docker-compose.yml` 分别以 `hostwatch`、`monitor` 和 `backup` 服务运行管理页、库存监控和每日备份；FlareSolverr 是独立的 `flaresolverr` 服务。三个 HostWatch 服务共享 `/data` 和 `/backups` 命名卷，管理页在容器内监听 `0.0.0.0:25678`，只将 TCP 25678 映射到宿主机。FlareSolverr 只通过 Compose 内网的 `flaresolverr:8191` 访问。

容器模式通过 `HOSTWATCH_CONTAINER=1` 禁用依赖宿主机的 `systemd`、`journalctl`、`systemd-run` 和 Docker socket 操作。网页配置、手动刷新、备份导入导出仍由管理页处理；主机侧 `hw update`、`hw restart` 和 `hw logs` 负责镜像更新、容器重启和日志查看。管理页的 Docker 更新按钮只复制 `hw update`，不会获得宿主机 Docker 控制权限。

## 更新前检查

在仓库根目录运行：

```sh
python3 -m unittest discover -q
python3 -m py_compile admin.py monitor.py catalog.py
node --check admin.js
node --check catalog.js
```

安装器会保留配置、库存状态、Telegram 设置和管理员账号；升级失败时使用管理员菜单生成的备份恢复数据，再重新执行安装脚本。
