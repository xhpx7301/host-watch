# 项目结构与开发

[返回首页](../README.md)

## 源码目录

| 路径 | 内容 |
| --- | --- |
| `app/` | Python 管理页、监控、商家适配与运维模块 |
| `web/` | 管理页及登录页 HTML、JavaScript |
| `config/` | 商品、环境变量与管理页配置示例 |
| `tests/` | Python 回归测试；`fixtures/akile/` 保存商家页面样本 |
| `deploy/native/` | Debian systemd、sudoers、管理菜单与 FlareSolverr 部署辅助脚本 |
| `deploy/egress/` | 出口节点的 systemd 与 OpenRC 服务模板 |
| `docker/` | Dockerfile、安装菜单、容器初始化和备份程序 |
| `scripts/` | 开发辅助工具与前端回归测试 |
| `docs/` | 按主题拆分的使用文档 |

根目录保留 README、依赖清单、Compose 配置及三个已公开的一键安装/卸载入口。`.dockerignore` 和 Git 配置留在根目录，便于工具自动识别。运行时配置、缓存、密钥及日志由 `.gitignore` 排除，不提交到仓库。

## 部署与升级兼容

Debian 安装器从上述目录读取源码，部署时仍将 Python、网页和配置示例放到 `/opt/vmiss-monitor/`，systemd、sudoers、`hw` 与旧版更新入口继续使用原路径。业务数据仍位于 `/var/lib/vmiss-monitor/`，已有服务器无需手动搬迁文件。

Compose 从仓库根目录构建，使用 `docker/Dockerfile`，镜像中的应用仍位于 `/app/`。管理页、监控和备份分别运行在 `hostwatch`、`monitor`、`backup` 服务中，共享 `/data` 和 `/backups` 命名卷。仅映射管理页端口 `25678`；FlareSolverr 通过内网 `flaresolverr:8191` 访问。容器不挂载宿主机 Docker socket，更新通过主机侧 `hw update` 执行。

## 本地开发

要求 Python 3.10+，先在仓库根目录创建虚拟环境并安装依赖：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config/config.example.json config.json
python3 app/admin.py
```

Windows 可使用 `.venv\Scripts\Activate.ps1` 激活环境。源代码模式使用根目录的本地配置和状态，静态资源从 `web/` 加载；真实凭据、缓存及状态文件不要提交。

在仓库根目录检查：

```sh
python3 -m compileall -q app docker tests
python3 -m unittest discover -v
node --check web/admin.js
node --check web/catalog.js
node --check web/login.js
node --test scripts/test-admin-accounts.cjs
```

默认 unittest discovery 会加载 `tests/__init__.py`，为应用和共享测试样本设置导入路径；定向运行可用 `python3 -m unittest tests.test_admin -v`。systemd 和部分安装测试仅在 Linux 执行，Windows 会按现有平台条件跳过。
