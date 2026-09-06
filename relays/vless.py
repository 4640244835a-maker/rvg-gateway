"""
RVG Gateway - VLESS over WebSocket Relay Engine
هندلر و تفکیککننده پروتکل VLESS مبتنی بر وبسوکت، بههمراه اعتبارسنجی اولیه،
بهینهسازی سطح پایین سوکت (TCP_NODELAY, SO_KEEPALIVE) و حسابرسی بیدرنگ ترافیک.
"""

import asyncio
import logging
import socket
import struct
import uuid
from typing import Tuple, Optional
from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

import config
import database

logger = logging.getLogger("rvg.vless")
logger.setLevel(logging.INFO)


class VlessHeaderParser:
    """
    مفسر پکت باینری پروتکل VLESS
    بر اساس ساختار استاندارد V2Ray/Xray VLESS Protocol Specification
    """
    @staticmethod
    def parse(raw_data: bytes) -> Tuple[str, int, str, int, bytes]:
        """
        تجزیه هدر اولیه VLESS
        خروجی: (client_uuid_str, command, target_host, target_port, initial_payload)
        """
        if len(raw_data) < 24:
            raise ValueError(f"Packet too short for VLESS header: {len(raw_data)} bytes")

        # 1. Version (1 byte - معمولاً 0x00)
        version = raw_data[0]
        if version != 0x00:
            logger.warning(f"Unexpected VLESS version: {version}")

        # 2. Client UUID (16 bytes raw binary)
        client_uuid_bytes = raw_data[1:17]
        client_uuid = str(uuid.UUID(bytes=client_uuid_bytes))

        # 3. Protocol Addons / Protobuf length (1 byte)
        cursor = 17
        addons_len = raw_data[cursor]
        cursor += 1
        if addons_len > 0:
            cursor += addons_len  # رد کردن بایت‌های افزودنی

        # 4. Command (1 byte): 0x01 = TCP, 0x02 = UDP, 0x03 = Mux
        if cursor >= len(raw_data):
            raise ValueError("Truncated header at command byte")
        command = raw_data[cursor]
        cursor += 1

        # 5. Target Port (2 bytes Big-Endian uint16)
        if cursor + 2 > len(raw_data):
            raise ValueError("Truncated header at target port")
        target_port = struct.unpack("!H", raw_data[cursor:cursor + 2])[0]
        cursor += 2

        # 6. Address Type (1 byte):
        #    0x01 = IPv4 (4 bytes)
        #    0x02 = Domain Name (1 byte length prefix + ASCII string)
        #    0x03 = IPv6 (16 bytes)
        if cursor >= len(raw_data):
            raise ValueError("Truncated header at address type")
        addr_type = raw_data[cursor]
        cursor += 1

        target_host: str = ""
        if addr_type == 0x01:  # IPv4
            if cursor + 4 > len(raw_data):
                raise ValueError("Truncated IPv4 address bytes")
            target_host = socket.inet_ntoa(raw_data[cursor:cursor + 4])
            cursor += 4
        elif addr_type == 0x02:  # Domain
            if cursor >= len(raw_data):
                raise ValueError("Truncated domain length byte")
            domain_len = raw_data[cursor]
            cursor += 1
            if cursor + domain_len > len(raw_data):
                raise ValueError("Truncated domain string bytes")
            target_host = raw_data[cursor:cursor + domain_len].decode("utf-8", errors="ignore")
            cursor += domain_len
        elif addr_type == 0x03:  # IPv6
            if cursor + 16 > len(raw_data):
                raise ValueError("Truncated IPv6 address bytes")
            target_host = socket.inet_ntop(socket.AF_INET6, raw_data[cursor:cursor + 16])
            cursor += 16
        else:
            raise ValueError(f"Unsupported VLESS address type: {addr_type}")

        # باقی‌مانده پکت به عنوان دیتای اولیه برنامه (Payload) جهت ارسال به مقصد
        initial_payload = raw_data[cursor:]
        return client_uuid, command, target_host, target_port, initial_payload


async def handle_vless_websocket(websocket: WebSocket, db: Session):
    """
    هندلر اصلی اتصال WebSocket در مسیر /vless
    مراحل:
    ۱. پذیرش اتصال وبسوکت
    ۲. دریافت اولین پکت باینری حاوی هدر VLESS
    ۳. اعتبارسنجی هویت و سهمیه کاربر از دیتابیس
    ۴. برقراری سوکت TCP به سرور مقصد (Upstream) با بافر 512KB و TCP_NODELAY
    ۵. ارسال پاسخ موفقیت VLESS (b"\x00\x00")
    ۶. آغاز رله ترافیک دوطرفه همگام با محاسبه ترافیک مصرفی
    """
    await websocket.accept()
    upstream_reader: Optional[asyncio.StreamReader] = None
    upstream_writer: Optional[asyncio.StreamWriter] = None

    try:
        # دریافت پکت اول شامل هدر VLESS
        first_chunk = await websocket.receive_bytes()
        if not first_chunk:
            await websocket.close(code=1003, reason="Empty initial packet")
            return

        # تجزیه مشخصات درخواست
        try:
            client_uuid, command, target_host, target_port, initial_payload = VlessHeaderParser.parse(first_chunk)
        except Exception as parse_err:
            logger.warning(f"Failed to parse VLESS header: {parse_err}")
            await websocket.close(code=1008, reason="Malformed VLESS packet")
            return

        # اعتبارسنجی کاربر و کنترل سهمیه
        user = database.get_link_by_uuid(db, client_uuid)
        if not user:
            logger.warning(f"Unauthorized VLESS connection attempt with UUID: {client_uuid}")
            await websocket.close(code=1008, reason="Unauthorized UUID")
            return

        if not user.is_active:
            logger.warning(f"Connection rejected: User {user.name} ({client_uuid}) is disabled")
            await websocket.close(code=1008, reason="Account disabled")
            return

        if user.quota_bytes > 0 and user.used_bytes >= user.quota_bytes:
            logger.warning(f"Connection rejected: User {user.name} has exceeded quota")
            await websocket.close(code=1008, reason="Quota exceeded")
            return

        logger.info(f"VLESS connection accepted for [{user.name}] -> Target: {target_host}:{target_port} (CMD={command})")

        # برقراری اتصال TCP به سرور مقصد
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=target_host,
                    port=target_port,
                    limit=config.BUFFER_SIZE
                ),
                timeout=10.0
            )
        except Exception as conn_err:
            logger.error(f"Failed to connect to upstream {target_host}:{target_port} -> {conn_err}")
            await websocket.close(code=1011, reason=f"Upstream unreachable: {target_host}")
            return

        # اعمال بهینه‌سازی‌های شبکه روی سوکت خام سیستم عامل
        sock = upstream_writer.get_extra_info("socket")
        if sock:
            try:
                # 1. غیرفعال کردن الگوریتم Nagle جهت به حداقل رساندن Latency در بسته‌های کوچک
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                # 2. فعال‌سازی Keep-Alive سوکت جهت شناسایی و حفظ اتصالات نیمه‌باز
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                # 3. تنظیم بافرهای ورودی و خروجی روی 512KB برای پهنای باند بالا
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, config.BUFFER_SIZE)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, config.BUFFER_SIZE)
            except Exception as sock_opt_err:
                logger.debug(f"Could not apply some socket options: {sock_opt_err}")

        # ارسال پاسخ تایید هدر VLESS به کلاینت:
        # Version 0x00 + Protobuf addons length 0x00
        await websocket.send_bytes(b"\x00\x00")

        # در صورت وجود دیتای اولیه (مثلا TLS Client Hello)، بلافاصله به سرور مقصد هدایت شود
        if initial_payload:
            upstream_writer.write(initial_payload)
            await upstream_writer.drain()
            database.record_traffic(db, user.id, len(initial_payload))

        # اجرای رله دوطرفه (Bi-Directional Relay)
        ws_to_upstream_task = asyncio.create_task(
            _relay_ws_to_tcp(websocket, upstream_writer, user.id, db)
        )
        upstream_to_ws_task = asyncio.create_task(
            _relay_tcp_to_ws(upstream_reader, websocket, user.id, db)
        )

        # انتظار تا بسته شدن یکی از دو مسیر
        done, pending = await asyncio.wait(
            [ws_to_upstream_task, upstream_to_ws_task],
            return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()

    except WebSocketDisconnect:
        logger.info("Client WebSocket disconnected gracefully")
    except Exception as e:
        logger.error(f"Error in VLESS WebSocket lifecycle: {e}", exc_info=config.DEBUG)
    finally:
        if upstream_writer:
            try:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            except Exception:
                pass


async def _relay_ws_to_tcp(
    websocket: WebSocket,
    writer: asyncio.StreamWriter,
    user_id: int,
    db: Session
):
    """انتقال بسته‌ها از کلاینت وبسوکت به سوکت مقصد به همراه محاسبه ترافیک"""
    try:
        while True:
            data = await websocket.receive_bytes()
            if not data:
                break
            chunk_len = len(data)
            writer.write(data)
            await writer.drain()

            # محاسبه ترافیک و قطع اتصال در صورت اتمام حجم
            has_quota = database.record_traffic(db, user_id, chunk_len)
            if not has_quota:
                logger.warning(f"User {user_id} exceeded quota during upload. Terminating connection.")
                break
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as e:
        logger.debug(f"WS to TCP relay stopped: {e}")


async def _relay_tcp_to_ws(
    reader: asyncio.StreamReader,
    websocket: WebSocket,
    user_id: int,
    db: Session
):
    """انتقال بسته‌ها از سوکت مقصد به کلاینت وبسوکت به همراه محاسبه ترافیک"""
    try:
        while True:
            data = await reader.read(config.BUFFER_SIZE)
            if not data:
                break
            chunk_len = len(data)
            await websocket.send_bytes(data)

            # محاسبه ترافیک و قطع اتصال در صورت اتمام حجم
            has_quota = database.record_traffic(db, user_id, chunk_len)
            if not has_quota:
                logger.warning(f"User {user_id} exceeded quota during download. Terminating connection.")
                break
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as e:
        logger.debug(f"TCP to WS relay stopped: {e}")
