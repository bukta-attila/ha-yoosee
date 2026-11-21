from __future__ import annotations

import hmac
import logging
import secrets
from datetime import date, timedelta

import voluptuous as vol
from aiohttp import web

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, LIVE_URL, STREAM_URL, STREAM_URL_TTL_S
from .controller import Controller
from .gwell import fmp4, gute
from .gwell.client import GwellError
from .login import LoginUnavailable

_LOGGER = logging.getLogger(__name__)


LIVE_TOKEN = secrets.token_urlsafe(32)

_TARGET = {vol.Optional("entry_id"): str, vol.Optional("device_id"): vol.Coerce(int)}


def _ctrl(hass: HomeAssistant, entry_id: str | None = None, device_id: int | None = None) -> Controller | None:
    for eid, account in hass.data.get(DOMAIN, {}).items():
        if entry_id and eid != entry_id:
            continue
        for dev, c in account.cameras.items():
            if device_id is None or dev == device_id:
                return c
    return None


def live_url(hass: HomeAssistant, c: Controller) -> str:
    api = hass.config.api
    scheme = "https" if api.use_ssl else "http"
    return f"{scheme}://127.0.0.1:{api.port}{LIVE_URL}/{c.entry.entry_id}/{c.dev_id}?auth={LIVE_TOKEN}"


def _hms(t) -> str:
    return "%02d:%02d:%02d" % (t.hour, t.minute, t.second)


def _err(connection, msg_id, e: Exception) -> None:
    code = "login_unavailable" if isinstance(e, LoginUnavailable) else "camera_error"
    connection.send_error(msg_id, code, str(e))


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/cameras"})
@callback
def ws_cameras(hass, connection, msg):
    cams = []
    reg = er.async_get(hass)
    for eid, account in hass.data.get(DOMAIN, {}).items():
        for dev, c in account.cameras.items():
            live = c.live_camera or reg.async_get_entity_id("camera", DOMAIN, f"{eid}_{dev}")
            cams.append({"entry_id": eid, "device_id": dev, "name": c.name, "live_camera": live})
    connection.send_result(msg["id"], {"cameras": cams})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/days", **_TARGET})
@websocket_api.async_response
async def ws_days(hass, connection, msg):
    c = _ctrl(hass, msg.get("entry_id"), msg.get("device_id"))
    if c is None:
        connection.send_error(msg["id"], "not_found", "no such camera")
        return
    try:
        days = await c.days()
    except (GwellError, LoginUnavailable, OSError) as e:
        _err(connection, msg["id"], e)
        return
    connection.send_result(msg["id"], {"days": [d.isoformat() for d in days]})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/recordings", vol.Required("date"): str, **_TARGET})
@websocket_api.async_response
async def ws_recordings(hass, connection, msg):
    c = _ctrl(hass, msg.get("entry_id"), msg.get("device_id"))
    if c is None:
        connection.send_error(msg["id"], "not_found", "no such camera")
        return
    try:
        day = date.fromisoformat(msg["date"])
        recs = await c.recordings(day)
    except ValueError:
        connection.send_error(msg["id"], "invalid_format", "date must be YYYY-MM-DD")
        return
    except (GwellError, LoginUnavailable, OSError) as e:
        _err(connection, msg["id"], e)
        return
    segs = []
    for r in recs:
        end = r.end if r.end.date() == day else r.start.replace(hour=23, minute=59, second=59)
        segs.append([_hms(r.start), _hms(end), r.descriptor.hex(), r.rec_type])
    connection.send_result(msg["id"], {"segs": segs, "imgs": []})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/clip", vol.Required("name"): str,
                                  vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0, max=86400)),
                                  **_TARGET})
@callback
def ws_clip(hass, connection, msg):
    c = _ctrl(hass, msg.get("entry_id"), msg.get("device_id"))
    try:
        gute.parse_descriptor(bytes.fromhex(msg["name"]))
    except ValueError:
        connection.send_error(msg["id"], "invalid_format", "bad recording name")
        return
    if c is None:
        connection.send_error(msg["id"], "not_found", "no such camera")
        return
    path = f"{STREAM_URL}/{c.entry.entry_id}/{c.dev_id}/{msg['name']}?offset={msg['offset']}"
    url = async_sign_path(hass, path, timedelta(seconds=STREAM_URL_TTL_S),
                          refresh_token_id=connection.refresh_token_id)
    connection.send_result(msg["id"], {"url": url})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/stop", **_TARGET})
@websocket_api.async_response
async def ws_stop(hass, connection, msg):
    c = _ctrl(hass, msg.get("entry_id"), msg.get("device_id"))
    if c:
        await c.stop_stream()
    connection.send_result(msg["id"], {})


@callback
def async_register_ws(hass: HomeAssistant) -> None:
    for cmd in (ws_cameras, ws_days, ws_recordings, ws_clip, ws_stop):
        websocket_api.async_register_command(hass, cmd)


async def _serve(request: web.Request, gen, label: str, live: bool = False) -> web.StreamResponse:
    log = _LOGGER.debug if live else _LOGGER.info
    try:
        try:
            first = await gen.__anext__()
        except StopAsyncIteration:
            log("%s: no video", label)
            return web.Response(status=502, text="no video")
        except (GwellError, LoginUnavailable, OSError) as e:
            log("%s failed: %s", label, e)
            return web.Response(status=502, text=str(e))
        headers = {"Content-Type": "video/x-flv", "Cache-Control": "no-store"}
        if not live:
            headers = {"Content-Type": "video/mp4", "Cache-Control": "no-store",
                       "X-Codecs": fmp4.codec_from_init(first) or "avc1.42E01E"}
        resp = web.StreamResponse(headers=headers)
        await resp.prepare(request)
        try:
            await resp.write(first)
            async for chunk in gen:
                await resp.write(chunk)
            await resp.write_eof()
        except (ConnectionResetError, GwellError) as e:
            _LOGGER.debug("stream ended: %s", e)
        return resp
    finally:
        await gen.aclose()


class StreamView(HomeAssistantView):
    url = STREAM_URL + "/{entry_id}/{device_id}/{name}"
    name = f"api:{DOMAIN}:stream"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, device_id: str, name: str) -> web.StreamResponse:
        hass = request.app["hass"]
        try:
            c = _ctrl(hass, entry_id, int(device_id))
            desc = bytes.fromhex(name)
            gute.parse_descriptor(desc)
            offset = max(0, min(86400, int(request.query.get("offset", "0"))))
        except ValueError:
            return web.Response(status=400)
        if c is None:
            return web.Response(status=404)
        return await _serve(request, c.stream(desc, offset), "playback %s offset %d" % (name, offset))


class LiveView(HomeAssistantView):
    url = LIVE_URL + "/{entry_id}/{device_id}"
    name = f"api:{DOMAIN}:live"
    requires_auth = False

    async def get(self, request: web.Request, entry_id: str, device_id: str) -> web.StreamResponse:
        token = request.query.get("auth", "")
        if not hmac.compare_digest(token.encode(), LIVE_TOKEN.encode()) or not device_id.isdigit():
            return web.Response(status=404)
        c = _ctrl(request.app["hass"], entry_id, int(device_id))
        if c is None:
            return web.Response(status=404)
        return await _serve(request, c.live_stream(), "live %s" % device_id, live=True)
