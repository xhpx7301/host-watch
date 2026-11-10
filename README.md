# HostWatch 服务器库存监控

自托管的多厂商 VPS 库存监控工具，提供库存大盘、Telegram 补货提醒、二手市场与账户余额通知，以及待付款下单辅助。

支持 VMISS、VPS.SOY、Akile、Isvoro、DMIT、搬瓦工、独角鲸云、Hytron 和通用 HTTPS 商品页；不同商家的可用功能见[监控说明](docs/MONITORING.md)。网页、通知和日志统一使用北京时间。

## 安装

**推荐 Debian 原生部署**，网页更新、服务管理和日志查看的运维功能更完整。Docker 可用于容器化运行，但更新、重启和日志等操作需在主机终端执行，详见[部署差异](docs/DEPLOYMENT.md#如何选择部署方式)。两种方式的管理页均通过 `http://服务器公网地址:25678` 访问，防火墙需放行 TCP `25678`。

### 1. Debian 原生安装（推荐）

适用于 **Debian 12 / 13 + systemd**，使用 root 或 sudo 执行：

```sh
curl -fsSL https://raw.githubusercontent.com/xhpx7301/host-watch/main/install.sh | sudo bash
```

安装器部署 Python 应用和 systemd 服务，默认同时安装 Docker 及 FlareSolverr 容器（仅监听本机 `8191`，可[选择跳过](docs/CONFIGURATION.md#网页请求与-flaresolverr)）。首次安装的管理员为 `admin` / `admin123456789`，登录后请修改密码；忘记密码可执行 `hw passwd`。已有账号和配置会保留，库存自动监控默认关闭。

### 2. Docker Compose 安装

适用于已安装 **Docker Engine、Compose 插件和 Git** 的 Debian / Ubuntu：

```sh
curl -fsSL https://raw.githubusercontent.com/xhpx7301/host-watch/main/docker/install.sh | sudo sh
```

自动启动管理页、监控、备份及 FlareSolverr 服务。首次账号和密码为 `admin` / `admin123456789`，登录后立即修改；已有数据卷沿用原账号，无法登录时执行 `hw reset-admin`。FlareSolverr 仅在容器内网开放。

两种安装均提供 `hw` 管理菜单；安装路径、旧版升级、账号与访问设置见[部署与管理](docs/DEPLOYMENT.md)。

## 开始使用

1. 登录管理页，在 **VPS 库存大盘** 选择商家和套餐，加入库存监控或库存通知。
2. 需要提醒时，在 **Telegram 通知** 中填写机器人 token 和 chat ID，先测试再保存；也可为不同通知类型指定独立机器人。
3. 在对应模块设置检查间隔与自动刷新，保存该卡片。库存监控用于网页查看，库存通知用于 Telegram 提醒。

补货时发送一条新消息，持续有货或售罄时更新原消息。首次检查默认只建立基线，可单独开启首次有货提醒。遇到 HTTP 403、429 或验证页时，先暂停并确认商家允许的访问方式与频率。

自动下单已内置适配 VMISS、Akile、搬瓦工和 Isvoro 普通商品；**DMIT 下单适配尚未完成，实际自动下单暂不可用**；VPS.SOY、Hytron 及通用网页尚未内置适配，请选手动模式。**独角鲸云自动购买会直接扣余额并创建单台月付实例，默认开启自动续费（可逐项关闭）；其他已适配商家只创建待付款订单，需到官网付款**。详见[商家能力表](docs/ACCOUNTS-AND-ORDERS.md#商家登录与抢购能力)。结果待核对时先到官网检查，避免重复下单。

## 常用管理

在服务器终端输入 `hw` 打开菜单，也可直接运行：

| 操作 | Debian 原生 | Docker Compose |
| --- | --- | --- |
| 更新 | `hw update` | `hw update` |
| 查看状态 | `hw status` | `hw status` |
| 立即备份 | `hw backup` | `hw backup` |
| 重置管理员密码 | `hw passwd` | `hw reset-admin` |
| 完全卸载（需二次确认） | `hw uninstall` | `hw uninstall` |

更新会保留业务配置与管理员账号。公网管理建议使用 HTTPS 反向代理或限制管理 IP，设置方式见[管理页访问](docs/DEPLOYMENT.md#管理页访问)。

## 详细文档

| 文档 | 内容 |
| --- | --- |
| [部署与管理](docs/DEPLOYMENT.md) | 两种安装、访问方式、管理菜单和升级 |
| [库存监控与通知](docs/MONITORING.md) | 商家支持、大盘筛选、监控与提醒规则 |
| [配置指南](docs/CONFIGURATION.md) | Telegram、商品示例、FlareSolverr 与网页请求 |
| [交易、余额与下单](docs/ACCOUNTS-AND-ORDERS.md) | 二手市场、余额通知、网站账户与待付款订单 |
| [运维与排障](docs/OPERATIONS.md) | 管理员、备份、出口节点与访问异常 |
| [项目结构与开发](docs/ARCHITECTURE.md) | 源码目录、部署兼容与本地检查 |
