import asyncio
import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

import _support
from gwell import audio, av, fmp4
from synth_av import HAVE_FFMPEG, av_blob


def units(limit=None):
    data = av_blob()
    return list(av.video_units(av.parse_stream([data[i:i + 1008] for i in range(0, len(data), 1008)])))[:limit]


def boxes(buf):
    out = []
    i = 0
    while i + 8 <= len(buf):
        n = struct.unpack_from(">I", buf, i)[0]
        out.append(buf[i + 4:i + 8])
        i += n
    return out, i


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class MuxTest(unittest.TestCase):
    def test_structure_and_bounded_fragments(self):
        m = fmp4.Fmp4Muxer(start_s=12.0)
        chunks = []
        for u in units():
            chunks.extend(m.feed(u))
        chunks.extend(m.flush())
        out = b"".join(chunks)
        kinds, end = boxes(out)
        self.assertEqual(end, len(out))
        self.assertEqual(kinds[:2], [b"ftyp", b"moov"])
        self.assertEqual(set(kinds[2:]) - {b"moof", b"mdat", b"ftyp", b"moov"}, set())
        self.assertTrue(m.codec.startswith("avc1."))
        self.assertLess(max(len(c) for c in chunks), 2_000_000)
        first_moof = out.index(b"tfdt")
        self.assertEqual(struct.unpack_from(">Q", out, first_moof + 8)[0], 12 * fmp4.TIMESCALE)

    def test_sps_dimensions(self):
        u = next(u for u in units(40) if u.keyframe)
        sps = next(n for n in fmp4.split_nals(u.data) if n[0] & 0x1F == 7)
        w, h = fmp4.sps_dimensions(sps)
        self.assertEqual((w, h), (1280, 720))

    def test_ffprobe_reads_stream(self):
        m = fmp4.Fmp4Muxer(start_s=5.0)
        out = b"".join(c for u in units(120) for c in m.feed(u)) + b"".join(m.flush())
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.mp4"
            p.write_bytes(out)
            r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                                "-show_entries", "stream=codec_name,width,height,nb_read_frames:format=start_time",
                                "-of", "json", str(p)], capture_output=True, text=True, timeout=60)
            info = json.loads(r.stdout)
            st = info["streams"][0]
            self.assertEqual(st["codec_name"], "h264")
            self.assertGreater(int(st["nb_read_frames"]), 50)
            self.assertAlmostEqual(float(info["format"]["start_time"]), 5.0, delta=0.2)
            dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-f", "null", "-"],
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(dec.returncode, 0, dec.stderr[-500:])


def probe(out):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.mp4"
        p.write_bytes(out)
        r = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                            "stream=codec_type,codec_name,sample_rate,channels,start_time,nb_read_frames",
                            "-of", "json", str(p)], capture_output=True, text=True, timeout=60)
        dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-f", "null", "-"],
                             capture_output=True, text=True, timeout=120)
    return {s["codec_type"]: s for s in json.loads(r.stdout)["streams"]}, dec


async def aac_frames():
    got = []
    enc = audio.AacEncoder("ffmpeg", lambda pts, aac: got.append((pts, aac)))
    assert await enc.start()
    for f in av.parse_stream([av_blob()]):
        if isinstance(f, av.AVFrame):
            enc.feed(av.AudioUnit(f.pts2, f.slices))
    enc.proc.stdin.close()
    await asyncio.wait_for(enc._reader, 30)
    await enc.close()
    return got


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class AudioMuxTest(unittest.TestCase):
    def test_two_tracks(self):
        aac = asyncio.run(aac_frames())
        m = fmp4.Fmp4Muxer(start_s=7.0)
        m.audio = True
        chunks = []
        for u in units(120):
            chunks += m.feed(u)
            while aac and aac[0][0] <= u.pts:
                chunks += m.feed_audio(*aac.pop(0))
        out = b"".join(chunks + list(m.flush()))
        self.assertEqual(boxes(out)[1], len(out))
        self.assertTrue(fmp4.codec_from_init(chunks[0]).startswith("avc1."))
        self.assertTrue(fmp4.codec_from_init(chunks[0]).endswith(",mp4a.40.2"))
        st, dec = probe(out)
        self.assertEqual(dec.returncode, 0, dec.stderr[-500:])
        self.assertEqual((st["audio"]["codec_name"], st["audio"]["sample_rate"], st["audio"]["channels"]),
                         ("aac", "16000", 1))
        self.assertGreater(int(st["audio"]["nb_read_frames"]), 100)
        self.assertGreater(int(st["video"]["nb_read_frames"]), 100)
        self.assertAlmostEqual(float(st["video"]["start_time"]), 7.0, delta=0.05)
        self.assertAlmostEqual(float(st["audio"]["start_time"]), 7.0, delta=0.1)

    def test_video_only_init_unchanged(self):
        m = fmp4.Fmp4Muxer()
        init = next(m.feed(units(1)[0]))
        self.assertNotIn(b"mp4a", init)
        self.assertEqual(fmp4.codec_from_init(init).count(","), 0)
        self.assertEqual(list(m.feed_audio(0, b"x")), [])


class UnsupportedTest(unittest.TestCase):
    def test_hevc_reported(self):
        m = fmp4.Fmp4Muxer()
        self.assertEqual(list(m.feed(av.VideoUnit(0, b"\x00\x00\x00\x01\x40\x01", True, "hevc"))), [])
        self.assertEqual(m.unsupported, "hevc")


if __name__ == "__main__":
    unittest.main()
