from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from .gwell.account import DEVICE_LIST_URL, AppKeys, build_device_list_request, phone_uuid
from .gwell.cloud import P2PAccount
from .gwell.crypto import decrypt_gw5
from .login import LoginUnavailable, account_login, unique_seed

FALLBACK_USER_ID = "0517401"
LANGUAGE = "en"
TIMEOUT_S = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
NAME_KEYS = ("remarkName", "devName", "deviceName", "name")


class DeviceListError(LoginUnavailable):
    pass


def _decode_secret(user_id: str, secret_key: str) -> str:
    for uid in dict.fromkeys((user_id, FALLBACK_USER_ID)):
        try:
            password = decrypt_gw5(uid, secret_key)
        except ValueError:
            continue
        if password and password != "0":
            return password
    raise DeviceListError("device secret key could not be decoded")


def devices(response: dict) -> list[dict]:
    data = response.get("data") if isinstance(response, dict) else None
    items = data.get("deviceList") if isinstance(data, dict) else None
    if not isinstance(items, list):
        code = response.get("code") if isinstance(response, dict) else None
        raise DeviceListError(f"device list failed (code={code})")
    return [i for i in items if isinstance(i, dict) and str(i.get("devId", "")).isdigit()]


def device_names(response: dict) -> dict[str, str]:
    names = {}
    for item in devices(response):
        dev = str(item["devId"])
        names[dev] = next((str(item[k]) for k in NAME_KEYS if item.get(k)), dev)
    return names


def find_secret_key(response: dict, device_id: int) -> str:
    for item in devices(response):
        if str(item.get("devId")) != str(device_id):
            continue
        gwell = item.get("gwell")
        secret_key = gwell.get("secretKey") if isinstance(gwell, dict) else None
        if not isinstance(secret_key, str) or not secret_key:
            raise DeviceListError("device list has no Gwell secret key for this camera")
        return secret_key
    raise DeviceListError("camera was not found in the device list")


def parse_device_password(response: dict, device_id: int, user_id: str) -> str:
    return _decode_secret(user_id, find_secret_key(response, device_id))


def _post(url: str, headers: dict[str, str], body: bytes) -> dict:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise DeviceListError(f"device list HTTP error ({e.code})") from None
    except (urllib.error.URLError, OSError) as e:
        raise DeviceListError(f"device list request failed ({type(e).__name__})") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DeviceListError("device list response is too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise DeviceListError("device list response is not valid JSON") from None


def device_list_request(account: P2PAccount, keys: AppKeys, unique_id: str,
                        region: str) -> tuple[str, dict[str, str], bytes]:
    try:
        headers, body = build_device_list_request(keys, account, unique_id=unique_id, region=region.upper(),
                                                  language=LANGUAGE)
    except ValueError as e:
        raise DeviceListError(str(e)) from None
    return DEVICE_LIST_URL, headers, body


def load_device_list(account: P2PAccount, keys: AppKeys, unique_id: str, region: str) -> dict:
    return _post(*device_list_request(account, keys, unique_id, region))


async def fetch_device_list(account: P2PAccount, keys: AppKeys, account_name: str, region: str) -> dict:
    uid = phone_uuid(unique_seed(account_name))
    response = await asyncio.to_thread(load_device_list, account, keys, uid, region)
    devices(response)
    return response


async def sign_in(keys: AppKeys, account: str, password: str, region: str) -> tuple[P2PAccount, dict]:
    acc = await account_login(keys, account, password, region)
    return acc, await fetch_device_list(acc, keys, account, region)
