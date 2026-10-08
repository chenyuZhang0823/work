"""二进制报文编码、版本检查和 CRC32。"""

from __future__ import annotations

import struct
import zlib

from .config import MESSAGE_TYPES
from .message import AcousticMessage


class PacketCodec:
    """V2：14字节固定头+载荷+CRC32；不兼容V1，不提供密码学认证。"""

    HEADER = struct.Struct("!BBBIHHHB")
    MAX_PAYLOAD = 32
    MAX_PACKET = HEADER.size + MAX_PAYLOAD + 4
    _NAMES = {value: key for key, value in MESSAGE_TYPES.items()}

    def encode(self, message):
        if not isinstance(message, AcousticMessage):
            raise TypeError("需要AcousticMessage")
        body = (
            self.HEADER.pack(
                0xA2,
                message.sender_id,
                message.receiver_id,
                message.session_id,
                message.sequence,
                message.ack_sequence,
                message.task_id,
                MESSAGE_TYPES[message.message_type],
            )
            + message.payload
        )
        return body + struct.pack("!I", zlib.crc32(body))

    def decode(self, packet):
        if not isinstance(packet, bytes):
            raise TypeError("需要bytes")
        if not self.HEADER.size + 4 <= len(packet) <= self.MAX_PACKET:
            raise ValueError("包长错误")
        body = packet[:-4]
        if zlib.crc32(body) != struct.unpack("!I", packet[-4:])[0]:
            raise ValueError("CRC错误")
        version, sender, receiver, session, seq, ack, task, kind = self.HEADER.unpack_from(body)
        if version != 0xA2 or kind not in self._NAMES:
            raise ValueError("版本或消息类型错误")
        return AcousticMessage(
            sender, receiver, session, seq, self._NAMES[kind], task, body[self.HEADER.size :], ack
        )
