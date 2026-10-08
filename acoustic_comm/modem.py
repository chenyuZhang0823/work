"""上下扫频调制、流式解调与恒定多普勒补偿。"""

from __future__ import annotations

import math
import struct

import numpy as np
from scipy.signal import correlate
from scipy.signal.windows import tukey


class ChirpModem:
    """二进制上下扫频原型；逐包估计恒定时间伸缩，不跟踪包内加速度。"""

    def __init__(
        self,
        sample_rate=48000,
        low_freq=4000,
        high_freq=10000,
        symbol_seconds=0.004,
        gap_seconds=0.001,
        max_packet=160,
    ):
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
        self._symbols = np.array(
            [self._chirp(False, self.symbol_size), self._chirp(True, self.symbol_size)]
        )
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
        symbols[:, : self.symbol_size] = self._symbols[bits].real
        return 0.45 * np.concatenate([self._preamble.real, symbols.ravel()]).astype(np.float32)

    @staticmethod
    def _peak(samples, template, earliest=False):
        n = len(template)
        if len(samples) < n:
            return 0.0, 0
        samples = samples.astype(np.float64)
        energy = np.concatenate(([0.0], np.cumsum(samples**2)))
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
            i = start + int(np.argmax(score[start : start + 64]))
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
        best = max(
            (item for item in candidates if item[1] <= first_index + 32), key=lambda item: item[0]
        )
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
            template[: len(preamble)] = preamble
            positions = np.arange(len(preamble), len(template)) / candidate - len(self._preamble)
            symbol = np.clip(np.floor(positions / self.stride).astype(int), 0, 31)
            local = positions - symbol * self.stride
            down = bits[symbol].astype(bool)
            f0 = np.where(down, self.high_freq, self.low_freq)
            f1 = np.where(down, self.low_freq, self.high_freq)
            t = local / self.sample_rate
            phase = (
                2
                * np.pi
                * (f0 * t + 0.5 * (f1 - f0) / (self.symbol_size / self.sample_rate) * t * t)
            )
            window = np.interp(
                local, np.arange(self.symbol_size), self._symbol_window, left=0, right=0
            )
            template[len(preamble) :] = np.exp(1j * phase) * window
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
                    self._buffer = self._buffer[-self._keep :]
                    break
                index, scale = found
                left = max(0, index - 8)
                self._buffer = self._buffer[left:]
                self._offset = index - left
                self._locked = scale
                self._length = None
            scale = self._locked
            if self._length is None:
                needed = (
                    self._offset
                    + int(np.ceil((len(self._preamble) + 32 * self.stride) * scale))
                    + 24
                )
                if len(self._buffer) < needed:
                    break
                header = self._decode_bytes(4, scale)
                length, inverse = struct.unpack("!HH", header)
                if length ^ inverse != 0xFFFF or not 1 <= length <= self.max_packet:
                    self._buffer = self._buffer[self._offset + 1 :]
                    self._locked = None
                    continue
                scale = self._refine_header(header, scale)
                self._locked, self._length = scale, length
            length = self._length
            total = self._offset + int(
                np.ceil((len(self._preamble) + (length + 4) * 8 * self.stride) * scale)
            )
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
