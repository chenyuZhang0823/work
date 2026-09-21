"""双机器人声波通信：五个业务类、命令行入口及离线测试。

Python 3.10+。运行 python acoustic_comm.py --help 查看使用方法。
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import math
import struct
import sys
import time
from uuid import uuid4
import zlib

import numpy as np
from scipy.signal import correlate
from scipy.signal.windows import tukey




@dataclass(frozen=True, slots=True)
class AcousticMessage:
    """两台机器人之间的一条消息；创建后不修改。"""

    sender_id: int
    receiver_id: int
    session_id: str
    sequence: int
    message_type: str
    task_id: int
    payload: bytes = b""

    def __post_init__(self):
        for name, maximum in (("sender_id", 65535), ("receiver_id", 65535),
                              ("sequence", 0xFFFFFFFF), ("task_id", 0xFFFFFFFF)):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError(f"{name} 必须是0至{maximum}之间的整数")
        if (not isinstance(self.session_id, str) or len(self.session_id) != 32
                or any(c not in "0123456789abcdefABCDEF" for c in self.session_id)):
            raise ValueError("session_id 必须是 uuid4().hex 格式")
        object.__setattr__(self, "session_id", self.session_id.lower())
        if (not isinstance(self.message_type, str) or not 1 <= len(self.message_type) <= 24
                or not all(33 <= ord(c) <= 126 for c in self.message_type)):
            raise ValueError("消息类型须为1至24个不含空白的可打印ASCII字符")
        if not isinstance(self.payload, bytes):
            raise TypeError("payload 必须是 bytes；文字请先 encode('utf-8')")
        if len(self.payload) > 64:
            raise ValueError("payload 最多64字节，请传短消息")


class PacketCodec:
    """紧凑二进制数据包，使用 CRC32 检测错误；不提供身份认证。"""

    HEADER = struct.Struct("!2sBHH16sIIBB")
    MAX_PAYLOAD = 64
    MAX_TYPE = 24
    MAX_PACKET = HEADER.size + MAX_TYPE + MAX_PAYLOAD + 4

    def encode(self, message: AcousticMessage) -> bytes:
        if not isinstance(message, AcousticMessage):
            raise TypeError("encode()需要AcousticMessage")
        session = bytes.fromhex(message.session_id)
        kind = message.message_type.encode("ascii")
        header = self.HEADER.pack(
            b"AC", 1, message.sender_id, message.receiver_id, session,
            message.sequence, message.task_id, len(kind), len(message.payload),
        )
        body = header + kind + message.payload
        return body + struct.pack("!I", zlib.crc32(body))

    def decode(self, packet: bytes) -> AcousticMessage:
        if not isinstance(packet, bytes):
            raise TypeError("decode()需要bytes")
        if not self.HEADER.size + 5 <= len(packet) <= self.MAX_PACKET:
            raise ValueError("数据包长度错误")
        body, crc = packet[:-4], packet[-4:]
        if zlib.crc32(body) != struct.unpack("!I", crc)[0]:
            raise ValueError("CRC校验失败")
        magic, version, sender, receiver, session, seq, task, nk, payload_size = self.HEADER.unpack_from(body)
        if magic != b"AC" or version != 1:
            raise ValueError("不是受支持的协议版本")
        if not 1 <= nk <= self.MAX_TYPE or payload_size > self.MAX_PAYLOAD:
            raise ValueError("消息类型或载荷长度错误")
        if len(body) != self.HEADER.size + nk + payload_size:
            raise ValueError("数据包长度不匹配")
        pos = self.HEADER.size
        try:
            kind = body[pos:pos + nk].decode("ascii")
        except UnicodeError as exc:
            raise ValueError("消息类型不是ASCII") from exc
        return AcousticMessage(sender, receiver, session.hex(), seq, kind, task, body[pos + nk:])


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
    """停等ARQ：一端发起询问，另一端只在收到请求后应答。

    send/tick/receive/status 必须在同一主线程调用。ACK仅确认消息收到。
    主循环须持续调用 tick()，包括暂时没有业务消息时。
    """

    CONTROL = {"_ACK", "_POLL", "_IDLE"}

    def __init__(self, local_id, peer_id, audio, modem, initiator,
                 max_attempts=3, poll_interval=0.5):
        if local_id == peer_id or not all(type(v) is int and 0 <= v <= 65535 for v in (local_id, peer_id)):
            raise ValueError("两端编号须不同，且在0至65535之间")
        if audio.sample_rate != modem.sample_rate:
            raise ValueError("音频和调制器的采样率必须一致")
        if (type(max_attempts) is not int or max_attempts < 1
                or not math.isfinite(poll_interval) or poll_interval <= 0):
            raise ValueError("发送次数和轮询间隔必须为正")
        if type(initiator) is not bool:
            raise ValueError("initiator 必须是True或False，两端应互补")
        self.local_id, self.peer_id = local_id, peer_id
        self.audio, self.modem = audio, modem
        self.initiator = initiator
        self.codec = PacketCodec()
        if modem.max_packet < self.codec.MAX_PACKET:
            raise ValueError("调制器允许的最大包长不足")
        self.session_id = uuid4().hex
        self.max_attempts = max_attempts
        self.poll_interval = poll_interval
        # 留给对方切换收发、尾音消退及声波传播的时间。
        self.turnaround = audio.guard_seconds + 0.15
        self._sequence = 0
        self._outgoing = deque()
        self._incoming = deque()
        self._seen = set()
        self._states = {}
        self._inflight = None
        self._attempts = 0
        self._deadline = None
        self._start_timer = False
        self._retry_ready = False
        self._reply = None
        self._reply_at = 0.0
        self._next_send = 0.0
        self._poll_next = False
        self.crc_or_packet_errors = 0
        self.audio_discontinuities = 0

    def _new_message(self, kind, task_id=0, payload=b""):
        if self._sequence == 0xFFFFFFFF:
            raise RuntimeError("消息序号已用尽，请在空闲时重新创建链路")
        message = AcousticMessage(self.local_id, self.peer_id, self.session_id,
                                  self._sequence + 1, kind, task_id, payload)
        self._sequence += 1
        return message

    def send(self, message_type, task_id=0, payload=b""):
        """将业务消息排队，返回序号；不等待播放或对方确认。"""
        if isinstance(message_type, str) and message_type.startswith("_"):
            raise ValueError("下划线开头的类型保留给通信协议")
        message = self._new_message(message_type, task_id, payload)
        self.codec.encode(message)  # 排队前检查参数，避免在发声时才失败。
        self._outgoing.append(message)
        self._states[message.sequence] = "queued"
        return message.sequence

    def status(self, sequence):
        """queued / waiting_ack / acknowledged / failed；未知序号返回None。"""
        return self._states.get(sequence)

    def receive(self):
        """取出下一条去重后的业务消息，无消息时返回None。"""
        return self._incoming.popleft() if self._incoming else None

    @staticmethod
    def _reply_to(message, kind):
        # 控制回复回显请求的会话、序号和任务号，用于精确匹配。
        return AcousticMessage(message.receiver_id, message.sender_id,
                               message.session_id, message.sequence, kind, message.task_id)

    def _matches(self, reply):
        request = self._inflight
        return request is not None and (
            reply.session_id, reply.sequence, reply.task_id
        ) == (request.session_id, request.sequence, request.task_id)

    def _finish(self, result, now):
        message = self._inflight
        if self.initiator and message is not None:
            self._poll_next = message.message_type != "_POLL"
        if message is not None and message.message_type not in self.CONTROL:
            self._states[message.sequence] = result
        if self._reply is not None and self._reply[1]:
            self._reply = None
        self._inflight = None
        self._deadline = None
        self._start_timer = False
        self._retry_ready = False
        self._attempts = 0
        self._next_send = now + self.turnaround

    def _transmit(self, message, tracked):
        self.modem.reset()  # 本机发声期间不尝试解码残留的半包。
        self.audio.play(self.modem.modulate(self.codec.encode(message)))
        if tracked:
            if self._inflight != message:
                self._attempts = 0
            self._inflight = message
            self._attempts += 1
            self._start_timer = True
            self._deadline = None
            self._retry_ready = False
            if message.message_type not in self.CONTROL:
                self._states[message.sequence] = "waiting_ack"

    def _handle(self, message, now):
        if message.sender_id != self.peer_id or message.receiver_id != self.local_id:
            return
        kind = message.message_type
        if kind in self.CONTROL and message.payload:
            return
        if kind == "_ACK":
            if self._matches(message) and self._inflight.message_type != "_POLL":
                self._finish("acknowledged", now)
        elif kind == "_IDLE":
            if self.initiator and self._matches(message) and self._inflight.message_type == "_POLL":
                self._finish("acknowledged", now)
                self._next_send = now + self.poll_interval
        elif kind == "_POLL":
            if self.initiator:
                return
            if self._inflight is not None and self._attempts >= self.max_attempts:
                self._finish("failed", now)
            if self._inflight is not None:
                self._reply = (self._inflight, True)
            elif self._outgoing:
                self._reply = (self._outgoing.popleft(), True)
            else:
                self._reply = (self._reply_to(message, "_IDLE"), False)
            self._reply_at = now + self.turnaround
        elif not kind.startswith("_"):
            if self.initiator:
                # BR业务消息仅在TR已发出询问并等待应答时接纳。
                if self._inflight is None or self._inflight.message_type != "_POLL":
                    return
                self._finish("acknowledged", now)
            key = (message.sender_id, message.session_id, message.sequence)
            if key not in self._seen:
                self._seen.add(key)
                self._incoming.append(message)
            # 重复包也回复ACK，否则ACK丢失时发送端会一直重传。
            self._reply = (self._reply_to(message, "_ACK"), False)
            self._reply_at = now + self.turnaround

    def tick(self, now=None):
        """推进接收、超时和轮流发送；不等待音频播放，但解调需要CPU时间。"""
        live_clock = now is None
        now = time.monotonic() if now is None else now
        try:
            samples = self.audio.read_samples()
        except BufferError:
            self.audio_discontinuities += 1
            self.modem.reset()
            samples = []
        if len(samples):
            rejected_before = self.modem.rejected_packets
            packets = self.modem.feed_samples(samples, validator=self.codec.decode)
            self.crc_or_packet_errors += self.modem.rejected_packets - rejected_before
            # 解调耗时不应缩短对方切换收发所需的保护时间。
            now = time.monotonic() if live_clock else now
            for packet in packets:
                try:
                    message = self.codec.decode(packet)
                except ValueError:
                    self.crc_or_packet_errors += 1
                    continue
                self._handle(message, now)
        if self.audio.busy:
            return
        if self._start_timer:
            # 必须在播放完成后计时，不能让长包尚未播完便触发重传。
            if self._inflight.message_type == "_POLL":
                expected_bytes = self.codec.MAX_PACKET
            else:
                expected_bytes = len(self.codec.encode(self._reply_to(self._inflight, "_ACK")))
            self._deadline = now + self.modem.duration(expected_bytes) + self.turnaround + 0.5
            self._start_timer = False
        if self._deadline is not None and now >= self._deadline:
            if self._inflight.message_type == "_POLL" or self._attempts >= self.max_attempts:
                self._finish("failed", now)
            else:
                self._deadline = None
                self._retry_ready = True
        if self._reply is not None and now >= self._reply_at:
            message, tracked = self._reply
            self._reply = None
            self._transmit(message, tracked)
            self._next_send = now + self.turnaround
            return
        if not self.initiator or self._reply is not None or now < self._next_send:
            return
        if self._inflight is not None:
            if self._retry_ready:
                self._transmit(self._inflight, True)
            return
        if self._outgoing and not self._poll_next:
            message = self._outgoing.popleft()
        else:
            message = self._new_message("_POLL")
        self._transmit(message, True)


def run_self_tests() -> bool:
    """不打开音频设备；测试代码只在主动运行自测时执行。"""
    from dataclasses import replace
    from types import SimpleNamespace
    import unittest
    from scipy.signal import resample_poly

    MESSAGE = AcousticMessage(1, 2, "ab" * 16, 7, "READY", 100, "塔基".encode())


    class PacketTests(unittest.TestCase):
        def test_message_validation_and_session_normalization(self):
            self.assertEqual(replace(MESSAGE, session_id="AB" * 16), MESSAGE)
            for changes in ({"sequence": True}, {"receiver_id": 1.2},
                            {"message_type": "BAD\nTYPE"}, {"message_type": None}):
                with self.assertRaises(ValueError):
                    replace(MESSAGE, **changes)
            with self.assertRaises(TypeError):
                replace(MESSAGE, payload="请先编码")

        def test_roundtrip_unicode_and_binary(self):
            codec = PacketCodec()
            for payload in (b"", "塔基，数量1".encode(), bytes(range(64))):
                message = replace(MESSAGE, payload=payload)
                self.assertEqual(codec.decode(codec.encode(message)), message)

        def test_corruption_and_truncation_rejected(self):
            codec = PacketCodec()
            packet = codec.encode(MESSAGE)
            for index in range(len(packet)):
                corrupt = bytearray(packet)
                corrupt[index] ^= 1
                with self.assertRaises(ValueError):
                    codec.decode(bytes(corrupt))
            for end in range(len(packet)):
                with self.assertRaises(ValueError):
                    codec.decode(packet[:end])

        def test_invalid_outgoing_fields(self):
            for changes in ({"session_id": "bad"}, {"sender_id": -1}, {"task_id": 2 ** 32},
                            {"message_type": "中文"}, {"payload": b"x" * 65}):
                with self.assertRaises(ValueError):
                    PacketCodec().encode(replace(MESSAGE, **changes))


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
            long_packet = codec.encode(replace(MESSAGE, payload=b"x" * 64))
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
            packet = PacketCodec().encode(replace(MESSAGE, payload=bytes(range(64))))
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


    class LinkTests(unittest.TestCase):
        def test_invalid_send_does_not_consume_sequence(self):
            channel, a, b = self.pair()
            with self.assertRaises(ValueError):
                a.send("READY", payload=b"x" * 65)
            self.assertEqual(a.send("READY"), 1)

        def test_crc_failures_counted_and_not_delivered(self):
            channel, a, b = self.pair()
            damaged = bytearray(PacketCodec().encode(MESSAGE))
            damaged[-1] ^= 1
            channel.ends[1].rx.append(np.frombuffer(bytes(damaged), dtype=np.uint8))
            b.tick(0)
            self.assertIsNone(b.receive())
            self.assertEqual(b.crc_or_packet_errors, 1)

        def pair(self, drop=None, physical=False):
            channel = SimulatedChannel(drop, physical)
            cls = ChirpModem if physical else ByteModem
            a = ReliableAcousticLink(1, 2, SimulatedAudio(channel, 0), cls(), True)
            b = ReliableAcousticLink(2, 1, SimulatedAudio(channel, 1), cls(), False)
            return channel, a, b

        def run_until(self, channel, a, b, predicate, seconds=60):
            for _ in range(int(seconds / 0.02)):
                channel.step()
                a.tick(channel.now)
                b.tick(channel.now)
                if predicate():
                    return
            self.fail("模拟时间内未达到预期状态")

        def test_both_directions_without_collision(self):
            channel, a, b = self.pair()
            sa = a.send("READY", 11, b"TR")
            sb = b.send("NEED", 12, b"BR")
            self.run_until(channel, a, b, lambda: a.status(sa) == b.status(sb) == "acknowledged")
            self.assertEqual(b.receive().payload, b"TR")
            self.assertEqual(a.receive().payload, b"BR")
            self.assertEqual(channel.collisions, 0)

        def test_responder_gets_turn_before_initiator_queue_drains(self):
            channel, a, b = self.pair()
            a.send("READY", 1)
            second = a.send("READY", 2)
            reply = b.send("NEED", 3)
            self.run_until(channel, a, b, lambda: b.status(reply) == "acknowledged")
            self.assertNotEqual(a.status(second), "acknowledged")
            self.assertEqual(a.receive().task_id, 3)

        def test_missing_peer_stops_after_attempt_limit(self):
            sent = []

            def drop(who, msg):
                sent.append(msg)
                return True

            channel, a, b = self.pair(drop)
            seq = a.send("READY", 1)
            self.run_until(channel, a, b, lambda: a.status(seq) == "failed")
            self.assertEqual(sum(m.message_type == "READY" for m in sent), 3)

        def test_lost_ack_retries_without_duplicate_delivery_both_directions(self):
            for source in (0, 1):
                with self.subTest(ack_sender=source):
                    dropped = []

                    def drop(sender, message):
                        if sender == source and message.message_type == "_ACK" and not dropped:
                            dropped.append(message)
                            return True
                        return False

                    channel, a, b = self.pair(drop)
                    sender, receiver = (a, b) if source == 1 else (b, a)
                    seq = sender.send("READY", 17, b"only-once")
                    self.run_until(channel, a, b, lambda: sender.status(seq) == "acknowledged")
                    self.assertEqual(len(dropped), 1)
                    self.assertEqual(receiver.receive().payload, b"only-once")
                    self.assertIsNone(receiver.receive())
                    self.assertEqual(channel.collisions, 0)

        def test_all_acks_lost_reports_failed_but_does_not_duplicate(self):
            for source in (0, 1):
                channel, a, b = self.pair(lambda who, msg: msg.message_type == "_ACK")
                sender, receiver = (a, b) if source == 0 else (b, a)
                seq = sender.send("READY", 10)
                self.run_until(channel, a, b, lambda: sender.status(seq) == "failed")
                self.assertIsNotNone(receiver.receive())
                self.assertIsNone(receiver.receive())

        def test_wrong_peer_and_stale_ack_do_not_complete_send(self):
            channel, a, b = self.pair(lambda who, msg: True)
            seq = a.send("READY", 5)
            a.tick(0)
            wrong = AcousticMessage(3, 1, a.session_id, seq, "_ACK", 5)
            stale = replace(wrong, sender_id=2, session_id="12" * 16)
            wrong_task = replace(wrong, sender_id=2, task_id=6)
            for message in (wrong, stale, wrong_task):
                channel.ends[0].rx.append(np.frombuffer(PacketCodec().encode(message), dtype=np.uint8))
                a.tick(0)
            self.assertEqual(a.status(seq), "waiting_ack")

        def test_new_sender_session_can_reuse_sequence(self):
            channel, a, b = self.pair()
            first = a.send("READY", 5)
            self.run_until(channel, a, b, lambda: a.status(first) == "acknowledged")
            self.assertIsNotNone(b.receive())
            a.session_id = "12" * 16
            a._sequence = 0
            second = a.send("READY", 6)
            self.run_until(channel, a, b, lambda: a.status(second) == "acknowledged")
            self.assertEqual(b.receive().task_id, 6)

        def test_real_waveform_two_way_pipeline(self):
            channel, a, b = self.pair(physical=True)
            sa = a.send("READY", 1, b"TR")
            sb = b.send("READY", 1, b"BR")
            self.run_until(channel, a, b, lambda: a.status(sa) == b.status(sb) == "acknowledged")
            self.assertEqual(b.receive().payload, b"TR")
            self.assertEqual(a.receive().payload, b"BR")
            self.assertEqual(channel.collisions, 0)

    suite = unittest.TestSuite()
    for case in (PacketTests, ModemTests, AudioTests, LinkTests):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()



def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="双机器人声波通信：单文件版")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--role", choices=["tr", "br"], help="TR发起轮询，BR应答")
    mode.add_argument("--list-devices", action="store_true", help="列出本机音频设备")
    mode.add_argument("--self-test", action="store_true", help="离线自测，不打开麦克风和扬声器")
    parser.add_argument("--input-device", type=int)
    parser.add_argument("--output-device", type=int)
    parser.add_argument("--tr-id", type=int, default=1)
    parser.add_argument("--br-id", type=int, default=2)
    parser.add_argument("--message-type", default="READY", help="启动时发送的消息类型")
    parser.add_argument("--text", help="UTF-8消息内容，编码后最多64字节")
    parser.add_argument("--task-id", type=int, default=1)
    args = parser.parse_args(argv)
    if args.self_test:
        return 0 if run_self_tests() else 1
    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return 0
    initiator = args.role == "tr"
    audio = AudioIO(input_device=args.input_device, output_device=args.output_device)
    modem = ChirpModem()
    link = ReliableAcousticLink(
        args.tr_id if initiator else args.br_id,
        args.br_id if initiator else args.tr_id,
        audio, modem, initiator=initiator,
    )
    payload = (args.text if args.text is not None else args.role.upper()).encode("utf-8")
    sequence = link.send(args.message_type, task_id=args.task_id, payload=payload)
    print(f"{args.role.upper()} 已排队{args.message_type}，消息序号={sequence}；Ctrl+C退出")
    print("确认仅表示收到消息；此示例不会操作机器人。")
    previous = None
    previous_errors = (0, 0)
    try:
        audio.start()
        while True:
            link.tick()
            state = link.status(sequence)
            if state != previous:
                print("发送状态:", state, flush=True)
                previous = state
            message = link.receive()
            while message is not None:
                print(f"收到 {message.sender_id}: {message.message_type} "
                      f"task={message.task_id} "
                      f"内容={message.payload.decode('utf-8', errors='backslashreplace')}", flush=True)
                message = link.receive()
            errors = (link.audio_discontinuities, link.crc_or_packet_errors)
            if errors != previous_errors:
                print(f"累计音频不连续={errors[0]}，校验失败={errors[1]}", flush=True)
                previous_errors = errors
            time.sleep(0.01)
    except KeyboardInterrupt:
        print("\n结束")
    finally:
        audio.close()
    return 0


if __name__ == "__main__":
    # PortAudio异常也继承Exception；命令行友好提示，作为库导入时仍保留原异常。
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"启动或运行失败：{exc}\n请检查README中的依赖、设备编号和参数说明。", file=sys.stderr)
        raise SystemExit(1)
