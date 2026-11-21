import struct
import unittest

from _support import DEV_ID
from gwell import relay

PHONE_ID = 0x81000002


class PongTest(unittest.TestCase):
    def test_pong_for(self):
        ping = relay.Ping(relay.PING, 0x11223344, DEV_ID, PHONE_ID, 7, 123456)
        pong = relay.pong_for(ping, PHONE_ID, relayed=False)
        self.assertEqual((pong.sub, pong.dst_id, pong.counter, pong.clock_ms), (relay.PONG, DEV_ID, 7, 123456))
        raw = relay.build_ping(pong)
        self.assertEqual(struct.unpack_from("<I", raw, 8)[0], 0)


class StreamSplitTest(unittest.TestCase):
    def test_split(self):
        a = relay.build(relay.TYPE_DIRECT_DATA, b"x" * 30)
        b = relay.build(relay.TYPE_RELAY_DATA_OUT, b"y" * 9, DEV_ID)
        frames, rest = relay.split_stream(a + b + b[:5])
        self.assertEqual(frames, [a, b])
        self.assertEqual(rest, b[:5])
        self.assertEqual(relay.parse(b).dest_id, DEV_ID)


if __name__ == "__main__":
    unittest.main()
