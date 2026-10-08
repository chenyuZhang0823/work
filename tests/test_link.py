"""LinkTests：从原 V2 自测迁移的回归用例。"""

from __future__ import annotations

import unittest
from dataclasses import replace

import numpy as np

from acoustic_comm.codec import PacketCodec
from acoustic_comm.config import CONTROL_TYPES
from acoustic_comm.link import ReliableAcousticLink
from acoustic_comm.message import AcousticMessage

from .helpers import MESSAGE, ByteModem, LinkTestMixin


class LinkTests(LinkTestMixin, unittest.TestCase):
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
        outgoing = [
            msg.sequence for who, msg in sent if who == 0 and msg.message_type not in CONTROL_TYPES
        ]
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
                self.run_until(
                    channel,
                    a,
                    b,
                    lambda: (
                        a.connected and b.connected and a.session_id == b.session_id != old_session
                    ),
                )
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
        b = ReliableAcousticLink(
            2, 1, channel.ends[1], ByteModem(), False, clock=lambda: channel.now
        )
        self.run_until(
            channel,
            a,
            b,
            lambda: a.connected and b.connected and a.session_id == b.session_id != old,
        )

    def test_initiator_process_restart(self):
        channel, a, b = self.pair()
        self.run_until(channel, a, b, lambda: a.connected and b.connected)
        old = a.session_id
        a = ReliableAcousticLink(
            1, 2, channel.ends[0], ByteModem(), True, clock=lambda: channel.now
        )
        self.run_until(
            channel,
            a,
            b,
            lambda: a.connected and b.connected and a.session_id == b.session_id != old,
        )

    def test_delayed_old_hello_after_responder_retry_still_recovers(self):
        saved = []
        channel, a, b = self.pair(lambda who, msg: saved.append(msg) or False)
        self.run_until(channel, a, b, lambda: a.connected and b.connected)
        hello = next(msg for msg in saved if msg.message_type == "_HELLO")
        old = a.session_id
        b.reset_for_retry()
        channel.ends[1].rx.append(np.frombuffer(PacketCodec().encode(hello), dtype=np.uint8))
        b.tick()
        self.run_until(
            channel,
            a,
            b,
            lambda: a.connected and b.connected and a.session_id == b.session_id != old,
        )

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
