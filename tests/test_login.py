import asyncio
import base64
import hashlib
import hmac
import json
import sys
import tempfile
import types
import unittest
from dataclasses import astuple
from pathlib import Path
from unittest import mock

import _support
from gwell import account as A

if "yoosee_cloud" not in sys.modules:
    _pkg = types.ModuleType("yoosee_cloud")
    _pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components" / "yoosee_cloud")]
    sys.modules["yoosee_cloud"] = _pkg
from yoosee_cloud import login

KEYS = A.AppKeys("a" * 32, "b" * 32, "c" * 64)


def _unchain(buf: bytes) -> bytes:
    b = bytearray(buf)
    n = len(b)
    b[n - 1] ^= b[0]
    for i in range(n - 2, -1, -1):
        b[i] ^= b[i + 1]
    return bytes(b)


def _from_32digit(s: str) -> int:
    v = 0
    for ch in s:
        v = (v << 5) | A._B32.index(ch)
    return v


class HelperTest(unittest.TestCase):
    def test_password_md5(self):
        self.assertEqual(A.password_hash("abc"), "900150983cd24fb0d6963f7d28e17f72")

    def test_version_code(self):
        self.assertEqual(A.version_code("00.46.06.47"), 3016239)
        self.assertEqual(str(A.version_code("00.46.06.47")), A.APP_VERSION)

    def test_java_hash_and_uuid(self):
        self.assertEqual(A.java_string_hash("hello"), 99162322)
        self.assertEqual(A.java_string_hash("polygenelubricants"), -2147483648)
        u = A.phone_uuid("hello")
        self.assertEqual(u, "00000000-05e9-18d2-ffff-ffffca01fdf4")

    def test_bkdr(self):
        self.assertEqual(A.bkdr32(b""), 0)
        self.assertEqual(A.bkdr32(b"ab"), (97 * 131 + 98))


class AnonymousKeyTest(unittest.TestCase):
    TAG = "d" * 32 + A.APP_VERSION

    def test_access_id_fields(self):
        now, rnd = 1_790_000_000, 0x15
        v = A.anonymous_access_id(self.TAG, now, rnd)
        b = v.to_bytes(8, "little")
        self.assertEqual(b[7], 0xE0)
        raw = b[0:1] + _unchain(b[1:7]) + b[7:8]
        r = int.from_bytes(raw, "little")
        self.assertEqual((r >> 32) & 0xFFFFFF, A.bkdr32(self.TAG.encode()) & 0xFFFFFF)
        self.assertEqual(r & 0xFFFFFFFF, ((now // 60) << 5) | rnd)

    def test_32digit_roundtrip(self):
        for v in (0xE012345678ABCDEF, 0x7E18FC2D035A4B69 ^ 1, 1 << 63):
            s = A.tostr_32digit(v)
            self.assertTrue(set(s) <= set(A._B32))
            self.assertEqual(_from_32digit(s), v ^ 0x7E18FC2D035A4B69)
        self.assertEqual(A.tostr_32digit(0x7E18FC2D035A4B69), "")

    def test_secret(self):
        acc, sec = A.anonymous_secure_key(self.TAG, 1_790_000_000, 3)
        v = int(acc)
        self.assertGreaterEqual(v, 1 << 63)
        exp = hmac.new(self.TAG.encode(), A.tostr_32digit(v).encode(), hashlib.md5).hexdigest()
        self.assertEqual(sec, exp)
        self.assertRegex(sec, "^[0-9a-f]{32}$")


class RequestTest(unittest.TestCase):
    def build(self):
        return A.build_login_request(KEYS, "user@example.com", "secret pw", unique_id="u-1", login_region="HU",
                                     language="en", region="HU", area="HU", now_ms=1_790_000_000_123,
                                     nonce=12345, rnd=7)

    def test_signature_known_input(self):
        pairs = {"x-iotvideo-nonce": "1", "host": "h", "payload": "p"}
        msg = b"host:h\npayload:p\nx-iotvideo-nonce:1"
        exp = base64.b64encode(hmac.new(b"k", msg, hashlib.sha1).digest()).decode()
        self.assertEqual(A.signature(pairs, "k"), exp)

    def test_headers_and_body(self):
        h, body = self.build()
        d = json.loads(body)
        self.assertEqual(list(d)[:5], ["email", "pwd", "uniqueId", "loginRegion", "loginMode"])
        self.assertEqual(d["pwd"], hashlib.md5(b"secret pw").hexdigest())
        self.assertEqual(d["loginMode"], "email")
        self.assertEqual(d["accessId"], 0)
        self.assertEqual((d["terminalOS"], d["apiVersion"], d["platform"], d["funcSupport"]), (3, 2, 1, 1))
        self.assertEqual((d["appVersion"], d["channel"], d["pkgName"], d["appName"]),
                         ("3016239", "google", "com.yoosee", "Yoosee"))
        self.assertNotIn(b" ", body.replace(b"secret pw", b""))
        self.assertEqual(h["x-iotvideo-timestamp"], "1790000001")
        self.assertEqual(h["x-iotvideo-nonce"], "12345")
        self.assertEqual(h["x-iotvideo-appid"], KEYS.iot_video_app_id)
        self.assertEqual(h["x-iotvideo-accesstoken"], "")
        self.assertEqual(h["x-iotvideo-anonymous"], "anonymous")
        acc, sec = A.anonymous_secure_key(KEYS.iot_video_app_id + A.APP_VERSION, 1_790_000_000, 7)
        self.assertEqual(h["x-iotvideo-accessid"], acc)
        msg = "\n".join([
            f"host:{A.LOGIN_HOST}",
            f"payload:{hashlib.sha256(body).hexdigest()}",
            f"x-iotvideo-accessid:{acc}",
            f"x-iotvideo-appid:{KEYS.iot_video_app_id}",
            f"x-iotvideo-appver:{A.APP_VERSION}",
            "x-iotvideo-nonce:12345",
            "x-iotvideo-timestamp:1790000001",
        ])
        exp = base64.b64encode(hmac.new(sec.encode(), msg.encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(h["x-iotvideo-signature"], exp)


class ResponseTest(unittest.TestCase):
    def test_parse_ok(self):
        acc = A.parse_login_response({"code": 0, "data": {
            "userId": "12345678", "sessionId": "-5", "sessionId2": "7", "vcode1": "123", "vcode2": "99999999999",
            "accessId": "x", "accessToken": "y", "area": "HU", "regRegion": "EU"}})
        self.assertEqual(acc.user_id & 0xFFFFFFFF, 0x80BC614E)
        self.assertEqual((acc.session_id1, acc.session_id2, acc.code1, acc.code2), (-5, 7, 123, 0))
        self.assertEqual((acc.cloud_user_id, acc.cloud_session_id), ("12345678", "-5"))
        self.assertEqual((acc.access_id, acc.access_token, acc.area, acc.reg_region), ("x", "y", "HU", "EU"))
        self.assertEqual(acc.legacy_user_id, "012345678")
        self.assertEqual(astuple(login.account_from_dict(login.account_to_dict(acc))), astuple(acc))

    def test_parse_error(self):
        with self.assertRaises(A.LoginError) as cm:
            A.parse_login_response({"code": 10001, "msg": "bad", "data": None})
        self.assertEqual(cm.exception.code, 10001)


class AccountLoginTest(unittest.TestCase):
    def test_flow(self):
        seen = {}

        def post(url, headers, body):
            seen.update(url=url, headers=headers, body=json.loads(body))
            return {"code": 0, "data": {"userId": "1", "sessionId": "2", "sessionId2": "3",
                                        "vcode1": "4", "vcode2": "5"}}

        with mock.patch.object(login, "_post", post):
            acc = asyncio.run(login.account_login(KEYS, " User@example.com ", "pw", "hu"))
        self.assertEqual(seen["url"], A.LOGIN_URL)
        self.assertEqual(seen["body"]["email"], "User@example.com")
        self.assertEqual(seen["body"]["loginRegion"], "HU")
        self.assertEqual(seen["headers"]["x-iotvideo-uniqueid"], A.phone_uuid(login.unique_seed("user@example.com")))
        self.assertEqual((acc.user_id & 0xFFFFFFFF, acc.session_id1, acc.code2), (0x80000001, 2, 5))

    def test_app_keys(self):
        fields = {"app_id": KEYS.app_id, "iot_video_app_id": KEYS.iot_video_app_id, "app_token": KEYS.app_token}
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "app_keys.json"
            p.write_text(json.dumps(fields))
            self.assertEqual(astuple(login.load_app_keys(None, p)), astuple(KEYS))
            self.assertEqual(astuple(login.load_app_keys({"app_id": "x", "iot_video_app_id": "", "app_token": ""}, p)), astuple(KEYS))
            self.assertEqual(astuple(login.load_app_keys({k: k for k in fields}, p)), tuple(fields))
        with self.assertRaises(login.LoginUnavailable):
            login.load_app_keys(None, Path("/nonexistent/app_keys.json"))
        self.assertTrue(all(astuple(login.load_app_keys())))

    def test_rejects_numeric_and_error_response(self):
        with self.assertRaises(login.LoginFailed):
            asyncio.run(login.account_login(KEYS, "12345", "pw", "HU"))
        with mock.patch.object(login, "_post", lambda *a: {"code": 2, "msg": "x", "data": None}):
            with self.assertRaises(login.LoginFailed):
                asyncio.run(login.account_login(KEYS, "a@b.c", "pw", "HU"))


if __name__ == "__main__":
    unittest.main()
