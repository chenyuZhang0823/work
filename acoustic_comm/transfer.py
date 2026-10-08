"""由传感器驱动的交接状态检查，不控制机构。"""

from __future__ import annotations

import math

from .message import _uint


class TransferCoordinator:
    """可选的直接交接状态检查器。只输出许可，不直接操作任何机构。

    两端begin同一任务；本机必须调用arrived/confirm_gripped/confirm_released等
    传感器回调。重试后必须重新begin，不能靠历史通信恢复释放许可。
    """

    def __init__(self, link, stage_timeout=10.0, release_valid_for=1.0):
        if any(not math.isfinite(v) or v <= 0 for v in (stage_timeout, release_valid_for)):
            raise ValueError("阶段时限和释放许可有效期须为正数")
        self.link = link
        self.stage_timeout, self.release_valid_for = stage_timeout, release_valid_for
        self._deadline = None
        self._used_tasks = set()
        self.task_id = None
        self.generation = link.generation
        self.state = "idle"
        self.at_transfer_zone = False
        self.peer_ready = False
        self._grip_report = None
        self._release_report = None

    def begin(self, task_id, item_type, count, *, local_load):
        _uint("task_id", task_id, 65535)
        if type(item_type) is not int or item_type not in (0, 1, 2) or type(count) is not int:
            raise ValueError("物料0=塔基/中段，1=塔顶，2=五色石；数量须为整数")
        # 这是直接交接批次，受BR最多两件约束；不是TR的整趟运载量。
        if not 1 <= count <= 2 or (item_type == 2 and count != 1):
            raise ValueError("直接交接每批1至2件，五色石每批1件")
        _uint("local_load", local_load, 3 if self.link.initiator else 2)
        if self.link.initiator and local_load < count:
            raise ValueError("TR当前载荷不足以提供这一批物料")
        if not self.link.initiator and local_load + count > 2:
            raise ValueError("接收后BR载荷将超过两件，请先放下已有物料")
        if not self.link.connected:
            raise RuntimeError("先完成连接并重新核对库存，再开始交接")
        if (self.link.generation, task_id) in self._used_tasks:
            raise ValueError("同一会话不能重复使用交接任务号，请分配新任务号")
        self._used_tasks.add((self.link.generation, task_id))
        if self.task_id is not None:
            self.link.cancel_task(self.task_id)
        self.task_id = task_id
        self.items = bytes((item_type, count))
        self.generation = self.link.generation
        self.state = "approaching"
        self.at_transfer_zone = self.peer_ready = False
        self._grip_report = self._release_report = None
        self._deadline = self.link.clock() + self.stage_timeout

    def update(self, message=None):
        if self.generation != self.link.generation:
            self.state = "resync_required"
            self.at_transfer_zone = self.peer_ready = False
            return
        if (
            self.state not in ("idle", "held", "resync_required", "complete")
            and self._deadline is not None
            and self.link.clock() >= self._deadline
        ):
            self.hold()
            return
        report = (
            self._grip_report
            if self.state == "gripped"
            else self._release_report
            if self.state == "released"
            else None
        )
        if report is not None and self.link.status(report) in ("failed", "expired", "cancelled"):
            self.hold()
            return
        if message is None or self.task_id is None:
            return
        if (
            message.session_id != self.link.session_id
            or message.task_id != self.task_id
            or message.sender_id != self.link.peer_id
            or message.receiver_id != self.link.local_id
            or message.payload != self.items
        ):
            return
        if message.message_type == "HOLD":
            self.state = "held"
            self.peer_ready = False
        elif self.link.initiator:
            if message.message_type == "CAN_RECEIVE" and self.state in ("ready", "grip_allowed"):
                self.peer_ready = True
            elif (
                message.message_type == "GRIPPED"
                and self.state == "ready"
                and self.at_transfer_zone
            ):
                self.state = "release_allowed"
                self._deadline = self.link.clock() + self.release_valid_for
            elif message.message_type == "RECEIVED" and self.state == "released":
                self.state = "complete"
        else:
            if message.message_type == "READY" and self.state in ("approaching", "ready"):
                self.peer_ready = True
            elif message.message_type == "RELEASED" and self.state == "gripped":
                self.state = "verify_received"
                self._deadline = self.link.clock() + self.stage_timeout

    def arrived(self):
        """调用前由本机定位确认：机器人和交接物料满足传递区边界要求。"""
        self.update()
        if self.state != "approaching":
            raise RuntimeError("当前阶段不允许报告到位")
        self.at_transfer_zone = True
        self.state = "ready"
        self._deadline = self.link.clock() + self.stage_timeout
        return self.link.send(
            "READY" if self.link.initiator else "CAN_RECEIVE",
            self.task_id,
            self.items,
            priority=2,
            ttl=5,
            latest_only=True,
        )

    @property
    def can_grip(self):
        self.update()
        return (
            not self.link.initiator
            and self.state == "ready"
            and self.peer_ready
            and self.at_transfer_zone
        )

    def confirm_gripped(self):
        """只在BR传感器确认夹稳后调用。"""
        self.update()
        if self.link.initiator or self.state != "gripping":
            raise RuntimeError("尚未进入夹取阶段")
        self.state = "gripped"
        self._deadline = self.link.clock() + self.stage_timeout
        self._grip_report = self.link.send("GRIPPED", self.task_id, self.items, priority=3, ttl=5)

    def begin_grip(self):
        if not self.can_grip:
            raise RuntimeError("尚未满足夹取前提")
        self.state = "gripping"
        self._deadline = self.link.clock() + self.stage_timeout

    @property
    def can_release(self):
        self.update()
        return self.link.initiator and self.state == "release_allowed" and self.at_transfer_zone

    def confirm_released(self):
        """TR本机再次确认释放条件并完成释放后调用。"""
        self.update()
        if not self.link.initiator or self.state != "releasing":
            raise RuntimeError("尚未进入释放阶段")
        self.state = "released"
        self._deadline = self.link.clock() + self.stage_timeout
        self._release_report = self.link.send(
            "RELEASED", self.task_id, self.items, priority=3, ttl=5
        )

    def begin_release(self):
        if not self.can_release:
            raise RuntimeError("尚未获得当前任务的释放许可")
        self.state = "releasing"
        self._deadline = self.link.clock() + self.stage_timeout

    def confirm_received(self):
        """BR确认物料仍被可靠持有、TR已释放后调用。"""
        self.update()
        if self.link.initiator or self.state != "verify_received":
            raise RuntimeError("尚未满足接收完成条件")
        self.link.send("RECEIVED", self.task_id, self.items, priority=3, ttl=5)
        self.state = "complete"

    def hold(self):
        """本机立即取消动作许可；远端通知仍需等待下一合法声学时隙。"""
        self.state = "held"
        self.at_transfer_zone = self.peer_ready = False
        if self.task_id is not None:
            self.link.cancel_task(self.task_id)
            try:
                return self.link.send("HOLD", self.task_id, self.items, priority=3, ttl=5)
            except BufferError:
                return None  # 本地已停止许可；远端停止不能依赖声波通知。
