import struct
import base64
import unittest

import _support
from gwell import crypto


class RC5Test(unittest.TestCase):
    def test_rivest_rc5_32_12_16_chain(self):
        chain = [
            ("00000000000000000000000000000000", "0000000000000000", "21a5dbee154b8f6d"),
            ("915f4619be41b2516355a50110a9ce91", "21a5dbee154b8f6d", "f7c013ac5b2b8952"),
            ("783348e75aeb0f2fd7b169bb8dc16787", "f7c013ac5b2b8952", "2f42b3b70369fc92"),
            ("dc49db1375a5584f6485b413b5f12baf", "2f42b3b70369fc92", "65c178b284d197cc"),
            ("5269f149d41ba0152497574d7f153125", "65c178b284d197cc", "eb44e415da319824"),
        ]
        for key, pt, ct in chain:
            c = crypto.RC5(bytes.fromhex(key), 12)
            self.assertEqual(c.encrypt_block(bytes.fromhex(pt)).hex(), ct)
            self.assertEqual(c.decrypt_block(bytes.fromhex(ct)).hex(), pt)

    def test_ecb_keeps_tail(self):
        c = crypto.RC5(b"www.gwell.cc")
        data = bytes(range(21))
        enc = c.encrypt_ecb(data)
        self.assertEqual(enc[16:], data[16:])
        self.assertNotEqual(enc[:16], data[:16])
        self.assertEqual(c.decrypt_ecb(enc), data)


class GuteCryptoTest(unittest.TestCase):
    def _plain(self, mode):
        f = bytearray(b"\x7f\x27\x2f\x00" + bytes(range(4, 0x2F)))
        f[0x14] = 0x04 | mode
        return bytes(f)

    def test_mode1_round_trip(self):
        p = self._plain(1)
        e = crypto.gute_encrypt(p)
        self.assertEqual(e[0:4], p[0:4])
        self.assertEqual(e[0x14:0x18], p[0x14:0x18])
        self.assertEqual(e[0x28:], p[0x28:])
        self.assertEqual(crypto.gute_decrypt(e), p)

    def test_mode2_needs_key(self):
        p = self._plain(2)
        with self.assertRaises(ValueError):
            crypto.gute_encrypt(p)
        key = bytes(range(32))
        e = crypto.gute_encrypt(p, key)
        self.assertEqual(crypto.gute_decrypt(e, crypto.RC5(key)), p)
        self.assertEqual(e[4:12], crypto.gute_encrypt(self._plain(1))[4:12])

    def test_mode0_untouched(self):
        p = self._plain(0)
        self.assertEqual(crypto.gute_encrypt(p), p)


class MtpCryptoTest(unittest.TestCase):
    def test_round_trip_and_plain_flag(self):
        rec = bytes([4, 0xF1, 0x20, 0x00]) + b"\xff\xff\xff\x88" + bytes(range(24))
        e = crypto.mtp_encrypt(rec)
        self.assertEqual(e[:8], rec[:8])
        self.assertNotEqual(e[8:], rec[8:])
        self.assertEqual(crypto.mtp_decrypt(e), rec)
        plain = bytes([4, 0x00, 0x20, 0x00]) + rec[4:]
        self.assertEqual(crypto.mtp_encrypt(plain), plain)


class PasswordTest(unittest.TestCase):
    def test_entry_pwd(self):
        self.assertFalse(crypto.is_need_encrypt("123456"))
        self.assertEqual(crypto.entry_pwd("123456"), 123456)
        self.assertTrue(crypto.is_need_encrypt("1234567890"))
        self.assertTrue(crypto.is_need_encrypt("abcdef"))
        self.assertFalse(crypto.is_need_encrypt("abcde"))
        v = crypto.entry_pwd("abcdef")
        self.assertEqual(v, crypto.encrypt_gw1("abcdef"))
        self.assertTrue(0 <= v < 999999999)
        self.assertFalse(crypto._is_weak(v))

    def test_weak_predicate(self):
        for v, exp in [(111111, True), (123456, True), (654321, True), (1, True), (135791, False), (192837, False)]:
            self.assertEqual(crypto._is_weak(v), exp, v)

    def test_encode_decode(self):
        for x in (0, 1, 123456, 999999998, 0xFFFFFFFF):
            self.assertEqual(crypto.gw_decode_password(crypto.gw_encode_password(x)), x)
        self.assertNotEqual(crypto.gw_encode_password(123456), 123456)


class GW5Test(unittest.TestCase):
    def test_des_known_vector(self):
        key = bytes.fromhex("133457799bbcdff1")
        keys = crypto._des_keys(key)
        plain = bytes.fromhex("0123456789abcdef")
        encrypted = crypto._des_block(plain, keys, False)
        self.assertEqual(encrypted.hex(), "85e813540f0ab405")
        self.assertEqual(crypto._des_block(encrypted, keys, True), plain)

    def test_gw5_password_round_trip(self):
        user_id = 12345678
        password = b"camera123\0\0\0\0\0\0\0"
        seed = (user_id | 0x80000000) & 0xFFFFFFFF
        xor_key = crypto._gw5_password_key(seed)
        des_keys = crypto._des_keys(crypto._DES_KEY)
        encoded = bytearray()
        for offset in range(0, len(password), 8):
            encrypted = crypto._des_block(password[offset:offset + 8], des_keys, True)
            encoded.extend(a ^ b for a, b in zip(encrypted, xor_key))
        secret_key = base64.b64encode(encoded).decode()
        self.assertEqual(crypto.decrypt_gw5(str(user_id), secret_key), "camera123")

    def test_gw5_vector(self):
        self.assertEqual(crypto.decrypt_gw5("12345678", "WQGS+FmfdcM="), "camera12")
        self.assertEqual(crypto.decrypt_gw5("012345678", "WQGS+FmfdcM="), "camera12")

    def test_gw5_rejects_bad_input(self):
        for uid, secret in (("x", "AAAA"), ("1", "not base64"), ("1", "AQ==")):
            with self.subTest(uid=uid, secret=secret), self.assertRaises(ValueError):
                crypto.decrypt_gw5(uid, secret)


if __name__ == "__main__":
    unittest.main()
