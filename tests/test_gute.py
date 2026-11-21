import unittest
from datetime import date, datetime, timedelta

from _support import DEV_ID
from gwell import gute


class FrameShapeTest(unittest.TestCase):
    def test_mode1_request_frame_shape(self):
        body = gute.build_mesg(DEV_ID, 123456, 0x1234, gute.build_day_record_list_request(date(2026, 11, 17)))
        f = gute.build_frame(gute.SUBTYPE_MESG, body, sender_id=0x11223344, frame_id=0x6B8B4567, opt_tail=b"\x3c\x01\x00")
        self.assertEqual(len(f), 60)
        self.assertEqual(f[-4:], bytes.fromhex("0b11173b"))
        fr = gute.decode_frame(f)
        self.assertTrue(fr.checksum_ok)
        self.assertEqual(fr.header.opt, 0x05)
        env, payload = gute.parse_mesg(fr.body)
        self.assertEqual((env.dev_id, env.password, env.msg_id, env.flag, env.length), (DEV_ID, 123456, 0x1234, 0, 16))
        self.assertEqual(gute.parse_record_list_request(payload),
                         (datetime(2026, 11, 17, 0, 0), datetime(2026, 11, 17, 23, 59)))
        g = gute.build_frame(gute.SUBTYPE_MESG, gute.build_mesg(DEV_ID, 1, 2, gute.build_day_list_request(date(2026, 11, 17), 3)),
                             sender_id=1, frame_id=2)
        self.assertEqual(len(g), 52)
        self.assertEqual(g[-4:], bytes.fromhex("ea070b11"))
        self.assertNotIn("123456", repr(env))


class DescriptorTest(unittest.TestCase):
    def test_round_trip(self):
        d = datetime(2026, 11, 16, 23, 30, 4)
        desc = gute.build_descriptor(d, "A")
        self.assertEqual(desc.hex(), "ea071b10171e0441")
        self.assertEqual(gute.parse_descriptor(desc), (d, "A"))
        self.assertEqual(d + timedelta(seconds=1144), datetime(2026, 11, 16, 23, 49, 8))


if __name__ == "__main__":
    unittest.main()
