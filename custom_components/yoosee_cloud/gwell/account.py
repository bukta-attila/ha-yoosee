from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import time
import uuid
from dataclasses import dataclass

from .cloud import P2PAccount

LOGIN_URL = "https://openapi-iot.cloudlinks.cn/openapi/app/user/login/account"
LOGIN_HOST = "openapi-iot.cloudlinks.cn"
DEVICE_LIST_URL = "https://openapi-iot.cloudlinks.cn/openapi/v2/user/device/list"
DEVICE_LIST_HOST = "openapi-iot.cloudlinks.cn"
APP_NAME = "Yoosee"
PKG_NAME = "com.yoosee"
APP_VERSION = "3016239"
API_VERSION = 2
PLATFORM = 1
TERMINAL_OS = 3
FUNC_SUPPORT = 1
CHANNEL = "google"
DEVICE_PLATFORM = 1
DEVICE_CHANNEL = "googleplay"
DEVICE_FUNC_SUPPORT = 7

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_M64 = (1 << 64) - 1


@dataclass(frozen=True)
class AppKeys:
    app_id: str
    iot_video_app_id: str
    app_token: str


class LoginError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"login failed: code={code} msg={msg}")
        self.code = code
        self.msg = msg


def password_hash(password: str) -> str:
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def version_code(name: str) -> int:
    a, b, c, d = (int(x) for x in name.split("."))
    return (a << 24) | (b << 16) | (c << 8) | d


def java_string_hash(s: str) -> int:
    h = 0
    data = s.encode("utf-16-be")
    for i in range(0, len(data), 2):
        h = (h * 31 + int.from_bytes(data[i:i + 2], "big")) & 0xFFFFFFFF
    return h - (1 << 32) if h & 0x80000000 else h


def phone_uuid(seed: str) -> str:
    msb = java_string_hash(seed) & _M64
    lsb = -905839116 & _M64
    return str(uuid.UUID(int=(msb << 64) | lsb))


def bkdr32(s: bytes) -> int:
    h = 0
    for b in s:
        h = (h * 131 + b) & 0xFFFFFFFF
    return h


def _xor_chain(buf: bytearray) -> None:
    n = len(buf)
    for i in range(n):
        buf[i] ^= buf[(i + 1) % n]


def anonymous_access_id(tag: str, unix_s: int, rnd: int) -> int:
    v = (0xE0 << 56) | ((bkdr32(tag.encode()) & 0xFFFFFF) << 32)
    v |= ((((unix_s & 0xFFFFFFFF) // 60) << 5) | (rnd & 0x1F)) & 0xFFFFFFFF
    b = bytearray(v.to_bytes(8, "little"))
    mid = bytearray(b[1:7])
    _xor_chain(mid)
    b[1:7] = mid
    return int.from_bytes(b, "little")


def tostr_32digit(v: int) -> str:
    x = (v ^ 0x7E18FC2D035A4B69) & _M64
    out = []
    for shift in range(60, -1, -5):
        d = (x >> shift) & 0x1F
        if d or out:
            out.append(_B32[d])
    return "".join(out)


def anonymous_secure_key(tag: str, unix_s: int, rnd: int) -> tuple[str, str]:
    v = anonymous_access_id(tag, unix_s, rnd)
    secret = hmac.new(tag.encode(), tostr_32digit(v).encode(), hashlib.md5).hexdigest()
    return str(v), secret


def to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _long_or_zero(s: str) -> int:
    try:
        n = int(s)
    except (TypeError, ValueError):
        return 0
    return n if -(1 << 63) <= n < (1 << 63) else 0


def login_body(keys: AppKeys, email: str, password: str, unique_id: str, login_region: str,
               access_id: str, language: str, region: str, reg_region: str = "") -> dict:
    return {
        "email": email,
        "pwd": password_hash(password),
        "uniqueId": unique_id,
        "loginRegion": login_region,
        "loginMode": "email",
        "language": language,
        "terminalOS": TERMINAL_OS,
        "pkgName": PKG_NAME,
        "appName": APP_NAME,
        "appId": keys.app_id,
        "appToken": keys.app_token,
        "apiVersion": API_VERSION,
        "platform": PLATFORM,
        "channel": CHANNEL,
        "region": region,
        "regRegion": reg_region,
        "accessId": _long_or_zero(access_id),
        "appVersion": APP_VERSION,
        "funcSupport": FUNC_SUPPORT,
    }


def _sign(pairs: dict[str, str], key: bytes) -> str:
    msg = "\n".join(f"{k}:{pairs[k]}" for k in sorted(pairs))
    mac = hmac.new(key, msg.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(mac).decode()


def signature(pairs: dict[str, str], secret: str) -> str:
    return _sign(pairs, secret.encode("utf-8"))


def token_signing_key(access_token: str) -> bytes:
    if not isinstance(access_token, str) or len(access_token) < 128:
        raise ValueError("access token is too short")
    try:
        raw = bytes.fromhex(access_token[:128])
    except ValueError:
        raise ValueError("access token is not hex") from None
    return raw[48:64]


def token_signature(pairs: dict[str, str], access_token: str) -> str:
    return _sign(pairs, token_signing_key(access_token))


def build_login_request(keys: AppKeys, email: str, password: str, *, unique_id: str, login_region: str,
                        language: str, region: str, area: str, now_ms: int | None = None,
                        nonce: int | None = None, rnd: int | None = None) -> tuple[dict[str, str], bytes]:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    nonce = random.randrange(0, 0x7FFFFFFF) if nonce is None else nonce
    rnd = random.getrandbits(5) if rnd is None else rnd
    access_id, secret = anonymous_secure_key(keys.iot_video_app_id + APP_VERSION, now_ms // 1000, rnd)
    body = to_json(login_body(keys, email, password, unique_id, login_region, access_id, language, region))
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-iotvideo-anonymous": "anonymous",
        "x-iotvideo-accessid": access_id,
        "x-iotvideo-nonce": str(nonce),
        "x-iotvideo-timestamp": str((now_ms + 999) // 1000),
        "x-iotvideo-area": area,
        "x-iotvideo-appver": APP_VERSION,
        "x-iotvideo-uniqueid": unique_id,
        "x-iotvideo-appid": keys.iot_video_app_id,
        "x-iotvideo-accesstoken": "",
    }
    pairs = {
        "host": LOGIN_HOST,
        "x-iotvideo-accessid": access_id,
        "x-iotvideo-nonce": headers["x-iotvideo-nonce"],
        "x-iotvideo-timestamp": headers["x-iotvideo-timestamp"],
        "x-iotvideo-appid": keys.iot_video_app_id,
        "x-iotvideo-appver": APP_VERSION,
        "payload": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    headers["x-iotvideo-signature"] = signature(pairs, secret)
    return headers, body.encode("utf-8")


def device_list_body(keys: AppKeys, account: P2PAccount, region: str, language: str,
                     last_device_id: int = 0) -> dict:
    body = {
        "region": region,
        "lastDeviceId": last_device_id,
        "language": language,
        "terminalOS": TERMINAL_OS,
        "pkgName": PKG_NAME,
        "appVersion": APP_VERSION,
        "appName": APP_NAME,
        "appId": keys.app_id,
        "appToken": keys.app_token,
        "apiVersion": API_VERSION,
        "platform": DEVICE_PLATFORM,
        "channel": DEVICE_CHANNEL,
    }
    if account.reg_region:
        body["regRegion"] = account.reg_region
    body["accessId"] = _long_or_zero(account.access_id)
    body["funcSupport"] = DEVICE_FUNC_SUPPORT
    return body


def build_device_list_request(keys: AppKeys, account: P2PAccount, *, unique_id: str, region: str, language: str,
                              last_device_id: int = 0, now_ms: int | None = None,
                              nonce: int | None = None) -> tuple[dict[str, str], bytes]:
    if not account.access_id or not account.access_token:
        raise ValueError("login response is missing the access token")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    nonce = random.randrange(0, 0x80000000) if nonce is None else nonce
    body = to_json(device_list_body(keys, account, region, language, last_device_id))
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-iotvideo-accessid": account.access_id,
        "x-iotvideo-nonce": str(nonce),
        "x-iotvideo-timestamp": str(now_ms // 1000),
        "x-iotvideo-area": account.area or "",
        "x-iotvideo-accesstoken": account.access_token,
        "x-iotvideo-appver": APP_VERSION,
        "x-iotvideo-appid": keys.iot_video_app_id,
        "x-iotvideo-uniqueid": unique_id,
    }
    pairs = {
        "host": DEVICE_LIST_HOST,
        "x-iotvideo-accessid": account.access_id,
        "x-iotvideo-nonce": headers["x-iotvideo-nonce"],
        "x-iotvideo-timestamp": headers["x-iotvideo-timestamp"],
        "payload": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    headers["x-iotvideo-signature"] = token_signature(pairs, account.access_token)
    return headers, body.encode("utf-8")


def _opt_str(v) -> str | None:
    return None if v is None else str(v)


def _java_int(s) -> int:
    try:
        t = str(s)
        n = int(t)
    except (TypeError, ValueError):
        return 0
    if not t.lstrip("-").isdigit():
        return 0
    return n if -(1 << 31) <= n < (1 << 31) else 0


def _s32(n: int) -> int:
    n &= 0xFFFFFFFF
    return n - (1 << 32) if n & 0x80000000 else n


def parse_login_response(resp: dict) -> P2PAccount:
    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, dict) or data.get("userId") is None:
        code = resp.get("code") if isinstance(resp, dict) else None
        msg = resp.get("msg") if isinstance(resp, dict) else None
        raise LoginError(code, msg)
    raw_uid = str(data["userId"])
    uid = _s32(_java_int(raw_uid) | 0x80000000)
    return P2PAccount(uid, _java_int(data.get("sessionId")), _java_int(data.get("sessionId2")),
                      _java_int(data.get("vcode1")), _java_int(data.get("vcode2")),
                      raw_uid, str(data.get("sessionId", "")), _opt_str(data.get("accessId")),
                      _opt_str(data.get("accessToken")), _opt_str(data.get("area")), _opt_str(data.get("regRegion")))
