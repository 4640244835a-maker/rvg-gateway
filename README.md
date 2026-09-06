# ⚡ RVG Gateway - Self-Hosted Multi-Protocol Proxy Manager
سیستم جامع و خود-میزبانی مدیریت پروکسی چندپروتکلی مشابه **RVG Gateway**، نوشته‌شده با پایتون (**Python**) و فریم‌ورک سریع **FastAPI**، آماده دیپلوی مستقیم روی **Railway**، **Docker** یا هر سرور لینوکسی.

---

## 🌟 ویژگی‌های کلیدی (Key Features)

1. **موتور رله VLESS over WebSocket**:
   - مسیر پیش‌فرض: `/vless` با رمزنگاری TLS.
   - مفسر دودویی مستقیم هدرهای VLESS با اعتبارسنجی دقیق UUID در لایه اول.
   - رله پکت‌ها به مقصد اصلی بدون سربار اضافی.

2. **پروکسی داخلی SOCKS5**:
   - هندلر موازی TCP برای ترافیک پروتکل SOCKS5 (پورت پیش‌فرض: 1080).
   - احراز هویت اختیاری یا مبتنی بر UUID برای هر کاربر.

3. **حسابداری بیدرنگ ترافیک (Real-Time Traffic Accounting)**:
   - محاسبه خودکار حجم ارسالی و دریافتی (`len(chunk)`).
   - ذخیره دائمی در دیتابیس SQLite.
   - قطع آنی اتصال به محض رسیدن مصرف به سقف سهمیه (`used_bytes >= quota_bytes`).

4. **داشبورد مدیریتی مدرن (Admin Dashboard)**:
   - رابط کاربری شیک، دارک‌مود، و سازگار کامل با زبان فارسی و انگلیسی (RTL / LTR).
   - ایجاد، ویرایش، تغییر سهمیه (MB / GB / TB) و تعیین تاریخ انقضا برای لینک‌ها.
   - فعال‌سازی یا غیرفعال‌سازی آنی لینک بدون ری‌استارت سرور.
   - تولید خودکار لینک پیکربندی استاندارد `vless://...` و نمایش **QR Code** اختصاصی با جاوااسکریپت.

5. **بهینه‌سازی شبکه و کارایی فوق‌العاده (Performance & Low Latency)**:
   - تنظیم بافرهای سوکت روی **512KB** (`SO_RCVBUF`, `SO_SNDBUF`).
   - فعال‌سازی `TCP_NODELAY` جهت غیرفعال‌سازی الگوریتم Nagle و کاهش تاخیر پکت‌های کوچک.
   - فعال‌سازی `SO_KEEPALIVE` روی تمامی سوکت‌ها برای حفظ پایداری ارتباط در شرایط نوسان شبکه.

---

## 📁 ساختار پروژه (Project Structure)

```text
├── main.py              # نقطه ورود برنامه، راه‌اندازی FastAPI و مدیریت مسیرها
├── config.py            # مدیریت متغیرهای محیطی و تنظیمات مرکزی
├── database.py          # مدل‌های SQLAlchemy، تراکنش‌ها و حسابداری ترافیک
├── relays/
│   ├── __init__.py
│   ├── vless.py         # هندلر و رله VLESS WebSocket با بافر 512KB
│   └── socks.py         # سرور داخلی SOCKS5 TCP
├── templates/
│   ├── dashboard.html   # قالب داشبورد با Tailwind CSS و QR Code
│   └── login.html       # صفحه ورود به پنل مدیریت
├── Dockerfile           # فایل ساخت ایمیج بهینه Python 3.10-slim
├── railway.json         # پیکربندی دیپلوی مستقیم روی پلتفرم Railway
├── requirements.txt     # کتابخانه‌های مورد نیاز پایتون
└── test_relays.py       # تست‌های واحد تجزیه هدرهای باینری
```

---

## 🚀 روش‌های دیپلوی (Deployment Methods)

### روش ۱: دیپلوی مستقیم روی Railway (One-Click / Git)
1. ریپازیتوری را در حساب گیت‌هاب خود Fork یا Push کنید.
2. وارد [Railway.app](https://railway.app) شوید و پروژه جدید از گیت‌هاب بسازید.
3. فایل `railway.json` و `Dockerfile` به صورت خودکار شناسایی و سرویس بیلد می‌شود.
4. متغیرهای محیطی اختیاری (اختیاری) را در بخش **Variables** ست کنید:
   - `ADMIN_PASSWORD`: رمز عبور دلخواه برای ورود به پنل داشبورد.
   - `PUBLIC_DOMAIN`: آدرس دامنه تخصیص‌یافته توسط ریل‌وی (مثال: `myapp.up.railway.app`).

### روش ۲: اجرا با Docker
```bash
# بیلد ایمیج
docker build -t rvg-gateway .

# اجرای کانتینر با دیتابیس ماندگار (Volume)
docker run -d \
  --name rvg-gateway \
  -p 8000:8000 \
  -p 1080:1080 \
  -v $(pwd)/data:/app/data \
  -e ADMIN_PASSWORD="your-secure-password" \
  -e PUBLIC_DOMAIN="your-vps-domain.com" \
  --restart unless-stopped \
  rvg-gateway
```

### روش ۳: اجرای مستقیم با پایتون روی سرور لینوکس (VPS)
```bash
# ۱. ایجاد محیط مجازی
python3 -m venv venv
source venv/bin/activate

# ۲. نصب وابستگی‌ها
pip install -r requirements.txt

# ۳. اجرای سرویس
uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## 📱 کلاینت‌های سازگار و نحوه اتصال
لینک‌های تولید شده در این داشبورد با تمامی کلاینت‌های استاندارد سازگارند:
- **اندروید**: v2rayNG, Nekoray, Matsuri
- **ویندوز**: v2rayN, Nekoray, Clash Verge
- **آی‌اواس (iOS)**: Shadowrocket, Streisand, FoXray, V2Box
- **مک / لینوکس**: Nekoray, Clash Meta
