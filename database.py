"""
RVG Gateway - Database Layer
مدیریت پایگاه داده SQLite با SQLAlchemy، پشتیبانی از تراکنشهای همزمان و حسابرسی حجم مصرفی
"""

import uuid
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

    def to_dict(self) -> Dict[str, Any]:
        """تبدیل به ساختار دیکشنری جهت ارسال به API و قالبهای HTML"""
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
            "vless_url": generate_vless_url(self.uuid, self.name),
            "socks5_url": generate_socks5_url(self.uuid, self.name),
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


def generate_vless_url(user_uuid: str, name: str) -> str:
    """تولید لینک استاندارد پیکربندی VLESS over WebSocket با TLS"""
    domain = config.PUBLIC_DOMAIN
    port = config.PUBLIC_PORT
    security = "tls" if config.PUBLIC_TLS else "none"
    path = config.WS_PATH
    import urllib.parse
    encoded_name = urllib.parse.quote(name)
    encoded_path = urllib.parse.quote(path)
    return f"vless://{user_uuid}@{domain}:{port}?type=ws&security={security}&path={encoded_path}#{encoded_name}"


def generate_socks5_url(user_uuid: str, name: str) -> str:
    """تولید لینک استاندارد SOCKS5 داخلی"""
    domain = config.PUBLIC_DOMAIN
    port = config.SOCKS5_PORT
    import urllib.parse
    encoded_name = urllib.parse.quote(name)
    return f"socks5://{user_uuid}@{domain}:{port}#{encoded_name}"


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
    """دریافت تمام لینکها با مرتبسازی نزولی بر اساس تاریخ ایجاد"""
    return db.query(UserLink).order_by(UserLink.id.desc()).all()


def get_link_by_uuid(db: Session, user_uuid: str) -> Optional[UserLink]:
    """اعتبارسنجی و واکشی کاربر با UUID"""
    return db.query(UserLink).filter(UserLink.uuid == user_uuid).first()


def get_link_by_id(db: Session, link_id: int) -> Optional[UserLink]:
    """واکشی کاربر با شناسه اولیه (ID)"""
    return db.query(UserLink).filter(UserLink.id == link_id).first()


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
    return link


def toggle_link_status(db: Session, link_id: int) -> Optional[UserLink]:
    """تغییر وضعیت آنی فعال/غیرفعال بودن یک لینک"""
    link = get_link_by_id(db, link_id)
    if link:
        link.is_active = not link.is_active
        db.commit()
        db.refresh(link)
    return link


def delete_link(db: Session, link_id: int) -> bool:
    """حذف دائمی یک لینک از پایگاه داده"""
    link = get_link_by_id(db, link_id)
    if link:
        db.delete(link)
        db.commit()
        return True
    return False


def reset_link_traffic(db: Session, link_id: int) -> Optional[UserLink]:
    """صفر کردن میزان حجم مصرفی لینک"""
    link = get_link_by_id(db, link_id)
    if link:
        link.used_bytes = 0
        db.commit()
        db.refresh(link)
    return link


def record_traffic(db: Session, link_id: int, byte_count: int) -> bool:
    """
    حسابداری ترافیک (Traffic Accounting):
    افزودن بایت‌های منتقل شده به رکورد دیتابیس و بررسی سقف مجاز سهمیه
    اگر حجم به پایان رسیده باشد، False برمیگرداند تا اتصال بلافاصله قطع شود.
    """
    if byte_count <= 0:
        return True

    link = get_link_by_id(db, link_id)
    if not link or not link.is_active:
        return False

    link.used_bytes += byte_count
    quota_exceeded = (link.quota_bytes > 0) and (link.used_bytes >= link.quota_bytes)
    db.commit()

    if quota_exceeded:
        return False  # ترافیک تمام شده و اتصال باید فورا خاتمه یابد
    return True
