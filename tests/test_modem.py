"""ModemTests：从原 V2 自测迁移的回归用例。"""

from __future__ import annotations

import unittest
from dataclasses import replace

import numpy as np
from scipy.signal import resample_poly

from acoustic_comm.codec import PacketCodec
from acoustic_comm.modem import ChirpModem

from .helpers import MESSAGE


class ModemTests(unittest.TestCase):
    def test_nonfinite_parameters_rejected(self):
        for config in (
            {"gap_seconds": float("nan")},
            {"low_freq": float("inf")},
            {"symbol_seconds": float("inf")},
            {"sample_rate": 0},
        ):
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
        wave = np.concatenate(
            [
                tx.modulate(long_packet)[:cut],
                np.zeros(1200),
                tx.modulate(good_packet),
                np.zeros(240000),
            ]
        )
        rx = ChirpModem()
        recovered = []
        for index in range(0, len(wave), 1024):
            recovered.extend(rx.feed_samples(wave[index : index + 1024], validator=codec.decode))
        self.assertEqual(recovered, [good_packet])
        self.assertGreaterEqual(rx.rejected_packets, 1)

    @staticmethod
    def feed(modem, wave, chunk=1024):
        output = []
        for i in range(0, len(wave), chunk):
            output.extend(modem.feed_samples(wave[i : i + chunk]))
        return output

    def test_arbitrary_chunks_and_multiple_packets(self):
        tx = ChirpModem()
        wave = np.concatenate(
            [
                np.zeros(379),
                tx.modulate(b"first"),
                np.zeros(613),
                tx.modulate(b"second"),
                np.zeros(3000),
            ]
        )
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
