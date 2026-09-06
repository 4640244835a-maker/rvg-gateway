"""
RVG Gateway - Configuration Module
مدیریت متغیرهای محیطی و تنظیمات مرکزی سرور پروکسی چندپروتکلی
"""

import os
from pathlib import Path

# Base directory
BASE_DIR = Path(__file__).resolve().parent

# Server Network Configuration
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))
DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

# Security & Dashboard Authentication
SECRET_KEY: str = os.getenv("SECRET_KEY", "rvg-gateway-super-secret-key-change-in-production-2026")
ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "admin123")
SESSION_COOKIE_NAME: str = "rvg_session_token"

# Domain & Public Addressing (used in generated client links)
# Auto-detect domain from Railway or custom environment variable
_env_domain = (
    os.getenv("PUBLIC_DOMAIN")
    or os.getenv("RAILWAY_PUBLIC_DOMAIN")
    or os.getenv("RAILWAY_STATIC_URL")
    or ""
).strip()
# Remove protocol prefix if user accidentally added https://
if _env_domain.startswith("https://"):
    _env_domain = _env_domain[8:]
elif _env_domain.startswith("http://"):
    _env_domain = _env_domain[7:]
PUBLIC_DOMAIN: str = _env_domain if _env_domain else "localhost"

# Check if running in a cloud/TLS environment like Railway
_is_cloud_tls = (
    bool(os.getenv("RAILWAY_PUBLIC_DOMAIN"))
    or bool(os.getenv("RAILWAY_STATIC_URL"))
    or "railway.app" in PUBLIC_DOMAIN
    or os.getenv("PUBLIC_TLS", "true").lower() == "true"
)
_default_public_port = "443" if (_is_cloud_tls and PUBLIC_DOMAIN != "localhost") else str(PORT)
PUBLIC_PORT: int = int(os.getenv("PUBLIC_PORT", _default_public_port))
PUBLIC_TLS: bool = _is_cloud_tls if PUBLIC_DOMAIN != "localhost" else (os.getenv("PUBLIC_TLS", "true").lower() == "true")
WS_PATH: str = os.getenv("WS_PATH", "/vless")

# Database Configuration (Persistent SQLite or Redis/PostgreSQL)
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_DB_PATH = f"sqlite:///{DATA_DIR}/rvg_gateway.db"
DATABASE_URL: str = os.getenv("DATABASE_URL", DEFAULT_DB_PATH)

# High-Performance Network Tuning
# 512KB buffers for high-throughput streaming
BUFFER_SIZE: int = int(os.getenv("BUFFER_SIZE", str(512 * 1024)))  # 524288 bytes
SOCKET_TIMEOUT: int = int(os.getenv("SOCKET_TIMEOUT", "60"))  # seconds

# SOCKS5 Internal Relay Configuration
ENABLE_SOCKS5: bool = os.getenv("ENABLE_SOCKS5", "true").lower() == "true"
SOCKS5_HOST: str = os.getenv("SOCKS5_HOST", "0.0.0.0")
SOCKS5_PORT: int = int(os.getenv("SOCKS5_PORT", "1080"))

# Upstream DNS & Resolution
DEFAULT_DNS: str = os.getenv("DEFAULT_DNS", "1.1.1.1")
