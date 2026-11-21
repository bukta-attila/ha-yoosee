import random
import struct
import unittest

import _support
from gwell import kcp


class _Sink:
    def __init__(self):
        self.out = []

    def __call__(self, data):
        self.out.append(data)


class LossyLinkTest(unittest.TestCase):
    def test_lossy_reordered_link_delivers_in_order(self):
        rnd = random.Random(7)
        a_out, b_out = [], []
        a = kcp.KCP(5, a_out.append)
        b = kcp.KCP(5, b_out.append, rcv_queue_limit=8)
        msgs = [struct.pack("<I", i) * 50 for i in range(300)]
        for m in msgs:
            a.send(m)
        got = []
        now = 0
        while len(got) < len(msgs) and now < 120000:
            now += 15
            a.update(now)
            b.update(now)
            batch_a, batch_b = a_out[:], b_out[:]
            a_out.clear()
            b_out.clear()
            rnd.shuffle(batch_a)
            for d in batch_a:
                if rnd.random() > 0.2:
                    b.input(d)
            for d in batch_b:
                if rnd.random() > 0.2:
                    a.input(d)
            if now % 45 == 0:
                while (m := b.recv()) is not None:
                    got.append(m)
            self.assertLessEqual(len(b.rcv_buf) + len(b.rcv_queue), b.rcv_wnd + 8)
        self.assertEqual(got, msgs)
        self.assertFalse(a.dead)

    def test_window_shrinks_when_consumer_stalls(self):
        out = []
        b = kcp.KCP(9, out.append, rcv_queue_limit=64)
        for sn in range(64):
            seg = kcp.Segment(9, kcp.CMD_PUSH, 0, 64, 0, sn, 0, b"x").encode()
            b.input(seg)
        b.update(100)
        last = [s for d in out for s in kcp.iter_segments(d)][-1]
        self.assertEqual(last[3], 0)
        for sn in range(64, 300):
            b.input(kcp.Segment(9, kcp.CMD_PUSH, 0, 64, 0, sn, 0, b"y").encode())
        self.assertEqual(len(b.rcv_queue), 64)
        self.assertEqual(len(b.rcv_buf), 64)


if __name__ == "__main__":
    unittest.main()
