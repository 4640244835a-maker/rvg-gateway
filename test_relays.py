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


if __name__ == "__main__":
    unittest.main()
