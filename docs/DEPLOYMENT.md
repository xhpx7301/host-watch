# 部署与管理

[返回首页](../README.md)

## 如何选择部署方式

**当前优先推荐 Debian 12/13 原生部署**，适合需要完整网页运维和 `hw` 管理菜单的服务器。Docker 使用同一份库存、账户和抢购代码，但容器内的系统管理能力有限；切换部署方式不会为尚未适配的商家增加自动抢购能力。

| 功能 | Debian 原生 | Docker Compose |
| --- | --- | --- |
| 库存大盘、监控、Telegram、商家登录和已适配抢购 | 使用同一套业务代码 | 使用同一套业务代码，仍需保证商家及出口网络可达 |
| 网页更新 | 网页直接启动更新任务 | 网页复制命令，在部署主机执行 `hw update` |
| 管理页 / 服务重启 | 网页和 `hw` 菜单管理 systemd 服务 | 主机终端执行 `hw restart`；网页不能重启容器或服务器 |
| 运行日志 | 网页读取 journal 日志 | 在主机执行 `hw logs`；网页日志只显示终端命令提示 |
| 服务状态 | 读取 systemd 实际状态 | 网页返回容器模式状态，实际容器健康请用 `hw status` 查看 |
| 自动备份 | systemd 定时任务 | 独立 backup 容器；需保持该容器运行 |
| 备份、商品配置与通知设置 | 网页及菜单操作 | 可使用网页及对应 `hw` 命令；主机级管理功能不等同原生部署 |

Docker 的上述限制不能通过切换商家登录方式解决。需要定位某个无法使用的业务功能时，先运行 `hw status`、`hw logs`，再核对该商家的登录与抢购能力。原生安装也默认使用独立 FlareSolverr 容器，只将 HostWatch 主程序交给 systemd 管理。

两种方式**首次部署的默认管理员均为 `admin` / `admin123456789`**。已有账号和旧密码在更新时保留；登录后在“设置 → 管理员账号”修改密码。

## Debian 原生部署（非 Docker，推荐）

适用于 Debian 12/13，要求 systemd 和 root/sudo 权限。脚本会安装依赖、创建 `vmiss-monitor` 系统用户、自动部署 Docker 和官方 FlareSolverr 容器、安装程序和 systemd 单元，并启动监听 `0.0.0.0:25678` 的公网管理页。防火墙需允许 TCP `25678`；FlareSolverr 仍仅监听本机 `127.0.0.1:8191`。库存监控默认关闭，安装脚本不会启用或启动它。首次部署需要下载浏览器镜像，请预留内存、磁盘和到 ghcr.io 的网络连接；浏览器请求按商品顺序执行。

```sh
curl -fsSL https://raw.githubusercontent.com/xhpx7301/host-watch/main/install.sh | sudo bash
```

脚本可重复执行用于升级，已有的 `/var/lib/vmiss-monitor/config.json`、`/etc/vmiss-monitor.env` 和 `/etc/vmiss-monitor-admin.env` 会保留；管理页服务名为 `hostwatch.service`，旧的 `vmiss-monitor-admin.service` 仅作为兼容别名。

全新安装会自动创建管理页管理员 `admin`，初始密码为 `admin123456789`，在首次安装终端显示；认证文件仅保存密码哈希。首次登录后请修改密码，遗失后可运行 `hw passwd` 重置。安装器保留已有账号、旧密码及自定义账号文件；旧版未配置账号的安装可运行 `hw init-admin` 补建，不会重置已有账号。

项目已更名为 HostWatch，仓库名为 `host-watch`。为兼容已部署版本，继续沿用 `vmiss-monitor` 安装目录、系统用户和监控服务名，以及 `/etc/stockwatch-users.json`、`/var/backups/stockwatch/` 等数据路径；管理页服务名使用 `hostwatch.service`，菜单快捷命令为 `hw`。

## Docker Compose 部署

Docker 部署不需要 systemd，也不挂载宿主机 Docker socket。管理页、监控、自动备份和 FlareSolverr 会自动创建为 Compose 服务，管理页对公网监听 `25678`；防火墙只需放行 TCP `25678`，FlareSolverr 的 `8191` 保持内网访问。

在全新 Debian/Ubuntu 服务器执行一条命令：

```sh
curl -fsSL https://raw.githubusercontent.com/xhpx7301/host-watch/main/docker/install.sh | sudo sh
```

安装脚本会检查 Docker Compose、克隆项目到 `/opt/host-watch`、构建并启动服务，并安装 `hw` 快捷命令。Docker 首次初始化管理员为 `admin` / `admin123456789`；登录后请立即修改密码。也可以查看启动日志确认：

```sh
docker compose -f /opt/host-watch/docker-compose.yml logs --no-log-prefix hostwatch | grep '初始密码'
```

访问 `http://服务器公网地址:25678`，使用 `admin` / `admin123456789` 登录。以后在任意目录输入 `hw` 呼出 Docker 管理菜单；常用命令如下：

```sh
hw                  # 交互式菜单
hw update           # 拉取 main、更新 FlareSolverr、重建并启动
hw status           # 查看容器状态
hw logs             # 查看管理页、监控和备份日志
hw backup           # 立即备份数据
hw restart          # 重启 HostWatch 容器
hw reset-admin      # 将 Docker 管理员重置为 admin / admin123456789
hw uninstall        # 二次确认后完全卸载 Docker 部署
```

管理页“设置 → 运维操作”中的 Docker 更新按钮会复制 `hw update`。管理页容器不控制宿主机 Docker，复制后请在部署服务器终端执行；这样即使管理页对公网开放，也不会把 Docker socket 暴露给网页进程。

管理页 Compose 服务名称为 `hostwatch`，监控和备份服务名称分别为 `monitor`、`backup`。升级脚本会自动清理旧版的 `admin` 容器，不会删除命名卷中的数据。

已有旧版 Compose 部署时，在项目目录执行一次下面的命令即可补装 `hw` 菜单并升级到当前配置；命名卷中的配置、账号和备份会保留：

```sh
cd /你的/host-watch目录
sudo env HOSTWATCH_DIR="$PWD" sh docker/install.sh
```

如果 `admin` / `admin123456789` 登录失败，说明数据卷中已有旧管理员账号。执行 `hw reset-admin` 即可新增或重置 `admin`，不会删除其他管理员；然后重新打开登录页再试。

`hw uninstall` 需要输入 `DELETE HOSTWATCH` 确认，会删除 HostWatch 容器、项目镜像、命名卷、配置、管理员账号、备份、项目目录和 `hw` 命令；其他 Docker 项目和宿主机 Docker 本身不受影响。

## Debian 管理菜单

安装或升级完成后，在 Debian 12/13 终端运行：

```sh
hw
```

直接输入 `hw` 即可调出菜单；普通用户会自动通过 sudo 请求权限，root 无需 sudo。`host-watch` 命令也可调用同一菜单，原 `stockwatch` 命令继续兼容。菜单按“监控服务”“管理页服务”“更新与备份”“管理员账号”分成二级菜单，常用配置、更新、日志和账号操作也可在网页的“管理员管理”中完成。菜单还提供“完全删除 HostWatch”选项：输入 `DELETE HOSTWATCH` 二次确认后，会停止服务并删除程序、配置、库存历史、管理员账号、备份、快捷命令、HostWatch 创建的 FlareSolverr 容器及系统账号；不会卸载 Docker、Python 等共享依赖，也不会删除外部 FlareSolverr 容器。也可以直接执行：

旧版使用 `jk` 的服务器，重新执行一键安装后改用 `hw`；安装器会移除内容与本项目旧启动脚本完全一致的 `jk` 文件，不会删除其他同名脚本。

```sh
hw update       # 下载更新，先备份，再安装
hw backup       # 备份程序、配置和账号
hw status       # 查看服务状态
hw users        # 列出管理页用户
hw add-user     # 交互式添加用户
hw passwd       # 交互式修改密码
hw delete-user  # 交互式删除用户
hw init-admin   # 尚无认证时创建 admin 和默认初始密码，已有账号保持不变
hw admin-domain              # 兼容旧命令：检查账号并重启，已无需设置域名
hw uninstall   # 二次确认后从服务器完全删除 HostWatch
```

这里的用户是**管理页管理员**，都有完整配置权限，不是 Debian/SSH 系统账号。密码在终端中隐藏输入，不支持通过命令行参数传递。账号保存于 `/etc/stockwatch-users.json`，使用独立随机盐和 PBKDF2-SHA256 哈希，权限为 `root:vmiss-monitor 0640`。不能删除最后一个管理员。

首次创建账号后，菜单会将旧 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 配置替换为 `ADMIN_USERS_FILE`，保留域名等其他设置，并重启管理页。以后修改密码或删除账号后，旧凭据在下一次请求即失效。仅本机 SSH 隧道访问的旧安装可继续沿用原认证方式；域名反向代理必须配置账号或原有密码。

更新使用 GitHub `main` 的安装程序，保留商品、Telegram 和账号配置；成功后仅恢复更新前正在运行的监控服务。下载脚本发生网络超时等临时错误时最多尝试三次，最后一次使用 GitHub 官方备用入口；证书校验失败、HTTP 404 或内容格式异常直接停止，不关闭 TLS 校验、不使用第三方镜像。下载脚本失败发生在备份和停止服务之前，现有程序、配置和服务均不改动。进入安装阶段后失败则不会自动恢复监控，终端会输出备份路径。备份位于 `/var/backups/stockwatch/`，仅 root 可访问；其中包含配置凭据，应按敏感文件保管。备份不包含虚拟环境，恢复程序后可重新安装依赖。菜单不提供自动恢复操作。

首次使用时，在管理页配置商品，分别选择库存监控和库存通知的自动刷新方式，点击各自卡片的保存按钮应用。两个模块都设为仅手动刷新时，HostWatch 自动检查服务会停止；商品配置保持原样。管理菜单同一时间只允许一个终端运行。

## 管理页访问

### 公网访问

原生部署的管理页监听 `0.0.0.0:25678`，放行防火墙后直接访问：

```sh
http://服务器公网地址:25678
```

公网访问必须使用管理员账号；生产环境建议前置 HTTPS 反向代理或限制固定管理 IP。

### SSH 隧道（可选）

如果不希望从公网直接访问，也可以通过 SSH 隧道访问同一端口：

```sh
ssh -N -L 25678:127.0.0.1:25678 your-user@your-server
```

保持 SSH 窗口运行，然后打开 [http://127.0.0.1:25678](http://127.0.0.1:25678)。

### 反向代理域名

使用 Nginx、Caddy 或面板将 HTTPS 域名转发到 `http://127.0.0.1:25678`，并传递原始 `Host`，即可用管理员账号登录。管理页不限制访问域名，无需逐个添加允许列表；旧配置中的 `ADMIN_ALLOWED_HOSTS`（包括 `*`）不再生效，也不会阻止启动。

新安装自动创建 `admin`，初始密码为 `admin123456789`，并在终端显示。旧安装如果尚无账号，先执行：

```sh
hw init-admin
```

已有账号、旧版密码或自定义账号文件会保留。没有配置账号时，通过域名访问会提示初始化账号；原有本地 SSH 隧道访问仍可使用。忘记密码可执行 `hw passwd`。菜单中的“管理页服务 → 检查管理页账号并重启”也可补建缺失账号。

迁移服务器或更换域名时，只需调整 DNS 和反向代理，无需修改 HostWatch 允许列表。账号仍由新服务器的安装及账号配置管理，导入业务备份不会覆盖管理员账号。旧命令 `hw admin-domain [域名]` 保留兼容，只检查账号并重启服务，不再保存或限制域名。

管理服务监听 `0.0.0.0:25678`，管理员认证和浏览器跨站请求校验保持启用。通过公网地址、域名或 SSH 隧道打开管理页时会显示 HostWatch 登录界面，登录成功后使用仅限当前浏览器的 HttpOnly 会话 Cookie；页面右上角可退出登录。旧版 Basic Auth 请求仍兼容，便于已有脚本和反向代理继续使用。

## 升级

在网页管理员管理区点击“更新 HostWatch”，或在服务器运行 `hw update`。升级不会覆盖商品、Telegram 或管理员配置；更新前自动备份，成功后恢复原来正在运行的监控。网页更新启动后约 10 秒自动刷新管理页，并恢复刷新前所在的栏目。手动刷新浏览器也会保留当前栏目。旧版本首次升级请先运行一次 `hw update`。
