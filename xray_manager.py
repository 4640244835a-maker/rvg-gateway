"""
RVG Gateway - Xray Core Configuration & Lifecycle Manager
=========================================================
ماژول جامع مدیریت هسته Xray-core شامل:
1. بارگذاری و فیلتر داده‌های کلاینت‌ها از پایگاه‌داده SQLite با SQLAlchemy
2. بازتولید ساختار استاندارد JSON برای config.json (Inbound: VLESS+WS, dynamic UUIDs)
3. اعمال ریلود پروسه Xray بدون قطعی اتصالات موجود (Zero-downtime SIGHUP / Process Signal)
4. ارتباط با Stats API داخلی Xray جهت استعلام آمار مصرف آپلود و دانلود هر کاربر
"""

import json
import logging
import os
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

# مسیرهای فایل و پورت‌های هسته Xray
DEFAULT_CONFIG_PATH = Path(os.getenv("XRAY_CONFIG_PATH", config.BASE_DIR / "config.json"))
XRAY_PID_FILE = Path(os.getenv("XRAY_PID_FILE", "/tmp/xray.pid"))
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
        # بررسی انقضای زمانی (در صورت تعریف تاریخ انقضا)
        if user.expire_at is not None:
            exp = user.expire_at.replace(tzinfo=timezone.utc) if user.expire_at.tzinfo is None else user.expire_at
            if exp < now:
                continue

        # بررسی سقف حجم مجاز (در صورتی که quota_bytes بزرگتر از صفر باشد)
        if user.quota_bytes > 0 and user.used_bytes >= user.quota_bytes:
            continue

        # افزودن کلاینت مجاز به لیست ورودی Xray
        clients.append({
            "id": user.uuid,
            "email": f"user_{user.id}@{user.uuid}",
            "level": 0
        })

    # تعیین پورت و مسیر وب‌سوکت
    public_port = int(os.getenv("PORT", config.PORT or 8080))
    ws_path = config.WS_PATH if config.WS_PATH.startswith("/") else f"/{config.WS_PATH}"

    # ساختار کامل پیکربندی استاندارد Xray-core
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
            # ۱. اینباند اصلی: VLESS روی وب‌سوکت با فال‌بک به سرور داخلی FastAPI
            {
                "tag": "vless-in",
                "listen": "0.0.0.0",
                "port": public_port,
                "protocol": "vless",
                "settings": {
                    "clients": clients,
                    "decryption": "none",
                    "fallbacks": [
                        {
                            # هدایت کلیه درخواست‌های HTTP به وب‌سرور FastAPI
                            "dest": FASTAPI_INTERNAL_PORT
                        }
                    ]
                },
                "streamSettings": {
                    "network": "ws",
                    # امنیت TLS در لایه Edge توسط پلتفرم اعمال شده و به صورت Plain به کانتینر فوروارد می‌شود
                    "security": "none",
                    "wsSettings": {
                        "path": ws_path
                    }
                }
            },
            # ۲. اینباند کنترل و دریافت آمار مصرف ترافیک Stats API
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
    جهت جلوگیری از خواندن فایل ناقص هنگام ریلود.
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

        # نوشتن اتمیک (Atomic Write) با استفاده از فایل موقت
        tmp_file = target_path.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
        tmp_file.replace(target_path)

        # همگام‌سازی مسیر پیش‌فرض کانتینر /app/config.json
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
    ارسال سیگنال ریلود بدون قطعی (SIGHUP) به پروسه در حال اجرای Xray-core
    به منظور اعمال تغییرات کاربران بدون بستن اتصالات فعال.
    """
    reloaded = False

    # ۱. بررسی فایل PID پروسه Xray
    if XRAY_PID_FILE.exists():
        try:
            pid = int(XRAY_PID_FILE.read_text().strip())
            # ارسال سیگنال SIGHUP (شماره ۱) که در سیستم‌های یونیکس برای Hot-Reload کانفیگ استفاده می‌شود
            os.kill(pid, 1)
            logger.info(f"[XrayManager] Successfully sent SIGHUP reload signal to Xray (PID: {pid})")
            reloaded = True
        except ProcessLookupError:
            logger.debug("[XrayManager] Stale PID file detected. Process not active.")
        except Exception as err:
            logger.warning(f"[XrayManager] Failed to signal Xray PID: {err}")

    # ۲. فال‌بک با pkill -HUP xray در صورتی که فایل PID موجود نباشد
    if not reloaded:
        try:
            res = subprocess.run(["pkill", "-HUP", "xray"], capture_output=True, text=True)
            if res.returncode == 0:
                logger.info("[XrayManager] Reloaded Xray process via 'pkill -HUP xray'")
                reloaded = True
        except Exception as err:
            logger.debug(f"[XrayManager] pkill fallback skipped: {err}")

    return reloaded


def get_user_traffic_stats(user_id: int, user_uuid: str, reset: bool = False) -> Dict[str, int]:
    """
    ارتباط با Stats API داخلی Xray-core جهت استعلام مصرف ترافیک کاربر.
    خروجی به صورت بایت برای آپلود و دانلود برگردانده می‌شود.
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
        logger.debug("[XrayManager] 'xray' executable not found in current PATH.")
    except subprocess.TimeoutExpired:
        logger.warning(f"[XrayManager] Stats API query timed out for {email}")
    except Exception as err:
        logger.debug(f"[XrayManager] Stats query failed for {email}: {err}")

    return stats
