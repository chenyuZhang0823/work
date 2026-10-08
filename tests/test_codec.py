"""PacketTests：从原 V2 自测迁移的回归用例。"""

from __future__ import annotations

import struct
import unittest
import zlib
from dataclasses import replace

from acoustic_comm.codec import PacketCodec

from .helpers import MESSAGE


class PacketTests(unittest.TestCase):
    def test_compact_roundtrip(self):
        codec = PacketCodec()
        self.assertEqual(codec.HEADER.size, 14)
        for payload in (b"", b"\x00\x02", bytes(range(32)), "塔顶".encode()):
            message = replace(MESSAGE, payload=payload, ack_sequence=28)
            packet = codec.encode(message)
            self.assertEqual(len(packet), 18 + len(payload))
            self.assertEqual(codec.decode(packet), message)

    def test_corrupt_and_truncated(self):
        codec = PacketCodec()
        packet = codec.encode(MESSAGE)
        for i in range(len(packet)):
            damaged = bytearray(packet)
            damaged[i] ^= 1
            with self.assertRaises(ValueError):
                codec.decode(bytes(damaged))
        for n in range(len(packet)):
            with self.assertRaises(ValueError):
                codec.decode(packet[:n])

    def test_bad_parameters(self):
        for changes in (
            {"sender_id": 256},
            {"sequence": 65536},
            {"sequence": 0},
            {"session_id": "abc"},
            {"task_id": True},
            {"payload": b"x" * 33},
            {"message_type": "UNKNOWN"},
        ):
            with self.assertRaises(ValueError):
                replace(MESSAGE, **changes)

    def test_old_version_rejected(self):
        packet = bytearray(PacketCodec().encode(MESSAGE))
        packet[0] = 0xA1
        packet[-4:] = struct.pack("!I", zlib.crc32(packet[:-4]))
        with self.assertRaises(ValueError):
            PacketCodec().decode(bytes(packet))
