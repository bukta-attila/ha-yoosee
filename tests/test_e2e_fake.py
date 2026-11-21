import asyncio
import struct
import unittest
from datetime import date, datetime, timedelta

import _support
from fake_gwell import DEV_ID, RECORDS, AvSource, FakeCamera, FakeServer
from gwell import client as gclient
from gwell import cloud, fmp4, gute, mtp, playback
from gwell.crypto import entry_pwd, gw_encode_password
from synth_av import HAVE_FFMPEG, av_blob, trafs
from test_fmp4 import probe

DEVICE_PASSWORD = "offline-test-pw"


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class EndToEndTest(unittest.TestCase):
    def test_list_play_seek_stop(self):
        asyncio.run(asyncio.wait_for(self._run(), 60))

    async def _run(self):
        acc = cloud.P2PAccount(0x81000001, 11, 22, 33, 44)
        blob = av_blob()
        cam = FakeCamera(gw_encode_password(entry_pwd(DEVICE_PASSWORD)), AvSource(blob))
        await cam.start()
        srv = FakeServer(acc, cam)
        await srv.start()
        c = gclient.GwellClient(acc, DEV_ID, DEVICE_PASSWORD, hosts=("127.0.0.1",), listsrv_port=srv.port)
        try:
            await c.start()
            self.assertTrue(c.alive)
            self.assertEqual(c.codec.mode, cloud.MODE_SESSION)
            self.assertEqual(srv.seen[:2], [cloud.ST_LISTSRV, cloud.ST_DETECT])

            days = await c.day_list(date(2026, 11, 17))
            self.assertEqual(days, [date(2026, 11, 16), date(2026, 11, 17)])
            recs = await c.record_list(date(2026, 11, 17))
            self.assertEqual([(r.start, r.duration) for r in recs], RECORDS)
            self.assertEqual(srv.mesg_payloads[0][0], struct.pack("<I", entry_pwd(DEVICE_PASSWORD)))
            self.assertEqual(srv.mesg_payloads[1][1][:2], bytes((gute.CMD_RECORD_LIST, 1)))

            media = await c.call()
            self.assertIs(await c.call(), media)
            self.assertEqual(srv.calls, 1)

            got = bytearray()
            gen = playback.stream_fmp4(media, recs[1].descriptor, 30)
            async for chunk in gen:
                got += chunk
                if len(got) > 300_000:
                    break
            await gen.aclose()
            await asyncio.sleep(0.3)
            self.assertEqual(got[4:8], b"ftyp")
            self.assertNotIn(b"mp4a", got)
            tfdt = got.index(b"tfdt")
            self.assertEqual(struct.unpack_from(">Q", got, tfdt + 8)[0], 30 * 90000)
            starts = [r for r in cam.records if r[0] == "sc" and r[1] == mtp.SC_START]
            self.assertTrue(starts)
            self.assertEqual({(r[2], r[3], r[4]) for r in starts},
                             {(mtp.CALL_PLAYBACK, recs[1].descriptor, c.password_enc)})
            self.assertIn(("switch", recs[1].descriptor, 30), cam.records)
            ready = [r for r in cam.records if r[0] == "sc" and r[1] == mtp.SC_READY]
            self.assertEqual(ready[0][4], 0)
            self.assertEqual(cam.closed_streams, 1)
            self.assertIsNone(media.stream_id)
            self.assertEqual(media.av.qsize(), 0)

            got2 = bytearray()
            async for chunk in playback.stream_fmp4(media, recs[0].descriptor, 0):
                got2 += chunk
                if len(got2) > 100_000:
                    break
            await asyncio.sleep(0.3)
            self.assertEqual(cam.closed_streams, 2)
            tfdt = got2.index(b"tfdt")
            self.assertEqual(struct.unpack_from(">Q", got2, tfdt + 8)[0], 0)
            self.assertLessEqual(len(media.kcp_media.rcv_buf) + len(media.kcp_media.rcv_queue), 2 * 64)

            got3 = bytearray()
            gen = playback.stream_fmp4(media, recs[1].descriptor, 30, ffmpeg="ffmpeg")
            async for chunk in gen:
                got3 += chunk
                if len(trafs(got3).get(2, ())) > 4:
                    break
            await gen.aclose()
            await asyncio.sleep(0.3)
            self.assertEqual(cam.closed_streams, 3)
            self.assertTrue(fmp4.codec_from_init(bytes(got3)).endswith(",mp4a.40.2"))
            t = trafs(got3)
            self.assertEqual(t[1][0][0], 30 * 90000)
            self.assertAlmostEqual(t[2][0][0], 30 * 16000, delta=1600)
            st, dec = probe(bytes(got3))
            self.assertEqual((st["audio"]["codec_name"], st["audio"]["sample_rate"]), ("aac", "16000"))
            self.assertAlmostEqual(float(st["audio"]["start_time"]), float(st["video"]["start_time"]), delta=0.1)
        finally:
            await c.stop()
            srv.close()
            cam.close()
        self.assertIsNone(c.media)
        self.assertEqual(c._tasks, set())
        self.assertFalse(c.alive)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class LiveTest(unittest.TestCase):
    def test_live(self):
        asyncio.run(asyncio.wait_for(self._run(), 60))

    async def _run(self):
        acc = cloud.P2PAccount(0x81000001, 11, 22, 33, 44)
        cam = FakeCamera(gw_encode_password(entry_pwd(DEVICE_PASSWORD)), AvSource(av_blob()))
        await cam.start()
        srv = FakeServer(acc, cam)
        await srv.start()
        c = gclient.GwellClient(acc, DEV_ID, DEVICE_PASSWORD, hosts=("127.0.0.1",), listsrv_port=srv.port)
        try:
            await c.start()
            media = await c.call()
            units = []
            gen = playback.stream_live(media)
            async for unit in gen:
                units.append(unit)
                if sum(len(getattr(u, "data", b"")) for u in units) > 100_000:
                    break
            await gen.aclose()
            await asyncio.sleep(0.3)
        finally:
            await c.stop()
            srv.close()
            cam.close()
        video = [u for u in units if not hasattr(u, "frames")]
        audio = [u for u in units if hasattr(u, "frames")]
        self.assertTrue(video[0].keyframe)
        self.assertEqual(video[0].codec, "h264")
        self.assertIsInstance(units[0], playback.AudioUnit)
        for a, b in zip(audio, audio[1:]):
            self.assertEqual(b.pts, a.pts + 20000 * len(a.frames))
        self.assertEqual(mtp.live_param(), bytes((1, 0, 0, 0, 0, 0, 0, 0)))
        starts = {(r[2], r[3], r[4]) for r in cam.records if r[0] == "sc" and r[1] == mtp.SC_START}
        self.assertEqual(starts, {(mtp.CALL_LIVE, mtp.live_param(), c.password_enc)})
        self.assertFalse([r for r in cam.records if r[0] == "switch"])
        self.assertEqual(cam.closed_streams, 1)


class RecordPagingTest(unittest.TestCase):
    def test_pages_of_64(self):
        asyncio.run(asyncio.wait_for(self._run(), 30))

    async def _run(self):
        acc = cloud.P2PAccount(0x81000001, 11, 22, 33, 44)
        srv = FakeServer(acc, None)
        base = datetime(2026, 11, 17, 0, 20, 30)
        srv.records = [(base + timedelta(minutes=9 * i), 60) for i in range(150)]
        await srv.start()
        c = gclient.GwellClient(acc, DEV_ID, DEVICE_PASSWORD, hosts=("127.0.0.1",), listsrv_port=srv.port)
        try:
            await c.start()
            recs = await c.record_list(date(2026, 11, 17))
        finally:
            await c.stop()
            srv.close()
        self.assertEqual(sorted(r.start for r in recs), [t for t, _ in srv.records])
        queries = [gute.parse_record_list_request(p) for _, p in srv.mesg_payloads if p[0] == gute.CMD_RECORD_LIST]
        self.assertEqual(len(queries), 3)
        self.assertEqual(queries[0], (datetime(2026, 11, 17), datetime(2026, 11, 17, 23, 59)))
        self.assertEqual(queries[1][1], srv.records[150 - 64][0].replace(second=0))
        self.assertTrue(all(q[0] == datetime(2026, 11, 17) for q in queries))


if __name__ == "__main__":
    unittest.main()
