"""
RVG Gateway - VLESS over WebSocket Relay Engine
هندلر و تفکیککننده پروتکل VLESS مبتنی بر وبسوکت، بههمراه اعتبارسنجی اولیه،
بهینهسازی سطح پایین سوکت (TCP_NODELAY, SO_KEEPALIVE) و حسابرسی بیدرنگ ترافیک.
"""

import asyncio
import base64
import concurrent.futures
import ipaddress
import logging
import socket
import struct
import time
import uuid
from typing import Tuple, Optional, List, Dict, Any
from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

import config
import database

logger = logging.getLogger("rvg.vless")
logger.setLevel(logging.INFO)

# استخر ترد اختصاصی برای کوئری‌های DNS جهت جلوگیری از مسدود شدن Event Loop اصلی
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=64,
    thread_name_prefix="vless_dns"
)
# کش موقت DNS با TTL پنج دقیقه جهت کاهش Latency و حذف کوئری‌های تکراری
_DNS_CACHE: Dict[str, Tuple[List[Any], float]] = {}
_DNS_CACHE_TTL = 300.0


def _is_ip_address(host: str) -> bool:
    """بررسی سریع این‌که آیا رشته ورودی یک آدرس آی‌پی (IPv4 یا IPv6) است یا نه"""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


async def _resolve_host(
    host: str,
    port: int,
    timeout: float = 2.5
) -> List[Tuple[int, int, int, str, Tuple]]:
    """
    حل نام دامنه با اولویت IPv4، کشینگ در حافظه، استخر ترد اختصاصی و تایم‌اوت مشخص
    برای جلوگیری از قفل شدن Event Loop و رفع قطعی خطای context deadline exceeded در سرورهای ابری.
    """
    clean_host = host.strip("[]")
    if _is_ip_address(clean_host):
        family = socket.AF_INET6 if ":" in clean_host else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (clean_host, port))]

    cache_key = f"{host}:{port}"
    now = time.time()
    cached = _DNS_CACHE.get(cache_key)
    if cached and (now - cached[1]) < _DNS_CACHE_TTL:
        return cached[0]

    loop = asyncio.get_running_loop()

    # ۱. تلاش سریع برای پیدا کردن آدرس IPv4 (کمترین Latency در سرورهای ابری مثل Railway)
    try:
        addrinfo = await asyncio.wait_for(
            loop.run_in_executor(
                _DNS_EXECUTOR,
                socket.getaddrinfo,
                host,
                port,
                socket.AF_INET,
                socket.SOCK_STREAM
            ),
            timeout=timeout
        )
        if addrinfo:
            _DNS_CACHE[cache_key] = (addrinfo, now)
            return addrinfo
    except Exception as e:
        logger.debug(f"IPv4 resolution for {host}:{port} skipped/failed: {e}")

    # ۲. در صورت نبود IPv4 یا خطا، امتحان کلیه خانواده‌ها (IPv4 / IPv6) با مرتب‌سازی به نفع IPv4
    try:
        addrinfo = await asyncio.wait_for(
            loop.run_in_executor(
                _DNS_EXECUTOR,
                socket.getaddrinfo,
                host,
                port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM
            ),
            timeout=timeout
        )
        if addrinfo:
            sorted_addrs = sorted(addrinfo, key=lambda item: 0 if item[0] == socket.AF_INET else 1)
            _DNS_CACHE[cache_key] = (sorted_addrs, now)
            return sorted_addrs
    except Exception as e:
        raise ConnectionError(f"DNS lookup failed for {host}:{port}: {e}")

    raise ConnectionError(f"No address found for {host}:{port}")


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
            logger.debug(f"VLESS version: {version}")

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


async def _connect_tcp_upstream(
    target_host: str,
    target_port: int,
    dns_timeout: float = 2.5,
    connect_timeout: float = 4.0
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """
    برقراری اتصال سریع و بهینه‌شده به سرور مقصد با اولویت آدرس‌های IPv4،
    استفاده از کش موقت DNS و جلوگیری قطعی از معلق ماندن اتصال (context deadline exceeded).
    """
    resolved_addrs = await _resolve_host(target_host, target_port, timeout=dns_timeout)

    loop = asyncio.get_running_loop()
    last_err: Optional[Exception] = None

    for family, socktype, proto, canonname, sockaddr in resolved_addrs:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            sock.setblocking(False)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, config.BUFFER_SIZE)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, config.BUFFER_SIZE)
            except Exception:
                pass

            await asyncio.wait_for(loop.sock_connect(sock, sockaddr), timeout=connect_timeout)
            reader, writer = await asyncio.open_connection(sock=sock, limit=config.BUFFER_SIZE)
            return reader, writer
        except Exception as err:
            last_err = err
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            continue

    raise ConnectionError(f"Could not connect to {target_host}:{target_port}: {last_err}")


async def handle_vless_websocket(websocket: WebSocket, db: Session):
    """
    هندلر اصلی اتصال WebSocket برای پروتکل VLESS
    - پشتیبانی از 0-RTT Early Data در هدر Sec-WebSocket-Protocol و پارامتر ed
    - اعتبارسنجی کاربر، سهمیه و وضعیت فعال بودن در دیتابیس
    - ارسال فوری تاییدیه هدر VLESS (b"\x00\x00") جهت جلوگیری از خطای context deadline exceeded در کلاینت
    - تفکیک هوشمند پکت‌های TCP (دستور 0x01) و UDP (دستور 0x02 برای DNS/QUIC)
    """
    # بررسی و دریافت احتمالی Early Data از هدرهای WebSocket (استاندارد Xray/v2rayNG برای کاهش تأخیر)
    sec_ws_proto = websocket.headers.get("sec-websocket-protocol", "")
    early_data: Optional[bytes] = None

    if sec_ws_proto:
        try:
            padded_proto = sec_ws_proto + "=" * ((4 - len(sec_ws_proto) % 4) % 4)
            early_data = base64.urlsafe_b64decode(padded_proto)
            await websocket.accept(subprotocol=sec_ws_proto)
        except Exception:
            await websocket.accept()
    else:
        await websocket.accept()

    try:
        if early_data and len(early_data) >= 24:
            first_chunk = early_data
        else:
            first_chunk = await asyncio.wait_for(websocket.receive_bytes(), timeout=8.0)

        if not first_chunk:
            await websocket.close(code=1003, reason="Empty initial packet")
            return

        # تجزیه مشخصات پکت اول VLESS
        try:
            client_uuid, command, target_host, target_port, initial_payload = VlessHeaderParser.parse(first_chunk)
        except Exception as parse_err:
            logger.warning(f"Failed to parse VLESS header: {parse_err}")
            await websocket.close(code=1008, reason="Malformed VLESS packet")
            return

        # اعتبارسنجی کاربر و کنترل حجم/تاریخ
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

        # ارسال بیدرنگ تاییدیه VLESS Response (Version 0x00 + Addons len 0x00)
        # این ارسال بلادرنگ باعث می‌شود کلاینت فوراً پاسخ هندشیک را دریافت کند و
        # به دلیل تأخیر در DNS یا TCP اتصال با خطای context deadline exceeded قطع نشود
        await websocket.send_bytes(b"\x00\x00")

        # هندلینگ دستور 0x02 (UDP): ضروری برای کوئری‌های DNS و جلوگیری از تایم‌اوت کلاینت
        if command == 0x02:
            await _handle_vless_udp(websocket, target_host, target_port, initial_payload, user.id, db)
            return

        # هندلینگ دستور 0x01 (TCP): ترافیک عادی وب و اپلیکیشن‌ها
        try:
            upstream_reader, upstream_writer = await _connect_tcp_upstream(
                target_host=target_host,
                target_port=target_port,
                dns_timeout=2.5,
                connect_timeout=4.0
            )
        except Exception as conn_err:
            logger.error(f"Failed to connect to upstream {target_host}:{target_port} -> {conn_err}")
            await websocket.close(code=1011, reason=f"Upstream unreachable: {target_host}")
            return

        # ارسال داده‌های اولیه در صورت وجود (مثلاً TLS Client Hello)
        if initial_payload:
            upstream_writer.write(initial_payload)
            await upstream_writer.drain()
            database.record_traffic(db, user.id, len(initial_payload))

        # آغاز رله دوطرفه همگام با دیتابیس
        ws_to_upstream_task = asyncio.create_task(
            _relay_ws_to_tcp(websocket, upstream_writer, user.id, db)
        )
        upstream_to_ws_task = asyncio.create_task(
            _relay_tcp_to_ws(upstream_reader, websocket, user.id, db)
        )

        done, pending = await asyncio.wait(
            [ws_to_upstream_task, upstream_to_ws_task],
            return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()

    except WebSocketDisconnect:
        logger.debug("Client WebSocket disconnected")
    except asyncio.TimeoutError:
        logger.warning("VLESS handshake timeout while waiting for initial data")
    except Exception as e:
        logger.error(f"Error in VLESS WebSocket lifecycle: {e}", exc_info=config.DEBUG)
    finally:
        if 'upstream_writer' in locals() and upstream_writer:
            try:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            except Exception:
                pass


async def _handle_vless_udp(
    websocket: WebSocket,
    target_host: str,
    target_port: int,
    initial_payload: bytes,
    user_id: int,
    db: Session
):
    """
    هندلر اختصاصی پکت‌های UDP برای پروتکل VLESS (به ویژه کوئری‌های DNS)
    قالب پکت‌های VLESS UDP: [2-byte big-endian length][UDP Datagram]
    """
    loop = asyncio.get_running_loop()
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.setblocking(False)

    if _is_ip_address(target_host):
        dest_addr = (target_host, target_port)
    else:
        try:
            resolved = await _resolve_host(target_host, target_port, timeout=2.0)
            dest_addr = resolved[0][4]
        except Exception:
            dest_addr = (target_host, target_port)

    try:
        # ارسال دیتای اولیه UDP در صورت وجود
        if initial_payload:
            offset = 0
            while offset + 2 <= len(initial_payload):
                pkt_len = struct.unpack("!H", initial_payload[offset:offset+2])[0]
                offset += 2
                if offset + pkt_len <= len(initial_payload):
                    pkt = initial_payload[offset:offset+pkt_len]
                    offset += pkt_len
                    try:
                        udp_sock.sendto(pkt, dest_addr)
                        database.record_traffic(db, user_id, len(pkt))
                    except Exception:
                        pass
                else:
                    break
            if offset == 0 and len(initial_payload) > 0:
                try:
                    udp_sock.sendto(initial_payload, dest_addr)
                    database.record_traffic(db, user_id, len(initial_payload))
                except Exception:
                    pass

        async def _ws_to_udp():
            try:
                while True:
                    data = await websocket.receive_bytes()
                    if not data:
                        break
                    offset = 0
                    while offset + 2 <= len(data):
                        pkt_len = struct.unpack("!H", data[offset:offset+2])[0]
                        offset += 2
                        if offset + pkt_len <= len(data):
                            pkt = data[offset:offset+pkt_len]
                            offset += pkt_len
                            udp_sock.sendto(pkt, dest_addr)
                            has_quota = database.record_traffic(db, user_id, len(pkt))
                            if not has_quota:
                                return
                        else:
                            break
                    if offset == 0 and len(data) > 0:
                        udp_sock.sendto(data, dest_addr)
                        has_quota = database.record_traffic(db, user_id, len(data))
                        if not has_quota:
                            return
            except (WebSocketDisconnect, asyncio.CancelledError):
                pass
            except Exception as e:
                logger.debug(f"VLESS UDP upload error: {e}")

        async def _udp_to_ws():
            try:
                while True:
                    data = await loop.sock_recv(udp_sock, 65535)
                    if not data:
                        break
                    # بسته‌بندی پکت به ساختار VLESS UDP با پیشوند طول ۲ بایتی
                    framed = struct.pack("!H", len(data)) + data
                    await websocket.send_bytes(framed)
                    has_quota = database.record_traffic(db, user_id, len(data))
                    if not has_quota:
                        return
            except (WebSocketDisconnect, asyncio.CancelledError):
                pass
            except Exception as e:
                logger.debug(f"VLESS UDP download error: {e}")

        ws_task = asyncio.create_task(_ws_to_udp())
        udp_task = asyncio.create_task(_udp_to_ws())

        done, pending = await asyncio.wait(
            [ws_task, udp_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
    finally:
        udp_sock.close()


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

            has_quota = database.record_traffic(db, user_id, chunk_len)
            if not has_quota:
                logger.warning(f"User {user_id} exceeded quota during download. Terminating connection.")
                break
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as e:
        logger.debug(f"TCP to WS relay stopped: {e}")
