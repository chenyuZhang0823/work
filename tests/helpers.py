"""测试专用样本、模拟音频通道与链路工具。"""

from __future__ import annotations

import numpy as np

from acoustic_comm.codec import PacketCodec
from acoustic_comm.link import ReliableAcousticLink
from acoustic_comm.message import AcousticMessage
from acoustic_comm.modem import ChirpModem

MESSAGE = AcousticMessage(1, 2, 123456, 7, "READY", 100, "塔基".encode())


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
            collision = any(
                s < end and e > start and who == target for s, e, who in self.transmissions
            )
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


class LinkTestMixin:
    def pair(self, drop=None, physical=False):
        channel = SimulatedChannel(drop, physical)
        cls = ChirpModem if physical else ByteModem
        a = ReliableAcousticLink(
            1, 2, SimulatedAudio(channel, 0), cls(), True, clock=lambda: channel.now
        )
        b = ReliableAcousticLink(
            2, 1, SimulatedAudio(channel, 1), cls(), False, clock=lambda: channel.now
        )
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
