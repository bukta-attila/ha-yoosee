import asyncio
import sys
import types
import unittest
from datetime import date, datetime
from pathlib import Path

import _support
from fake_gwell import DEV_ID, AvSource, FakeCamera, FakeServer
from gwell import cloud, mtp
from gwell.crypto import entry_pwd, gw_encode_password
from synth_av import HAVE_FFMPEG, av_blob


def _stub_homeassistant():
    if "homeassistant" in sys.modules:
        return
    ha = types.ModuleType("homeassistant")
    ce = types.ModuleType("homeassistant.config_entries")
    core = types.ModuleType("homeassistant.core")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    ce.ConfigEntry = object
    core.HomeAssistant = object
    dt.now = lambda: datetime(2026, 11, 17, 12, 0)
    util.dt = dt
    for name, mod in (("homeassistant", ha), ("homeassistant.config_entries", ce), ("homeassistant.core", core),
                      ("homeassistant.util", util), ("homeassistant.util.dt", dt)):
        sys.modules[name] = mod
    pkg = types.ModuleType("yoosee_cloud")
    pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components" / "yoosee_cloud")]
    sys.modules["yoosee_cloud"] = pkg


_stub_homeassistant()
from yoosee_cloud import controller as ctl
from yoosee_cloud.const import (CONF_ACCOUNT, CONF_ACCOUNT_PASSWORD, CONF_DEVICE_ID, CONF_LOGIN_REGION, CONF_P2P,
                                SUBENTRY_CAMERA)
from yoosee_cloud.login import account_to_dict


class _Sub:
    def __init__(self, dev_id, title="Cam", **data):
        self.subentry_id = "s%s" % dev_id
        self.subentry_type = SUBENTRY_CAMERA
        self.unique_id = str(dev_id)
        self.title = title
        self.data = {CONF_DEVICE_ID: str(dev_id), **data}


class _Entry:
    def __init__(self, data, cameras):
        self.data = data
        self.options = {}
        self.entry_id = "e1"
        self.tasks = []
        self.subentries = {}
        for dev_id, spec in cameras.items():
            s = _Sub(dev_id, **spec)
            self.subentries[s.subentry_id] = s

    def async_create_background_task(self, hass, coro, name):
        t = asyncio.ensure_future(coro)
        self.tasks.append(t)
        return t


class _Hass:
    def __init__(self):
        self.config_entries = types.SimpleNamespace(async_update_entry=lambda e, data: setattr(e, "data", data))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class ControllerTest(unittest.TestCase):
    def test_cache_replace_and_shutdown(self):
        asyncio.run(asyncio.wait_for(self._run(), 60))

    async def _run(self):
        acc = cloud.P2PAccount(0x81000001, 11, 22, 33, 44, "1", "2", "3", "ab" * 64, "HU")
        pw = "offline-test-pw"
        cam = FakeCamera(gw_encode_password(entry_pwd(pw)), AvSource(av_blob()))
        await cam.start()
        srv = FakeServer(acc, cam)
        await srv.start()
        real = ctl.GwellClient
        ctl.GwellClient = lambda a, d, p: real(a, d, p, hosts=("127.0.0.1",), listsrv_port=srv.port)
        real_fetch, real_parse = ctl.fetch_device_list, ctl.parse_device_password

        async def fake_fetch(account, keys, account_name, region):
            self.assertEqual((account.access_id, account_name, region), ("3", "x", "HU"))
            return {"fake": True}

        def fake_parse(response, device_id, user_id):
            self.assertEqual((response, device_id), ({"fake": True}, DEV_ID))
            return pw

        ctl.fetch_device_list, ctl.parse_device_password = fake_fetch, fake_parse
        entry = _Entry({CONF_ACCOUNT: "x", CONF_ACCOUNT_PASSWORD: "y", CONF_LOGIN_REGION: "HU",
                        CONF_P2P: account_to_dict(acc)}, {DEV_ID: {"title": "Cam"}})
        account = ctl.Account(_Hass(), entry)
        self.assertEqual(list(account.cameras), [DEV_ID])
        c = account.cameras[DEV_ID]
        try:
            recs = await c.recordings(date(2026, 11, 17))
            again = await c.recordings(date(2026, 11, 17))
            self.assertIs(recs, again)
            self.assertEqual(len(srv.mesg_payloads), 1)
            self.assertEqual([r.start for r in recs], sorted(r.start for r in recs))

            first_bytes = []

            async def consume(desc, off, limit):
                n = 0
                gen = c.live_stream() if desc is None else c.stream(desc, off)
                try:
                    async for chunk in gen:
                        n += len(chunk)
                        first_bytes.append(chunk[:8])
                        if n > limit:
                            break
                finally:
                    await gen.aclose()
                return n

            t1 = asyncio.ensure_future(consume(recs[0].descriptor, 0, 10 ** 9))
            await asyncio.sleep(1.5)
            self.assertFalse(t1.done())
            n2 = await consume(recs[1].descriptor, 20, 100_000)
            self.assertTrue(t1.cancelled() or t1.done())
            self.assertGreater(n2, 100_000)
            self.assertEqual(srv.calls, 1)
            self.assertEqual(c._streams, 0)
            self.assertIn(("switch", recs[1].descriptor, 20), cam.records)

            t3 = asyncio.ensure_future(consume(recs[0].descriptor, 0, 10 ** 9))
            await asyncio.sleep(1.5)
            with self.assertRaises(ctl.GwellError):
                await consume(None, 0, 1)
            self.assertFalse(t3.done())
            self.assertFalse(c.live)
            t3.cancel()
            await asyncio.wait([t3])

            first_bytes.clear()
            t4 = asyncio.ensure_future(consume(None, 0, 10 ** 9))
            await asyncio.sleep(1.5)
            self.assertTrue(c.live)
            self.assertFalse(t4.done())
            self.assertEqual(first_bytes[0][:5], b"FLV\x01\x05")
            n_sc = len(cam.records)
            self.assertGreater(await consume(None, 0, 20_000), 20_000)
            self.assertIn(first_bytes[-1][:1], (b"\x08", b"\x09"))
            self.assertIn(b"\x08", {b[:1] for b in first_bytes})
            self.assertTrue(c.keyframe and await c.snapshot_keyframe() is c.keyframe)
            self.assertFalse([r for r in cam.records[n_sc:] if r[0] == "sc"])
            live_starts = {r[3] for r in cam.records if r[0] == "sc" and r[1] == mtp.SC_START and r[2] == mtp.CALL_LIVE}
            self.assertEqual(live_starts, {mtp.live_param(mtp.DEF_HD)})
            t5 = asyncio.ensure_future(consume(recs[1].descriptor, 0, 50_000))
            await asyncio.sleep(0)
            with self.assertRaises(ctl.GwellError):
                await consume(None, 0, 1)
            self.assertGreater(await t5, 50_000)
            self.assertTrue(t4.done())
            self.assertFalse(c.live)
            self.assertIn(mtp.CALL_LIVE, {r[2] for r in cam.records if r[0] == "sc" and r[1] == mtp.SC_START})

            key = await c.snapshot_keyframe()
            self.assertIn(b"\x00\x00\x01", key)
            self.assertFalse(c.live)
            self.assertTrue(c.client.media is None or c.client.media.closed)

            other = ctl.Controller(account, DEV_ID + 1, _Sub(DEV_ID + 1))
            account.cameras[DEV_ID + 1] = other
            other._active = asyncio.ensure_future(asyncio.sleep(10))
            with self.assertRaises(ctl.GwellError):
                await consume(None, 0, 1)
            other._active.cancel()
            del account.cameras[DEV_ID + 1]
        finally:
            await account.shutdown()
            ctl.GwellClient = real
            ctl.fetch_device_list, ctl.parse_device_password = real_fetch, real_parse
            srv.close()
            cam.close()
        self.assertIsNone(c.client)


if __name__ == "__main__":
    unittest.main()
