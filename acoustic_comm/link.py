"""半双工轮询、握手、确认、重传与队列管理。"""

from __future__ import annotations

import hashlib
import math
import secrets
import time
from collections import deque
from dataclasses import replace

from .codec import PacketCodec
from .config import CONTROL_TYPES, STATE_TYPES
from .message import AcousticMessage, _uint


class ReliableAcousticLink:
    """TR发请求；BR回复时同时确认请求并携带自己的业务消息。

    所有公开方法由同一线程调用。ACK仅代表通信收讫，业务动作仍需传感器。
    clock可注入单调时钟供模拟使用，实机保持默认值。
    """

    def __init__(
        self,
        local_id,
        peer_id,
        audio,
        modem,
        initiator,
        max_attempts=3,
        poll_interval=0.25,
        clock=time.monotonic,
    ):
        _uint("local_id", local_id, 255)
        _uint("peer_id", peer_id, 255)
        if local_id == peer_id or type(initiator) is not bool:
            raise ValueError("两端编号须不同，initiator须为bool")
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts须为正整数")
        if not math.isfinite(poll_interval) or poll_interval < 0:
            raise ValueError("poll_interval须为非负有限数")
        if audio.sample_rate != modem.sample_rate or modem.max_packet < PacketCodec.MAX_PACKET:
            raise ValueError("采样率不一致或调制器允许的包长不足")
        self.local_id, self.peer_id = local_id, peer_id
        self.audio, self.modem, self.initiator = audio, modem, initiator
        self.codec, self.clock = PacketCodec(), clock
        self.max_attempts, self.poll_interval = max_attempts, poll_interval
        self.turnaround = audio.guard_seconds + 0.15
        self.session_id = 0
        self.connected = False
        self.generation = 0  # 每次重试/远端换会话都会变化，业务层可据此撤销旧动作许可。
        self._nonce = secrets.token_bytes(16)
        self._peer_nonce = None
        self._retired_nonces = set()
        self._retired_sessions = set()
        self._sequence = 0
        self._queue = []
        self._pending = None
        self._request = None
        self._response_to = None
        self._response_at = 0.0
        self._next_send = 0.0
        self._timer_after_tx = False
        self._deadline = None
        self._ack_peer = 0
        self._ack_dirty = False
        self._seen = set()
        self._latest_received = {}
        self._incoming = deque()
        self._states = {}
        self._events = deque()
        self.crc_or_packet_errors = 0
        self.audio_discontinuities = 0
        self.handshake_timeouts = 0

    def _next_sequence(self):
        if self._sequence >= 65535:
            raise RuntimeError("本进程序号耗尽，请在安全停机后重启程序")
        self._sequence += 1
        return self._sequence

    @staticmethod
    def _token(tr_nonce, br_nonce):
        return (
            int.from_bytes(hashlib.blake2s(tr_nonce + br_nonce, digest_size=4).digest(), "big") or 1
        )

    def _frame(self, kind, seq=0, task=0, payload=b"", ack=0, session=None):
        return AcousticMessage(
            self.local_id,
            self.peer_id,
            self.session_id if session is None else session,
            seq,
            kind,
            task,
            payload,
            ack,
        )

    def send(
        self, message_type, task_id=0, payload=b"", *, priority=1, ttl=10.0, latest_only=False
    ):
        """排队并返回序号。priority越大越优先；ttl为允许开始发送的最长等待。

        latest_only仅替换同类型、同task_id的旧状态。已发出的声波无法撤回。
        """
        if message_type in CONTROL_TYPES:
            raise ValueError("控制消息由链路自动生成")
        _uint("priority", priority, 3)
        if ttl is not None and (not math.isfinite(ttl) or ttl <= 0):
            raise ValueError("ttl须为正数或None")
        if type(latest_only) is not bool:
            raise ValueError("latest_only须为bool")
        # 先验证，再占用序号。
        probe = self._frame(message_type, 1, task_id, payload)
        self._prune(self.clock())
        if len(self._queue) >= 32 and not (
            latest_only
            and any(
                e["message"].message_type == message_type and e["message"].task_id == task_id
                for e in self._queue
            )
        ):
            raise BufferError("发送队列已满，最多32条；请合并状态更新")
        if latest_only:
            for entry in self._queue + ([self._pending] if self._pending else []):
                old = entry["message"]
                if old.message_type == message_type and old.task_id == task_id:
                    self._terminal(entry, "superseded")
        sequence = self._next_sequence()
        entry = dict(
            message=replace(probe, sequence=sequence),
            priority=priority,
            expires=None if ttl is None else self.clock() + ttl,
            attempts=0,
            ack_due=None,
        )
        self._queue.append(entry)
        self._states[sequence] = "queued"
        return sequence

    def status(self, sequence):
        return self._states.get(sequence)

    def receive(self):
        return self._incoming.popleft() if self._incoming else None

    def receive_event(self):
        """连接/重试事件：字典包含event、generation、session_id。"""
        return self._events.popleft() if self._events else None

    def _terminal(self, entry, state):
        sequence = entry["message"].sequence
        if self._states.get(sequence) in ("queued", "waiting_ack"):
            self._states[sequence] = state

    def cancel_task(self, task_id):
        """取消本机此任务的未完成发送，不撤销已送达的消息或对方动作。"""
        _uint("task_id", task_id, 65535)
        for entry in self._queue + ([self._pending] if self._pending else []):
            if entry["message"].task_id == task_id:
                self._terminal(entry, "cancelled")

    def _invalidate(self, reason, *, new_nonce=True):
        for entry in self._queue + ([self._pending] if self._pending else []):
            self._terminal(entry, "cancelled")
        self._queue.clear()
        self._pending = None
        self._incoming.clear()
        self._seen.clear()
        self._latest_received.clear()
        self._ack_peer = 0
        self._ack_dirty = False
        if self.session_id:
            self._retired_sessions.add(self.session_id)
        self.session_id = 0
        self.connected = False
        self.generation += 1
        self._request = None
        self._deadline = None
        self._timer_after_tx = False
        self._response_to = None
        self._next_send = self.clock() + self.turnaround
        self.modem.reset()
        if new_nonce:
            self._nonce = secrets.token_bytes(16)
        self._events.append(dict(event=reason, generation=self.generation, session_id=0))

    def reset_for_retry(self):
        """获准重试后调用；取消旧任务并重建会话，不自动恢复任何机械动作。"""
        self._invalidate("local_retry")

    def _set_connected(self):
        if not self.connected:
            self.connected = True
            self._events.append(
                dict(event="connected", generation=self.generation, session_id=self.session_id)
            )

    def _prune(self, now):
        for entry in self._queue + ([self._pending] if self._pending else []):
            if entry["expires"] is not None and now >= entry["expires"]:
                self._terminal(entry, "expired")
        active = lambda e: self._states[e["message"].sequence] in ("queued", "waiting_ack")
        self._queue = [e for e in self._queue if active(e)]
        if self._pending is not None and not active(self._pending):
            self._pending = None

    def _select(self, now):
        self._prune(now)
        if self._pending is not None:
            if self._pending["attempts"] >= self.max_attempts:
                self._terminal(self._pending, "failed")
                self._pending = None
            elif (
                self._queue and max(e["priority"] for e in self._queue) > self._pending["priority"]
            ):
                self._queue.append(self._pending)
                self._pending = None
        if self._pending is None and self._queue:
            selected = min(
                range(len(self._queue)),
                key=lambda i: (-self._queue[i]["priority"], self._queue[i]["message"].sequence),
            )
            self._pending = self._queue.pop(selected)
        return self._pending

    def _acknowledge(self, sequence):
        if self._pending is not None and self._pending["message"].sequence == sequence:
            self._terminal(self._pending, "acknowledged")
            self._pending = None

    def _deliver(self, message):
        if message.message_type in STATE_TYPES:
            key = (message.message_type, message.task_id)
            if message.sequence <= self._latest_received.get(key, 0):
                return
            self._latest_received[key] = message.sequence
        if message.sequence not in self._seen:
            self._seen.add(message.sequence)
            self._incoming.append(message)

    def _transmit(self, frame, entry, now, request=False):
        self.modem.reset()
        wave = self.modem.modulate(self.codec.encode(frame))
        self.audio.play(wave)
        if entry is not None:
            entry["attempts"] += 1
            entry["message"] = frame
            entry["ack_due"] = (
                now
                + self.modem.duration(len(self.codec.encode(frame)))
                + self.modem.duration(self.codec.MAX_PACKET)
                + self.poll_interval
                + 2 * self.turnaround
                + 0.5
            )
            self._states[frame.sequence] = "waiting_ack"
        if request:
            self._request = (frame, entry)
            self._deadline = None
            self._timer_after_tx = True
            self._ack_dirty = False

    def _handle_responder(self, message, now):
        kind = message.message_type
        if kind == "_HELLO":
            if message.session_id != 0 or len(message.payload) != 16 or message.sequence == 0:
                return
            if message.payload in self._retired_nonces:
                return
            if self._peer_nonce != message.payload:
                if self._peer_nonce is not None:
                    self._retired_nonces.add(self._peer_nonce)
                    self._invalidate("peer_retry")
                self._peer_nonce = message.payload
            if not self.session_id:
                self.session_id = self._token(self._peer_nonce, self._nonce)
            self._response_to = message
            self._response_at = now + self.turnaround
            return
        if kind in CONTROL_TYPES and kind != "_POLL":
            return
        if message.sequence == 0 or (kind == "_POLL" and (message.payload or message.task_id)):
            return
        if not self.session_id or message.session_id != self.session_id:
            if self.connected and message.session_id in self._retired_sessions:
                return
            self._response_to = message  # 用RESET请求重新握手，不执行旧业务。
            self._response_at = now + self.turnaround
            return
        self._set_connected()
        self._acknowledge(message.ack_sequence)
        if kind not in CONTROL_TYPES:
            self._deliver(message)
        self._response_to = message
        self._response_at = now + self.turnaround

    def _handle_initiator(self, message, now):
        if self._request is None:
            return
        request, entry = self._request
        if message.ack_sequence != request.sequence:
            return
        if request.message_type == "_HELLO":
            if (
                message.message_type != "_WELCOME"
                or len(message.payload) != 32
                or message.payload[:16] != self._nonce
                or message.sequence
                or message.task_id
            ):
                return
            peer_nonce = message.payload[16:]
            token = self._token(self._nonce, peer_nonce)
            if message.session_id != token or token in self._retired_sessions:
                return
            self._peer_nonce = peer_nonce
            self.session_id = token
            self._set_connected()
        else:
            if message.session_id != self.session_id:
                return
            if message.message_type == "_RESET":
                if len(message.payload) == 16:
                    self._invalidate("peer_retry")
                return
            if message.message_type in CONTROL_TYPES and message.message_type != "_IDLE":
                return
            if message.message_type == "_IDLE" and (
                message.payload or message.sequence or message.task_id
            ):
                return
            if entry is not None:
                self._terminal(entry, "acknowledged")
                if self._pending is entry:
                    self._pending = None
            if message.message_type not in CONTROL_TYPES:
                self._deliver(message)
                self._ack_peer = message.sequence
                self._ack_dirty = True
        self._request = None
        self._deadline = None
        self._timer_after_tx = False
        self._next_send = now + (self.turnaround if self._ack_dirty else self.poll_interval)

    def tick(self, now=None):
        """推进通信；需持续调用。now只供使用同一模拟时钟的测试。"""
        live = now is None
        now = self.clock() if live else now
        try:
            samples = self.audio.read_samples()
        except BufferError:
            self.audio_discontinuities += 1
            self.modem.reset()
            samples = []
        if len(samples):
            old_errors = self.modem.rejected_packets
            packets = self.modem.feed_samples(samples, validator=self.codec.decode)
            self.crc_or_packet_errors += self.modem.rejected_packets - old_errors
            now = self.clock() if live else now
            for packet in packets:
                message = self.codec.decode(packet)
                if message.sender_id != self.peer_id or message.receiver_id != self.local_id:
                    continue
                if self.initiator:
                    self._handle_initiator(message, now)
                else:
                    self._handle_responder(message, now)
        self._prune(now)
        if (
            not self.initiator
            and self._pending is not None
            and self._pending["attempts"] >= self.max_attempts
            and self._pending["ack_due"] is not None
            and now >= self._pending["ack_due"]
        ):
            self._terminal(self._pending, "failed")
            self._pending = None
        if self.audio.busy:
            return
        if not self.initiator:
            if self._response_to is None or now < self._response_at:
                return
            request, self._response_to = self._response_to, None
            if request.message_type == "_HELLO":
                frame = self._frame(
                    "_WELCOME", payload=request.payload + self._nonce, ack=request.sequence
                )
                entry = None
            elif not self.session_id or request.session_id != self.session_id:
                frame = self._frame(
                    "_RESET", payload=self._nonce, ack=request.sequence, session=request.session_id
                )
                entry = None
            else:
                entry = self._select(now)
                frame = (
                    replace(
                        entry["message"], session_id=self.session_id, ack_sequence=request.sequence
                    )
                    if entry
                    else self._frame("_IDLE", ack=request.sequence)
                )
            self._transmit(frame, entry, now)
            return
        if self._timer_after_tx:
            self._deadline = (
                now + self.modem.duration(self.codec.MAX_PACKET) * 1.02 + self.turnaround + 0.5
            )
            self._timer_after_tx = False
        if self._request is not None:
            if self._deadline is None or now < self._deadline:
                return
            frame, entry = self._request
            if frame.message_type == "_HELLO":
                self.handshake_timeouts += 1
            elif entry is not None and entry["attempts"] >= self.max_attempts:
                self._terminal(entry, "failed")
            self._request = None
            self._deadline = None
            self._next_send = now + self.turnaround
        if now < self._next_send:
            return
        if not self.connected:
            frame = self._frame("_HELLO", self._next_sequence(), payload=self._nonce, session=0)
            entry = None
        else:
            entry = self._select(now)
            frame = (
                replace(entry["message"], session_id=self.session_id, ack_sequence=self._ack_peer)
                if entry
                else self._frame("_POLL", self._next_sequence(), ack=self._ack_peer)
            )
        self._transmit(frame, entry, now, request=True)
