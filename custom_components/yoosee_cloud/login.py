from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path

from .gwell.account import LOGIN_URL, AppKeys, LoginError, build_login_request, parse_login_response, phone_uuid
from .const import DOMAIN, KEY_FIELDS
from .gwell.cloud import P2PAccount

APP_KEYS_FILE = Path(__file__).parent / "app_keys.json"
LANGUAGE = "en"
TIMEOUT_S = 20


class LoginUnavailable(Exception):
    pass


class LoginFailed(LoginUnavailable):
    pass


def unique_seed(account: str) -> str:
    return f"homeassistant-{DOMAIN}-{account.strip().lower()}"


def load_app_keys(override: dict | None = None, path: Path = APP_KEYS_FILE) -> AppKeys:
    if override and all(override.get(k) for k in KEY_FIELDS):
        return AppKeys(*(str(override[k]).strip() for k in KEY_FIELDS))
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return AppKeys(*(str(d[k]) for k in KEY_FIELDS))
    except (OSError, ValueError, KeyError) as e:
        raise LoginUnavailable(f"app keys missing or invalid: {path.name}") from e


def _post(url: str, headers: dict[str, str], body: bytes) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except ValueError:
            raise LoginUnavailable(f"HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LoginUnavailable(f"login request failed: {type(e).__name__}") from None


async def account_login(keys: AppKeys, account: str, password: str, login_region: str) -> P2PAccount:
    account = account.strip()
    if account.lstrip("+-").isdigit():
        raise LoginFailed("only e-mail login is supported")
    region = login_region.upper()
    headers, body = build_login_request(keys, account, password, unique_id=phone_uuid(unique_seed(account)),
                                        login_region=region, language=LANGUAGE, region=region, area=region)
    resp = await asyncio.to_thread(_post, LOGIN_URL, headers, body)
    try:
        return parse_login_response(resp)
    except LoginError as e:
        raise LoginFailed(str(e)) from None


_OPTIONAL = ("cloud_user_id", "cloud_session_id", "access_id", "access_token", "area", "reg_region")


def account_from_dict(d: dict | None) -> P2PAccount | None:
    if not d:
        return None
    try:
        extra = {k: str(d[k]) if d.get(k) is not None else None for k in _OPTIONAL}
        return P2PAccount(int(d["user_id"]), int(d["session_id1"]), int(d["session_id2"]),
                          int(d["code1"]), int(d["code2"]), **extra)
    except (KeyError, TypeError, ValueError):
        return None


def account_to_dict(a: P2PAccount) -> dict:
    result = {"user_id": a.user_id, "session_id1": a.session_id1, "session_id2": a.session_id2,
              "code1": a.code1, "code2": a.code2}
    for k in _OPTIONAL:
        v = getattr(a, k)
        if v is not None:
            result[k] = v
    return result
