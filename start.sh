#!/usr/bin/env bash
set -e

echo "=================================================="
echo " [RVG] Booting Gateway Engine with Xray-Core"
echo " Time: $(date -u)"
echo " Railway Public Port: ${PORT:-8080}"
echo " Internal FastAPI Port: ${FASTAPI_INTERNAL_PORT:-8000}"
echo "=================================================="

# 1. آماده‌سازی دایرکتوری‌های دیتابیس و داده‌ها
mkdir -p /app/data /tmp

# 2. راه‌اندازی اولیه دیتابیس و تولید config.json بر اساس کاربران فعلی SQLite
echo "[RVG] Initializing SQLite database and building Xray config.json..."
python3 -c "
import database
from xray_manager import regenerate_config
database.init_db()
regenerate_config()
print('[RVG] Initial config.json built successfully.')
" || {
    echo "[RVG] Warning: Initial config generation encountered an issue, proceeding with fallback..."
}

# 3. اجرای وب‌سرور داخلی FastAPI (داشبورد، API، احراز هویت)
FASTAPI_PORT="${FASTAPI_INTERNAL_PORT:-8000}"
echo "[RVG] Starting FastAPI/Uvicorn on 127.0.0.1:${FASTAPI_PORT}..."
uvicorn main:app --host 127.0.0.1 --port "${FASTAPI_PORT}" &
FASTAPI_PID=$!
echo "${FASTAPI_PID}" > /tmp/fastapi.pid

# مهلت کوتاه برای اطمینان از بالا آمدن FastAPI
sleep 1.5

# 4. اجرای هسته Xray-core روی پورت عمومی سرور
if command -v xray >/dev/null 2>&1; then
    echo "[RVG] Launching official Xray-core binary on port ${PORT:-8080}..."
    CONFIG_FILE="/app/config.json"
    if [ ! -f "$CONFIG_FILE" ]; then
        CONFIG_FILE="config.json"
    fi
    xray run -c "$CONFIG_FILE" &
    XRAY_PID=$!
    echo "${XRAY_PID}" > /tmp/xray.pid
    echo "[RVG] Xray-core daemon started successfully (PID: ${XRAY_PID})."
else
    echo "[RVG] Warning: xray binary not found in PATH! Running in standalone FastAPI mode on port ${PORT:-8080}."
    # در صورت عدم وجود باینری xray، یوویکورن را مستقیماً روی پورت عمومی اجرا می‌کنیم تا کانتینر کرش نکند
    kill "$FASTAPI_PID" 2>/dev/null || true
    exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8080}"
fi

# 5. مدیریت سیگنال‌های خاتمه کانتینر (Graceful Shutdown)
cleanup() {
    echo "[RVG] Stopping all daemon processes..."
    if [ -n "$FASTAPI_PID" ]; then
        kill -TERM "$FASTAPI_PID" 2>/dev/null || true
    fi
    if [ -n "$XRAY_PID" ]; then
        kill -TERM "$XRAY_PID" 2>/dev/null || true
    fi
    rm -f /tmp/xray.pid /tmp/fastapi.pid
    exit 0
}

trap cleanup SIGTERM SIGINT

# 6. حلقه مانیتورینگ سلامت پروسه‌ها
while kill -0 "$FASTAPI_PID" 2>/dev/null && kill -0 "$XRAY_PID" 2>/dev/null; do
    sleep 3
done

echo "[RVG] Critical: A core service stopped unexpectedly! Triggering restart..."
cleanup
