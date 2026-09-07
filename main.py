"""
RVG Gateway - Main Application Entrypoint
سرور مرکزی مبتنی بر FastAPI و Uvicorn با پشتیبانی از WebSocket VLESS، سرور داخلی SOCKS5،
سیستم احراز هویت ادمین و داشبورد مدیریتی Jinja2.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI,
    WebSocket,
    Depends,
    HTTPException,
    status,
    Request,
    Response,
    Form
)
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import config
import database
from relays.socks import Socks5Server
from xray_manager import regenerate_config, reload_xray, get_user_traffic_stats

# پیکربندی سیستم لاگینگ
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("rvg.main")

# متغیر سراسری سرور SOCKS5
socks5_instance: Optional[Socks5Server] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """مدیریت چرخه حیات سرویس (Startup & Shutdown)"""
    logger.info("Starting RVG Gateway initialization...")
    # مقداردهی اولیه پایگاه داده و ایجاد جداول
    database.init_db()
    logger.info(f"Database initialized at: {config.DATABASE_URL}")

    # تولید اولیه فایل کانفیگ Xray-core بر اساس کاربران فعال پایگاه داده
    try:
        with database.SessionLocal() as startup_db:
            regenerate_config(startup_db)
        reload_xray()
    except Exception as xray_err:
        logger.warning(f"Initial Xray config generation deferred: {xray_err}")

    # راه‌اندازی تسک غیرمسدودکننده تخلیه دسته‌ای ترافیک به دیتابیس
    traffic_worker_task = asyncio.create_task(database.start_traffic_flush_worker(interval=2.0))

    # راه‌اندازی سرور داخلی SOCKS5 در صورت فعال بودن
    global socks5_instance
    if config.ENABLE_SOCKS5:
        socks5_instance = Socks5Server(host=config.SOCKS5_HOST, port=config.SOCKS5_PORT)
        asyncio.create_task(socks5_instance.start())
        logger.info(f"SOCKS5 Engine started on port {config.SOCKS5_PORT}")

    logger.info(f"RVG Gateway HTTP & WebSocket ready on port {config.PORT}")
    yield

    # فرآیند خروج و آزادسازی منابع
    logger.info("Shutting down RVG Gateway...")
    traffic_worker_task.cancel()
    database.flush_traffic_sync()
    if socks5_instance:
        await socks5_instance.stop()



app = FastAPI(
    title="RVG Gateway - Multi-Protocol Proxy Manager",
    description="سیستم خود-میزبانی مدیریت پروکسی چندپروتکلی VLESS و SOCKS5",
    version="1.2.0",
    lifespan=lifespan
)

# تنظیم موتور قالب Jinja2
templates_dir = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


def render_template(
    template_name: str,
    request: Request,
    context: Optional[dict] = None,
    status_code: int = 200
) -> HTMLResponse:
    """
    رندر امن قالب‌های Jinja2 با سازگاری ۱۰۰٪ با نسخه‌های جدید و قدیم FastAPI/Starlette.
    (رفع خطای Internal Server Error در Starlette 0.36+ / FastAPI 0.108+)
    """
    ctx = dict(context or {})
    ctx["request"] = request

    # ۱. روش استاندارد Starlette 0.36+ با آرگومان‌های نام‌دار
    try:
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=ctx,
            status_code=status_code
        )
    except TypeError:
        pass
    except Exception as e:
        logger.warning(f"TemplateResponse (named) failed: {e}")

    # ۲. روش سنتی Starlette
    try:
        return templates.TemplateResponse(
            template_name,
            ctx,
            status_code=status_code
        )
    except Exception as e:
        logger.warning(f"TemplateResponse (positional) failed: {e}")

    # ۳. رندر مستقیم از شیء Jinja2 به عنوان پشتیبان بدون خطا
    template = templates.get_template(template_name)
    content = template.render(ctx)
    return HTMLResponse(content=content, status_code=status_code)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """ثبت لاگ کامل خطاها و جلوگیری از سقوط بدون ردگیری سرور"""
    logger.error(f"Unhandled server exception on {request.url.path}: {exc}", exc_info=True)
    return HTMLResponse(
        content=f"""
        <!DOCTYPE html>
        <html lang="fa" dir="rtl">
        <head><meta charset="UTF-8"><title>خطای سرور</title></head>
        <body style="font-family:sans-serif;background:#090d16;color:#f1f5f9;padding:40px;text-align:center;">
          <h2 style="color:#f43f5e;">⚠️ خطای غیرمنتظره در سرور</h2>
          <p style="color:#94a3b8;font-size:14px;">خطا در آدرس: <code>{request.url.path}</code></p>
          <pre style="background:#1e293b;padding:15px;border-radius:10px;display:inline-block;text-align:left;color:#cbd5e1;font-size:12px;max-width:90%;overflow:auto;">{str(exc)}</pre>
          <p><a href="/login" style="color:#10b981;">بازگشت به صفحه ورود</a></p>
        </body>
        </html>
        """,
        status_code=500
    )


# ==========================================
# سیستم احراز هویت داشبورد (Session Auth)
# ==========================================

def is_authenticated(request: Request) -> bool:
    """بررسی توکن نشست در کوکی کاربر"""
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    return token == config.SECRET_KEY


def require_auth(request: Request):
    """وابستگی احراز هویت جهت محافظت از مسیرهای حساس"""
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"}
        )


# ==========================================
# مسیرهای وب و صفحات HTML (Web Pages)
# ==========================================

@app.get("/", response_class=RedirectResponse)
async def root_redirect():
    """تغییر مسیر صفحه اصلی به داشبورد"""
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None):
    """نمایش فرم ورود به سیستم"""
    if is_authenticated(request):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return render_template("login.html", request, {"error": error})


@app.post("/login")
async def login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...)
):
    """بررسی اطلاعات کاربری ورود"""
    if username == config.ADMIN_USERNAME and password == config.ADMIN_PASSWORD:
        redirect = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
        redirect.set_cookie(
            key=config.SESSION_COOKIE_NAME,
            value=config.SECRET_KEY,
            httponly=True,
            max_age=86400 * 7,  # ۷ روز اعتبار
            samesite="lax"
        )
        return redirect
    return render_template(
        "login.html",
        request,
        {"error": "نام کاربری یا رمز عبور اشتباه است."},
        status_code=status.HTTP_401_UNAUTHORIZED
    )


@app.get("/logout")
async def logout():
    """خروج از حساب کاربری و ابطال کوکی نشست"""
    redirect = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    redirect.delete_cookie(config.SESSION_COOKIE_NAME)
    return redirect


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_view(
    request: Request,
    db: Session = Depends(database.get_db)
):
    """صفحه داشبورد اصلی مدیریت لینک‌ها و ترافیک با تشخیص پویای دامنه سرور"""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    # تشخیص هوشمند دامنه عمومی سرور از روی هدرهای ورودی ریل‌وی یا پروکسی معکوس
    raw_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    client_domain = raw_host.split(":")[0].strip() if raw_host else ""

    detected_domain = config.PUBLIC_DOMAIN
    detected_port = config.PUBLIC_PORT
    detected_tls = config.PUBLIC_TLS

    if client_domain and client_domain not in ("localhost", "127.0.0.1", "0.0.0.0"):
        detected_domain = client_domain
        proto = request.headers.get("x-forwarded-proto", "https")
        if proto == "https" or "railway.app" in client_domain:
            detected_port = 443
            detected_tls = True

    raw_links = database.get_all_links(db)
    links_data = [
        link.to_dict(domain=detected_domain, port=detected_port, tls=detected_tls)
        for link in raw_links
    ]

    total_used_bytes = sum(link.used_bytes for link in raw_links)

    return render_template(
        "dashboard.html",
        request,
        {
            "links": links_data,
            "total_used_formatted": database.format_bytes(total_used_bytes),
            "public_domain": detected_domain,
            "public_port": detected_port,
            "public_tls": detected_tls,
            "ws_path": config.WS_PATH,
            "socks5_port": config.SOCKS5_PORT,
            "enable_socks5": config.ENABLE_SOCKS5
        }
    )


# ==========================================
# پروب سلامت درگاه VLESS (مدیریت توسط هسته Xray-core)
# ==========================================

_configured_ws_path = config.WS_PATH if config.WS_PATH.startswith("/") else f"/{config.WS_PATH}"

@app.api_route(_configured_ws_path, methods=["GET", "POST", "HEAD"])
@app.api_route("/vless", methods=["GET", "POST", "HEAD"])
@app.api_route("/vless/", methods=["GET", "POST", "HEAD"])
async def vless_http_probe():
    """
    پاسخ به پروب‌های HTTP کلاینت‌ها و تایید آنلاین بودن درگاه VLESS (مدیریت‌شده با Xray-core)
    """
    return Response(
        content="RVG Gateway VLESS Active (Powered by official Xray-core Engine)\n",
        media_type="text/plain",
        status_code=status.HTTP_200_OK
    )


# ==========================================
# REST API Endpoints (مدیریت لینک‌ها و مانیتورینگ)
# ==========================================

@app.get("/api/links")
async def list_links(request: Request, db: Session = Depends(database.get_db)):
    """دریافت فهرست تمام لینک‌ها به فرمت JSON"""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    raw_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    client_domain = raw_host.split(":")[0].strip() if raw_host else ""
    active_domain = client_domain if (client_domain and client_domain not in ("localhost", "127.0.0.1", "0.0.0.0")) else config.PUBLIC_DOMAIN
    active_port = 443 if ("railway.app" in active_domain) else config.PUBLIC_PORT
    active_tls = True if (active_port == 443 or "railway.app" in active_domain) else config.PUBLIC_TLS
    links = database.get_all_links(db)
    return [l.to_dict(domain=active_domain, port=active_port, tls=active_tls) for l in links]


@app.post("/api/links")
async def create_new_link(
    request: Request,
    name: str = Form(...),
    quota_val: float = Form(...),
    quota_unit: str = Form("GB"),
    expire_days: int = Form(0),
    db: Session = Depends(database.get_db)
):
    """ایجاد لینک جدید با مشخص کردن حجم سهمیه و مدت انقضا"""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    unit_multipliers = {
        "MB": 1024 * 1024,
        "GB": 1024 * 1024 * 1024,
        "TB": 1024 * 1024 * 1024 * 1024
    }
    multiplier = unit_multipliers.get(quota_unit.upper(), unit_multipliers["GB"])
    quota_bytes = int(quota_val * multiplier)

    expire_at = None
    if expire_days > 0:
        expire_at = datetime.now(timezone.utc) + timedelta(days=expire_days)

    link = database.create_link(db, name=name, quota_bytes=quota_bytes, expire_at=expire_at)
    logger.info(f"Created new proxy link: '{link.name}' with UUID: {link.uuid}")

    # همگام‌سازی بلادرنگ با کانفیگ Xray-core
    try:
        regenerate_config(db)
        reload_xray()
    except Exception as xray_sync_err:
        logger.warning(f"Xray config reload failed during link creation: {xray_sync_err}")

    # در صورتی که درخواست از طریق فرم HTML ارسال شده باشد، به داشبورد برمی‌گردیم
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return link.to_dict()


@app.post("/api/links/{link_id}/toggle")
async def toggle_link(
    link_id: int,
    request: Request,
    db: Session = Depends(database.get_db)
):
    """تغییر آنی وضعیت فعال/غیرفعال بودن یک لینک"""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    updated = database.toggle_link_status(db, link_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Link not found")

    # همگام‌سازی بلادرنگ با کانفیگ Xray-core
    try:
        regenerate_config(db)
        reload_xray()
    except Exception as xray_sync_err:
        logger.warning(f"Xray config reload failed during link toggle: {xray_sync_err}")

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return updated.to_dict()


@app.post("/api/links/{link_id}/reset")
async def reset_link(
    link_id: int,
    request: Request,
    db: Session = Depends(database.get_db)
):
    """صفر کردن حجم مصرفی لینک"""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    updated = database.reset_link_traffic(db, link_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Link not found")

    # همگام‌سازی بلادرنگ با کانفیگ Xray-core در صورت مجاز شدن مجدد کاربر
    try:
        regenerate_config(db)
        reload_xray()
    except Exception as xray_sync_err:
        logger.warning(f"Xray config reload failed during link reset: {xray_sync_err}")

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return updated.to_dict()


@app.post("/api/links/{link_id}/delete")
async def remove_link(
    link_id: int,
    request: Request,
    db: Session = Depends(database.get_db)
):
    """حذف دائمی لینک"""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    success = database.delete_link(db, link_id)
    if not success:
        raise HTTPException(status_code=404, detail="Link not found")

    # همگام‌سازی بلادرنگ با کانفیگ Xray-core
    try:
        regenerate_config(db)
        reload_xray()
    except Exception as xray_sync_err:
        logger.warning(f"Xray config reload failed during link deletion: {xray_sync_err}")

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return {"status": "success", "message": "Link deleted"}


@app.get("/api/config/{user_uuid}")
async def get_config_by_uuid(user_uuid: str, request: Request, db: Session = Depends(database.get_db)):
    """دریافت اطلاعات و لینک کانفیگ استاندارد کلاینت با UUID"""
    link = database.get_link_by_uuid(db, user_uuid)
    if not link:
        raise HTTPException(status_code=404, detail="Config not found")
    raw_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    client_domain = raw_host.split(":")[0].strip() if raw_host else ""
    active_domain = client_domain if (client_domain and client_domain not in ("localhost", "127.0.0.1", "0.0.0.0")) else config.PUBLIC_DOMAIN
    active_port = 443 if ("railway.app" in active_domain) else config.PUBLIC_PORT
    active_tls = True if (active_port == 443 or "railway.app" in active_domain) else config.PUBLIC_TLS
    return link.to_dict(domain=active_domain, port=active_port, tls=active_tls)


@app.get("/api/health")
async def health_check():
    """بررسی سلامت سیستم (Health Check) مناسب برای پایش Docker و Railway"""
    return {
        "status": "healthy",
        "service": "RVG Gateway",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "vless_active": True,
        "socks5_active": config.ENABLE_SOCKS5
    }


@app.get("/api/diag/tcp-test")
async def diagnostic_tcp_test():
    """
    نقطه پایانی تشخیصی بدون احراز هویت جهت اعتبارسنجی شبکه خروجی کانتینر
    تست مستقیم TCP و تست رزولوشن DNS با تابع _resolve_host پروتکل VLESS
    """
    tcp_results = {}
    tcp_targets = [("8.8.8.8", 443), ("google.com", 443), ("1.1.1.1", 443)]
    for host, port in tcp_targets:
        start = time.time()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=5.0
            )
            elapsed = time.time() - start
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            tcp_results[f"{host}:{port}"] = {
                "status": "success",
                "time_sec": round(elapsed, 2)
            }
        except Exception as e:
            elapsed = time.time() - start
            tcp_results[f"{host}:{port}"] = {
                "status": "failed",
                "error": str(e),
                "time_sec": round(elapsed, 2)
            }

    dns_results = {}
    dns_targets = ["google.com", "cloudflare.com", "dns.google"]
    for domain in dns_targets:
        start = time.time()
        try:
            loop = asyncio.get_running_loop()
            resolved = await asyncio.wait_for(
                loop.getaddrinfo(domain, 443),
                timeout=3.0
            )
            elapsed = time.time() - start
            ips = list(dict.fromkeys([item[4][0] for item in resolved]))
            dns_results[domain] = {
                "status": "success",
                "time_sec": round(elapsed, 2),
                "resolved_ips": ips
            }
        except Exception as e:
            elapsed = time.time() - start
            dns_results[domain] = {
                "status": "failed",
                "error": str(e),
                "time_sec": round(elapsed, 2)
            }

    return {
        "tcp_connect_tests": tcp_results,
        "dns_resolve_tests": dns_results
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=config.DEBUG)
