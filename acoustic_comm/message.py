"""消息数据与字段校验。"""

from __future__ import annotations

from dataclasses import dataclass

from .config import CONTROL_TYPES, MESSAGE_TYPES


def _uint(name, value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name}须为0至{maximum}的整数")


@dataclass(frozen=True, slots=True)
class AcousticMessage:
    sender_id: int
    receiver_id: int
    session_id: int
    sequence: int
    message_type: str
    task_id: int
    payload: bytes = b""
    ack_sequence: int = 0

    def __post_init__(self):
        for name, limit in (
            ("sender_id", 255),
            ("receiver_id", 255),
            ("session_id", 0xFFFFFFFF),
            ("sequence", 65535),
            ("ack_sequence", 65535),
            ("task_id", 65535),
        ):
            _uint(name, getattr(self, name), limit)
        if not isinstance(self.message_type, str) or self.message_type not in MESSAGE_TYPES:
            raise ValueError("未知消息类型，请使用MESSAGE_TYPES中的名称")
        if self.message_type not in CONTROL_TYPES and self.sequence == 0:
            raise ValueError("业务消息序号不能为0")
        if not isinstance(self.payload, bytes):
            raise TypeError("payload须为bytes，文本先encode('utf-8')")
        if len(self.payload) > 32:
            raise ValueError("payload最多32字节，比赛中建议使用1至4字节数值状态")
