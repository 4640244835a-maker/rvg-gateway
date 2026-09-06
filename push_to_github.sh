#!/usr/bin/env bash
set -e

# ==============================================================================
# RVG Gateway - GitHub Sync & Push Helper Script (Initial & Update)
# ==============================================================================

echo "======================================================="
echo "   RVG Gateway - بارگذاری و بروزرسانی خودکار در گیت‌هاب"
echo "======================================================="

# Check git is installed
if ! command -v git &> /dev/null; then
    echo "❌ Git نصب نیست! لطفاً ابتدا Git را بر روی سیستم نصب کنید."
    exit 1
fi

BRANCH="${2:-main}"
DEFAULT_MSG="update: remove VOLUME from Dockerfile and add docker-compose for Railway"
COMMIT_MSG="${3}"

# Check if already inside an existing Git repository
if [ -d ".git" ]; then
    echo "🔍 مخزن Git محلی شناسایی شد."
    CURRENT_REMOTE=$(git remote get-url origin 2>/dev/null || true)
    
    if [ -n "$CURRENT_REMOTE" ]; then
        echo "🔗 ریموت متصل: ${CURRENT_REMOTE}"
    else
        REPO_URL="$1"
        if [ -z "$REPO_URL" ]; then
            read -p "📌 آدرس مخزن گیت‌هاب را وارد کنید (مثال: https://github.com/username/rvg-gateway.git): " REPO_URL
        fi
        if [ -n "$REPO_URL" ]; then
            git remote add origin "${REPO_URL}"
        fi
    fi

    if [ -z "$COMMIT_MSG" ]; then
        read -p "💬 پیام کامیت بروزرسانی [پیش‌فرض: ${DEFAULT_MSG}]: " USER_MSG
        COMMIT_MSG="${USER_MSG:-$DEFAULT_MSG}"
    fi

    echo "📦 اضافه کردن تغییرات و فایل‌های جدید به ایندکس Git..."
    git add .

    # Check if there are changes to commit
    if git diff --cached --quiet; then
        echo "ℹ️ تغییری برای ثبت کامیت جدید وجود ندارد."
    else
        echo "💾 ثبت کامیت بروزرسانی: '${COMMIT_MSG}'..."
        git commit -m "${COMMIT_MSG}"
    fi

    echo "🌿 تنظیم شاخه بر روی ${BRANCH}..."
    git branch -M "${BRANCH}"

    echo "🚀 ارسال (Push) بروزرسانی به گیت‌هاب..."
    git push -u origin "${BRANCH}"

    echo "======================================================="
    echo "✅ فایل‌های پروژه در گیت‌هاب با موفقیت بروزرسانی شدند!"
    echo "======================================================="
    exit 0
fi

# Not a git repo yet - Initial setup
REPO_URL="$1"
if [ -z "$REPO_URL" ]; then
    read -p "📌 آدرس مخزن گیت‌هاب را وارد کنید (مثال: https://github.com/username/rvg-gateway.git): " REPO_URL
fi

if [ -z "$REPO_URL" ]; then
    echo "❌ آدرس مخزن مشخص نشد. عملیات لغو شد."
    exit 1
fi

if [ -z "$COMMIT_MSG" ]; then
    read -p "💬 پیام کامیت [پیش‌فرض: ${DEFAULT_MSG}]: " USER_MSG
    COMMIT_MSG="${USER_MSG:-$DEFAULT_MSG}"
fi

echo "🔄 در حال آماده‌سازی مخزن محلی..."
git init

echo "📦 اضافه کردن فایل‌ها به ایندکس..."
git add .

echo "💾 ثبت کامیت..."
git commit -m "${COMMIT_MSG}" || true

echo "🌿 تنظیم شاخه بر روی ${BRANCH}..."
git branch -M "${BRANCH}"

echo "🔗 اتصال به ریموت origin (${REPO_URL})..."
git remote remove origin 2>/dev/null || true
git remote add origin "${REPO_URL}"

echo "🚀 ارسال (Push) به گیت‌هاب..."
git push -u origin "${BRANCH}"

echo "======================================================="
echo "✅ پروژه با موفقیت در گیت‌هاب بارگذاری شد!"
echo "🌐 آدرس مخزن: ${REPO_URL}"
echo "======================================================="

