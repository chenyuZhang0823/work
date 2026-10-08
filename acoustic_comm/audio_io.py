"""麦克风采集、扬声器播放与流的生命周期。"""

from __future__ import annotations

import math
from collections import deque

import numpy as np


class AudioIO:
    """单个双工音频流；协议半双工，播放及尾音期间不提交录音。"""

    def __init__(
        self, sample_rate=48000, input_device=None, output_device=None, guard_seconds=0.10
    ):
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
            samplerate=self.sample_rate,
            blocksize=1024,
            channels=(1, 1),
            dtype="float32",
            device=(self.input_device, self.output_device),
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
            outdata[:count, 0] = tx[self._position : self._position + count]
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
