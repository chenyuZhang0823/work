"""V2 消息类型与协议保留类型。"""

from __future__ import annotations

MESSAGE_TYPES = {
    "_POLL": 0,
    "_IDLE": 1,
    "_HELLO": 2,
    "_WELCOME": 3,
    "_RESET": 4,
    "READY": 16,
    "NEED": 17,
    "INVENTORY": 18,
    "PLACED": 19,
    "RECEIVED": 20,
    "CAN_RECEIVE": 21,
    "GRIPPED": 22,
    "RELEASED": 23,
    "TASK_DONE": 24,
    "HOLD": 25,
    "GUARD_VALID": 26,
    "STONE_TAKEN": 27,
    "STATE": 28,
    "TEXT": 29,
}

CONTROL_TYPES = {name for name in MESSAGE_TYPES if name.startswith("_")}

STATE_TYPES = {"NEED", "INVENTORY", "STATE", "GUARD_VALID"}
