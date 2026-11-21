import random
import unittest

import _support
from gwell import av, mtp
from synth_av import AUDIO_SLICE, FRAME_US, frame

IDR = b"\x00\x00\x00\x01\x67\x42\x00\x0a\x00\x00\x00\x01\x68\xce\x00\x00\x00\x01\x65" + bytes(range(200))
P = b"\x00\x00\x00\x01\x41" + bytes(range(120))
SWITCH_DESC = bytes.fromhex("ea071b10171e0441")


def stream():
    parts = [mtp.build_user_data(3, 10, (1).to_bytes(4, "little") + SWITCH_DESC), mtp.build_user_data(3, 1, b"")]
    for i in range(60):
        parts.append(frame(IDR if i % 30 == 0 else P, 5_000_000 + i * FRAME_US))
        if i == 30:
            parts.append(mtp.build_user_data(3, 8, b""))
    return b"".join(parts)


def parse_chunked(data, seed):
    rnd = random.Random(seed)
    p = av.AVParser()
    items = []
    pos = 0
    while pos < len(data):
        n = rnd.choice((1, 3, 27, 29, 500, 1008, 4096, 70000))
        items += p.feed(data[pos:pos + n])
        pos += n
    items += p.close()
    return items, p


class AVParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = stream()
        cls.items, cls.parser = parse_chunked(cls.data, 1)
        cls.frames = [i for i in cls.items if isinstance(i, av.AVFrame)]
        cls.uds = [i for i in cls.items if isinstance(i, av.AVUserData)]

    def test_counts(self):
        self.assertEqual(len(self.frames), 60)
        self.assertEqual(len(self.uds), 3)
        self.assertEqual(self.parser.skipped, 0)

    def test_chunking_independent(self):
        whole = av.AVParser()
        items = whole.feed(self.data) + whole.close()
        for seed in (2, 3):
            other, _ = parse_chunked(self.data, seed)
            self.assertEqual(other, items)
        self.assertEqual(items, self.items)

    def test_content(self):
        f0 = self.frames[0]
        self.assertEqual(f0.video, IDR)
        self.assertEqual(f0.video_codec, "h264")
        self.assertEqual(f0.slices, (AUDIO_SLICE,))
        self.assertEqual(f0.slice_pts(1) - f0.slice_pts(0), 20000)
        self.assertTrue(all(f.slice_table2 is None for f in self.frames))
        self.assertEqual(self.frames[1].pts - f0.pts, FRAME_US)

    def test_keyframes(self):
        units = list(av.video_units(self.items))
        self.assertEqual([i for i, u in enumerate(units) if u.keyframe], [0, 30])

    def test_inband_user_data(self):
        kinds = [(u.data.builder, u.data.channel, u.data.sub_cmd) for u in self.uds]
        self.assertEqual(kinds, [(2, 3, 10), (2, 3, 1), (2, 3, 8)])
        self.assertEqual(self.uds[0].data.payload[4:12], SWITCH_DESC)
        self.assertEqual(av.MAGIC, mtp.USER_DATA_MAGIC)

    def test_resync_over_garbage(self):
        clean = av.AVParser()
        ref = clean.feed(self.data) + clean.close()
        p = av.AVParser()
        got = p.feed(b"\x00garbage\xff\xff\xff") + p.feed(self.data) + p.close()
        self.assertEqual([i.offset - 11 for i in got], [i.offset for i in ref])
        self.assertEqual(p.resyncs, 1)


if __name__ == "__main__":
    unittest.main()
