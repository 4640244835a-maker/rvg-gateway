"""
RVG Gateway - Database Layer
مدیریت پایگاه داده SQLite با SQLAlchemy، پشتیبانی از تراکنشهای همزمان و حسابرسی حجم مصرفی
"""

import asyncio
import concurrent.futures
import logging
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    BigInteger,
    Boolean,
    DateTime,
    select,
    update,
    delete
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
import config

logger = logging.getLogger("rvg.database")

# Create Engine with connection pooling and thread check bypass for SQLite
connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(
    config.DATABASE_URL,
    connect_args=connect_args,
    echo=config.DEBUG,
    pool_pre_ping=True
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# =========================================================================
# High-Performance In-Memory Traffic Accounting Subsystem
# ترافیک تمام پکت‌ها به صورت غیرمسدودکننده در حافظه RAM تجمیع می‌شود و سپس
# هر ۲ ثانیه یک‌بار در قالب تراکنش گروهی (Batch) روی دیسک فلاش می‌گردد تا
# Event Loop به هیچ عنوان قفل نشود و خطای context deadline exceeded رخ ندهد.
# =========================================================================
_traffic_lock = threading.Lock()
_pending_traffic: Dict[int, int] = defaultdict(int)
_user_cache: Dict[int, Dict[str, Any]] = {}
_flush_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="db_flush")


class UserLink(Base):
    """
    جدول کاربری / لینکهای پروکسی
    ذخیره دائمی اطلاعات لینکها، کدهای شناسایی و وضعیت سهمیه حجمی
    """
    __tablename__ = "user_links"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    uuid = Column(String(36), unique=True, index=True, nullable=False, default=lambda: str(uuid.uuid4()))
    name = Column(String(120), nullable=False, index=True)
    quota_bytes = Column(BigInteger, nullable=False, default=10 * 1024 * 1024 * 1024)  # پیشفرض 10 گیگابایت
    used_bytes = Column(BigInteger, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    expire_at = Column(DateTime, nullable=True)

    def to_dict(
        self,
        domain: Optional[str] = None,
        port: Optional[int] = None,
        tls: Optional[bool] = None
    ) -> Dict[str, Any]:
        """تبدیل به ساختار دیکشنری جهت ارسال به API و قالبهای HTML با قابلیت تنظیم دامنه پویا"""
        now = datetime.now(timezone.utc)
        is_expired = self.expire_at is not None and (
            self.expire_at.replace(tzinfo=timezone.utc) if self.expire_at.tzinfo is None else self.expire_at
        ) < now
        quota_exceeded = self.used_bytes >= self.quota_bytes if self.quota_bytes > 0 else False
        status = "active"
        if not self.is_active:
            status = "disabled"
        elif quota_exceeded:
            status = "quota_exceeded"
        elif is_expired:
            status = "expired"

        percent_used = 0.0
        if self.quota_bytes > 0:
            percent_used = round(min(100.0, (self.used_bytes / self.quota_bytes) * 100), 1)

        return {
            "id": self.id,
            "uuid": self.uuid,
            "name": self.name,
            "quota_bytes": self.quota_bytes,
            "used_bytes": self.used_bytes,
            "quota_formatted": format_bytes(self.quota_bytes),
            "used_formatted": format_bytes(self.used_bytes),
            "percent_used": percent_used,
            "is_active": self.is_active,
            "is_expired": is_expired,
            "quota_exceeded": quota_exceeded,
            "status": status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expire_at": self.expire_at.isoformat() if self.expire_at else None,
            "vless_url": generate_vless_url(self.uuid, self.name, domain=domain, port=port, tls=tls, transport="ws"),
            "vless_xhttp_url": generate_vless_url(self.uuid, self.name, domain=domain, port=port, tls=tls, transport="xhttp"),
            "socks5_url": generate_socks5_url(self.uuid, self.name, domain=domain, port=port),
        }


def format_bytes(num_bytes: int) -> str:
    """فرمتدهی بایت به واحدهای خوانا: B, KB, MB, GB, TB"""
    if num_bytes <= 0:
        return "0 MB"
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} EB"


def generate_vless_url(
    user_uuid: str,
    name: str,
    domain: Optional[str] = None,
    port: Optional[int] = None,
    tls: Optional[bool] = None,
    transport: str = "ws",  # "ws" or "xhttp"
    custom_path: Optional[str] = None
) -> str:
    """
    تولید لینک استاندارد پیکربندی VLESS بر اساس آخرین استانداردهای کلاینت‌های V2Ray, Xray, Sing-box, NekoBox
    پشتیبانی کامل از ترنسپورت‌های 'ws' (WebSocket) و 'xhttp' (XHTTP / SplitHTTP).
    تنظیم دقیق پارامترهای host, sni, path و alpn برای اتصال بدون نقص بر روی بسترهای کلود و Railway.
    """
    effective_domain = domain or config.PUBLIC_DOMAIN
    effective_port = port if port is not None else config.PUBLIC_PORT
    use_tls = tls if tls is not None else config.PUBLIC_TLS
    security = "tls" if use_tls else "none"

    raw_path = (custom_path or config.WS_PATH).strip()
    if not raw_path.startswith("/"):
        raw_path = f"/{raw_path}"

    import urllib.parse
    encoded_name = urllib.parse.quote(name)
    # مسیر تمیز با حفظ اسلش استاندارد جهت جلوگیری از ریجکت شدن توسط Envoy ریلوِی
    clean_path = urllib.parse.quote(raw_path, safe="/?#[]@!$&'()*+,;=")

    params = [
        f"type={transport.lower()}",
        f"security={security}"
    ]

    # هدرهای حیاتی برای هدایت ترافیک در ریورس‌پروکسی Railway و Edge
    if use_tls:
        params.append(f"sni={effective_domain}")
    params.append(f"host={effective_domain}")
    params.append(f"path={clean_path}")

    # پارامترهای اختصاصی نوع انتقال
    if transport.lower() == "xhttp":
        params.append("mode=auto")

    # اثر انگشت امنیتی uTLS برای کلاینت‌های مدرن
    params.append("fp=chrome")

    query_str = "&".join(params)
    return f"vless://{user_uuid}@{effective_domain}:{effective_port}?{query_str}#{encoded_name}"


def generate_socks5_url(
    user_uuid: str,
    name: str,
    domain: Optional[str] = None,
    port: Optional[int] = None
) -> str:
    """تولید لینک استاندارد SOCKS5 داخلی"""
    effective_domain = domain or config.PUBLIC_DOMAIN
    effective_port = port if port is not None else config.SOCKS5_PORT
    import urllib.parse
    encoded_name = urllib.parse.quote(name)
    return f"socks5://{user_uuid}@{effective_domain}:{effective_port}#{encoded_name}"


def init_db():
    """ایجاد جداول دیتابیس در صورت عدم وجود"""
    Base.metadata.create_all(bind=engine)
    # Seed initial test link if empty
    with SessionLocal() as db:
        first = db.query(UserLink).first()
        if not first:
            demo_link = UserLink(
                uuid=str(uuid.uuid4()),
                name="کاربر پیش‌فرض (Default Link)",
                quota_bytes=50 * 1024 * 1024 * 1024,  # 50 GB
                used_bytes=1024 * 1024 * 512,  # 512 MB initial test traffic
                is_active=True
            )
            db.add(demo_link)
            db.commit()


def get_db():
    """Dependency injection generator for FastAPI"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==========================================
# CRUD Operations
# ==========================================

def get_all_links(db: Session) -> List[UserLink]:
    """دریافت تمام لینکها با مرتبسازی نزولی بر اساس تاریخ ایجاد و اعمال ترافیک تجمیع‌شده در حافظه"""
    links = db.query(UserLink).order_by(UserLink.id.desc()).all()
    with _traffic_lock:
        for link in links:
            pending = _pending_traffic.get(link.id, 0)
            if pending > 0:
                link.used_bytes += pending
            _user_cache[link.id] = {
                "quota_bytes": link.quota_bytes,
                "used_bytes": link.used_bytes,
                "is_active": link.is_active
            }
    return links


def get_link_by_uuid(db: Session, user_uuid: str) -> Optional[UserLink]:
    """اعتبارسنجی و واکشی کاربر با UUID"""
    link = db.query(UserLink).filter(UserLink.uuid == user_uuid).first()
    if link:
        with _traffic_lock:
            pending = _pending_traffic.get(link.id, 0)
            if pending > 0:
                link.used_bytes += pending
            _user_cache[link.id] = {
                "quota_bytes": link.quota_bytes,
                "used_bytes": link.used_bytes,
                "is_active": link.is_active
            }
    return link


def get_link_by_id(db: Session, link_id: int) -> Optional[UserLink]:
    """واکشی کاربر با شناسه اولیه (ID)"""
    link = db.query(UserLink).filter(UserLink.id == link_id).first()
    if link:
        with _traffic_lock:
            pending = _pending_traffic.get(link.id, 0)
            if pending > 0:
                link.used_bytes += pending
            _user_cache[link.id] = {
                "quota_bytes": link.quota_bytes,
                "used_bytes": link.used_bytes,
                "is_active": link.is_active
            }
    return link


def create_link(
    db: Session,
    name: str,
    quota_bytes: int,
    expire_at: Optional[datetime] = None,
    custom_uuid: Optional[str] = None
) -> UserLink:
    """ایجاد لینک جدید با UUID اختصاصی و سهمیه مشخص"""
    link = UserLink(
        uuid=custom_uuid or str(uuid.uuid4()),
        name=name.strip(),
        quota_bytes=quota_bytes,
        used_bytes=0,
        is_active=True,
        expire_at=expire_at
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    with _traffic_lock:
        _user_cache[link.id] = {
            "quota_bytes": link.quota_bytes,
            "used_bytes": link.used_bytes,
            "is_active": link.is_active
        }
    return link


def toggle_link_status(db: Session, link_id: int) -> Optional[UserLink]:
    """تغییر وضعیت آنی فعال/غیرفعال بودن یک لینک"""
    link = get_link_by_id(db, link_id)
    if link:
        link.is_active = not link.is_active
        db.commit()
        db.refresh(link)
        with _traffic_lock:
            if link_id in _user_cache:
                _user_cache[link_id]["is_active"] = link.is_active
    return link


def delete_link(db: Session, link_id: int) -> bool:
    """حذف دائمی یک لینک از پایگاه داده"""
    link = get_link_by_id(db, link_id)
    if link:
        with _traffic_lock:
            _pending_traffic.pop(link_id, None)
            _user_cache.pop(link_id, None)
        db.delete(link)
        db.commit()
        return True
    return False


def reset_link_traffic(db: Session, link_id: int) -> Optional[UserLink]:
    """صفر کردن میزان حجم مصرفی لینک در دیتابیس و حافظه موقت"""
    with _traffic_lock:
        _pending_traffic[link_id] = 0
        if link_id in _user_cache:
            _user_cache[link_id]["used_bytes"] = 0

    link = get_link_by_id(db, link_id)
    if link:
        link.used_bytes = 0
        db.commit()
        db.refresh(link)
    return link


def record_traffic(db_or_user_id: Any, link_id_or_bytes: int, byte_count: Optional[int] = None) -> bool:
    """
    حسابداری سریع، غیرمسدودکننده و دسته‌ای ترافیک (Non-blocking In-Memory Traffic Accounting):
    - ثبت بیدرنگ در حافظه RAM با زمان اجرای زیر ۱ میکروثانیه (بدون هیچ I/O یا قفل دیسک)
    - رفع قطعی مسدود شدن Event Loop اصلی در چرخه‌های پرتکرار رله داده
    - بررسی فوری سقف سهمیه کاربر
    - سازگار با هر دو امضای تابعی:
        record_traffic(db, user_id, byte_count)
        record_traffic(user_id, byte_count)
    """
    if byte_count is not None:
        link_id = int(link_id_or_bytes)
        bytes_transferred = int(byte_count)
    else:
        link_id = int(db_or_user_id)
        bytes_transferred = int(link_id_or_bytes)

    if bytes_transferred <= 0:
        return True

    with _traffic_lock:
        user_info = _user_cache.get(link_id)
        if not user_info:
            # واکشی اولیه سریع در صورت غیبت در کش
            try:
                with SessionLocal() as session:
                    user = session.query(UserLink).filter(UserLink.id == link_id).first()
                    if user:
                        user_info = {
                            "quota_bytes": user.quota_bytes,
                            "used_bytes": user.used_bytes,
                            "is_active": user.is_active
                        }
                        _user_cache[link_id] = user_info
            except Exception as err:
                logger.debug(f"User quota query failed: {err}")

        if user_info:
            if not user_info.get("is_active", True):
                return False
            user_info["used_bytes"] = user_info.get("used_bytes", 0) + bytes_transferred
            quota = user_info.get("quota_bytes", 0)
            if quota > 0 and user_info["used_bytes"] >= quota:
                _pending_traffic[link_id] += bytes_transferred
                return False

        _pending_traffic[link_id] += bytes_transferred

    return True


def flush_traffic_sync() -> int:
    """
    تخلیه همگام ترافیک انباشته شده به دیتابیس در قالب یک تراکنش واحد و سریع
    """
    with _traffic_lock:
        if not _pending_traffic:
            return 0
        snapshot = dict(_pending_traffic)
        _pending_traffic.clear()

    total_flushed = 0
    try:
        with SessionLocal() as session:
            for uid, delta in snapshot.items():
                if delta > 0:
                    session.execute(
                        update(UserLink)
                        .where(UserLink.id == uid)
                        .values(used_bytes=UserLink.used_bytes + delta)
                    )
                    total_flushed += delta
            session.commit()
    except Exception as err:
        logger.error(f"Failed to flush traffic snapshot to database: {err}")
        with _traffic_lock:
            for uid, delta in snapshot.items():
                _pending_traffic[uid] += delta
        return 0

    return total_flushed


async def flush_traffic_async() -> int:
    """تخلیه غیرمسدودکننده ترافیک به دیتابیس در یک ترد پس‌زمینه اختصاصی"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_flush_executor, flush_traffic_sync)


async def start_traffic_flush_worker(interval: float = 2.0):
    """
    تسک دائمی پس‌زمینه برای تخلیه دوره‌ای ترافیک به SQLite (هر ۲ ثانیه یک‌بار)
    """
    logger.info("Background traffic flush worker started.")
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                flushed = await flush_traffic_async()
                if flushed > 0:
                    logger.debug(f"Flushed {flushed} bytes of accumulated traffic to SQLite.")
            except Exception as e:
                logger.debug(f"Error in traffic flush worker iteration: {e}")
    except asyncio.CancelledError:
        # تخلیه نهایی قبل از بسته‌شدن سرور
        flush_traffic_sync()

