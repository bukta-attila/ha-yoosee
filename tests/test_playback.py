import asyncio
import struct
import time
import unittest

import _support
from gwell import fmp4, mtp, playback
from gwell.client import GwellError
from synth_av import AUDIO_SLICE, FRAME_US, HAVE_FFMPEG, frame, h264_units, trafs

DESC = bytes.fromhex("ea071b1106000141")
OTHER = bytes.fromhex("ea071b1108130041")


class _Media:
    def __init__(self, items, gap=0.0, repeat=False):
        self.items = list(items)
        self.gap = gap
        self.repeat = repeat
        self.opened = None
        self.closed = 0
        self.seeks = []

    async def open_playback(self, descriptor):
        self.opened = descriptor

    def switch_file(self, descriptor, offset_s):
        self.seeks.append((descriptor, offset_s, len(self.items)))

    async def read(self, timeout):
        if not self.items:
            await asyncio.sleep(timeout)
            return None
        item = self.items[0]
        if not self.repeat or len(self.items) > 1:
            self.items.pop(0)
        await asyncio.sleep(self.gap)
        return item

    def close_stream(self):
        self.closed += 1


def _marker(sub, desc=b""):
    payload = struct.pack("<I", 1) + desc if desc else bytes(20)
    return mtp.build_user_data(3, sub, payload)


async def _collect(media, desc, offset, **kw):
    out = bytearray()
    async for chunk in playback.stream_fmp4(media, desc, offset, **kw):
        out += chunk
    return out


class MarkerTimeoutTest(unittest.TestCase):
    def test_stale_data_without_marker_fails_fast(self):
        media = _Media([_marker(10, OTHER) + _marker(1)], gap=0.01, repeat=True)
        t0 = time.monotonic()
        with self.assertRaises(GwellError):
            asyncio.run(_collect(media, DESC, 461, idle_timeout=5, marker_timeout=0.3))
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(media.opened, DESC)
        self.assertEqual([x[:2] for x in media.seeks], [(DESC, 461)])
        self.assertEqual(media.closed, 1)

    def test_silence_before_marker_fails_at_deadline(self):
        media = _Media([])
        t0 = time.monotonic()
        with self.assertRaises(GwellError):
            asyncio.run(_collect(media, DESC, 0, idle_timeout=5, marker_timeout=0.3))
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(media.closed, 1)

    def test_seek_sent_after_stream_start(self):
        media = _Media([b"", _marker(1), _marker(10, DESC) + _marker(1)])
        asyncio.run(_collect(media, DESC, 461, idle_timeout=0.2, marker_timeout=5))
        self.assertEqual(media.seeks, [(DESC, 461, 1)])

    def test_no_seek_at_offset_zero(self):
        media = _Media([_marker(1)])
        asyncio.run(_collect(media, DESC, 0, idle_timeout=0.2, marker_timeout=5))
        self.assertEqual(media.seeks, [])

    def test_marker_logged(self):
        media = _Media([_marker(10, DESC) + _marker(1)])
        with self.assertLogs("gwell.playback", "DEBUG") as cm:
            out = asyncio.run(_collect(media, DESC, 461, idle_timeout=0.2, marker_timeout=5))
        self.assertEqual(out, b"")
        self.assertTrue(any("ch3/sub10 01000000" + DESC.hex() in m for m in cm.output))
        self.assertTrue(any("ch3/sub1 " in m for m in cm.output))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class SilentPlaybackTest(unittest.TestCase):
    def test_no_audio_keeps_video_only(self):
        units = h264_units()[:60]
        for ffmpeg, slices in ((None, (AUDIO_SLICE,)), ("ffmpeg", ()), ("/nonexistent/ffmpeg", (AUDIO_SLICE,))):
            items = [_marker(1)] + [frame(u, i * FRAME_US, slices) for i, u in enumerate(units)]
            out = asyncio.run(_collect(_Media(items), DESC, 0, ffmpeg=ffmpeg, idle_timeout=0.2, marker_timeout=5))
            self.assertEqual(out[4:8], b"ftyp")
            self.assertNotIn(b"mp4a", out)
            self.assertEqual(fmp4.codec_from_init(out).count(","), 0)
            t = trafs(out)
            self.assertEqual(set(t), {1})
            self.assertEqual(sum(n for _, n in t[1]), 60)
            self.assertEqual(t[1][0][0], 0)


if __name__ == "__main__":
    unittest.main()
