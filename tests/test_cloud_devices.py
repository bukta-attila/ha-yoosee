import base64
import hashlib
import hmac
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import _support

if "yoosee_cloud" not in sys.modules:
    _pkg = types.ModuleType("yoosee_cloud")
    _pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components" / "yoosee_cloud")]
    sys.modules["yoosee_cloud"] = _pkg

from yoosee_cloud import cloud_devices
from gwell import account as A
from gwell import crypto
from gwell.cloud import P2PAccount

KEYS = A.AppKeys("app-id", "iot-app-id", "app-token")
TOKEN = bytes(range(64)).hex().upper() + "ffff"
ACC = P2PAccount(0x81123456, 1, 2, 3, 4, "19088743", "s-id", "16140901064495857664", TOKEN, "HU", "EU")


def _gw5(user_id: str, password: bytes) -> str:
    seed = (int(user_id) | 0x80000000) & 0xFFFFFFFF
    xor_key = crypto._gw5_password_key(seed)
    des_keys = crypto._des_keys(crypto._DES_KEY)
    out = bytearray()
    for i in range(0, len(password), 8):
        out.extend(a ^ b for a, b in zip(crypto._des_block(password[i:i + 8], des_keys, True), xor_key))
    return base64.b64encode(out).decode()


def _response(secret):
    return {"code": 0, "data": {"deviceList": [
        {"devId": "42", "gwell": {"secretKey": "other"}},
        {"devId": "10000001", "secretKey": "top", "gwell": {"secretKey": secret}},
    ]}}


class _Open:
    def __init__(self, payload):
        self.payload = payload
        self.seen = []

    def __call__(self, request, timeout):
        self.seen.append((request, timeout))
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(self.payload).encode()
        resp.__enter__.return_value = resp
        return resp


class ParseTest(unittest.TestCase):
    def test_uses_gwell_secret_of_target_with_legacy_user_id(self):
        secret = _gw5("019088743", b"camera12")
        self.assertEqual(cloud_devices.parse_device_password(_response(secret), 10000001, ACC.legacy_user_id),
                         "camera12")

    def test_falls_back_to_shared_user_id(self):
        secret = _gw5(cloud_devices.FALLBACK_USER_ID, b"shared01")
        with self.assertRaises(ValueError):
            crypto.decrypt_gw5("019088743", secret)
        self.assertEqual(cloud_devices.parse_device_password(_response(secret), 10000001, "019088743"), "shared01")

    def test_device_names(self):
        resp = _response("x")
        resp["data"]["deviceList"][0]["remarkName"] = "Garden"
        resp["data"]["deviceList"].append({"devId": None})
        self.assertEqual(cloud_devices.device_names(resp), {"42": "Garden", "10000001": "10000001"})

    def test_errors(self):
        with self.assertRaises(cloud_devices.DeviceListError):
            cloud_devices.parse_device_password({"code": 10007, "msg": "x", "data": None}, 10000001, "1")
        with self.assertRaises(cloud_devices.DeviceListError):
            cloud_devices.parse_device_password(_response("x"), 999, "1")
        with self.assertRaises(cloud_devices.DeviceListError):
            cloud_devices.parse_device_password(_response("AAAAAAAAAAA="), 10000001, "1")


class OpenApiRequestTest(unittest.TestCase):
    def test_signing_key_is_token_bytes_48_to_64(self):
        self.assertEqual(A.token_signing_key(TOKEN), bytes(range(48, 64)))
        self.assertEqual(A.token_signing_key(TOKEN.lower()), bytes(range(48, 64)))
        for bad in ("ab" * 63, "zz" * 64):
            with self.subTest(bad=bad[:4]), self.assertRaises(ValueError):
                A.token_signing_key(bad)

    def test_single_signed_post(self):
        opener = _Open(_response(_gw5("019088743", b"camera12")))
        with mock.patch.object(cloud_devices.urllib.request, "urlopen", opener):
            response = cloud_devices.load_device_list(ACC, KEYS, "uuid-1", "hu")
        self.assertEqual(cloud_devices.parse_device_password(response, 10000001, ACC.legacy_user_id), "camera12")
        self.assertEqual(len(opener.seen), 1)
        request, timeout = opener.seen[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://openapi-iot.cloudlinks.cn/openapi/v2/user/device/list")
        self.assertEqual(timeout, cloud_devices.TIMEOUT_S)
        h = {k.lower(): v for k, v in request.header_items()}
        body = json.loads(request.data)
        self.assertEqual(body["region"], "HU")
        self.assertEqual(body["lastDeviceId"], 0)
        self.assertEqual(body["accessId"], 0)
        self.assertEqual((body["appId"], body["appToken"], body["appName"], body["pkgName"]),
                         ("app-id", "app-token", "Yoosee", "com.yoosee"))
        self.assertEqual((body["terminalOS"], body["apiVersion"], body["platform"], body["channel"],
                          body["appVersion"], body["funcSupport"], body["regRegion"], body["language"]),
                         (3, 2, 1, "googleplay", "3016239", 7, "EU", "en"))
        self.assertEqual((h["x-iotvideo-accessid"], h["x-iotvideo-accesstoken"], h["x-iotvideo-area"],
                          h["x-iotvideo-appid"], h["x-iotvideo-appver"], h["x-iotvideo-uniqueid"]),
                         (ACC.access_id, TOKEN, "HU", "iot-app-id", "3016239", "uuid-1"))
        self.assertNotIn("x-iotvideo-anonymous", h)
        msg = "\n".join([
            "host:openapi-iot.cloudlinks.cn",
            "payload:" + hashlib.sha256(request.data).hexdigest(),
            "x-iotvideo-accessid:" + ACC.access_id,
            "x-iotvideo-nonce:" + h["x-iotvideo-nonce"],
            "x-iotvideo-timestamp:" + h["x-iotvideo-timestamp"],
        ])
        expected = base64.b64encode(hmac.new(bytes(range(48, 64)), msg.encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(h["x-iotvideo-signature"], expected)

    def test_access_id_long_or_zero(self):
        body = A.device_list_body(KEYS, ACC, "HU", "en")
        self.assertEqual(body["accessId"], 0)
        small = P2PAccount(1, 1, 2, 3, 4, access_id="-123", access_token=TOKEN)
        self.assertEqual(A.device_list_body(KEYS, small, "HU", "en")["accessId"], -123)

    def test_missing_token_is_error(self):
        with self.assertRaises(cloud_devices.DeviceListError):
            cloud_devices.device_list_request(P2PAccount(1, 1, 2, 3, 4, "1", "2"), KEYS, "u", "HU")


if __name__ == "__main__":
    unittest.main()
