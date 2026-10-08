"""AudioTests：从原 V2 自测迁移的回归用例。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

from acoustic_comm.audio_io import AudioIO


class AudioTests(unittest.TestCase):
    def test_callback_playback_mute_and_capture(self):
        audio = AudioIO(sample_rate=1000, guard_seconds=0.1)
        audio._stream = SimpleNamespace(active=True, time=0)
        audio.play(np.array([0.1, 0.2, 0.3, 0.4, 0.5]))
        out = np.empty((4, 1), dtype=np.float32)
        incoming = np.ones((4, 1), dtype=np.float32)
        audio._callback(
            incoming, out, 4, SimpleNamespace(inputBufferAdcTime=0, outputBufferDacTime=0), False
        )
        np.testing.assert_allclose(out[:, 0], [0.1, 0.2, 0.3, 0.4])
        audio._callback(
            incoming,
            out,
            4,
            SimpleNamespace(inputBufferAdcTime=0.004, outputBufferDacTime=0.004),
            False,
        )
        np.testing.assert_allclose(out[:, 0], [0.5, 0, 0, 0])
        self.assertEqual(len(audio.read_samples()), 0)
        self.assertTrue(audio.busy)
        audio._stream.time = 0.2
        audio._callback(
            incoming,
            out,
            4,
            SimpleNamespace(inputBufferAdcTime=0.2, outputBufferDacTime=0.2),
            False,
        )
        np.testing.assert_array_equal(audio.read_samples(), np.ones(4))
        self.assertFalse(audio.busy)

    def test_discontinuity_reported(self):
        audio = AudioIO()
        audio._stream = SimpleNamespace(active=True, time=0)
        audio._discontinuity = True
        with self.assertRaises(BufferError):
            audio.read_samples()
