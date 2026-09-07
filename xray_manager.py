"""
RVG Gateway - Xray Core Configuration & Lifecycle Manager
=========================================================
ماژول جامع مدیریت هسته Xray-core شامل:
1. بارگذاری و فیلتر داده‌های کلاینت‌ها از پایگاه‌داده SQLite با SQLAlchemy
2. بازتولید ساختار استاندارد JSON برای config.json (Inbound: VLESS+WS, dynamic UUIDs)
3. اعمال ریلود پروسه با استفاده از سیگنال SIGUSR1 به جای SIGHUP جهت تفکیک
   ریلود پاکیزه (Clean Reload) از کرش تصادفی در ناظر start.sh
4. ارتباط با Stats API داخلی Xray جهت استعلام آمار مصرف آپلود و دانلود هر کاربر
"""

import json
import logging
import os
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Union

try:
    from sqlalchemy.orm import Session
except ImportError:
    Session = Any  # type: ignore

import config

logger = logging.getLogger("rvg.xray")

# مسیرهای فایل و پورت‌های هسته Xray و ناظر
DEFAULT_CONFIG_PATH = Path(os.getenv("XRAY_CONFIG_PATH", config.BASE_DIR / "config.json"))
XRAY_PID_FILE = Path(os.getenv("XRAY_PID_FILE", "/tmp/xray.pid"))
SUPERVISOR_PID_FILE = Path(os.getenv("SUPERVISOR_PID_FILE", "/tmp/supervisor.pid"))
XRAY_RELOAD_FLAG = Path(os.getenv("XRAY_RELOAD_FLAG", "/tmp/xray.reload"))
XRAY_INTERNAL_PORT = int(os.getenv("XRAY_INTERNAL_PORT", "10080"))
XRAY_API_PORT = int(os.getenv("XRAY_API_PORT", "10085"))
FASTAPI_INTERNAL_PORT = int(os.getenv("FASTAPI_INTERNAL_PORT", "8000"))


def generate_xray_config_dict(db: Session) -> Dict[str, Any]:
    """
    ۱) بارگذاری اطلاعات کاربران از دیتابیس با SQLAlchemy
    ۲) فیلتر کردن کاربران منقضی شده یا اتمام حجم یافته
    ۳) ساخت ساختار معتبر JSON برای Xray-core با پشتیبانی از پروتکل VLESS + WebSocket
    """
    from database import UserLink

    now = datetime.now(timezone.utc)

    # واکشی تمام لینک‌های فعال از پایگاه داده با SQLAlchemy
    all_users: List[UserLink] = db.query(UserLink).filter(UserLink.is_active == True).all()

    clients = []
    for user in all_users:
        if user.expire_at is not None:
            exp = user.expire_at.replace(tzinfo=timezone.utc) if user.expire_at.tzinfo is None else user.expire_at
            if exp < now:
                continue

        if user.quota_bytes > 0 and user.used_bytes >= user.quota_bytes:
            continue

        clients.append({
            "id": user.uuid,
            "email": f"user_{user.id}@{user.uuid}",
            "level": 0
        })

    public_port = int(os.getenv("PORT", config.PORT or 8080))
    ws_path = config.WS_PATH if config.WS_PATH.startswith("/") else f"/{config.WS_PATH}"

    xray_config: Dict[str, Any] = {
        "log": {
            "loglevel": "warning" if not config.DEBUG else "debug"
        },
        "stats": {},
        "api": {
            "tag": "api",
            "services": [
                "StatsService",
                "HandlerService"
            ]
        },
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True
                }
            },
            "system": {
                "statsInboundUplink": True,
                "statsInboundDownlink": True
            }
        },
        "inbounds": [
            {
                "tag": "vless-in",
                "listen": "127.0.0.1",
                "port": XRAY_INTERNAL_PORT,
                "protocol": "vless",
                "settings": {
                    "clients": clients,
                    "decryption": "none"
                },
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {
                        "path": ws_path,
                        "maxEarlyData": 2048,
                        "earlyDataHeaderName": "Sec-WebSocket-Protocol"
                    }
                }
            },
            {
                "tag": "api-in",
                "listen": "127.0.0.1",
                "port": XRAY_API_PORT,
                "protocol": "dokodemo-door",
                "settings": {
                    "address": "127.0.0.1"
                }
            }
        ],
        "outbounds": [
            {
                "tag": "direct",
                "protocol": "freedom",
                "settings": {}
            },
            {
                "tag": "blocked",
                "protocol": "blackhole",
                "settings": {}
            }
        ],
        "routing": {
            "rules": [
                {
                    "inboundTag": ["api-in"],
                    "outboundTag": "api",
                    "type": "field"
                }
            ]
        }
    }

    return xray_config


def regenerate_config(
    db: Optional[Session] = None,
    output_path: Optional[Union[str, Path]] = None
) -> Dict[str, Any]:
    """
    بازتولید فایل config.json برای هسته Xray-core با استفاده از atomic file write
    """
    import database

    target_path = Path(output_path) if output_path else DEFAULT_CONFIG_PATH

    owns_session = False
    if db is None:
        db = database.SessionLocal()
        owns_session = True

    try:
        config_data = generate_xray_config_dict(db)
        client_count = len(config_data.get("inbounds", [{}])[0].get("settings", {}).get("clients", []))

        target_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_file = target_path.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
        tmp_file.replace(target_path)

        app_container_path = Path("/app/config.json")
        if app_container_path.parent.exists() and app_container_path.resolve() != target_path.resolve():
            try:
                tmp_app = app_container_path.with_suffix(".tmp")
                with open(tmp_app, "w", encoding="utf-8") as f:
                    json.dump(config_data, f, indent=2, ensure_ascii=False)
                tmp_app.replace(app_container_path)
            except Exception as e:
                logger.debug(f"[XrayManager] Notice: /app/config.json write skipped: {e}")

        logger.info(
            f"[XrayManager] config.json regenerated successfully with {client_count} active clients."
        )
        return config_data
    except Exception as err:
        logger.error(f"[XrayManager] Failed to regenerate config.json: {err}", exc_info=True)
        raise
    finally:
        if owns_session:
            db.close()


def reload_xray() -> bool:
    """
    اعمال تغییرات کانفیگ با استفاده از سیگنال SIGUSR1 به جای SIGHUP:
    
    1. ایجاد فایل پرچم /tmp/xray.reload
    2. ارسال سیگنال SIGUSR1 به ناظر start.sh (یا پروسه Xray)
    3. ناظر start.sh با دریافت SIGUSR1، توقف پروسه را به عنوان «ریلود پاکیزه» (Clean Reload)
       شناسایی کرده، کانتینر را زنده نگه می‌دارد و فوراً نمونه جدید Xray-core را بالا می‌آورد.
    """
    signaled = False
    try:
        # ۱. ایجاد پرچم هماهنگی ریلود
        XRAY_RELOAD_FLAG.touch(exist_ok=True)

        # ۲. ارسال SIGUSR1 به ناظر کانتینر (start.sh) در صورت وجود فایل PID ناظر
        if SUPERVISOR_PID_FILE.exists():
            try:
                raw_sup = SUPERVISOR_PID_FILE.read_text().strip()
                if raw_sup:
                    sup_pid = int(raw_sup)
                    os.kill(sup_pid, signal.SIGUSR1)
                    logger.info(f"[XrayManager] Sent SIGUSR1 to supervisor start.sh (PID: {sup_pid})")
                    signaled = True
            except ProcessLookupError:
                logger.debug("[XrayManager] Supervisor PID file stale.")
            except Exception as e:
                logger.warning(f"[XrayManager] Failed to send SIGUSR1 to supervisor: {e}")

        # ۳. ارسال SIGUSR1 / SIGTERM به پروسه Xray
        if XRAY_PID_FILE.exists():
            try:
                raw_pid = XRAY_PID_FILE.read_text().strip()
                if raw_pid:
                    xray_pid = int(raw_pid)
                    # ارسال SIGUSR1 به پروسه
                    os.kill(xray_pid, signal.SIGUSR1)
                    logger.info(f"[XrayManager] Sent SIGUSR1 to Xray process (PID: {xray_pid})")
                    signaled = True
            except ProcessLookupError:
                logger.debug("[XrayManager] Xray PID file stale.")
            except Exception as e:
                logger.warning(f"[XrayManager] Failed to send SIGUSR1 to Xray: {e}")

        # ۴. فال‌بک سیستمی با pkill -USR1
        if not signaled:
            try:
                res = subprocess.run(["pkill", "-USR1", "-f", "start.sh"], capture_output=True, text=True)
                if res.returncode == 0:
                    signaled = True
            except Exception:
                pass

        # در صورتی که ناظر فعال نبود، توقف مستقیم با SIGTERM جهت خروج تمیز
        if not signaled:
            subprocess.run(["pkill", "-TERM", "xray"], capture_output=True)
            logger.info("[XrayManager] Standalone fallback: sent SIGTERM to xray")

        return True
    except Exception as err:
        logger.error(f"[XrayManager] Error during SIGUSR1 reload: {err}", exc_info=True)
        return False


def get_user_traffic_stats(user_id: int, user_uuid: str, reset: bool = False) -> Dict[str, int]:
    """
    استعلام آمار مصرف ترافیک لحظه‌ای از Stats API داخلی Xray-core
    """
    email = f"user_{user_id}@{user_uuid}"
    stats = {"uplink": 0, "downlink": 0}

    try:
        cmd = [
            "xray", "api", "statsquery",
            f"--server=127.0.0.1:{XRAY_API_PORT}",
            f"--pattern=user>>>{email}>>>traffic"
        ]
        if reset:
            cmd.append("--reset")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            for item in data.get("stat", []):
                name = item.get("name", "")
                val = int(item.get("value", 0))
                if "uplink" in name:
                    stats["uplink"] = val
                elif "downlink" in name:
                    stats["downlink"] = val
    except FileNotFoundError:
        logger.debug("[XrayManager] 'xray' binary not found in current PATH.")
    except subprocess.TimeoutExpired:
        logger.warning(f"[XrayManager] Stats API query timed out for {email}")
    except Exception as err:
        logger.debug(f"[XrayManager] Stats query skipped or failed for {email}: {err}")

    return stats


def get_xray_diagnostics(tail_lines: int = 50) -> Dict[str, Any]:
    """
    بررسی وضعیت تشخیصی پروسه Xray-core:
    1. وضعیت در حال اجرا بودن (is_running)
    2. شناسه پروسه (PID)
    3. میزان مصرف حافظه RAM (RSS و VMS)
    4. آخرین خطوط لاگ از فایل خروجی Xray
    """
    log_file = Path(os.getenv("XRAY_LOG_FILE", "/tmp/xray.log"))
    pid: Optional[int] = None
    is_running = False
    process_comm: Optional[str] = None
    memory_info: Dict[str, Any] = {
        "rss_bytes": 0,
        "rss_human": "0 MB",
        "vms_bytes": 0,
        "vms_human": "0 MB"
    }

    # ۱. استخراج PID از فایل پرچم
    if XRAY_PID_FILE.exists():
        try:
            raw_pid = XRAY_PID_FILE.read_text().strip()
            if raw_pid and raw_pid.isdigit():
                pid = int(raw_pid)
        except Exception:
            pid = None

    # ۲. بررسی زنده بودن پروسه
    if pid and pid > 0:
        try:
            os.kill(pid, 0)
            is_running = True
            comm_file = Path(f"/proc/{pid}/comm")
            if comm_file.exists():
                process_comm = comm_file.read_text().strip()
            else:
                process_comm = "xray"
        except (ProcessLookupError, PermissionError):
            is_running = False

    # فال‌بک با pgrep
    if not is_running:
        try:
            pg = subprocess.run(["pgrep", "-x", "xray"], capture_output=True, text=True)
            if pg.returncode == 0 and pg.stdout.strip():
                pids = [int(p) for p in pg.stdout.strip().split() if p.isdigit()]
                if pids:
                    pid = pids[0]
                    is_running = True
                    process_comm = "xray"
        except Exception:
            pass

    # ۳. استخراج مصرف رم از /proc/{pid}/status
    if is_running and pid:
        try:
            proc_status = Path(f"/proc/{pid}/status")
            if proc_status.exists():
                for line in proc_status.read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            kb = int(parts[1])
                            memory_info["rss_bytes"] = kb * 1024
                            memory_info["rss_human"] = f"{kb / 1024:.2f} MB"
                    elif line.startswith("VmSize:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            kb = int(parts[1])
                            memory_info["vms_bytes"] = kb * 1024
                            memory_info["vms_human"] = f"{kb / 1024:.2f} MB"
            else:
                ps_out = subprocess.run(["ps", "-p", str(pid), "-o", "rss="], capture_output=True, text=True)
                if ps_out.returncode == 0 and ps_out.stdout.strip().isdigit():
                    kb = int(ps_out.stdout.strip())
                    memory_info["rss_bytes"] = kb * 1024
                    memory_info["rss_human"] = f"{kb / 1024:.2f} MB"
        except Exception as err:
            logger.debug(f"[XrayManager] Memory stats extraction error: {err}")

    # ۴. استخراج آخرین خطوط لاگ از فایل خروجی
    recent_logs: List[str] = []
    if log_file.exists():
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace")
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            recent_logs = lines[-tail_lines:] if len(lines) > tail_lines else lines
        except Exception as err:
            recent_logs = [f"Error reading log file ({log_file}): {err}"]
    else:
        recent_logs = [f"Output log file ({log_file}) has not been created yet or is currently empty."]

    # آمار کلاینت‌های رجیستر شده در کانفیگ
    active_clients_count = 0
    if DEFAULT_CONFIG_PATH.exists():
        try:
            with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                active_clients_count = len(cfg.get("inbounds", [{}])[0].get("settings", {}).get("clients", []))
        except Exception:
            pass

    return {
        "status": "running" if is_running else "stopped",
        "is_running": is_running,
        "pid": pid,
        "process_name": process_comm,
        "memory": memory_info,
        "active_clients_in_config": active_clients_count,
        "output_log_file": str(log_file),
        "total_lines_reported": len(recent_logs),
        "last_log_lines": recent_logs,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
