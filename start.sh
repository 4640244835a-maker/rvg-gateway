#!/usr/bin/env bash
set -e

echo "=================================================="
echo " [RVG] Booting Gateway Engine with Xray-Core Supervisor"
echo " Time: $(date -u)"
echo " Railway Public Port: ${PORT:-8080}"
echo " Internal FastAPI Port: ${FASTAPI_INTERNAL_PORT:-8000}"
echo "=================================================="

# 1. آماده‌سازی دایرکتوری‌ها و ثبت شناسه پروسه ناظر (Supervisor PID)
mkdir -p /app/data /tmp
rm -f /tmp/xray.reload /tmp/xray.pid /tmp/fastapi.pid /tmp/supervisor.pid
echo "$$" > /tmp/supervisor.pid

# 2. راه‌اندازی اولیه دیتابیس و تولید config.json بر اساس کاربران پایگاه‌داده
echo "[RVG] Initializing SQLite database and building initial Xray config.json..."
python3 -c "
import database
from xray_manager import regenerate_config
database.init_db()
regenerate_config()
print('[RVG] Initial config.json built successfully.')
" || {
    echo "[RVG] Warning: Initial config generation encountered an issue, proceeding..."
}

# 3. اجرای وب‌سرور داخلی FastAPI (داشبورد، API، احراز هویت)
FASTAPI_PORT="${FASTAPI_INTERNAL_PORT:-8000}"
echo "[RVG] Starting FastAPI/Uvicorn on 127.0.0.1:${FASTAPI_PORT}..."
uvicorn main:app --host 127.0.0.1 --port "${FASTAPI_PORT}" &
FASTAPI_PID=$!
echo "${FASTAPI_PID}" > /tmp/fastapi.pid

# مهلت کوتاه برای آماده‌سازی سوکت وب‌سرور
sleep 1.0

# 4. تابع اختصاصی راه‌اندازی هسته Xray-core
CONFIG_FILE="/app/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    CONFIG_FILE="config.json"
fi

start_xray() {
    echo "[RVG] Spawning Xray-core binary on port ${PORT:-8080}..."
    xray run -c "$CONFIG_FILE" >> /tmp/xray.log 2>&1 &
    XRAY_PID=$!
    echo "${XRAY_PID}" > /tmp/xray.pid
    echo "[RVG] Xray-core daemon started successfully (PID: ${XRAY_PID})."
}

# بررسی وجود باینری xray در PATH
if ! command -v xray >/dev/null 2>&1; then
    echo "[RVG] Warning: xray binary not found in PATH! Falling back to standalone FastAPI mode on port ${PORT:-8080}."
    kill "$FASTAPI_PID" 2>/dev/null || true
    exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8080}"
fi

# استارت اولیه Xray-core
start_xray
LAST_XRAY_START=$(date +%s)

# 5. مدیریت سیگنال‌های خاتمه کانتینر (Graceful Shutdown)
cleanup() {
    echo "[RVG] Shutdown signal received. Stopping all daemon processes..."
    if [ -n "$FASTAPI_PID" ]; then
        kill -TERM "$FASTAPI_PID" 2>/dev/null || true
    fi
    if [ -n "$XRAY_PID" ]; then
        kill -TERM "$XRAY_PID" 2>/dev/null || true
    fi
    rm -f /tmp/xray.pid /tmp/fastapi.pid /tmp/supervisor.pid /tmp/xray.reload
    exit 0
}

trap cleanup SIGTERM SIGINT

# 6. مدیریت سیگنال SIGUSR1 جهت تفکیک ریلود تمیز (Clean Reload) از کرش ناخواسته
CLEAN_RELOAD=0

on_sigusr1() {
    echo "[RVG] >>> Signal SIGUSR1 caught! Initiating Clean Reload without dropping container <<<"
    CLEAN_RELOAD=1
    touch /tmp/xray.reload
    if [ -n "$XRAY_PID" ] && kill -0 "$XRAY_PID" 2>/dev/null; then
        echo "[RVG] Terminating previous Xray instance (PID: ${XRAY_PID}) to apply fresh config..."
        kill -TERM "$XRAY_PID" 2>/dev/null || true
    fi
}

trap on_sigusr1 SIGUSR1

# 7. حلقه ناظر هوشمند (Smart Supervisor Loop)
CONSECUTIVE_CRASHES=0

while true; do
    # الف) بررسی سلامت وب‌سرور داخلی FastAPI
    if ! kill -0 "$FASTAPI_PID" 2>/dev/null; then
        echo "[RVG] Critical: FastAPI process died unexpectedly! Initiating container shutdown..."
        cleanup
        exit 1
    fi

    # ب) بررسی وضعیت اجرای پروسه Xray-core
    if ! kill -0 "$XRAY_PID" 2>/dev/null; then
        NOW=$(date +%s)
        
        # ۱. وضعیت ریلود پاکیزه با سیگنال SIGUSR1 یا پرچم /tmp/xray.reload
        if [ "$CLEAN_RELOAD" -eq 1 ] || [ -f /tmp/xray.reload ]; then
            echo "[RVG] Clean reload in progress: Spawning fresh Xray-core with updated config.json..."
            CLEAN_RELOAD=0
            rm -f /tmp/xray.reload
            CONSECUTIVE_CRASHES=0
            start_xray
            LAST_XRAY_START=$(date +%s)
            echo "[RVG] Clean reload completed successfully. Container remains alive and operational."

        # ۲. وضعیت کرش ناخواسته و غیرمنتظره
        else
            if [ $((NOW - LAST_XRAY_START)) -gt 20 ]; then
                CONSECUTIVE_CRASHES=0
            fi

            CONSECUTIVE_CRASHES=$((CONSECUTIVE_CRASHES + 1))
            echo "[RVG] Process crash detected: Xray (PID: $XRAY_PID) stopped unexpectedly! (Crash Count: ${CONSECUTIVE_CRASHES}/5)"

            # اگر ۵ بار پیاپی بدون وقفه کرش کرد، کانتینر را متوقف کن
            if [ "$CONSECUTIVE_CRASHES" -ge 5 ]; then
                echo "[RVG] Critical: Repeated process crashes (5 consecutive). Halting container..."
                cleanup
                exit 1
            fi

            # تاخیر کوتاه جهت جلوگیری از حلقه داغ پردازنده پیش از تلاش مجدد
            sleep 1.0
            start_xray
            LAST_XRAY_START=$(date +%s)
        fi
    fi

    sleep 0.5
done
