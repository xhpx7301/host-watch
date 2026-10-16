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

## 更新前检查

在仓库根目录运行：

```sh
python3 -m unittest discover -q
python3 -m py_compile admin.py monitor.py catalog.py
node --check admin.js
node --check catalog.js
```

安装器会保留配置、库存状态、Telegram 设置和管理员账号；升级失败时使用管理员菜单生成的备份恢复数据，再重新执行安装脚本。
