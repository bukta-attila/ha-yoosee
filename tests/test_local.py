import asyncio
import time
import unittest

from test_controller import _Entry, _Hass, ctl
from yoosee_cloud import local
from yoosee_cloud.const import CONF_RTSP_PASSWORD, CONF_SOURCE
from yoosee_cloud.login import LoginUnavailable

DEV = 10000001


def _reply(dev: int) -> bytes:
    return b"\x02\x00\x00\x00" + bytes(12) + dev.to_bytes(4, "little") + bytes(96)


class _Responder(asyncio.DatagramProtocol):
    def __init__(self, replies):
        self.replies = replies
        self.probes = 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data == local.PROBE:
            self.probes += 1
            for r in self.replies:
                self.transport.sendto(r, addr)


async def _responder(replies):
    t, p = await asyncio.get_running_loop().create_datagram_endpoint(lambda: _Responder(replies),
                                                                      local_addr=("127.0.0.1", 0))
    return t, p, t.get_extra_info("sockname")


class DiscoverTest(unittest.TestCase):
    def test_finds_device_and_stops_early(self):
        async def run():
            t, p, addr = await _responder([b"junk", b"\x02\x00\x00\x00" + bytes(4), _reply(7), _reply(DEV)])
            try:
                t0 = time.monotonic()
                found = await local.discover(DEV, timeout=2, target=addr)
                return found, time.monotonic() - t0, p.probes
            finally:
                t.close()

        found, took, probes = asyncio.run(run())
        self.assertEqual(found, {7: "127.0.0.1", DEV: "127.0.0.1"})
        self.assertLess(took, 1)
        self.assertEqual(probes, 1)

    def test_timeout_without_reply(self):
        async def run():
            t, _, addr = await _responder([])
            try:
                return await local.discover(DEV, timeout=0.2, target=addr)
            finally:
                t.close()

        self.assertEqual(asyncio.run(run()), {})

    def test_rtsp_url_quotes_password(self):
        self.assertEqual(local.rtsp_url("10.0.0.5", "a@b/c d"), "rtsp://admin:a%40b%2Fc%20d@10.0.0.5:554/onvif1")


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.addr = "192.168.1.50"
        self.real = local.discover

        async def fake(want=None, **kw):
            self.calls.append(want)
            return {want: self.addr} if self.addr else {}

        local.discover = fake
        self.addCleanup(setattr, local, "discover", self.real)

    def _ctrl(self, source=None, password="pw"):
        spec = {"title": "Cam"}
        if source:
            spec[CONF_SOURCE] = source
        if password:
            spec[CONF_RTSP_PASSWORD] = password
        account = ctl.Account(_Hass(), _Entry({}, {DEV: spec}))
        return account, account.cameras[DEV]

    def test_cloud_only_skips_discovery(self):
        _, c = self._ctrl("cloud")
        self.assertIsNone(asyncio.run(c.rtsp_url()))
        self.assertEqual(self.calls, [])

    def test_auto_uses_rtsp_and_caches_address(self):
        _, c = self._ctrl()

        async def run():
            return await c.rtsp_url(), await c.rtsp_url()

        self.assertEqual(asyncio.run(run()), (local.rtsp_url(self.addr, "pw"),) * 2)
        self.assertEqual(self.calls, [DEV])

    def test_auto_falls_back_when_not_found(self):
        self.addr = None
        _, c = self._ctrl("auto")
        self.assertIsNone(asyncio.run(c.rtsp_url()))

    def test_cloud_password_when_empty(self):
        account, c = self._ctrl(password=None)

        async def pw(dev):
            return "cloudpw"

        account.password = pw
        self.assertEqual(asyncio.run(c.rtsp_url()), local.rtsp_url(self.addr, "cloudpw"))

        async def fail(dev):
            raise LoginUnavailable("down")

        account.password = fail
        self.assertIsNone(asyncio.run(c.rtsp_url()))

    def test_broken_rtsp(self):
        _, auto = self._ctrl("auto")
        auto.rtsp_broken()
        self.assertIsNone(asyncio.run(auto.rtsp_url()))
        auto._rtsp_failed_at -= 301
        self.addr = "192.168.1.51"
        self.assertEqual(asyncio.run(auto.rtsp_url()), local.rtsp_url("192.168.1.51", "pw"))
        auto.rtsp_ok()
        self.assertIsNone(auto._rtsp_failed_at)

        _, only = self._ctrl("local")
        only.rtsp_broken()
        self.calls.clear()
        self.assertEqual(asyncio.run(only.rtsp_url()), local.rtsp_url("192.168.1.51", "pw"))
        self.assertEqual(self.calls, [DEV])

    def test_moved(self):
        _, c = self._ctrl()

        async def run():
            await c.rtsp_url()
            same = await c.rtsp_moved()
            self.addr = "192.168.1.52"
            moved = await c.rtsp_moved()
            self.addr = None
            return same, moved, await c.rtsp_moved()

        self.assertEqual(asyncio.run(run()), (False, True, False))


if __name__ == "__main__":
    unittest.main()
