#!/usr/bin/env bash
set -e

# ==============================================================================
# RVG Gateway - GitHub Quick Push Helper Script
# ==============================================================================

echo "======================================================="
echo "   RVG Gateway - بارگذاری خودکار در گیت‌هاب (GitHub)"
echo "======================================================="

# Check git is installed
if ! command -v git &> /dev/null; then
    echo "❌ Git نصب نیست! لطفاً ابتدا Git را نصب کنید."
    exit 1
fi

# Ask for repository URL if not supplied
REPO_URL="$1"
if [ -z "$REPO_URL" ]; then
    read -p "📌 آدرس مخزن گیت‌هاب را وارد کنید (مثال: https://github.com/username/rvg-gateway.git): " REPO_URL
fi

if [ -z "$REPO_URL" ]; then
    echo "❌ آدرس مخزن مشخص نشد. عملیات لغو شد."
    exit 1
fi

BRANCH="${2:-main}"

echo "🔄 در حال آماده‌سازی مخزن محلی..."
git init

echo "📦 اضافه کردن فایل‌ها به ایندکس..."
git add .

echo "💾 ثبت کامیت اولیه..."
git commit -m "feat: initial commit RVG Gateway multi-protocol proxy engine" || true

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
