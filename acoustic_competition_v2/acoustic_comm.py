"""ROBOCON 声波通信 V2。五个通信类及交接状态检查器，见随附README。"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, replace
import hashlib
import math
import secrets
import struct
import sys
import time
import zlib

import numpy as np
from scipy.signal import correlate
from scipy.signal.windows import tukey


MESSAGE_TYPES = {
    "_POLL": 0, "_IDLE": 1, "_HELLO": 2, "_WELCOME": 3, "_RESET": 4,
    "READY": 16, "NEED": 17, "INVENTORY": 18, "PLACED": 19,
    "RECEIVED": 20, "CAN_RECEIVE": 21, "GRIPPED": 22, "RELEASED": 23,
    "TASK_DONE": 24, "HOLD": 25, "GUARD_VALID": 26, "STONE_TAKEN": 27,
    "STATE": 28, "TEXT": 29,
}
CONTROL_TYPES = {name for name in MESSAGE_TYPES if name.startswith("_")}
STATE_TYPES = {"NEED", "INVENTORY", "STATE", "GUARD_VALID"}


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
        for name, limit in (("sender_id", 255), ("receiver_id", 255),
                            ("session_id", 0xFFFFFFFF), ("sequence", 65535),
                            ("ack_sequence", 65535), ("task_id", 65535)):
            _uint(name, getattr(self, name), limit)
        if not isinstance(self.message_type, str) or self.message_type not in MESSAGE_TYPES:
            raise ValueError("未知消息类型，请使用MESSAGE_TYPES中的名称")
        if self.message_type not in CONTROL_TYPES and self.sequence == 0:
            raise ValueError("业务消息序号不能为0")
        if not isinstance(self.payload, bytes):
            raise TypeError("payload须为bytes，文本先encode('utf-8')")
        if len(self.payload) > 32:
            raise ValueError("payload最多32字节，比赛中建议使用1至4字节数值状态")


class PacketCodec:
    """V2：14字节固定头+载荷+CRC32；不兼容V1，不提供密码学认证。"""
    HEADER = struct.Struct("!BBBIHHHB")
    MAX_PAYLOAD = 32
    MAX_PACKET = HEADER.size + MAX_PAYLOAD + 4
    _NAMES = {value: key for key, value in MESSAGE_TYPES.items()}

    def encode(self, message):
        if not isinstance(message, AcousticMessage):
            raise TypeError("需要AcousticMessage")
        body = self.HEADER.pack(
            0xA2, message.sender_id, message.receiver_id, message.session_id,
            message.sequence, message.ack_sequence, message.task_id,
            MESSAGE_TYPES[message.message_type],
        ) + message.payload
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
        return AcousticMessage(sender, receiver, session, seq, self._NAMES[kind], task,
                               body[self.HEADER.size:], ack)


class AudioIO:
    """单个双工音频流；协议半双工，播放及尾音期间不提交录音。"""

    def __init__(self, sample_rate=48000, input_device=None, output_device=None,
                 guard_seconds=0.10):
        if type(sample_rate) is not int or sample_rate <= 0:
            raise ValueError("sample_rate 必须是正整数")
        if not math.isfinite(guard_seconds) or guard_seconds < 0:
            raise ValueError("guard_seconds 必须是有限的非负数")
        self.sample_rate = sample_rate
        self.input_device = input_device
        self.output_device = output_device
        self.guard_seconds = guard_seconds
        self._stream = None
        self._rx = deque(maxlen=256)
        self._tx = None
        self._position = 0
        self._listen_after = 0.0
        self._discontinuity = False

    def start(self):
        import sounddevice as sd

        if self._stream is not None:
            if not self._stream.active:
                raise RuntimeError("音频流已停止，请先close()再start()")
            return
        self._stream = sd.Stream(
            samplerate=self.sample_rate, blocksize=1024, channels=(1, 1),
            dtype="float32", device=(self.input_device, self.output_device),
            callback=self._callback,
        )
        try:
            self._stream.start()
        except Exception:
            self.close()
            raise

    def _callback(self, indata, outdata, frames, timing, status):
        # 回调中只搬运音频，不做解调、打印或等待。
        outdata.fill(0)
        if status:
            self._discontinuity = True
        tx = self._tx
        if tx is not None:
            count = min(frames, len(tx) - self._position)
            outdata[:count, 0] = tx[self._position:self._position + count]
            self._position += count
            self._listen_after = (
                timing.outputBufferDacTime + count / self.sample_rate + self.guard_seconds
            )
            if self._position == len(tx):
                self._tx = None
        elif timing.inputBufferAdcTime >= self._listen_after:
            if len(self._rx) == self._rx.maxlen:
                self._discontinuity = True
            self._rx.append(indata[:, 0].copy())

    @property
    def busy(self):
        return self._tx is not None or (
            self._stream is not None and self._stream.time < self._listen_after
        )

    def play(self, samples):
        """提交一次播放并立即返回；播放完成前不可提交下一段。"""
        if self._stream is None or not self._stream.active:
            raise RuntimeError("音频流未启动或已停止")
        if self.busy:
            raise RuntimeError("当前正在播放或等待尾音消退")
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim != 1 or not len(samples) or not np.all(np.isfinite(samples)):
            raise ValueError("需要非空、有限值的单声道音频")
        if np.max(np.abs(samples)) > 1:
            raise ValueError("音频幅度必须在-1至1之间")
        self._rx.clear()
        self._position = 0
        self._tx = samples.copy()

    def read_samples(self):
        """返回累计录音；采集不连续时抛出 BufferError，解调器需重置。"""
        if self._stream is None or not self._stream.active:
            raise RuntimeError("音频流未启动或已停止")
        if self._discontinuity:
            self._discontinuity = False
            self._rx.clear()
            raise BufferError("音频溢出或欠载，请检查主循环频率和设备负载")
        blocks = []
        # 只读取此刻已有的块，避免持续采集令本次调用一直追赶新数据。
        for _ in range(len(self._rx)):
            blocks.append(self._rx.popleft())
        return np.concatenate(blocks) if blocks else np.empty(0, dtype=np.float32)

    def close(self):
        if self._stream is not None:
            stream, self._stream = self._stream, None
            stream.close()
        self._tx = None
        self._rx.clear()
        self._listen_after = 0.0
        self._discontinuity = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

class ChirpModem:
    """二进制上下扫频原型；逐包估计恒定时间伸缩，不跟踪包内加速度。"""

    def __init__(self, sample_rate=48000, low_freq=4000, high_freq=10000,
                 symbol_seconds=0.004, gap_seconds=0.001, max_packet=160):
        if type(sample_rate) is not int or sample_rate <= 0:
            raise ValueError("sample_rate 必须是正整数")
        if not all(math.isfinite(v) for v in (low_freq, high_freq, symbol_seconds, gap_seconds)):
            raise ValueError("频率和时长必须是有限数值")
        if type(max_packet) is not int:
            raise ValueError("max_packet 必须是整数")
        if not 0 < low_freq < high_freq < sample_rate / 2 / 1.03:
            raise ValueError("频段必须低于奈奎斯特频率，并为多普勒留出余量")
        if symbol_seconds <= 0 or gap_seconds < 0 or not 1 <= max_packet <= 65535:
            raise ValueError("符号时长、间隔或包长不合法")
        self.sample_rate = sample_rate
        self.low_freq, self.high_freq = low_freq, high_freq
        self.symbol_size = round(sample_rate * symbol_seconds)
        self.gap_size = round(sample_rate * gap_seconds)
        if self.symbol_size < 16:
            raise ValueError("每符号至少需要16个采样点")
        self.stride = self.symbol_size + self.gap_size
        self.max_packet = max_packet
        self._preamble_part = round(sample_rate * 0.024)
        self._symbols = np.array([self._chirp(False, self.symbol_size),
                                  self._chirp(True, self.symbol_size)])
        self._symbol_filters = self._symbols.conj().T.copy()
        self._symbol_window = tukey(self.symbol_size, 0.25)
        self._preamble_window = tukey(self._preamble_part, 0.25)
        self._preamble = self._make_preamble(1.0)
        self._scales = np.linspace(0.98, 1.02, 21)
        self._bank = [(s, self._make_preamble(s)) for s in self._scales]
        self._keep = max(len(t) for _, t in self._bank) + 4
        self.last_scale = None
        self.rejected_packets = 0
        self.reset()

    def _chirp(self, down, count, scale=1.0):
        n = round(count * scale)
        t = np.arange(n) / self.sample_rate / scale
        f0, f1 = (self.high_freq, self.low_freq) if down else (self.low_freq, self.high_freq)
        duration = count / self.sample_rate
        phase = 2 * np.pi * (f0 * t + 0.5 * (f1 - f0) / duration * t * t)
        return np.exp(1j * phase) * tukey(n, 0.25)

    def _make_preamble(self, scale):
        # 在统一时间轴上伸缩，避免分别取整两个扫频造成同步偏差。
        n = self._preamble_part
        position = np.arange(round(2 * n * scale)) / scale
        local = position % n
        down = position >= n
        f0 = np.where(down, self.high_freq, self.low_freq)
        f1 = np.where(down, self.low_freq, self.high_freq)
        t = local / self.sample_rate
        phase = 2 * np.pi * (f0 * t + 0.5 * (f1 - f0) / (n / self.sample_rate) * t * t)
        window = np.interp(local, np.arange(n), self._preamble_window, right=0)
        return np.exp(1j * phase) * window

    def reset(self):
        self._buffer = np.empty(0, dtype=np.float32)
        self._locked = None
        self._offset = 0
        self._length = None

    def duration(self, packet_bytes):
        return (len(self._preamble) + (packet_bytes + 4) * 8 * self.stride) / self.sample_rate

    def modulate(self, packet: bytes):
        if not isinstance(packet, bytes):
            raise TypeError("modulate()需要bytes")
        if not 1 <= len(packet) <= self.max_packet:
            raise ValueError("声学帧长度超限")
        # 长度及其按位取反用于及早拒绝损坏的物理帧头。
        header = struct.pack("!HH", len(packet), len(packet) ^ 0xFFFF)
        bits = np.unpackbits(np.frombuffer(header + packet, dtype=np.uint8))
        symbols = np.zeros((len(bits), self.stride), dtype=np.float32)
        symbols[:, :self.symbol_size] = self._symbols[bits].real
        return 0.45 * np.concatenate([self._preamble.real, symbols.ravel()]).astype(np.float32)

    @staticmethod
    def _peak(samples, template, earliest=False):
        n = len(template)
        if len(samples) < n:
            return 0.0, 0
        samples = samples.astype(np.float64)
        energy = np.concatenate(([0.0], np.cumsum(samples ** 2)))
        energy = energy[n:] - energy[:-n]
        numerator = np.abs(correlate(samples, template, mode="valid", method="fft"))
        # 实信号与解析模板的理想归一化峰值约为1。
        denominator = np.sqrt(np.maximum(energy, 1e-15) * np.vdot(template, template).real / 2)
        score = numerator / denominator
        score[energy < n * 1e-12] = 0  # 静音处的FFT舍入残差不应成为同步峰。
        if earliest:
            candidates = np.flatnonzero(score >= 0.60)
            if not len(candidates):
                return 0.0, 0
            start = int(candidates[0])
            i = start + int(np.argmax(score[start:start + 64]))
        else:
            i = int(np.argmax(score))
        return float(score[i]), i

    def _find_preamble(self):
        candidates = []
        for scale, template in self._bank:
            score, index = self._peak(self._buffer, template, earliest=True)
            if score >= 0.60:
                candidates.append((score, index, scale))
        if not candidates:
            return None
        # 按时间先后接收，不能让后面更干净的同步峰覆盖前一条消息。
        first_index = min(item[1] for item in candidates)
        best = max((item for item in candidates if item[1] <= first_index + 32),
                   key=lambda item: item[0])
        _, index, coarse = best
        # 在候选位置附近细化比例，减少长包的符号边界累计漂移。
        left = max(0, index - 32)
        right = min(len(self._buffer), index + self._keep + 32)
        for scale in np.linspace(max(0.98, coarse - 0.002), min(1.02, coarse + 0.002), 41):
            score, offset = self._peak(self._buffer[left:right], self._make_preamble(scale))
            if score > best[0]:
                best = score, left + offset, scale
        return best[1], best[2]

    def _decode_bytes(self, count, scale):
        # 对接收样本按估计的比例插值，恢复原符号时长与频率。
        start = self._offset + len(self._preamble) * scale
        positions = start + scale * (
            np.arange(count * 8)[:, None] * self.stride + np.arange(self.symbol_size)
        )
        chunks = np.interp(positions.ravel(), np.arange(len(self._buffer)), self._buffer)
        chunks = chunks.reshape(-1, self.symbol_size)
        scores = np.abs(chunks @ self._symbol_filters)
        return np.packbits(np.argmax(scores, axis=1).astype(np.uint8)).tobytes()

    def _refine_header(self, header, scale):
        """利用已解出的32位帧头延长训练段，减小长包中的定时漂移。"""
        bits = np.unpackbits(np.frombuffer(header, dtype=np.uint8))
        nominal = len(self._preamble) + 32 * self.stride
        right = min(len(self._buffer), self._offset + int(np.ceil(nominal * scale)) + 24)
        best = (0.0, self._offset, scale)
        for candidate in np.linspace(max(0.98, scale - 0.0002), min(1.02, scale + 0.0002), 41):
            template = np.zeros(round(nominal * candidate), dtype=complex)
            preamble = self._make_preamble(candidate)
            template[:len(preamble)] = preamble
            positions = np.arange(len(preamble), len(template)) / candidate - len(self._preamble)
            symbol = np.clip(np.floor(positions / self.stride).astype(int), 0, 31)
            local = positions - symbol * self.stride
            down = bits[symbol].astype(bool)
            f0 = np.where(down, self.high_freq, self.low_freq)
            f1 = np.where(down, self.low_freq, self.high_freq)
            t = local / self.sample_rate
            phase = 2 * np.pi * (f0 * t + 0.5 * (f1 - f0) /
                                (self.symbol_size / self.sample_rate) * t * t)
            window = np.interp(local, np.arange(self.symbol_size),
                               self._symbol_window, left=0, right=0)
            template[len(preamble):] = np.exp(1j * phase) * window
            score, offset = self._peak(self._buffer[:right], template)
            if score > best[0]:
                best = score, offset, candidate
        self._offset = best[1]
        return best[2]

    def feed_samples(self, samples, validator=None):
        """输入分段单声道样本，返回完整包。

        validator可传PacketCodec.decode；校验抛出ValueError时，在原缓冲区
        重新搜帧而非整段丢弃，避免损坏或截断包吞掉后续的完整消息。
        """
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim != 1 or not np.all(np.isfinite(samples)):
            raise ValueError("录音必须是有限值的单声道数组")
        self._buffer = np.concatenate([self._buffer, samples])
        packets = []
        while True:
            if self._locked is None:
                if len(self._buffer) < self._keep:
                    break
                found = self._find_preamble()
                if found is None:
                    self._buffer = self._buffer[-self._keep:]
                    break
                index, scale = found
                left = max(0, index - 8)
                self._buffer = self._buffer[left:]
                self._offset = index - left
                self._locked = scale
                self._length = None
            scale = self._locked
            if self._length is None:
                needed = self._offset + int(np.ceil((len(self._preamble) + 32 * self.stride) * scale)) + 24
                if len(self._buffer) < needed:
                    break
                header = self._decode_bytes(4, scale)
                length, inverse = struct.unpack("!HH", header)
                if length ^ inverse != 0xFFFF or not 1 <= length <= self.max_packet:
                    self._buffer = self._buffer[self._offset + 1:]
                    self._locked = None
                    continue
                scale = self._refine_header(header, scale)
                self._locked, self._length = scale, length
            length = self._length
            total = self._offset + int(np.ceil((len(self._preamble) + (length + 4) * 8 * self.stride) * scale))
            if len(self._buffer) < total:
                break
            packet = self._decode_bytes(length + 4, scale)[4:]
            if validator is not None:
                try:
                    validator(packet)
                except ValueError:
                    self.rejected_packets += 1
                    # 已确认帧头的长度/反码；跳过本次前导，避免反复锁定同一个坏包。
                    # 保留后面的全部采样，供搜索截断包之后的新前导。
                    skip = self._offset + int(len(self._preamble) * scale)
                    self._buffer = self._buffer[skip:]
                    self._locked = None
                    continue
            packets.append(packet)
            self.last_scale = scale
            self._buffer = self._buffer[total:]
            self._locked = None
        return packets


class ReliableAcousticLink:
    """TR发请求；BR回复时同时确认请求并携带自己的业务消息。

    所有公开方法由同一线程调用。ACK仅代表通信收讫，业务动作仍需传感器。
    clock可注入单调时钟供模拟使用，实机保持默认值。
    """
    def __init__(self, local_id, peer_id, audio, modem, initiator,
                 max_attempts=3, poll_interval=0.25, clock=time.monotonic):
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
        return int.from_bytes(hashlib.blake2s(tr_nonce + br_nonce, digest_size=4).digest(), "big") or 1

    def _frame(self, kind, seq=0, task=0, payload=b"", ack=0, session=None):
        return AcousticMessage(self.local_id, self.peer_id,
                               self.session_id if session is None else session,
                               seq, kind, task, payload, ack)

    def send(self, message_type, task_id=0, payload=b"", *, priority=1,
             ttl=10.0, latest_only=False):
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
        if len(self._queue) >= 32 and not (latest_only and any(
            e['message'].message_type == message_type and e['message'].task_id == task_id
            for e in self._queue
        )):
            raise BufferError("发送队列已满，最多32条；请合并状态更新")
        if latest_only:
            for entry in self._queue + ([self._pending] if self._pending else []):
                old = entry['message']
                if old.message_type == message_type and old.task_id == task_id:
                    self._terminal(entry, "superseded")
        sequence = self._next_sequence()
        entry = dict(message=replace(probe, sequence=sequence), priority=priority,
                     expires=None if ttl is None else self.clock() + ttl,
                     attempts=0, ack_due=None)
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
        sequence = entry['message'].sequence
        if self._states.get(sequence) in ("queued", "waiting_ack"):
            self._states[sequence] = state

    def cancel_task(self, task_id):
        """取消本机此任务的未完成发送，不撤销已送达的消息或对方动作。"""
        _uint("task_id", task_id, 65535)
        for entry in self._queue + ([self._pending] if self._pending else []):
            if entry['message'].task_id == task_id:
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
            self._events.append(dict(event="connected", generation=self.generation,
                                     session_id=self.session_id))

    def _prune(self, now):
        for entry in self._queue + ([self._pending] if self._pending else []):
            if entry['expires'] is not None and now >= entry['expires']:
                self._terminal(entry, "expired")
        active = lambda e: self._states[e['message'].sequence] in ("queued", "waiting_ack")
        self._queue = [e for e in self._queue if active(e)]
        if self._pending is not None and not active(self._pending):
            self._pending = None

    def _select(self, now):
        self._prune(now)
        if self._pending is not None:
            if self._pending['attempts'] >= self.max_attempts:
                self._terminal(self._pending, "failed")
                self._pending = None
            elif self._queue and max(e['priority'] for e in self._queue) > self._pending['priority']:
                self._queue.append(self._pending)
                self._pending = None
        if self._pending is None and self._queue:
            selected = min(range(len(self._queue)), key=lambda i: (
                -self._queue[i]['priority'], self._queue[i]['message'].sequence))
            self._pending = self._queue.pop(selected)
        return self._pending

    def _acknowledge(self, sequence):
        if self._pending is not None and self._pending['message'].sequence == sequence:
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
            entry['attempts'] += 1
            entry['message'] = frame
            entry['ack_due'] = (now + self.modem.duration(len(self.codec.encode(frame)))
                                + self.modem.duration(self.codec.MAX_PACKET)
                                + self.poll_interval + 2 * self.turnaround + 0.5)
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
            if (message.message_type != "_WELCOME" or len(message.payload) != 32
                    or message.payload[:16] != self._nonce or message.sequence or message.task_id):
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
            if message.message_type == "_IDLE" and (message.payload or message.sequence or message.task_id):
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
        if (not self.initiator and self._pending is not None
                and self._pending['attempts'] >= self.max_attempts
                and self._pending['ack_due'] is not None and now >= self._pending['ack_due']):
            self._terminal(self._pending, "failed")
            self._pending = None
        if self.audio.busy:
            return
        if not self.initiator:
            if self._response_to is None or now < self._response_at:
                return
            request, self._response_to = self._response_to, None
            if request.message_type == "_HELLO":
                frame = self._frame("_WELCOME", payload=request.payload + self._nonce,
                                    ack=request.sequence)
                entry = None
            elif not self.session_id or request.session_id != self.session_id:
                frame = self._frame("_RESET", payload=self._nonce, ack=request.sequence,
                                    session=request.session_id)
                entry = None
            else:
                entry = self._select(now)
                frame = (replace(entry['message'], session_id=self.session_id,
                                 ack_sequence=request.sequence) if entry else
                         self._frame("_IDLE", ack=request.sequence))
            self._transmit(frame, entry, now)
            return
        if self._timer_after_tx:
            self._deadline = now + self.modem.duration(self.codec.MAX_PACKET) * 1.02 + self.turnaround + 0.5
            self._timer_after_tx = False
        if self._request is not None:
            if self._deadline is None or now < self._deadline:
                return
            frame, entry = self._request
            if frame.message_type == "_HELLO":
                self.handshake_timeouts += 1
            elif entry is not None and entry['attempts'] >= self.max_attempts:
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
            frame = (replace(entry['message'], session_id=self.session_id, ack_sequence=self._ack_peer)
                     if entry else self._frame("_POLL", self._next_sequence(), ack=self._ack_peer))
        self._transmit(frame, entry, now, request=True)


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
        if (self.state not in ("idle", "held", "resync_required", "complete")
                and self._deadline is not None and self.link.clock() >= self._deadline):
            self.hold()
            return
        report = (self._grip_report if self.state == "gripped" else
                  self._release_report if self.state == "released" else None)
        if report is not None and self.link.status(report) in ("failed", "expired", "cancelled"):
            self.hold()
            return
        if message is None or self.task_id is None:
            return
        if (message.session_id != self.link.session_id or message.task_id != self.task_id
                or message.sender_id != self.link.peer_id or message.receiver_id != self.link.local_id
                or message.payload != self.items):
            return
        if message.message_type == "HOLD":
            self.state = "held"
            self.peer_ready = False
        elif self.link.initiator:
            if message.message_type == "CAN_RECEIVE" and self.state in ("ready", "grip_allowed"):
                self.peer_ready = True
            elif message.message_type == "GRIPPED" and self.state == "ready" and self.at_transfer_zone:
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
        return self.link.send("READY" if self.link.initiator else "CAN_RECEIVE",
                              self.task_id, self.items, priority=2, ttl=5, latest_only=True)

    @property
    def can_grip(self):
        self.update()
        return not self.link.initiator and self.state == "ready" and self.peer_ready and self.at_transfer_zone

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
        self._release_report = self.link.send("RELEASED", self.task_id, self.items, priority=3, ttl=5)

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


def run_self_tests():
    """离线测试，不启动麦克风或扬声器。"""
    from dataclasses import replace
    from types import SimpleNamespace
    import unittest
    from scipy.signal import resample_poly

    MESSAGE = AcousticMessage(1, 2, 123456, 7, "READY", 100, "塔基".encode())

    class ModemTests(unittest.TestCase):
        def test_nonfinite_parameters_rejected(self):
            for config in ({"gap_seconds": float("nan")}, {"low_freq": float("inf")},
                           {"symbol_seconds": float("inf")}, {"sample_rate": 0}):
                with self.assertRaises(ValueError):
                    ChirpModem(**config)

        def test_first_noisy_packet_not_skipped_for_later_clean_packet(self):
            tx = ChirpModem()
            first = tx.modulate(b"first")
            first += np.random.default_rng(33).normal(0, 0.03, len(first))
            wave = np.concatenate([first, np.zeros(4000), tx.modulate(b"second")])
            self.assertEqual(ChirpModem().feed_samples(wave), [b"first", b"second"])

        def test_truncated_packet_does_not_swallow_next_complete_packet(self):
            codec, tx = PacketCodec(), ChirpModem()
            long_packet = codec.encode(replace(MESSAGE, payload=b"x" * 32))
            good_packet = codec.encode(MESSAGE)
            # 保留长包的帧头，但去掉大部分载荷，再紧接一条完整消息。
            cut = int(tx.sample_rate * 0.6)
            wave = np.concatenate([tx.modulate(long_packet)[:cut], np.zeros(1200),
                                   tx.modulate(good_packet), np.zeros(240000)])
            rx = ChirpModem()
            recovered = []
            for index in range(0, len(wave), 1024):
                recovered.extend(rx.feed_samples(wave[index:index + 1024], validator=codec.decode))
            self.assertEqual(recovered, [good_packet])
            self.assertGreaterEqual(rx.rejected_packets, 1)

        @staticmethod
        def feed(modem, wave, chunk=1024):
            output = []
            for i in range(0, len(wave), chunk):
                output.extend(modem.feed_samples(wave[i:i + chunk]))
            return output

        def test_arbitrary_chunks_and_multiple_packets(self):
            tx = ChirpModem()
            wave = np.concatenate([np.zeros(379), tx.modulate(b"first"),
                                   np.zeros(613), tx.modulate(b"second"), np.zeros(3000)])
            for chunk in (317, 1024, len(wave)):
                with self.subTest(chunk=chunk):
                    self.assertEqual(self.feed(ChirpModem(), wave, chunk), [b"first", b"second"])

        def test_noise_echo_and_constant_doppler(self):
            packet = PacketCodec().encode(replace(MESSAGE, payload=bytes(range(32))))
            tx = ChirpModem()
            for up, down in ((1, 1), (99, 100), (101, 100), (1007, 1000), (19807, 20000)):
                with self.subTest(scale=up / down):
                    wave = resample_poly(tx.modulate(packet), up, down)
                    # 4ms延迟、幅度为直达声20%的单路回声。
                    wave = np.pad(wave, (379, 4000))
                    wave[192:] += 0.2 * wave[:-192].copy()
                    wave += np.random.default_rng(42).normal(0, 0.015, len(wave))
                    rx = ChirpModem()
                    self.assertEqual(self.feed(rx, wave), [packet])
                    self.assertAlmostEqual(rx.last_scale, up / down, delta=0.0002)

        def test_silence_and_noise_produce_no_packets(self):
            for wave in (np.zeros(10000), np.random.default_rng(9).normal(0, 0.05, 30000)):
                self.assertEqual(self.feed(ChirpModem(), wave), [])

        def test_corrupt_payload_is_rejected_by_codec_then_next_packet_recovers(self):
            codec = PacketCodec()
            packet = codec.encode(MESSAGE)
            corrupt = bytearray(packet)
            corrupt[-1] ^= 1
            tx = ChirpModem()
            wave = np.concatenate([tx.modulate(bytes(corrupt)), np.zeros(4800), tx.modulate(packet)])
            results = self.feed(ChirpModem(), wave)
            self.assertEqual(len(results), 2)
            with self.assertRaises(ValueError):
                codec.decode(results[0])
            self.assertEqual(codec.decode(results[1]), MESSAGE)

    class AudioTests(unittest.TestCase):
        def test_callback_playback_mute_and_capture(self):
            audio = AudioIO(sample_rate=1000, guard_seconds=0.1)
            audio._stream = SimpleNamespace(active=True, time=0)
            audio.play(np.array([0.1, 0.2, 0.3, 0.4, 0.5]))
            out = np.empty((4, 1), dtype=np.float32)
            incoming = np.ones((4, 1), dtype=np.float32)
            audio._callback(incoming, out, 4, SimpleNamespace(
                inputBufferAdcTime=0, outputBufferDacTime=0), False)
            np.testing.assert_allclose(out[:, 0], [0.1, 0.2, 0.3, 0.4])
            audio._callback(incoming, out, 4, SimpleNamespace(
                inputBufferAdcTime=0.004, outputBufferDacTime=0.004), False)
            np.testing.assert_allclose(out[:, 0], [0.5, 0, 0, 0])
            self.assertEqual(len(audio.read_samples()), 0)
            self.assertTrue(audio.busy)
            audio._stream.time = 0.2
            audio._callback(incoming, out, 4, SimpleNamespace(
                inputBufferAdcTime=0.2, outputBufferDacTime=0.2), False)
            np.testing.assert_array_equal(audio.read_samples(), np.ones(4))
            self.assertFalse(audio.busy)

        def test_discontinuity_reported(self):
            audio = AudioIO()
            audio._stream = SimpleNamespace(active=True, time=0)
            audio._discontinuity = True
            with self.assertRaises(BufferError):
                audio.read_samples()

    class ByteModem:
        """仅用于协议故障注入，绕过声学算法；声学算法另有测试。"""
        sample_rate = 48000
        max_packet = 160
        rejected_packets = 0

        def reset(self):
            pass

        def duration(self, n):
            return n / 1000

        def modulate(self, packet):
            return np.frombuffer(packet, dtype=np.uint8)

        def feed_samples(self, samples, validator=None):
            packet = samples.astype(np.uint8).tobytes()
            if validator is not None:
                try:
                    validator(packet)
                except ValueError:
                    self.rejected_packets += 1
                    return []
            return [packet]

    class SimulatedChannel:
        def __init__(self, drop=None, physical=False):
            self.now = 0.0
            self.events = []
            self.transmissions = []
            self.collisions = 0
            self.drop = drop or (lambda sender, message: False)
            self.physical = physical
            self.ends = []

        def step(self, dt=0.02):
            self.now += dt
            ready = [event for event in self.events if event[0] <= self.now]
            self.events = [event for event in self.events if event[0] > self.now]
            for end, start, target, wave in ready:
                collision = any(s < end and e > start and who == target
                                for s, e, who in self.transmissions)
                if collision:
                    self.collisions += 1
                else:
                    self.ends[target].rx.append(wave)

    class SimulatedAudio:
        sample_rate = 48000
        guard_seconds = 0.10

        def __init__(self, channel, index):
            self.channel, self.index = channel, index
            self.until = 0.0
            self.rx = []
            channel.ends.append(self)

        @property
        def busy(self):
            return self.channel.now < self.until

        def play(self, wave):
            if self.busy:
                raise RuntimeError("发生重叠播放")
            channel = self.channel
            duration = len(wave) / (48000 if channel.physical else 1000)
            end = channel.now + duration
            self.until = end + self.guard_seconds
            channel.transmissions.append((channel.now, end, self.index))
            if not channel.physical:
                message = PacketCodec().decode(wave.tobytes())
                if channel.drop(self.index, message):
                    return
            channel.events.append((end + 0.03, channel.now, 1 - self.index, wave.copy()))

        def read_samples(self):
            result = np.concatenate(self.rx) if self.rx else np.empty(0)
            self.rx.clear()
            return result

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
            for changes in ({"sender_id": 256}, {"sequence": 65536}, {"sequence": 0},
                            {"session_id": "abc"}, {"task_id": True},
                            {"payload": b"x" * 33}, {"message_type": "UNKNOWN"}):
                with self.assertRaises(ValueError):
                    replace(MESSAGE, **changes)

        def test_old_version_rejected(self):
            packet = bytearray(PacketCodec().encode(MESSAGE))
            packet[0] = 0xA1
            packet[-4:] = struct.pack("!I", zlib.crc32(packet[:-4]))
            with self.assertRaises(ValueError):
                PacketCodec().decode(bytes(packet))


    class LinkTests(unittest.TestCase):
        def pair(self, drop=None, physical=False):
            channel = SimulatedChannel(drop, physical)
            cls = ChirpModem if physical else ByteModem
            a = ReliableAcousticLink(1, 2, SimulatedAudio(channel, 0), cls(), True,
                                    clock=lambda: channel.now)
            b = ReliableAcousticLink(2, 1, SimulatedAudio(channel, 1), cls(), False,
                                    clock=lambda: channel.now)
            return channel, a, b

        def run_until(self, channel, a, b, predicate, seconds=60, coordinators=()):
            for _ in range(int(seconds / 0.02)):
                channel.step()
                a.tick()
                b.tick()
                for coordinator in coordinators:
                    coordinator.update()
                    message = coordinator.link.receive()
                    while message is not None:
                        coordinator.update(message)
                        message = coordinator.link.receive()
                if predicate():
                    return
            self.fail("模拟时间内未达到预期状态")

        def test_handshake_and_bidirectional_piggyback(self):
            sent = []
            channel, a, b = self.pair(lambda who, msg: sent.append((who, msg)) or False)
            sa = a.send("READY", 7, b"TR")
            sb = b.send("NEED", 7, b"BR")
            self.run_until(channel, a, b, lambda: a.status(sa) == b.status(sb) == "acknowledged")
            self.assertEqual(a.session_id, b.session_id)
            self.assertEqual(b.receive().payload, b"TR")
            self.assertEqual(a.receive().payload, b"BR")
            reply = next(msg for who, msg in sent if who == 1 and msg.message_type == "NEED")
            self.assertEqual(reply.ack_sequence, sa)
            self.assertFalse(any(msg.message_type == "_ACK" for _, msg in sent))
            self.assertEqual(channel.collisions, 0)

        def test_real_waveforms_end_to_end(self):
            channel, a, b = self.pair(physical=True)
            sa = a.send("READY", 9, b"\x00\x02", ttl=30)
            sb = b.send("NEED", 9, b"\x01\x01", ttl=30)
            self.run_until(channel, a, b, lambda: a.status(sa) == b.status(sb) == "acknowledged")
            self.assertEqual(a.receive().payload, b"\x01\x01")
            self.assertEqual(b.receive().payload, b"\x00\x02")
            self.assertEqual(channel.collisions, 0)

        def test_lost_reply_retries_without_duplicate(self):
            dropped = []
            def drop(who, msg):
                if who == 1 and msg.message_type == "STATE" and not dropped:
                    dropped.append(msg)
                    return True
                return False
            channel, a, b = self.pair(drop)
            sa = a.send("READY", 5, ttl=None)
            sb = b.send("STATE", 5, b"1", ttl=None)
            self.run_until(channel, a, b, lambda: a.status(sa) == b.status(sb) == "acknowledged")
            self.assertEqual(len(dropped), 1)
            self.assertIsNotNone(a.receive())
            self.assertIsNone(a.receive())
            self.assertIsNotNone(b.receive())
            self.assertIsNone(b.receive())
            self.assertEqual(channel.collisions, 0)

        def test_lost_piggyback_ack_retries_without_duplicate(self):
            dropped = []
            def drop(who, msg):
                if who == 0 and msg.ack_sequence and not dropped:
                    dropped.append(msg)
                    return True
                return False
            channel, a, b = self.pair(drop)
            seq = b.send("GRIPPED", 10, ttl=None)
            self.run_until(channel, a, b, lambda: b.status(seq) == "acknowledged")
            self.assertEqual(len(dropped), 1)
            self.assertEqual(a.receive().message_type, "GRIPPED")
            self.assertIsNone(a.receive())

        def test_priority_and_latest_state(self):
            sent = []
            channel, a, b = self.pair(lambda who, msg: sent.append((who, msg)) or False)
            old = a.send("INVENTORY", 3, b"1", priority=0, latest_only=True)
            newest = a.send("INVENTORY", 3, b"2", priority=0, latest_only=True)
            urgent = a.send("HOLD", 4, priority=3)
            self.run_until(channel, a, b, lambda: a.status(newest) == "acknowledged")
            self.assertEqual(a.status(old), "superseded")
            outgoing = [msg.sequence for who, msg in sent if who == 0 and msg.message_type not in CONTROL_TYPES]
            self.assertEqual(outgoing[:2], [urgent, newest])

        def test_expiry_cancel_and_queue_limit(self):
            channel, a, b = self.pair(lambda who, msg: True)
            seq = b.send("READY", 8, ttl=0.1)
            self.run_until(channel, a, b, lambda: b.status(seq) == "expired")
            cancelled = a.send("READY", 12)
            a.cancel_task(12)
            self.assertEqual(a.status(cancelled), "cancelled")
            for i in range(32):
                b.send("STATE", i, ttl=None)
            with self.assertRaises(BufferError):
                b.send("READY", 100)

        def test_latest_state_queue_stays_bounded(self):
            channel, a, b = self.pair()
            for i in range(100):
                b.send("STATE", 1, bytes([i]), latest_only=True)
            self.assertLessEqual(len(b._queue), 2)

        def test_out_of_order_states_rejected(self):
            channel, a, b = self.pair()
            newest = replace(MESSAGE, sequence=12, message_type="STATE", payload=b"new")
            older = replace(newest, sequence=11, payload=b"old")
            b._deliver(newest)
            b._deliver(older)
            self.assertEqual(b.receive().payload, b"new")
            self.assertIsNone(b.receive())

        def test_retry_both_roles_cancels_old_and_reconnects(self):
            for side in (0, 1):
                with self.subTest(side=side):
                    channel, a, b = self.pair()
                    self.run_until(channel, a, b, lambda: a.connected and b.connected)
                    old_session = a.session_id
                    old_a = a.send("READY", 7, ttl=None)
                    old_b = b.send("GRIPPED", 7, ttl=None)
                    (a, b)[side].reset_for_retry()
                    self.run_until(channel, a, b, lambda: a.connected and b.connected
                                   and a.session_id == b.session_id != old_session)
                    self.assertEqual(a.status(old_a), "cancelled")
                    self.assertEqual(b.status(old_b), "cancelled")
                    self.assertIsNone(a.receive())
                    self.assertIsNone(b.receive())
                    seq = a.send("STATE", 8)
                    self.run_until(channel, a, b, lambda: a.status(seq) == "acknowledged")

        def test_responder_process_restart(self):
            channel, a, b = self.pair()
            self.run_until(channel, a, b, lambda: a.connected and b.connected)
            old = a.session_id
            b = ReliableAcousticLink(2, 1, channel.ends[1], ByteModem(), False, clock=lambda: channel.now)
            self.run_until(channel, a, b, lambda: a.connected and b.connected and a.session_id == b.session_id != old)

        def test_initiator_process_restart(self):
            channel, a, b = self.pair()
            self.run_until(channel, a, b, lambda: a.connected and b.connected)
            old = a.session_id
            a = ReliableAcousticLink(1, 2, channel.ends[0], ByteModem(), True, clock=lambda: channel.now)
            self.run_until(channel, a, b, lambda: a.connected and b.connected and a.session_id == b.session_id != old)

        def test_delayed_old_hello_after_responder_retry_still_recovers(self):
            saved = []
            channel, a, b = self.pair(lambda who, msg: saved.append(msg) or False)
            self.run_until(channel, a, b, lambda: a.connected and b.connected)
            hello = next(msg for msg in saved if msg.message_type == "_HELLO")
            old = a.session_id
            b.reset_for_retry()
            channel.ends[1].rx.append(np.frombuffer(PacketCodec().encode(hello), dtype=np.uint8))
            b.tick()
            self.run_until(channel, a, b, lambda: a.connected and b.connected and a.session_id == b.session_id != old)

        def test_missing_welcome_never_delivers_business(self):
            sent = []
            def drop(who, msg):
                sent.append(msg)
                return msg.message_type == "_WELCOME"
            channel, a, b = self.pair(drop)
            sa = a.send("READY", ttl=2)
            sb = b.send("GRIPPED", ttl=2)
            self.run_until(channel, a, b, lambda: a.status(sa) == b.status(sb) == "expired")
            self.assertTrue(all(msg.message_type in CONTROL_TYPES for msg in sent))
            self.assertIsNone(a.receive())
            self.assertIsNone(b.receive())

        def test_old_session_packet_does_not_execute(self):
            channel, a, b = self.pair()
            self.run_until(channel, a, b, lambda: a.connected and b.connected)
            old = a.session_id
            a.reset_for_retry()
            self.run_until(channel, a, b, lambda: a.connected and b.connected and a.session_id != old)
            delayed = AcousticMessage(1, 2, old, 100, "RELEASED", 7)
            channel.ends[1].rx.append(np.frombuffer(PacketCodec().encode(delayed), dtype=np.uint8))
            b.tick()
            self.assertIsNone(b.receive())

        def test_old_welcome_cannot_restore_old_session(self):
            saved = []
            channel, a, b = self.pair(lambda who, msg: saved.append(msg) or False)
            self.run_until(channel, a, b, lambda: a.connected and b.connected)
            welcome = next(msg for msg in saved if msg.message_type == "_WELCOME")
            a.reset_for_retry()
            channel.step(0.4)
            a.tick()
            channel.ends[0].rx.append(np.frombuffer(PacketCodec().encode(welcome), dtype=np.uint8))
            a.tick()
            self.assertFalse(a.connected)

        def test_attempt_limit_after_connection(self):
            count = []
            channel, a, b = self.pair()
            self.run_until(channel, a, b, lambda: a.connected and b.connected)
            def drop(who, msg):
                if msg.message_type == "READY":
                    count.append(msg)
                    return True
                return False
            channel.drop = drop
            seq = a.send("READY", 1, ttl=None)
            self.run_until(channel, a, b, lambda: a.status(seq) == "failed")
            self.assertEqual(len(count), 3)

        def test_missing_peer_expiry_without_false_connection(self):
            channel, a, b = self.pair(lambda who, msg: True)
            seq = a.send("READY", ttl=1)
            self.run_until(channel, a, b, lambda: a.status(seq) == "expired")
            self.assertFalse(a.connected)

        def test_wrong_address_ignored(self):
            channel, a, b = self.pair()
            self.run_until(channel, a, b, lambda: a.connected and b.connected)
            packet = PacketCodec().encode(AcousticMessage(3, 2, b.session_id, 1, "READY", 1))
            channel.ends[1].rx.append(np.frombuffer(packet, dtype=np.uint8))
            b.tick()
            self.assertIsNone(b.receive())


    class TransferTests(LinkTests):
        # 不继承LinkTests测试方法，suite构造时只装入本类自有test方法。
        def setup_transfer(self, physical=False):
            channel, a, b = self.pair(physical=physical)
            self.run_until(channel, a, b, lambda: a.connected and b.connected)
            ca, cb = TransferCoordinator(a), TransferCoordinator(b)
            ca.begin(10, 0, 2, local_load=2)
            cb.begin(10, 0, 2, local_load=0)
            return channel, a, b, ca, cb

        def test_transfer_sensor_gated_sequence(self):
            self.exercise_transfer(False)

        def test_transfer_real_waveform(self):
            self.exercise_transfer(True)

        def exercise_transfer(self, physical):
            channel, a, b, ca, cb = self.setup_transfer(physical)
            ca.arrived()
            cb.arrived()
            self.assertFalse(ca.can_release)
            self.run_until(channel, a, b, lambda: cb.can_grip, coordinators=(ca, cb))
            cb.begin_grip()
            cb.confirm_gripped()
            self.run_until(channel, a, b, lambda: ca.can_release, coordinators=(ca, cb))
            ca.begin_release()
            ca.confirm_released()
            self.run_until(channel, a, b, lambda: cb.state == "verify_received", coordinators=(ca, cb))
            cb.confirm_received()
            self.run_until(channel, a, b, lambda: ca.state == cb.state == "complete", coordinators=(ca, cb))

        def test_release_requires_sensor_report_and_current_task(self):
            channel, a, b, ca, cb = self.setup_transfer()
            ca.arrived()
            with self.assertRaises(RuntimeError):
                ca.begin_release()
            wrong = AcousticMessage(2, 1, a.session_id, 77, "GRIPPED", 11, ca.items)
            ca.update(wrong)
            self.assertFalse(ca.can_release)

        def test_release_permission_expires_and_retry_revokes_it(self):
            channel, a, b, ca, cb = self.setup_transfer()
            ca.arrived()
            frame = AcousticMessage(2, 1, a.session_id, 77, "GRIPPED", 10, ca.items)
            ca.update(frame)
            self.assertTrue(ca.can_release)
            channel.now += 1.1
            self.assertFalse(ca.can_release)
            self.assertEqual(ca.state, "held")
            a.reset_for_retry()
            ca.update(frame)
            self.assertEqual(ca.state, "resync_required")

        def test_rules_capacity_and_task_reuse(self):
            channel, a, b, ca, cb = self.setup_transfer()
            for kind, count in ((0, 3), (2, 2)):
                with self.assertRaises(ValueError):
                    ca.begin(11, kind, count, local_load=2)
            with self.assertRaises(ValueError):
                ca.begin(10, 0, 2, local_load=2)
            with self.assertRaises(ValueError):
                cb.begin(11, 0, 2, local_load=1)
            with self.assertRaises(ValueError):
                ca.begin(11, 0, 2, local_load=4)

        def test_hold_blocks_late_gripped(self):
            channel, a, b, ca, cb = self.setup_transfer()
            ca.arrived()
            ca.hold()
            ca.update(AcousticMessage(2, 1, a.session_id, 77, "GRIPPED", 10, ca.items))
            self.assertFalse(ca.can_release)

    suite = unittest.TestSuite()
    for case in (PacketTests, ModemTests, AudioTests, LinkTests, TransferTests):
        for name in sorted(case.__dict__):
            if name.startswith('test_'):
                suite.addTest(case(name))
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


def main(argv=None):
    parser = argparse.ArgumentParser(description="ROBOCON声波通信V2：两端必须使用同一版本")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--role", choices=("tr", "br"))
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument("--list-devices", action="store_true")
    parser.add_argument("--input-device", type=int)
    parser.add_argument("--output-device", type=int)
    parser.add_argument("--tr-id", type=int, default=1)
    parser.add_argument("--br-id", type=int, default=2)
    parser.add_argument("--message-type", choices=sorted(set(MESSAGE_TYPES) - CONTROL_TYPES), default="READY")
    parser.add_argument("--task-id", type=int, default=1)
    parser.add_argument("--text", default="")
    args = parser.parse_args(argv)
    if args.self_test:
        return 0 if run_self_tests() else 1
    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return 0
    initiator = args.role == "tr"
    audio = AudioIO(input_device=args.input_device, output_device=args.output_device)
    link = ReliableAcousticLink(args.tr_id if initiator else args.br_id,
                               args.br_id if initiator else args.tr_id,
                               audio, ChirpModem(), initiator)
    sequence = link.send(args.message_type, args.task_id, args.text.encode("utf-8"), ttl=30)
    print("先自动建立会话，再发送一条消息。ACK仅表示通信收到；本程序不操作机构。")
    print("Ctrl+C退出。重试或断电恢复后，业务程序需要重新核对任务。")
    last_status = None
    try:
        with audio:
            while True:
                link.tick()
                state = link.status(sequence)
                if state != last_status:
                    print("发送状态:", state, flush=True)
                    last_status = state
                event = link.receive_event()
                while event is not None:
                    print("链路事件:", event, flush=True)
                    event = link.receive_event()
                message = link.receive()
                while message is not None:
                    print(f"收到 {message.message_type} task={message.task_id} "
                          f"payload={message.payload!r}", flush=True)
                    message = link.receive()
                time.sleep(0.01)
    except KeyboardInterrupt:
        print("已退出")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"运行失败：{exc}。请检查README中的依赖、设备与参数。", file=sys.stderr)
        raise SystemExit(1)
