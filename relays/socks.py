"""
RVG Gateway - SOCKS5 Relay Engine
سرور مستقل SOCKS5 مبتنی بر Asyncio جهت هدایت ترافیک پروکسی عمومی با پشتیبانی از
احراز هویت بر اساس UUID، بافرهای 512KB، TCP_NODELAY و قطع خودکار بهمحض اتمام سهمیه.
"""

import asyncio
import logging
import socket
import struct
from typing import Optional, Tuple
from sqlalchemy.orm import Session

import config
import database

logger = logging.getLogger("rvg.socks5")
logger.setLevel(logging.INFO)


class Socks5Server:
    """
    سرور داخلی SOCKS5 بر پایه پروتکل RFC 1928 و RFC 1929
    """

    def __init__(self, host: str = config.SOCKS5_HOST, port: int = config.SOCKS5_PORT):
        self.host = host
        self.port = port
        self.server: Optional[asyncio.AbstractServer] = None

    async def start(self):
        """راه‌اندازی سرور سوکت SOCKS5 در پس‌زمینه"""
        try:
            self.server = await asyncio.start_server(
                self.handle_client,
                self.host,
                self.port,
                limit=config.BUFFER_SIZE
            )
            addrs = ", ".join(str(sock.getsockname()) for sock in self.server.sockets)
            logger.info(f"SOCKS5 Relay Engine running on {addrs}")
        except Exception as e:
            logger.error(f"Could not start SOCKS5 server on port {self.port}: {e}")

    async def stop(self):
        """توقف سرور و بستن سوکت‌ها"""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("SOCKS5 Relay Engine stopped.")

    async def handle_client(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
        """مدیریت فرایند احراز هویت و برقراری ارتباط کلاینت SOCKS5"""
        upstream_writer: Optional[asyncio.StreamWriter] = None
        user_id: Optional[int] = None

        # اعمال بهینه‌سازی روی سوکت کلاینت
        client_sock = client_writer.get_extra_info("socket")
        if client_sock:
            try:
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, config.BUFFER_SIZE)
                client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, config.BUFFER_SIZE)
            except Exception:
                pass

        try:
            # 1. مرحله مذاکره احراز هویت (Handshake Negotiation)
            header = await client_reader.read(2)
            if len(header) < 2 or header[0] != 0x05:
                # پروتکل معتبر SOCKS5 نیست
                client_writer.close()
                return

            nmethods = header[1]
            methods = await client_reader.read(nmethods)
            if len(methods) < nmethods:
                client_writer.close()
                return

            # بررسی روش‌های پشتیبانی شده (0x00 = بدون رمز، 0x02 = نام کاربری/رمز با UUID)
            use_auth = 0x02 in methods
            if use_auth:
                client_writer.write(bytes([0x05, 0x02]))  # درخواست Username/Password
                await client_writer.drain()

                # دریافت اطلاعات کاربری (RFC 1929)
                auth_ver = await client_reader.read(1)
                if not auth_ver or auth_ver[0] != 0x01:
                    client_writer.write(bytes([0x01, 0x01]))
                    client_writer.close()
                    return

                ulen_raw = await client_reader.read(1)
                if not ulen_raw:
                    client_writer.close()
                    return
                ulen = ulen_raw[0]
                user_uuid = (await client_reader.read(ulen)).decode("utf-8", errors="ignore")

                plen_raw = await client_reader.read(1)
                if not plen_raw:
                    client_writer.close()
                    return
                plen = plen_raw[0]
                await client_reader.read(plen)  # پسورد (اختیاری)

                # اعتبارسنجی UUID کاربر از دیتابیس
                db = database.SessionLocal()
                try:
                    user = database.get_link_by_uuid(db, user_uuid)
                    if not user or not user.is_active or (user.quota_bytes > 0 and user.used_bytes >= user.quota_bytes):
                        client_writer.write(bytes([0x01, 0x01]))  # Auth failed
                        await client_writer.drain()
                        client_writer.close()
                        return
                    user_id = user.id
                finally:
                    db.close()

                client_writer.write(bytes([0x01, 0x00]))  # Auth success
                await client_writer.drain()

            elif 0x00 in methods:
                # اتصال عمومی بدون رمز عبور
                client_writer.write(bytes([0x05, 0x00]))
                await client_writer.drain()
            else:
                # روش غیرقابل پشتیبانی
                client_writer.write(bytes([0x05, 0xFF]))
                await client_writer.drain()
                client_writer.close()
                return

            # 2. دریافت درخواست اتصال (Connection Request)
            req_header = await client_reader.read(4)
            if len(req_header) < 4:
                client_writer.close()
                return

            ver, cmd, rsv, atyp = req_header
            if cmd != 0x01:  # فقط دستور CONNECT پشتیبانی می‌شود
                # 0x07 = Command not supported
                client_writer.write(bytes([0x05, 0x07, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
                await client_writer.drain()
                client_writer.close()
                return

            target_host = ""
            if atyp == 0x01:  # IPv4
                addr_bytes = await client_reader.read(4)
                target_host = socket.inet_ntoa(addr_bytes)
            elif atyp == 0x03:  # Domain Name
                dlen_raw = await client_reader.read(1)
                if not dlen_raw:
                    client_writer.close()
                    return
                domain_len = dlen_raw[0]
                domain_bytes = await client_reader.read(domain_len)
                target_host = domain_bytes.decode("utf-8", errors="ignore")
            elif atyp == 0x04:  # IPv6
                addr_bytes = await client_reader.read(16)
                target_host = socket.inet_ntop(socket.AF_INET6, addr_bytes)
            else:
                # 0x08 = Address type not supported
                client_writer.write(bytes([0x05, 0x08, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
                await client_writer.drain()
                client_writer.close()
                return

            port_bytes = await client_reader.read(2)
            if len(port_bytes) < 2:
                client_writer.close()
                return
            target_port = struct.unpack("!H", port_bytes)[0]

            logger.info(f"SOCKS5 request -> Target: {target_host}:{target_port}")

            # 3. برقراری اتصال به مقصد Upstream
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
                logger.warning(f"SOCKS5 failed to connect to {target_host}:{target_port} -> {conn_err}")
                # 0x05 = Connection refused
                client_writer.write(bytes([0x05, 0x05, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
                await client_writer.drain()
                client_writer.close()
                return

            # بهینه‌سازی سوکت سرور مقصد
            up_sock = upstream_writer.get_extra_info("socket")
            if up_sock:
                try:
                    up_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    up_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    up_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, config.BUFFER_SIZE)
                    up_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, config.BUFFER_SIZE)
                except Exception:
                    pass

            # ارسال پاسخ موفقیت اتصال به کلاینت (0x00 = Success)
            # BND.ADDR و BND.PORT به صورت 0 بازگردانده می‌شوند
            client_writer.write(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
            await client_writer.drain()

            # 4. رله دوطرفه دیتا با محاسبه ترافیک
            db = database.SessionLocal()
            try:
                t1 = asyncio.create_task(self._relay_stream(client_reader, upstream_writer, user_id, db))
                t2 = asyncio.create_task(self._relay_stream(upstream_reader, client_writer, user_id, db))
                done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()
            finally:
                db.close()

        except Exception as e:
            logger.debug(f"SOCKS5 client error: {e}")
        finally:
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except Exception:
                pass
            if upstream_writer:
                try:
                    upstream_writer.close()
                    await upstream_writer.wait_closed()
                except Exception:
                    pass

    async def _relay_stream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        user_id: Optional[int],
        db: Session
    ):
        """انتقال بایت‌ها بین دو استریم همراه با محاسبه مصرف و توقف در صورت اتمام سهمیه"""
        try:
            while True:
                data = await reader.read(config.BUFFER_SIZE)
                if not data:
                    break
                writer.write(data)
                await writer.drain()

                if user_id:
                    has_quota = database.record_traffic(db, user_id, len(data))
                    if not has_quota:
                        logger.warning(f"SOCKS5: User {user_id} exceeded quota. Terminating socket.")
                        break
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as err:
            logger.debug(f"Stream relay ended: {err}")
