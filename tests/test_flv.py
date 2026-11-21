import asyncio
import json
import struct
import subprocess
import unittest

import _support
from gwell import audio, av, flv
from synth_av import HAVE_FFMPEG, amr_frames, av_blob
from test_fmp4 import units


def tags(buf):
    out = []
    i = 13
    while i + 11 <= len(buf):
        n = int.from_bytes(buf[i + 1:i + 4], "big")
        ts = int.from_bytes(buf[i + 4:i + 7], "big") | buf[i + 7] << 24
        out.append((buf[i], ts, buf[i + 11:i + 11 + n]))
        assert struct.unpack_from(">I", buf, i + 11 + n)[0] == 11 + n
        i += 15 + n
    return out, i


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class FlvTest(unittest.TestCase):
    def mux(self, n=120):
        m = flv.FlvMuxer()
        items = [it for u in units(n) for it in m.feed(u)]
        return m, items, b"".join(d for d, _ in items)

    def test_structure(self):
        m, items, out = self.mux()
        self.assertEqual(out[:13], flv.FILE_HEADER)
        self.assertIs(items[0][1], None)
        self.assertTrue(items[1][1])
        self.assertEqual(m.head, items[0][0])
        ts, end = tags(out)
        self.assertEqual(end, len(out))
        self.assertEqual(ts[0][0], flv.TAG_SCRIPT)
        self.assertIn(b"videocodecid", ts[0][2])
        self.assertNotIn(b"audiocodecid", ts[0][2])
        self.assertEqual(ts[1][2][:2], b"\x17\x00")
        self.assertEqual({t[2][:2] for t in ts[2:]}, {b"\x17\x01", b"\x27\x01"})
        stamps = [t[1] for t in ts[1:]]
        self.assertEqual(stamps, sorted(stamps))

    def test_ffprobe_reads_stream(self):
        _, _, out = self.mux()
        r = subprocess.run(["ffprobe", "-v", "error", "-f", "flv", "-i", "pipe:0", "-select_streams", "v:0",
                            "-count_frames", "-show_entries", "stream=codec_name,width,height,nb_read_frames",
                            "-of", "json"], input=out, capture_output=True, timeout=60)
        st = json.loads(r.stdout)["streams"][0]
        self.assertEqual((st["codec_name"], st["width"], st["height"]), ("h264", 1280, 720))
        self.assertGreater(int(st["nb_read_frames"]), 50)

    def test_keyframe_decodes_to_jpeg(self):
        key = next(u for u in units(40) if u.keyframe).data
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "h264", "-i", "pipe:0",
                            "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                           input=key, capture_output=True, timeout=60)
        self.assertEqual(r.stdout[:2], b"\xff\xd8")


async def encode(chunks):
    got = []
    enc = audio.AacEncoder("ffmpeg", lambda pts, aac: got.append((pts, aac)))
    assert await enc.start()
    for u in chunks:
        enc.feed(u)
    enc.proc.stdin.close()
    await asyncio.wait_for(enc._reader, 30)
    await enc.close()
    return got, enc


def mux_av(aac, n=120):
    m = flv.FlvMuxer()
    m.audio = True
    out = []
    it = iter(aac)
    nxt = next(it, None)
    for u in units(n):
        out += [d for d, _ in m.feed(u)]
        while nxt and nxt[0] <= u.pts:
            if (t := m.feed_audio(*nxt)) is not None:
                out.append(t)
            nxt = next(it, None)
    return m, b"".join(out)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class AudioTest(unittest.TestCase):
    def test_encoder_timing_and_gap_fill(self):
        chunks = [av.AudioUnit(1_000_000, amr_frames(100)), av.AudioUnit(3_000_000, amr_frames(50, 1)),
                  av.AudioUnit(3_900_000, amr_frames(50, 2)), av.AudioUnit(5_000_000, amr_frames(50, 3)),
                  av.AudioUnit(65_000_000, amr_frames(50, 4))]
        got, enc = asyncio.run(encode(chunks))
        self.assertEqual(enc._fed, 300)
        self.assertEqual([p for p, _ in got[:60]], [1_000_000 + i * audio.AAC_FRAME_US for i in range(60)])
        self.assertGreater(len(got), 85)
        self.assertGreater(got[-1][0], 60_000_000)
        self.assertTrue(all(a for _, a in got))

    def test_flv_with_audio(self):
        src = list(av.parse_stream([av_blob()]))
        amr = [av.AudioUnit(f.pts2, f.slices) for f in src if isinstance(f, av.AVFrame)]
        aac, _ = asyncio.run(encode(amr))
        m, out = mux_av(aac)
        self.assertEqual(out[:13], flv.FILE_HEADER_AV)
        ts, end = tags(out)
        self.assertEqual(end, len(out))
        self.assertIn(b"audiocodecid", ts[0][2])
        self.assertEqual((ts[2][0], ts[2][2]), (flv.TAG_AUDIO, bytes((0xAF, 0, 0x14, 0x08))))
        au = [t for t in ts[3:] if t[0] == flv.TAG_AUDIO]
        self.assertGreater(len(au), 50)
        self.assertEqual({t[2][:2] for t in au}, {b"\xaf\x01"})
        self.assertEqual(au[0][1], 0)
        r = subprocess.run(["ffprobe", "-v", "error", "-f", "flv", "-i", "pipe:0", "-select_streams", "a:0",
                            "-count_frames", "-show_entries",
                            "stream=codec_name,sample_rate,channels,nb_read_frames", "-of", "json"],
                           input=out, capture_output=True, timeout=60)
        st = json.loads(r.stdout)["streams"][0]
        self.assertEqual((st["codec_name"], st["sample_rate"], st["channels"]), ("aac", "16000", 1))
        self.assertEqual(int(st["nb_read_frames"]), len(au))

    def test_audio_before_head_dropped(self):
        m = flv.FlvMuxer()
        m.audio = True
        self.assertIsNone(m.feed_audio(0, b"x"))
        list(m.feed(units(1)[0]))
        self.assertIsNone(m.feed_audio(m._origin_us - 1, b"x"))
        self.assertIsNotNone(m.feed_audio(m._origin_us, b"x"))


if __name__ == "__main__":
    unittest.main()
