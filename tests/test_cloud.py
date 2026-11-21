import struct
import unittest
from datetime import date

from _support import DEV_ID
from gwell import cloud, gute, rsa


class OfflineBuildTest(unittest.TestCase):
    def setUp(self):
        self.acc = cloud.P2PAccount(0x81000001, 11, 22, 33, 44)
        self.codec = cloud.GuteCodec(self.acc)
        self.codec.set_session_key(cloud.build_key_material(self.acc, 1_700_000_000, 1_700_000_100))

    def test_mode2_roundtrip_and_layouts(self):
        _, w = cloud.build_calling(self.codec, 0x1234, DEV_ID, 99)
        f = self.codec.decode(w)
        self.assertEqual((len(w), f.mode, f.proc, f.flags & 0xFF), (0x78, 2, 1, 2))
        self.assertEqual(struct.unpack_from("<IQQ", f.body, 0), (0x1234, self.acc.term_id, DEV_ID))
        self.assertEqual(struct.unpack_from("<I", f.body, 0x68 - 0x18)[0], 99)
        payload = gute.build_day_record_list_request(date(2026, 11, 17))
        _, w = cloud.build_remote_msg(self.codec, DEV_ID, 7, 123456, payload)
        self.assertEqual(len(w), 60)
        self.assertEqual(w[-4:], bytes.fromhex("0b11173b"))
        m = cloud.parse_remote_msg(self.codec.decode(w))
        self.assertEqual((m.dest_id, m.src_id, m.msg_id), (DEV_ID, self.acc.term_id, 7))
        self.assertEqual(m.payload[:4], struct.pack("<I", 123456))
        _, w = cloud.build_remote_msg(self.codec, DEV_ID, 8, 1, gute.build_day_list_request(date(2026, 11, 17), 3))
        self.assertEqual(len(w), 52)

    def test_rsa_pkcs1(self):
        p, q = 0xF7E75FDC469067FFDC4E847C51F452DF, 0xE85CED54AF57E53E092113E62F436F4F
        n = p * q
        d = pow(3, -1, (p - 1) * (q - 1))
        mod = n.to_bytes(32, "big")
        ct = rsa.pkcs1_v15_encrypt(b"0123456789", mod)
        pt = pow(int.from_bytes(ct, "big"), d, n).to_bytes(32, "big")
        self.assertEqual(pt[:2], b"\x00\x02")
        self.assertEqual(pt[-11:], b"\x000123456789")
        self.assertNotIn(0, pt[2:-11])

    def test_material(self):
        m = cloud.build_key_material(self.acc, 5, 7200 * 3 + 1)
        self.assertEqual(len(m), 32)
        self.assertEqual(struct.unpack_from("<IIII", m, 0), (5, 3, self.acc.term_id, 0))
        self.assertEqual(m[16:24], self.acc.code_bytes)
        self.assertEqual(cloud.material_hash(bytes(128)), cloud.EMPTY_PUBKEY_HASH)


if __name__ == "__main__":
    unittest.main()
