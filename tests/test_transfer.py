"""TransferTests：从原 V2 自测迁移的回归用例。"""

from __future__ import annotations

import unittest

from acoustic_comm.message import AcousticMessage
from acoustic_comm.transfer import TransferCoordinator

from .helpers import LinkTestMixin


class TransferTests(LinkTestMixin, unittest.TestCase):
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
        self.run_until(
            channel, a, b, lambda: ca.state == cb.state == "complete", coordinators=(ca, cb)
        )

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
