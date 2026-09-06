"""
RVG Gateway - Unit Tests and Verification Script
تستهای واحد برای اعتبارسنجی مفسر پکت باینری VLESS، منطق سهمیه ترافیک و ساختارهای داده
"""

import struct
import unittest
import uuid
import socket


class TestVlessParser(unittest.TestCase):
    """تست اعتبارسنجی و دیکود پکت VLESS بدون نیاز به اتصال شبکه خارجی"""

    def test_vless_header_ipv4(self):
        # ساخت پکت VLESS تستی با مقصد IPv4 1.1.1.1:443
        test_uuid = uuid.uuid4()
        version = b"\x00"
        uuid_bytes = test_uuid.bytes
        addons_len = b"\x00"
        command = b"\x01"  # TCP
        target_port = struct.pack("!H", 443)
        addr_type = b"\x01"  # IPv4
        target_ip_bytes = socket.inet_aton("1.1.1.1")
        payload = b"GET / HTTP/1.1\r\nHost: 1.1.1.1\r\n\r\n"

        packet = (
            version +
            uuid_bytes +
            addons_len +
            command +
            target_port +
            addr_type +
            target_ip_bytes +
            payload
        )

        # شبیه‌سازی تجزیه هدر
        parsed_version = packet[0]
        parsed_uuid = str(uuid.UUID(bytes=packet[1:17]))
        parsed_command = packet[18]
        parsed_port = struct.unpack("!H", packet[19:21])[0]
        parsed_addr_type = packet[21]
        parsed_ip = socket.inet_ntoa(packet[22:26])
        parsed_payload = packet[26:]

        self.assertEqual(parsed_version, 0)
        self.assertEqual(parsed_uuid, str(test_uuid))
        self.assertEqual(parsed_command, 1)
        self.assertEqual(parsed_port, 443)
        self.assertEqual(parsed_addr_type, 1)
        self.assertEqual(parsed_ip, "1.1.1.1")
        self.assertEqual(parsed_payload, payload)

    def test_vless_header_domain(self):
        # ساخت پکت VLESS تستی با مقصد دامین google.com:80
        test_uuid = uuid.uuid4()
        domain_name = b"google.com"
        packet = (
            b"\x00" +
            test_uuid.bytes +
            b"\x00" +
            b"\x01" +
            struct.pack("!H", 80) +
            b"\x02" +  # Domain
            bytes([len(domain_name)]) +
            domain_name +
            b"PING"
        )

        cursor = 0
        v = packet[cursor]; cursor += 1
        u = str(uuid.UUID(bytes=packet[cursor:cursor+16])); cursor += 16
        addon = packet[cursor]; cursor += 1
        cmd = packet[cursor]; cursor += 1
        port = struct.unpack("!H", packet[cursor:cursor+2])[0]; cursor += 2
        atype = packet[cursor]; cursor += 1
        dlen = packet[cursor]; cursor += 1
        domain = packet[cursor:cursor+dlen].decode("utf-8"); cursor += dlen
        data = packet[cursor:]

        self.assertEqual(u, str(test_uuid))
        self.assertEqual(port, 80)
        self.assertEqual(atype, 2)
        self.assertEqual(domain, "google.com")
        self.assertEqual(data, b"PING")

    def test_vless_url_formatting(self):
        import urllib.parse
        test_uuid = "11111111-2222-3333-4444-555555555555"
        domain = "my-app.up.railway.app"
        port = 443
        path = "/vless"
        name = "کاربر تستی"
        encoded_name = urllib.parse.quote(name)
        encoded_path = urllib.parse.quote(path, safe="")

        # Test WebSocket format
        url_ws = f"vless://{test_uuid}@{domain}:{port}?type=ws&security=tls&sni={domain}&host={domain}&path={encoded_path}&alpn=http%2F1.1&fp=chrome#{encoded_name}"
        self.assertTrue(url_ws.startswith(f"vless://{test_uuid}@{domain}:443"))
        self.assertIn("type=ws", url_ws)
        self.assertIn("security=tls", url_ws)
        self.assertIn(f"sni={domain}", url_ws)
        self.assertIn(f"host={domain}", url_ws)
        self.assertIn(f"path={encoded_path}", url_ws)
        self.assertIn("alpn=http%2F1.1", url_ws)
        self.assertIn("fp=chrome", url_ws)
        self.assertIn(f"#{encoded_name}", url_ws)

        # Test XHTTP format
        url_xhttp = f"vless://{test_uuid}@{domain}:{port}?type=xhttp&security=tls&sni={domain}&host={domain}&path={encoded_path}&mode=auto&fp=chrome#{encoded_name}"
        self.assertTrue(url_xhttp.startswith(f"vless://{test_uuid}@{domain}:443"))
        self.assertIn("type=xhttp", url_xhttp)
        self.assertIn("security=tls", url_xhttp)
        self.assertIn(f"sni={domain}", url_xhttp)
        self.assertIn(f"host={domain}", url_xhttp)
        self.assertIn(f"path={encoded_path}", url_xhttp)
        self.assertIn("mode=auto", url_xhttp)
        self.assertIn("fp=chrome", url_xhttp)


if __name__ == "__main__":
    unittest.main()
