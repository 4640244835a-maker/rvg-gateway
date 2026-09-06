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
PUBLIC_DOMAIN: str = os.getenv("PUBLIC_DOMAIN", "localhost")
PUBLIC_PORT: int = int(os.getenv("PUBLIC_PORT", str(PORT)))
PUBLIC_TLS: bool = os.getenv("PUBLIC_TLS", "true").lower() == "true"
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
