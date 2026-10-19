#!/usr/bin/env python3
"""Debian terminal menu for HostWatch; never operates on Linux login users."""
from __future__ import annotations

import argparse
import base64
from collections.abc import Callable
import fcntl
import getpass
import gzip
import grp
import io
import json
import os
import pwd
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import zlib
from datetime import datetime, timezone
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

from auth import DEFAULT_USERS_PATH, load_users, password_record, validate_username
from time_utils import BEIJING_TZ, format_beijing, now_beijing


INSTALL_DIR = Path("/opt/vmiss-monitor")
DATA_DIR = Path("/var/lib/vmiss-monitor")
ADMIN_ENV = Path("/etc/vmiss-monitor-admin.env")
LEGACY_ENV = Path("/etc/vmiss-monitor.env")
BACKUP_DIR = Path("/var/backups/stockwatch")
BACKUP_SETTINGS = Path("/etc/hostwatch-backup.json")
SYSTEMD_DIR = Path("/etc/systemd/system")
BACKUP_TIMER = "hostwatch-backup.timer"
BACKUP_SERVICE = "hostwatch-backup.service"
MONITOR = "vmiss-monitor.service"
ADMIN = "vmiss-monitor-admin.service"
SERVICE_USER = "vmiss-monitor"
SERVICE_GROUP = "vmiss-monitor"
INSTALLER_URL = "https://raw.githubusercontent.com/xhpx7301/host-watch/main/install.sh"
INSTALLER_FALLBACK_URL = "https://github.com/xhpx7301/host-watch/raw/refs/heads/main/install.sh"
MAX_BACKUP_UPLOAD = 20 * 1024 * 1024
MAX_BACKUP_EXPANDED = 64 * 1024 * 1024
MAX_BACKUP_REQUEST = (MAX_BACKUP_UPLOAD + 2) // 3 * 4 + 4096
MAX_BACKUPS = 30
RESTORE_DATA_FILES = ("config.json", "state.json", "state-seckill.json", "state-akile-market.json", "status.json", "catalog.json", "exchange-rates.json")


def run(*args: str) -> None:
    env = {**os.environ, "TZ": "Asia/Shanghai"} if args and args[0] == "journalctl" else None
    subprocess.run(args, check=True, env=env)


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


def activate_accounts(restart: bool = True) -> None:
    """Select hashed accounts, preserving unrelated proxy/host configuration."""
    if ADMIN_ENV.is_symlink():
        raise ValueError("管理页环境文件不能是符号链接")
    original = ADMIN_ENV.read_text(encoding="utf-8") if ADMIN_ENV.exists() else ""
    retained = [line for line in original.splitlines() if not re.match(r"^\s*(?:ADMIN_USERS_FILE|ADMIN_PASSWORD|ADMIN_USERNAME)\s*=", line)]
    retained.append(f"ADMIN_USERS_FILE={DEFAULT_USERS_PATH}")
    atomic_write(ADMIN_ENV, "\n".join(retained) + "\n", group=grp.getgrnam("vmiss-monitor").gr_gid)
    if restart:
        run("systemctl", "restart", ADMIN)


def admin_environment() -> dict:
    """Read the two EnvironmentFiles in the same order as the admin unit."""
    values = {}
    for path in (LEGACY_ENV, ADMIN_ENV):
        if path.is_symlink():
            raise ValueError(f"管理页环境文件不能是符号链接：{path}")
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*(ADMIN_[A-Z_]+)\s*=(.*)$", line)
            if match:
                values[match[1]] = " ".join(shlex.split(match[2], comments=False))
    return values


def initialize_admin(restart: bool = True) -> bool:
    """Create unique initial credentials only when no authentication exists."""
    environment = admin_environment()
    configured_path = environment.get("ADMIN_USERS_FILE", "").strip()
    if configured_path:
        load_users(Path(configured_path))
        print("已有管理员账号，保持原账号和密码。")
        return False
    if read_accounts():
        print("已有管理员账号，保持原账号和密码。")
        return False
    if environment.get("ADMIN_PASSWORD"):
        print("已有管理页密码，保持原认证设置。")
        return False
    password = secrets.token_urlsafe(24)
    save_accounts({"admin": password_record(password)})
    activate_accounts(restart=False)
    print("已创建初始管理页管理员（密码仅显示本次，不以明文保存）：")
    print("  用户名：admin")
    print(f"  随机密码：{password}")
    print("请保存上述密码；遗失可运行 hw passwd 重置 admin 的密码。")
    if restart:
        run("systemctl", "restart", ADMIN)
    return True


def configure_admin_domain(domain: str | None = None) -> None:
    """Compatibility entry point for older hw admin-domain instructions."""
    initialize_admin(restart=False)
    run("systemctl", "restart", ADMIN)
    print("管理页已取消域名允许列表，所有访问域名均可使用，无需逐个配置。")
    print("已检查管理员账号并重启管理页。反向代理后端保持 http://127.0.0.1:8765，使用 HTTPS 域名访问并登录。")


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
    timestamp = now_beijing().strftime("%Y%m%d-%H%M%S")
    fd, name = tempfile.mkstemp(prefix=f"host-watch-{timestamp}-", suffix=".tar.gz", dir=BACKUP_DIR)
    os.close(fd)
    target = Path(name)
    paths = [DATA_DIR, Path("/etc/vmiss-monitor.env"), ADMIN_ENV, DEFAULT_USERS_PATH, BACKUP_SETTINGS,
             SYSTEMD_DIR / BACKUP_SERVICE, SYSTEMD_DIR / BACKUP_TIMER,
             Path("/etc/systemd/system") / MONITOR, Path("/etc/systemd/system") / ADMIN,
             Path("/etc/sudoers.d/host-watch-admin"), Path("/etc/tmpfiles.d/host-watch.conf"),
             Path("/usr/local/bin/host-watch"), Path("/usr/local/bin/stockwatch"), Path("/usr/local/bin/hw")]
    if INSTALL_DIR.exists():
        paths.extend(path for path in INSTALL_DIR.iterdir() if path.name not in {".venv", "__pycache__"})
    try:
        with tarfile.open(target, "w:gz", dereference=False) as archive:
            for path in paths:
                if path.exists():
                    archive.add(path, arcname=str(path).lstrip("/"), filter=lambda member: None if Path(member.name).name in {"merchant-auth-cache.json", "merchant-auth-cache.key"} else member)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    print(f"备份已保存：{target}（仅 root 可访问，包含凭据，请妥善保管）")
    prune_backups()
    return target


def prune_backups() -> None:
    """Keep only the newest server-side backup archives."""
    if BACKUP_DIR.is_symlink() or not BACKUP_DIR.is_dir():
        return
    backups = []
    for path in BACKUP_DIR.glob("host-watch-*.tar.gz"):
        try:
            if path.is_file() and not path.is_symlink():
                backups.append((path.stat().st_mtime_ns, path.name, path))
        except OSError:
            continue
    backups.sort(reverse=True)
    for _, _, path in backups[MAX_BACKUPS:]:
        try:
            path.unlink()
        except OSError:
            pass


def backup_for_download() -> dict:
    """Create a one-time archive for the authenticated browser download."""
    target = backup()
    try:
        encoded = base64.b64encode(target.read_bytes()).decode("ascii")
        return {"name": target.name, "content": encoded}
    finally:
        target.unlink(missing_ok=True)


def validate_restore_config(value: dict) -> dict:
    """Validate using the installed application's dependencies, never archive code."""
    value = {**value, "enabled": False}
    result = subprocess.run(
        [str(INSTALL_DIR / ".venv/bin/python"), "-I", "-c",
         "import json,sys; sys.path.insert(0,sys.argv[1]); from admin import validate_config; "
         "print(json.dumps(validate_config(json.load(sys.stdin)),ensure_ascii=False))", str(INSTALL_DIR)],
        input=json.dumps(value, ensure_ascii=False), capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError("备份中的商品配置无法通过当前版本校验，请检查备份文件或先更新 HostWatch")
    return json.loads(result.stdout)


def read_restore_archive(content: str) -> tuple[dict, dict | None]:
    if not isinstance(content, str) or not content or len(content) > MAX_BACKUP_REQUEST - 4096:
        raise ValueError("导入备份文件必须为不超过 20 MB 的 tar.gz 文件")
    try:
        raw = base64.b64decode(content, validate=True)
    except ValueError:
        raise ValueError("备份内容编码无效") from None
    if not raw or len(raw) > MAX_BACKUP_UPLOAD:
        raise ValueError("导入备份文件必须为不超过 20 MB 的 tar.gz 文件")
    allowed = {str(DATA_DIR / name).lstrip("/"): name for name in RESTORE_DATA_FILES}
    allowed[str(BACKUP_SETTINGS).lstrip("/")] = "backup_schedule"
    contents, total, count = {}, 0, 0
    try:
        # Bound actual decompression (including PAX metadata) and verify gzip's
        # checksum before parsing tar headers or changing any server data.
        with tempfile.TemporaryFile() as unpacked:
            expanded = 0
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                while chunk := compressed.read(1024 * 1024):
                    expanded += len(chunk)
                    if expanded > MAX_BACKUP_EXPANDED:
                        raise ValueError("备份解压内容过大，无法导入")
                    unpacked.write(chunk)
            unpacked.seek(0)
            # Never extract archive paths, permissions, users, programs or services.
            with tarfile.open(fileobj=unpacked, mode="r:") as archive:
                for member in archive:
                    count += 1
                    total += member.size
                    path = PurePosixPath(member.name)
                    if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                        raise ValueError("备份含不安全的文件路径")
                    if member.size < 0 or count > 5000 or total > MAX_BACKUP_EXPANDED:
                        raise ValueError("备份解压内容过大，无法导入")
                    name = allowed.get(str(path))
                    if name is None:
                        continue
                    if not member.isfile() or member.issparse() or name in contents:
                        raise ValueError("备份数据文件必须为唯一的普通文件")
                    stream = archive.extractfile(member)
                    value = json.loads(stream.read().decode("utf-8"))
                    if not isinstance(value, dict):
                        raise ValueError("备份数据文件必须为 JSON 对象")
                    contents[name] = value
    except (tarfile.TarError, EOFError, OSError, zlib.error, UnicodeError, json.JSONDecodeError):
        raise ValueError("备份文件损坏或不是有效的 HostWatch tar.gz 备份") from None
    if "config.json" not in contents:
        raise ValueError("备份缺少 HostWatch 商品配置，无法导入")
    contents["config.json"] = validate_restore_config(contents["config.json"])
    schedule = contents.pop("backup_schedule", None)
    if schedule is not None:
        schedule = normalize_backup_schedule(schedule)
    return contents, schedule


def restore_backup(payload: dict) -> dict:
    contents, schedule = read_restore_archive(payload.get("content"))
    if DATA_DIR.is_symlink() or not DATA_DIR.is_dir():
        raise ValueError("数据目录无效，无法导入备份")
    targets = [DATA_DIR / name for name in RESTORE_DATA_FILES]
    if schedule is not None:
        targets.append(BACKUP_SETTINGS)
    for path in targets:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"拒绝恢复非普通文件：{path}")
    owner = pwd.getpwnam(SERVICE_USER).pw_uid
    group = grp.getgrnam(SERVICE_GROUP).gr_gid
    previous_schedule = read_backup_schedule() if schedule is not None else None
    was_active = _systemd_state(MONITOR) == "active"
    # Quiesce the monitor before taking the rollback snapshot or changing data.
    run("systemctl", "stop", MONITOR)
    snapshot = None
    changed = False
    try:
        originals = {path: path.read_text(encoding="utf-8") if path.exists() else None for path in targets}
        snapshot = backup()
        changed = True
        for name in RESTORE_DATA_FILES:
            value = contents.get(name, {})
            if name == "status.json":
                value = {**value, "state": "paused", "message": "备份已导入，监控已暂停，请确认配置后启动。"}
            atomic_write(DATA_DIR / name, json.dumps(value, ensure_ascii=False) + "\n", mode=0o600, owner=owner, group=group)
        if schedule is not None:
            save_backup_schedule(schedule)
        run("systemctl", "disable", MONITOR)
    except Exception as exc:
        try:
            if changed:
                for path, original in originals.items():
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write(path, original, mode=0o600, owner=owner if path.parent == DATA_DIR else 0,
                                     group=group if path.parent == DATA_DIR else None)
                if previous_schedule is not None:
                    apply_backup_schedule(previous_schedule)
            if was_active:
                run("systemctl", "start", MONITOR)
        except Exception:
            raise ValueError(f"导入失败且自动回退未完成，请检查服务日志；恢复前备份：{snapshot or '未生成'}") from None
        raise ValueError(f"导入失败，已保留或恢复原数据：{exc}") from None
    return {"ok": True, "message": "备份已导入，监控已暂停，请确认配置后手动启动。", "rollback_path": str(snapshot)}


def normalize_backup_schedule(value: object) -> dict:
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ValueError("自动备份启用状态必须是布尔值")
    clock_time = value.get("time")
    if not isinstance(clock_time, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock_time):
        raise ValueError("自动备份时间必须是 00:00 到 23:59，格式为 HH:MM")
    return {"enabled": value["enabled"], "time": clock_time}


def read_backup_schedule() -> dict:
    if BACKUP_SETTINGS.is_symlink():
        raise ValueError("备份计划文件不能是符号链接")
    if not BACKUP_SETTINGS.exists():
        return {"enabled": False, "time": "03:00"}
    return normalize_backup_schedule(json.loads(BACKUP_SETTINGS.read_text(encoding="utf-8")))


def apply_backup_schedule(settings: dict) -> None:
    settings = normalize_backup_schedule(settings)
    # All executable paths and unit names are fixed; only validated HH:MM is interpolated.
    atomic_write(SYSTEMD_DIR / BACKUP_SERVICE, """[Unit]
Description=HostWatch scheduled local backup

[Service]
Type=oneshot
User=root
UMask=0077
Environment=TZ=Asia/Shanghai
ExecStart=/usr/bin/python3 /opt/vmiss-monitor/manage.py scheduled-backup
TimeoutStartSec=15min
""", mode=0o644)
    atomic_write(SYSTEMD_DIR / BACKUP_TIMER, f"""[Unit]
Description=HostWatch daily backup schedule

[Timer]
OnCalendar=*-*-* {settings['time']}:00 Asia/Shanghai
Persistent=true
AccuracySec=1min
Unit={BACKUP_SERVICE}

[Install]
WantedBy=timers.target
""", mode=0o644)
    run("systemctl", "daemon-reload")
    if settings["enabled"]:
        run("systemctl", "enable", BACKUP_TIMER)
        run("systemctl", "restart", BACKUP_TIMER)
    else:
        run("systemctl", "disable", "--now", BACKUP_TIMER)


def backup_schedule_status() -> dict:
    error = ""
    try:
        settings = read_backup_schedule()
    except (OSError, ValueError) as exc:
        settings = {"enabled": False, "time": "03:00"}
        error = f"读取备份计划失败：{exc}"
    def properties(unit, keys):
        try:
            result = subprocess.run(["systemctl", "show", unit, *[f"--property={key}" for key in keys]],
                text=True, capture_output=True, check=False, timeout=5)
            return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        except (OSError, subprocess.TimeoutExpired):
            return {}
    timer = properties(BACKUP_TIMER, ["ActiveState", "UnitFileState", "NextElapseUSecRealtime", "LastTriggerUSec"])
    service = properties(BACKUP_SERVICE, ["Result", "ActiveState"])
    return {**settings, "timezone": "Asia/Shanghai (UTC+08:00)", "error": error,
        "timer_active": timer.get("ActiveState", "unknown"), "timer_enabled": timer.get("UnitFileState", "unknown"),
        "next_run": _systemd_time_beijing(timer.get("NextElapseUSecRealtime", "")), "last_run": _systemd_time_beijing(timer.get("LastTriggerUSec", "")),
        "last_result": service.get("Result", ""), "backup_active": service.get("ActiveState", "unknown")}


def _systemd_time_beijing(value: str) -> str:
    if not value or value == "n/a":
        return value
    match = re.search(r"(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2})(?:\s+(?P<zone>Z|UTC|GMT|CST|[+-]\d{2}(?::?\d{2})?))?", value)
    if not match:
        return value
    zone = match.group("zone") or "Asia/Shanghai"
    if zone == "CST":
        tz = BEIJING_TZ
    elif zone in {"Z", "UTC", "GMT"}:
        tz = timezone.utc
    elif zone.startswith(("+", "-")):
        normalized_zone = f"{zone}:00" if len(zone) == 3 else zone if ":" in zone else zone[:3] + ":" + zone[3:]
        tz = datetime.strptime(normalized_zone, "%z").tzinfo
    else:
        tz = BEIJING_TZ
    parsed = datetime.strptime(f"{match.group('date')} {match.group('time')}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    return format_beijing(parsed)


def save_backup_schedule(payload: dict) -> dict:
    settings = normalize_backup_schedule(payload)
    previous = read_backup_schedule()
    existed = BACKUP_SETTINGS.exists()
    atomic_write(BACKUP_SETTINGS, json.dumps(settings) + "\n", mode=0o600)
    try:
        apply_backup_schedule(settings)
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        try:
            if existed:
                atomic_write(BACKUP_SETTINGS, json.dumps(previous) + "\n", mode=0o600)
            else:
                BACKUP_SETTINGS.unlink(missing_ok=True)
            apply_backup_schedule(previous)
        except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise ValueError("备份计划应用失败，回退也未完成；请刷新状态并查看服务日志") from None
        raise ValueError("备份计划应用失败，已恢复原计划；请检查 systemd 服务日志") from None
    return {"ok": True, "message": "备份计划已保存并生效。", "backup_schedule": backup_schedule_status()}


def download_installer() -> bytes:
    """Retry transient downloads using only official GitHub HTTPS endpoints."""
    for attempt, url in enumerate((INSTALLER_URL, INSTALLER_URL, INSTALLER_FALLBACK_URL), 1):
        print(f"正在下载更新脚本（{attempt}/3，{('GitHub 官方备用入口' if url == INSTALLER_FALLBACK_URL else 'raw.githubusercontent.com')}）…", flush=True)
        try:
            with urlopen(url, timeout=60) as response:
                script = response.read(1024 * 1024 + 1)
            if len(script) > 1024 * 1024 or not script.startswith(b"#!/usr/bin/env bash"):
                raise ValueError("更新脚本格式异常，已取消；现有程序、配置和服务未改动")
            return script
        except (OSError, URLError) as exc:
            reason = getattr(exc, "reason", exc)
            retryable = True
            if isinstance(exc, HTTPError):
                detail = f"GitHub 返回 HTTP {exc.code}"
                retryable = exc.code in {408, 429} or 500 <= exc.code < 600
                exc.close()
            elif isinstance(reason, ssl.SSLCertVerificationError):
                detail = "TLS 证书校验失败，请检查系统时间、ca-certificates 和代理证书"
                retryable = False
            elif isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
                detail = "连接或 TLS 握手超时，请检查服务器到 GitHub 的网络、IPv4/IPv6 路由及代理"
            elif isinstance(reason, socket.gaierror):
                detail = "GitHub 域名解析失败，请检查服务器 DNS"
            else:
                detail = "无法完成 GitHub HTTPS 下载，请检查服务器网络和代理"
            if attempt == 3 or not retryable:
                raise OSError(f"更新脚本下载失败：{detail}。现有程序、配置和服务未改动；网络恢复后可重新执行 hw update") from None
            print(f"下载失败：{detail}；{attempt * 2} 秒后重试。", flush=True)
            time.sleep(attempt * 2)
    raise AssertionError("unreachable")


def update() -> None:
    print("将从 GitHub main 更新程序，保留商品、Telegram 和管理员配置。")
    # Download completely before stopping services; a network error changes nothing.
    script = download_installer()
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


def _systemd_state(unit: str) -> str:
    result = subprocess.run(["systemctl", "is-active", unit], text=True, capture_output=True, check=False, timeout=5)
    return result.stdout.strip() or "unknown"


def _flaresolverr_state() -> str:
    if shutil.which("docker") is None:
        return "not-installed"
    result = subprocess.run(
        ["docker", "container", "inspect", "--format", "{{.State.Status}}", "host-watch-flaresolverr"],
        text=True, capture_output=True, check=False, timeout=5,
    )
    state = result.stdout.strip().lower()
    return state if result.returncode == 0 and state else "not-deployed"


def _cloudscraper_state() -> str:
    """cloudscraper is a library, so report dependency availability instead of a service state."""
    interpreter = INSTALL_DIR / ".venv" / "bin" / "python"
    if not interpreter.is_file():
        return "not-installed"
    result = subprocess.run(
        [str(interpreter), "-c", "import cloudscraper; print(getattr(cloudscraper, '__version__', 'installed'))"],
        text=True, capture_output=True, check=False, timeout=5,
    )
    return "installed" if result.returncode == 0 and result.stdout.strip() else "unavailable"


def _state_label(state: str) -> str:
    labels = {
        "active": "运行中",
        "running": "运行中",
        "inactive": "已停止",
        "exited": "已停止",
        "dead": "已停止",
        "failed": "运行失败",
        "activating": "启动中",
        "deactivating": "停止中",
        "restarting": "重启中",
        "not-installed": "未安装",
        "not-deployed": "未部署",
        "installed": "可用",
        "unavailable": "不可用",
        "unknown": "状态未知",
    }
    return labels.get(state, state or "状态未知")


def print_menu_status() -> None:
    states = (
        ("FlareSolverr", _flaresolverr_state()),
        ("cloudscraper", _cloudscraper_state()),
        ("监控", _systemd_state(MONITOR)),
        ("管理页", _systemd_state(ADMIN)),
    )
    print("\n当前运行状态：\n" + "\n".join(f"  {name} {_state_label(state)}" for name, state in states))


def start_monitor() -> None:
    run("systemctl", "enable", "--now", MONITOR)
    print("HostWatch 监控服务已启动并设置开机自启；手动模式仍保持服务运行。")


def stop_monitor() -> None:
    run("systemctl", "disable", "--now", MONITOR)
    print("监控服务已停止并取消开机自启；网页配置已保留。")


def restart_monitor() -> None:
    run("systemctl", "restart", MONITOR)
    print("HostWatch 监控服务已重启，将重新读取配置。")


def apply_monitor() -> None:
    """Apply configuration while keeping the HostWatch service available.

    Automatic execution is controlled by each module schedule. The monitor
    process remains alive when both schedules are manual so manual refreshes,
    account checks, and the admin API do not appear to be a stopped service.
    """
    run("systemctl", "enable", MONITOR)
    run("systemctl", "restart", MONITOR)
    print("监控配置已应用：HostWatch 服务保持运行，自动任务按模块设置执行。")


def apply_monitor_background() -> None:
    """Queue a monitor restart for automatic saves and return immediately."""
    run("systemctl", "enable", MONITOR)
    run("systemctl", "--no-block", "restart", MONITOR)


def _remove_path(path: Path) -> None:
    """Remove an owned path without ever following a symlink."""
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _remove_launcher(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.name == "jk" and not path.is_symlink():
        try:
            contents = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        if str(INSTALL_DIR) not in contents or "manage.py" not in contents:
            print(f"保留未确认归属的快捷命令：{path}")
            return
    _remove_path(path)


def _remove_managed_flaresolverr() -> None:
    container = "host-watch-flaresolverr"
    if shutil.which("docker") is None:
        return
    inspected = subprocess.run(["docker", "container", "inspect", container], text=True, capture_output=True, check=False)
    if inspected.returncode:
        return
    label = subprocess.run(
        ["docker", "inspect", "--format", '{{index .Config.Labels "io.hostwatch.managed"}}', container],
        text=True, capture_output=True, check=False,
    ).stdout.strip()
    if label == "true":
        run("docker", "rm", "--force", container)
        print("已删除 HostWatch 创建的 FlareSolverr 容器。")
    else:
        print("发现同名但不属于 HostWatch 的 FlareSolverr 容器，已保留。")


def _validate_service_account() -> None:
    try:
        account = pwd.getpwnam(SERVICE_USER)
    except KeyError:
        return
    if account.pw_dir != str(INSTALL_DIR):
        raise ValueError(f"系统账号 {SERVICE_USER} 的 home 目录不是 {INSTALL_DIR}，已停止卸载以避免误删")


def _remove_service_account() -> None:
    _validate_service_account()
    try:
        pwd.getpwnam(SERVICE_USER)
    except KeyError:
        return
    run("userdel", SERVICE_USER)
    try:
        grp.getgrnam(SERVICE_GROUP)
    except KeyError:
        return
    run("groupdel", SERVICE_GROUP)


def uninstall() -> bool:
    print("\n警告：完全卸载会删除 HostWatch 的程序、配置、库存历史、管理员账号、备份和服务。此操作不可撤销。")
    print("Docker、Python 和其他系统依赖不会删除；外部或非 HostWatch 的 FlareSolverr 容器会保留。")
    if input("请输入 DELETE HOSTWATCH 确认：").strip() != "DELETE HOSTWATCH":
        print("已取消，服务器未修改。")
        return False

    _validate_service_account()
    for unit in (BACKUP_TIMER, BACKUP_SERVICE, MONITOR, ADMIN):
        subprocess.run(["systemctl", "disable", "--now", unit], check=False)
    still_running = [
        unit for unit in (BACKUP_SERVICE, MONITOR, ADMIN)
        if subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False).returncode == 0
    ]
    if still_running:
        raise ValueError(f"无法停止服务：{', '.join(still_running)}；未删除程序文件")
    _remove_managed_flaresolverr()
    for path in (
        Path("/etc/systemd/system") / MONITOR,
        Path("/etc/systemd/system") / ADMIN,
        SYSTEMD_DIR / BACKUP_TIMER,
        SYSTEMD_DIR / BACKUP_SERVICE,
        BACKUP_SETTINGS,
        Path("/etc/sudoers.d/host-watch-admin"),
        Path("/etc/tmpfiles.d/host-watch.conf"),
        Path("/etc/vmiss-monitor.env"),
        Path("/etc/vmiss-monitor-admin.env"),
        Path("/etc/stockwatch-users.json"),
        Path("/run/stockwatch-manager.lock"),
    ):
        _remove_path(path)
    for path in (Path("/usr/local/bin/host-watch"), Path("/usr/local/bin/stockwatch"), Path("/usr/local/bin/hw"), Path("/usr/local/bin/jk")):
        _remove_launcher(path)
    _remove_path(INSTALL_DIR)
    _remove_path(DATA_DIR)
    _remove_path(BACKUP_DIR)
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    _remove_service_account()
    print("HostWatch 已从服务器完全删除。Docker 服务及共享系统依赖仍保留。")
    return True


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
            try:
                result = subprocess.run(
                    ["systemctl", "show", unit, "--property=ActiveState", "--property=UnitFileState"],
                    text=True, capture_output=True, check=False, timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                return {"active": "unknown", "enabled": "unknown"}
            values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            return {"active": values.get("ActiveState", "unknown"), "enabled": values.get("UnitFileState", "unknown")}
        users = read_accounts()
        if not users:
            users = {"(当前使用旧版单账号认证；添加管理员后启用账号管理)": {}}
        backups = []
        if BACKUP_DIR.is_dir() and not BACKUP_DIR.is_symlink():
            for path in sorted(BACKUP_DIR.glob("host-watch-*.tar.gz"), key=lambda item: item.stat().st_mtime, reverse=True)[:MAX_BACKUPS]:
                if path.is_file() and not path.is_symlink():
                    backups.append({"name": path.name, "size": path.stat().st_size, "modified": int(path.stat().st_mtime)})
        return {"monitor": unit_state(MONITOR), "admin": unit_state(ADMIN), "users": sorted(users),
                "backups": backups, "backup_dir": str(BACKUP_DIR), "backup_schedule": backup_schedule_status()}
    if action == "logs":
        result = subprocess.run(["journalctl", "--no-pager", "-o", "short-iso", "-u", MONITOR, "-u", ADMIN, "-u", BACKUP_SERVICE, "-n", "100"], text=True, capture_output=True, check=False, timeout=15, env={**os.environ, "TZ": "Asia/Shanghai"})
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
            if action == "monitor-apply" and payload.get("background") is True:
                apply_monitor_background()
            else:
                {"monitor-restart": restart_monitor, "monitor-apply": apply_monitor}[action]()
        return {"ok": True, "message": "监控配置已提交，服务将在后台应用。" if action == "monitor-apply" and payload.get("background") is True else "监控服务操作已完成。"}
    if action == "hostwatch-restart":
        # Restart both project services after the current admin request has
        # returned. A delayed systemd job prevents the response from being
        # cut off when the admin service itself is restarted.
        run("systemd-run", "--quiet", "--on-active=2s", "/usr/bin/systemctl", "restart", MONITOR, ADMIN)
        return {"ok": True, "message": "HostWatch 服务组将在 2 秒后重启：监控服务和管理页服务都会重新读取配置。"}
    if action == "backup":
        path = backup()
        return {"ok": True, "message": f"自动备份完成：{path.name}", "path": str(path), "storage": str(BACKUP_DIR)}
    if action == "backup-download":
        archive = backup_for_download()
        return {"ok": True, "message": "备份已准备导出。", **archive}
    if action == "backup-import":
        return restore_backup(payload)
    if action == "backup-schedule":
        return save_backup_schedule(payload)
    if action == "update":
        stamp = now_beijing().strftime("%Y%m%d%H%M%S")
        unit = f"host-watch-web-update-{stamp}-{secrets.token_hex(4)}.service"
        run("systemd-run", "--quiet", "--no-block", f"--unit={unit.removesuffix('.service')}",
            "--property=Type=oneshot", "--property=RemainAfterExit=yes", "--property=TimeoutStartSec=15min",
            "/usr/bin/python3", str(INSTALL_DIR / "manage.py"), "web-update-worker")
        return {"ok": True, "message": "更新任务已启动。管理页和监控可能短暂重启。", "update_unit": unit}
    if action == "admin-restart":
        run("systemd-run", "--quiet", "--on-active=2s", "/usr/bin/systemctl", "restart", ADMIN)
        return {"ok": True, "message": "管理页将在 2 秒后重启，连接将短暂中断。"}
    if action == "server-restart":
        run("systemd-run", "--quiet", "--on-active=5s", "/usr/bin/systemctl", "reboot")
        return {"ok": True, "message": "服务器将在约 5 秒后重启，当前连接会中断；服务将按系统开机启动配置恢复。"}
    if action == "update-status":
        unit = payload.get("unit", "")
        if not isinstance(unit, str) or not re.fullmatch(r"host-watch-web-update-[0-9]{14}-[0-9a-f]{8}\.service", unit):
            raise ValueError("更新任务编号无效")
        result = subprocess.run(["systemctl", "show", unit, "--property=LoadState", "--property=ActiveState", "--property=SubState", "--property=Result"], text=True, capture_output=True, check=False, timeout=10)
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        logs = ""
        if payload.get("include_logs", True):
            output = subprocess.run(["journalctl", "--no-pager", "-u", unit, "-n", "30"], text=True, capture_output=True, check=False, timeout=10, env={**os.environ, "TZ": "Asia/Shanghai"})
            logs = output.stdout[-12_000:]
        return {"unit": unit, **values, "logs": logs}
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


MENU_ERRORS = (OSError, ValueError, KeyError, subprocess.CalledProcessError, subprocess.TimeoutExpired)


def _submenu(title: str, actions: dict[str, tuple[str, Callable[[], object]]]) -> None:
    while True:
        print(f"\n──────── HostWatch · {title} ────────")
        for key, (label, _) in actions.items():
            print(f" {key}. {label}")
        print(" 0. 返回上一级")
        choice = input("请选择：").strip()
        if choice == "0":
            return
        entry = actions.get(choice)
        if entry is None:
            print("请输入菜单中的编号。")
            continue
        try:
            entry[1]()
        except MENU_ERRORS as exc:
            print(f"操作失败：{exc}")


def monitor_menu() -> None:
    _submenu("监控服务", {
        "1": ("启动监控并设置开机自启", start_monitor),
        "2": ("暂停监控并取消开机自启", stop_monitor),
        "3": ("重启监控 / 载入配置", restart_monitor),
        "4": ("按网页配置应用监控状态", apply_monitor),
    })


def admin_service_menu() -> None:
    _submenu("管理页服务", {
        "1": ("重启管理页", lambda: run("systemctl", "restart", ADMIN)),
        "2": ("查看管理页和监控日志", lambda: run("journalctl", "--no-pager", "-u", MONITOR, "-u", ADMIN, "-n", "100")),
        "3": ("检查管理页账号并重启（域名不限）", configure_admin_domain),
    })


def maintenance_menu() -> None:
    _submenu("更新与备份", {
        "1": ("更新脚本（先备份）", update),
        "2": ("备份程序和配置", backup),
        "3": ("查看最近日志", lambda: run("journalctl", "--no-pager", "-u", MONITOR, "-u", ADMIN, "-n", "100")),
    })


def account_menu() -> None:
    _submenu("管理员账号", {
        "1": ("查看管理页用户", list_accounts),
        "2": ("添加管理页用户", add_account),
        "3": ("修改用户密码", change_password),
        "4": ("删除管理页用户", delete_account),
        "5": ("初始化缺失的管理员（已有账号保持不变）", initialize_admin),
    })


def menu() -> None:
    actions = {
        "1": ("查看服务状态", service_status),
        "2": ("监控服务", monitor_menu),
        "3": ("管理页服务", admin_service_menu),
        "4": ("更新与备份", maintenance_menu),
        "5": ("管理员账号", account_menu),
        "6": ("从服务器完全删除 HostWatch", uninstall),
    }
    while True:
        print("\n──────── HostWatch Debian 管理菜单 ────────")
        print_menu_status()
        print(" 1. 查看服务状态       2. 监控服务   >")
        print(" 3. 管理页服务         4. 更新与备份 >")
        print(" 5. 管理员账号         6. 完全删除 HostWatch")
        print("    常用配置、更新、日志和账号操作也可在网页‘管理员管理’中完成")
        print(" 0. 退出")
        choice = input("请选择：").strip()
        if choice == "0":
            return
        entry = actions.get(choice)
        if entry is None:
            print("请输入菜单中的编号。")
            continue
        try:
            removed = entry[1]()
            if choice == "6" and removed:
                return
        except MENU_ERRORS as exc:
            print(f"操作失败：{exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="HostWatch Debian 管理菜单；用户指管理页账号。")
    parser.add_argument("command", nargs="?", default="menu", choices=["menu", "status", "update", "backup", "users", "add-user", "passwd", "delete-user", "init-admin", "admin-domain", "uninstall", "web-action", "web-update-worker", "scheduled-backup", "backup-schedule-apply"])
    parser.add_argument("domain", nargs="?", help="兼容旧版 admin-domain 命令；域名已不限，此参数不再需要")
    args = parser.parse_args()
    if args.domain is not None and args.command != "admin-domain":
        parser.error("只有 admin-domain 命令接受域名参数")
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
    # The installer may be a child of the update worker, which already holds the
    # manager lock. Reapply persisted units without waiting on that parent's lock.
    if args.command == "backup-schedule-apply":
        try:
            apply_backup_schedule(read_backup_schedule())
            return 0
        except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            print(f"恢复自动备份计划失败：{exc}")
            return 1
    # Avoid two terminals racing an update/account mutation or overwriting users.
    lock_fd = os.open("/run/stockwatch-manager.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w", encoding="utf-8") as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            if args.command in {"web-update-worker", "scheduled-backup"}:
                fcntl.flock(lock, fcntl.LOCK_EX)
            else:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("已有管理菜单或网页操作正在运行，请稍后重试")
        try:
            if args.command == "web-action":
                raw = sys.stdin.read(MAX_BACKUP_REQUEST + 1)
                if len(raw) > MAX_BACKUP_REQUEST:
                    raise ValueError("管理请求过大")
                payload = json.loads(raw)
                if not isinstance(payload, dict) or (payload.get("action") != "backup-import" and len(json.dumps(payload)) > 16_384):
                    raise ValueError("管理请求无效或过大")
                print(json.dumps(web_action(payload), ensure_ascii=False))
            elif args.command == "web-update-worker":
                update()
            elif args.command == "scheduled-backup":
                if read_backup_schedule()["enabled"]:
                    backup()
                else:
                    print("自动备份已关闭，跳过本次任务。")
            elif args.command == "admin-domain":
                configure_admin_domain(args.domain)
            else:
                actions = {"menu": menu, "status": service_status, "update": update, "backup": backup,
                           "users": list_accounts, "add-user": add_account, "passwd": change_password,
                           "delete-user": delete_account, "init-admin": initialize_admin, "uninstall": uninstall}
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
