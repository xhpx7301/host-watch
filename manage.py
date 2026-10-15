#!/usr/bin/env python3
"""Debian terminal menu for HostWatch; never operates on Linux login users."""
from __future__ import annotations

import argparse
import fcntl
import getpass
import grp
import json
import os
import pwd
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from urllib.request import urlopen

from auth import DEFAULT_USERS_PATH, load_users, password_record, validate_username


INSTALL_DIR = Path("/opt/vmiss-monitor")
DATA_DIR = Path("/var/lib/vmiss-monitor")
ADMIN_ENV = Path("/etc/vmiss-monitor-admin.env")
BACKUP_DIR = Path("/var/backups/stockwatch")
MONITOR = "vmiss-monitor.service"
ADMIN = "vmiss-monitor-admin.service"
INSTALLER_URL = "https://raw.githubusercontent.com/xhpx7301/host-watch/main/install.sh"


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def atomic_write(path: Path, contents: str, mode: int = 0o640, group: int | None = None, owner: int = 0) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"拒绝写入非普通文件：{path}")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
            if group is not None:
                os.fchown(stream.fileno(), owner, group)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_accounts() -> dict:
    if DEFAULT_USERS_PATH.is_symlink():
        raise ValueError("账号文件不能是符号链接")
    if not DEFAULT_USERS_PATH.exists():
        return {}
    return load_users(DEFAULT_USERS_PATH)


def save_accounts(users: dict) -> None:
    if not users:
        raise ValueError("不能删除最后一个管理员，以免失去管理页访问权限")
    if len(users) > 50:
        raise ValueError("最多支持 50 个管理页账号")
    group = grp.getgrnam("vmiss-monitor").gr_gid
    atomic_write(DEFAULT_USERS_PATH, json.dumps({"version": 1, "users": users}, ensure_ascii=False, indent=2) + "\n", group=group)


def activate_accounts() -> None:
    """Select hashed accounts, preserving unrelated proxy/host configuration."""
    if ADMIN_ENV.is_symlink():
        raise ValueError("管理页环境文件不能是符号链接")
    original = ADMIN_ENV.read_text(encoding="utf-8") if ADMIN_ENV.exists() else ""
    retained = [line for line in original.splitlines() if not re.match(r"^\s*(?:ADMIN_USERS_FILE|ADMIN_PASSWORD|ADMIN_USERNAME)\s*=", line)]
    retained.append(f"ADMIN_USERS_FILE={DEFAULT_USERS_PATH}")
    atomic_write(ADMIN_ENV, "\n".join(retained) + "\n", group=grp.getgrnam("vmiss-monitor").gr_gid)
    run("systemctl", "restart", ADMIN)


def ask_password() -> str:
    password = getpass.getpass("新密码（至少 12 个字符，不会显示）：")
    confirmation = getpass.getpass("再次输入新密码：")
    if password != confirmation:
        raise ValueError("两次密码不一致，未修改账号")
    return password


def list_accounts() -> None:
    users = read_accounts()
    if not users:
        print("尚未启用多用户账号。当前仍使用原管理页认证设置。")
        return
    print("管理页管理员（均有完整的配置权限）：")
    for username in sorted(users):
        print(f"  • {username}")


def add_account() -> None:
    users = read_accounts()
    if not users:
        print("首次创建后将启用多用户认证，原 ADMIN_USERNAME/ADMIN_PASSWORD 会被替换。")
    username = validate_username(input("新管理页用户名：").strip())
    if username in users:
        raise ValueError("账号已存在，请使用修改密码功能")
    users[username] = password_record(ask_password())
    save_accounts(users)
    activate_accounts()
    print(f"账号 {username} 已创建。密码不会写入日志或以明文保存。")


def change_password() -> None:
    users = read_accounts()
    username = input("要修改密码的用户名：").strip()
    if username not in users:
        raise ValueError("账号不存在，请先创建管理页账号")
    users[username] = password_record(ask_password())
    save_accounts(users)
    activate_accounts()
    print(f"账号 {username} 的密码已更新。")


def delete_account() -> None:
    users = read_accounts()
    username = input("要删除的用户名：").strip()
    if username not in users:
        raise ValueError("账号不存在")
    if len(users) == 1:
        raise ValueError("不能删除最后一个管理员")
    if input(f"输入用户名 {username} 确认删除：").strip() != username:
        print("已取消。")
        return
    del users[username]
    save_accounts(users)
    print(f"账号 {username} 已删除，下次请求立即失效。")


def backup() -> Path:
    if BACKUP_DIR.is_symlink():
        raise ValueError("备份目录不能是符号链接")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(BACKUP_DIR, 0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    fd, name = tempfile.mkstemp(prefix=f"host-watch-{timestamp}-", suffix=".tar.gz", dir=BACKUP_DIR)
    os.close(fd)
    target = Path(name)
    paths = [DATA_DIR, Path("/etc/vmiss-monitor.env"), ADMIN_ENV, DEFAULT_USERS_PATH,
             Path("/etc/systemd/system") / MONITOR, Path("/etc/systemd/system") / ADMIN,
             Path("/etc/sudoers.d/host-watch-admin"), Path("/etc/tmpfiles.d/host-watch.conf"),
             Path("/usr/local/bin/host-watch"), Path("/usr/local/bin/stockwatch"), Path("/usr/local/bin/hw")]
    if INSTALL_DIR.exists():
        paths.extend(path for path in INSTALL_DIR.iterdir() if path.name not in {".venv", "__pycache__"})
    try:
        with tarfile.open(target, "w:gz", dereference=False) as archive:
            for path in paths:
                if path.exists():
                    archive.add(path, arcname=str(path).lstrip("/"))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    print(f"备份已保存：{target}（仅 root 可访问，包含凭据，请妥善保管）")
    return target


def update() -> None:
    print("将从 GitHub main 更新程序，保留商品、Telegram 和管理员配置。")
    # Download completely before stopping services; a network error changes nothing.
    with urlopen(INSTALLER_URL, timeout=60) as response:
        script = response.read(1024 * 1024 + 1)
    if len(script) > 1024 * 1024 or not script.startswith(b"#!/usr/bin/env bash"):
        raise ValueError("更新脚本格式异常，已取消")
    was_active = subprocess.run(["systemctl", "is-active", "--quiet", MONITOR], check=False).returncode == 0
    snapshot = backup()
    with tempfile.TemporaryDirectory(prefix="host-watch-update-") as temporary:
        installer = Path(temporary) / "install.sh"
        installer.write_bytes(script)
        run("bash", "-n", str(installer))
        run("systemctl", "stop", MONITOR, ADMIN)
        try:
            run("bash", str(installer))
            if was_active:
                run("systemctl", "start", MONITOR)
        except subprocess.CalledProcessError:
            print(f"更新失败；未自动恢复监控运行。原程序和配置备份：{snapshot}")
            subprocess.run(["systemctl", "start", ADMIN], check=False)
            raise
    print("更新完成。" + ("已恢复原本运行中的监控服务。" if was_active else "监控服务保持停用。"))


def service_status() -> None:
    # systemctl status returns 3 for an inactive unit, which is normal.
    subprocess.run(["systemctl", "--no-pager", "--full", "status", MONITOR, ADMIN], check=False)


def start_monitor() -> None:
    config = json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
    if config.get("enabled") is not True:
        raise ValueError("请先在管理页勾选“启用库存检查”并保存，再从菜单启动服务")
    run("systemctl", "enable", "--now", MONITOR)
    print("监控服务已启动并设置开机自启。")


def stop_monitor() -> None:
    run("systemctl", "disable", "--now", MONITOR)
    print("监控服务已停止并取消开机自启；网页配置已保留。")


def restart_monitor() -> None:
    config = json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
    if config.get("enabled") is not True:
        raise ValueError("请先在管理页勾选“启用库存检查”并保存")
    run("systemctl", "restart", MONITOR)
    print("监控服务已重启，将重新读取配置。")


def apply_monitor() -> None:
    """Make the service state match the enabled flag saved by the web admin."""
    config = json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
    if config.get("enabled") is True:
        run("systemctl", "enable", MONITOR)
        run("systemctl", "restart", MONITOR)
        print("监控配置已应用：服务已重启并设置开机自启。")
    else:
        run("systemctl", "disable", "--now", MONITOR)
        print("监控配置已应用：服务已停止并取消开机自启。")


def set_monitor_enabled(enabled: bool) -> None:
    config_path = DATA_DIR / "config.json"
    if config_path.is_symlink():
        raise ValueError("监控配置不能是符号链接")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("监控配置格式无效")
    config["enabled"] = enabled
    atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n", mode=0o600,
                 group=grp.getgrnam("vmiss-monitor").gr_gid, owner=pwd.getpwnam("vmiss-monitor").pw_uid)


def web_action(payload: dict) -> dict:
    action = payload.get("action")
    if not isinstance(action, str):
        raise ValueError("管理操作无效")
    if action == "status":
        def unit_state(unit: str) -> dict:
            active = subprocess.run(["systemctl", "is-active", unit], text=True, capture_output=True, check=False).stdout.strip()
            enabled = subprocess.run(["systemctl", "is-enabled", unit], text=True, capture_output=True, check=False).stdout.strip()
            return {"active": active or "unknown", "enabled": enabled or "unknown"}
        users = read_accounts()
        if not users:
            users = {"(当前使用旧版单账号认证；添加管理员后启用账号管理)": {}}
        backups = []
        if BACKUP_DIR.is_dir() and not BACKUP_DIR.is_symlink():
            for path in sorted(BACKUP_DIR.glob("host-watch-*.tar.gz"), key=lambda item: item.stat().st_mtime, reverse=True)[:20]:
                if path.is_file() and not path.is_symlink():
                    backups.append({"name": path.name, "size": path.stat().st_size, "modified": int(path.stat().st_mtime)})
        return {"monitor": unit_state(MONITOR), "admin": unit_state(ADMIN), "users": sorted(users), "backups": backups}
    if action == "logs":
        result = subprocess.run(["journalctl", "--no-pager", "-o", "short-iso", "-u", MONITOR, "-u", ADMIN, "-n", "100"], text=True, capture_output=True, check=False, timeout=15)
        if result.returncode:
            raise ValueError(result.stderr.strip()[:500] or "读取日志失败")
        return {"logs": result.stdout[-40_000:]}
    if action in {"monitor-start", "monitor-stop", "monitor-restart", "monitor-apply"}:
        if action == "monitor-start":
            set_monitor_enabled(True)
            start_monitor()
        elif action == "monitor-stop":
            set_monitor_enabled(False)
            stop_monitor()
        else:
            {"monitor-restart": restart_monitor, "monitor-apply": apply_monitor}[action]()
        return {"ok": True, "message": "监控服务操作已完成。"}
    if action == "backup":
        path = backup()
        return {"ok": True, "message": f"备份完成：{path.name}"}
    if action == "update":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        unit = f"host-watch-web-update-{stamp}-{secrets.token_hex(4)}.service"
        run("systemd-run", "--quiet", "--no-block", f"--unit={unit.removesuffix('.service')}",
            "--property=Type=oneshot", "--property=RemainAfterExit=yes", "--property=TimeoutStartSec=15min",
            "/usr/bin/python3", str(INSTALL_DIR / "manage.py"), "web-update-worker")
        return {"ok": True, "message": "更新任务已启动。管理页和监控可能短暂重启。", "update_unit": unit}
    if action == "admin-restart":
        run("systemd-run", "--quiet", "--on-active=2s", "/usr/bin/systemctl", "restart", ADMIN)
        return {"ok": True, "message": "管理页将在 2 秒后重启，连接将短暂中断。"}
    if action == "update-status":
        unit = payload.get("unit", "")
        if not isinstance(unit, str) or not re.fullmatch(r"host-watch-web-update-[0-9]{14}-[0-9a-f]{8}\.service", unit):
            raise ValueError("更新任务编号无效")
        result = subprocess.run(["systemctl", "show", unit, "--property=LoadState", "--property=ActiveState", "--property=SubState", "--property=Result"], text=True, capture_output=True, check=False, timeout=10)
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        logs = subprocess.run(["journalctl", "--no-pager", "-u", unit, "-n", "30"], text=True, capture_output=True, check=False, timeout=10)
        return {"unit": unit, **values, "logs": logs.stdout[-12_000:]}
    if action in {"admin-add", "admin-passwd", "admin-delete"}:
        username = validate_username(str(payload.get("username", "")))
        users = read_accounts()
        if action == "admin-add":
            if username in users:
                raise ValueError("管理员账号已存在")
            if len(users) >= 50:
                raise ValueError("最多支持 50 个管理页账号")
        elif username not in users:
            raise ValueError("管理员账号不存在")
        if action == "admin-delete":
            if len(users) == 1:
                raise ValueError("不能删除最后一个管理员")
            del users[username]
        else:
            password = payload.get("password")
            confirmation = payload.get("confirmation")
            if not isinstance(password, str) or password != confirmation:
                raise ValueError("两次密码不一致")
            users[username] = password_record(password)
        save_accounts(users)
        return {"ok": True, "message": {"admin-add": "管理员已添加。", "admin-passwd": "管理员密码已更新。", "admin-delete": "管理员已删除。"}[action]}
    raise ValueError("不支持此管理操作")


def menu() -> None:
    actions = {
        "1": service_status,
        "2": update,
        "3": start_monitor,
        "4": stop_monitor,
        "5": lambda: run("systemctl", "restart", ADMIN),
        "6": lambda: run("journalctl", "--no-pager", "-u", MONITOR, "-u", ADMIN, "-n", "100"),
        "7": list_accounts,
        "8": add_account,
        "9": change_password,
        "10": delete_account,
        "11": backup,
        "12": restart_monitor,
    }
    while True:
        print("\n──────── HostWatch Debian 管理菜单 ────────")
        print(" 1. 查看服务状态       2. 更新脚本（先备份）")
        print(" 3. 启动库存监控       4. 停止库存监控")
        print(" 5. 重启管理页         6. 查看最近日志")
        print(" 7. 查看管理页用户     8. 添加管理页用户")
        print(" 9. 修改用户密码      10. 删除管理页用户")
        print("11. 备份程序和配置    12. 重启监控 / 载入配置")
        print(" 0. 退出")
        choice = input("请选择：").strip()
        if choice == "0":
            return
        action = actions.get(choice)
        if action is None:
            print("请输入菜单中的编号。")
            continue
        try:
            action()
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
            print(f"操作失败：{exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="HostWatch Debian 管理菜单；用户指管理页账号。")
    parser.add_argument("command", nargs="?", default="menu", choices=["menu", "status", "update", "backup", "users", "add-user", "passwd", "delete-user", "web-action", "web-update-worker"])
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("请使用 sudo hw 或切换至 root")
    release = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            release[key] = value.strip("\"'")
    if release.get("ID") != "debian" or release.get("VERSION_ID") not in {"12", "13"}:
        parser.error("此菜单仅用于 Debian 12/13 系统")
    if not (INSTALL_DIR / "admin.py").is_file():
        parser.error("尚未安装 HostWatch，请先执行 README 中的一键安装")
    # Avoid two terminals racing an update/account mutation or overwriting users.
    lock_fd = os.open("/run/stockwatch-manager.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w", encoding="utf-8") as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            if args.command == "web-update-worker":
                fcntl.flock(lock, fcntl.LOCK_EX)
            else:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("已有管理菜单或网页操作正在运行，请稍后重试")
        try:
            if args.command == "web-action":
                payload = json.loads(sys.stdin.read(16_385))
                if not isinstance(payload, dict) or len(json.dumps(payload)) > 16_384:
                    raise ValueError("管理请求无效或过大")
                print(json.dumps(web_action(payload), ensure_ascii=False))
            elif args.command == "web-update-worker":
                update()
            else:
                actions = {"menu": menu, "status": service_status, "update": update, "backup": backup,
                           "users": list_accounts, "add-user": add_account, "passwd": change_password,
                           "delete-user": delete_account}
                actions[args.command]()
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            if args.command == "web-action":
                print(json.dumps({"error": str(exc)[:1000]}, ensure_ascii=False))
            else:
                print(f"操作失败：{exc}")
            return 1
        except (KeyboardInterrupt, EOFError):
            print("\n已退出管理菜单。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
