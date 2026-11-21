from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from datetime import date

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import local
from .cloud_devices import DeviceListError, fetch_device_list, parse_device_password
from .const import (
    CONF_ACCOUNT, CONF_ACCOUNT_PASSWORD, CONF_DEVICE_ID, CONF_LIVE, CONF_LOGIN_REGION, CONF_P2P, CONF_RTSP_PASSWORD,
    CONF_SOURCE, DAYLIST_SEARCH_DAYS, DISCOVERY_TTL_S, IDLE_CALL_S, IDLE_CLIENT_S, KEYFRAME_TIMEOUT_S, LIST_TTL_PAST_S,
    LIST_TTL_TODAY_S, LIVE_AUDIO_WAIT_US, LIVE_GOP_MAX, LIVE_QUEUE, RTSP_FALLBACK_S, SOURCE_AUTO, SOURCE_CLOUD,
    SUBENTRY_CAMERA,
)
from .gwell import gute
from .gwell.audio import AacEncoder
from .gwell.av import AudioUnit
from .gwell.client import GwellClient, GwellError, SessionExpired
from .gwell.flv import FlvMuxer
from .gwell.playback import Unsupported, stream_fmp4, stream_live
from .login import LoginFailed, LoginUnavailable, account_from_dict, account_login, account_to_dict, load_app_keys

_LOGGER = logging.getLogger(__name__)


def camera_subentries(entry: ConfigEntry) -> dict:
    return {s.subentry_id: s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_CAMERA}


class Account:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._lock = asyncio.Lock()
        self._list: dict | None = None
        self._addr_lock = asyncio.Lock()
        self._addrs: dict[int, str] = {}
        self._addr_at = 0.0
        self.options = dict(entry.options)
        subentries = camera_subentries(entry)
        self.subentry_state = {sid: dict(s.data) for sid, s in subentries.items()}
        self.cameras = {int(s.data[CONF_DEVICE_ID]): Controller(self, int(s.data[CONF_DEVICE_ID]), s)
                        for s in subentries.values()}

    async def account(self, force: bool = False):
        d = self.entry.data
        acc = None if force else account_from_dict(d.get(CONF_P2P))
        if acc is None or not acc.access_id or not acc.access_token:
            keys = await asyncio.to_thread(load_app_keys, self.entry.options)
            try:
                acc = await account_login(keys, d[CONF_ACCOUNT], d[CONF_ACCOUNT_PASSWORD], d[CONF_LOGIN_REGION])
            except LoginFailed:
                self.entry.async_start_reauth(self.hass)
                raise
            self.hass.config_entries.async_update_entry(self.entry, data={**d, CONF_P2P: account_to_dict(acc)})
        return acc

    async def device_list(self, force: bool = False) -> dict:
        async with self._lock:
            if self._list is None or force:
                keys = await asyncio.to_thread(load_app_keys, self.entry.options)
                d = self.entry.data
                for relogin in (False, True):
                    acc = await self.account(relogin)
                    try:
                        self._list = await fetch_device_list(acc, keys, d[CONF_ACCOUNT], d[CONF_LOGIN_REGION])
                        break
                    except DeviceListError:
                        if relogin:
                            raise
            return self._list

    async def password(self, dev_id: int) -> str:
        acc = await self.account()
        return parse_device_password(await self.device_list(), dev_id, acc.legacy_user_id or "")

    async def address(self, dev_id: int, force: bool = False) -> str | None:
        async with self._addr_lock:
            if force or time.monotonic() - self._addr_at > DISCOVERY_TTL_S:
                try:
                    found = await local.discover(dev_id)
                except OSError as e:
                    _LOGGER.debug("discovery failed: %s", e)
                    found = {}
                self._addrs.pop(dev_id, None)
                self._addrs.update(found)
                self._addr_at = time.monotonic()
            return self._addrs.get(dev_id)

    async def release(self, keep: Controller) -> None:
        for c in self.cameras.values():
            if c is not keep and c.client:
                await c.shutdown()

    async def shutdown(self) -> None:
        for c in self.cameras.values():
            await c.shutdown()


class Controller:
    def __init__(self, account: Account, dev_id: int, subentry) -> None:
        self.hub = account
        self.hass = account.hass
        self.entry = account.entry
        self.subentry = subentry
        self.dev_id = dev_id
        self._device_password: str | None = None
        self.client: GwellClient | None = None
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, object]] = {}
        self._last_use = time.monotonic()
        self._active: asyncio.Task | None = None
        self._streams = 0
        self._idle_task: asyncio.Task | None = None
        self.state = "idle"
        self._live: asyncio.Task | None = None
        self._subs: set[asyncio.Queue] = set()
        self._head: bytes | None = None
        self._gop: list[bytes] = []
        self._gop_size = 0
        self.keyframe: bytes | None = None
        self.keyframe_seq = 0
        self.ffmpeg = "ffmpeg"
        self._rtsp_failed_at: float | None = None

    @property
    def name(self) -> str:
        return self.subentry.title or str(self.dev_id)

    @property
    def live_camera(self) -> str | None:
        return self.subentry.data.get(CONF_LIVE)

    @property
    def source(self) -> str:
        return self.subentry.data.get(CONF_SOURCE, SOURCE_AUTO)

    async def rtsp_url(self, fresh: bool = False) -> str | None:
        source = self.source
        failed = self._rtsp_failed_at
        if source == SOURCE_CLOUD or source == SOURCE_AUTO and failed and time.monotonic() - failed < RTSP_FALLBACK_S:
            return None
        if not (host := await self.hub.address(self.dev_id, force=fresh or bool(failed))):
            return None
        password = self.subentry.data.get(CONF_RTSP_PASSWORD)
        if not password:
            try:
                password = await self.hub.password(self.dev_id)
            except LoginUnavailable as e:
                _LOGGER.debug("no device password for rtsp: %s", e)
                return None
        return local.rtsp_url(host, password)

    async def rtsp_moved(self) -> bool:
        old = self.hub._addrs.get(self.dev_id)
        return await self.hub.address(self.dev_id, force=True) not in (None, old)

    def rtsp_ok(self) -> None:
        if self._rtsp_failed_at:
            _LOGGER.info("camera %s: local live view works again", self.dev_id)
        self._rtsp_failed_at = None

    def rtsp_broken(self) -> None:
        if not self._rtsp_failed_at:
            _LOGGER.warning("camera %s: no picture over local RTSP, %s", self.dev_id,
                            "using the cloud" if self.source == SOURCE_AUTO else "will retry")
        self._rtsp_failed_at = time.monotonic()

    @property
    def live(self) -> bool:
        return self._live is not None and not self._live.done()

    def _busy(self) -> bool:
        return any(c._active and not c._active.done() for c in self.hub.cameras.values())

    async def _client(self) -> GwellClient:
        self._last_use = time.monotonic()
        async with self._lock:
            if self.client and self.client.alive:
                return self.client
            if self.client:
                await self.client.stop()
                self.client = None
            await self.hub.release(self)
            self.state = "connecting"
            acc = await self.hub.account()
            if self._device_password is None:
                self._device_password = await self.hub.password(self.dev_id)
            for force in (False, True):
                if force:
                    acc = await self.hub.account(force=True)
                c = GwellClient(acc, self.dev_id, self._device_password)
                try:
                    await c.start()
                except SessionExpired:
                    if force:
                        self.state = "error"
                        raise
                    continue
                except Exception:
                    self.state = "error"
                    raise
                self.client = c
                self.state = "ready"
                self._ensure_idle_watch()
                return c
        raise GwellError("unreachable")

    def _ensure_idle_watch(self) -> None:
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = self.entry.async_create_background_task(self.hass, self._idle_watch(), "yoosee idle")

    async def _idle_watch(self) -> None:
        while self.client:
            await asyncio.sleep(10)
            idle = time.monotonic() - self._last_use
            c = self.client
            if c is None or self._streams:
                continue
            if c.media and idle > IDLE_CALL_S:
                await c.media.close()
            if idle > IDLE_CLIENT_S:
                async with self._lock:
                    if self.client is c and not self._streams:
                        await c.stop()
                        self.client = None
                        self.state = "idle"

    def _cached(self, key: str, ttl: float):
        hit = self._cache.get(key)
        return hit[1] if hit and time.monotonic() - hit[0] < ttl else None

    async def days(self) -> list[date]:
        if (hit := self._cached("days", LIST_TTL_TODAY_S)) is not None:
            return hit
        c = await self._client()
        days = sorted(set(await c.day_list(dt_util.now().date(), DAYLIST_SEARCH_DAYS)))
        self._cache["days"] = (time.monotonic(), days)
        return days

    async def recordings(self, day: date) -> list[gute.Recording]:
        key = "rec:" + day.isoformat()
        ttl = LIST_TTL_TODAY_S if day >= dt_util.now().date() else LIST_TTL_PAST_S
        if (hit := self._cached(key, ttl)) is not None:
            return hit
        c = await self._client()
        recs = sorted(await c.record_list(day), key=lambda r: r.start)
        self._cache[key] = (time.monotonic(), recs)
        return recs

    async def stop_stream(self, old: asyncio.Task | None = None) -> None:
        old = old or self._active
        if old and not old.done() and old is not asyncio.current_task():
            old.cancel()
            await asyncio.wait([old], timeout=5)

    async def stop_live(self) -> None:
        task = self._live
        if task and not task.done():
            task.cancel()
            await asyncio.wait([task], timeout=5)

    async def _media(self):
        c = await self._client()
        for attempt in (0, 1):
            try:
                return await c.call()
            except GwellError:
                if attempt:
                    raise
                if c.media:
                    await c.media.close()

    async def stream(self, descriptor: bytes, offset_s: int = 0) -> AsyncIterator[bytes]:
        old = self._active
        me = asyncio.current_task()
        self._active = me
        self._streams += 1
        try:
            if old:
                await self.stop_stream(old)
            for c in self.hub.cameras.values():
                await c.stop_live()
            media = await self._media()
            self.state = "streaming"
            gen = stream_fmp4(media, descriptor, offset_s, ffmpeg=self.ffmpeg)
            try:
                async for chunk in gen:
                    self._last_use = time.monotonic()
                    yield chunk
            finally:
                await gen.aclose()
        finally:
            self._streams -= 1
            if self._active is me:
                self._active = None
                self.state = "ready" if self.client else "idle"

    async def live_stream(self) -> AsyncIterator[bytes]:
        if self._busy():
            raise GwellError("playback in progress")
        q: asyncio.Queue = asyncio.Queue(LIVE_QUEUE)
        if self.live:
            if self._head:
                q.put_nowait((self._head, None))
            for t in self._gop:
                q.put_nowait((t, bool(t is self._gop[0])))
        else:
            self._live = self.entry.async_create_background_task(self.hass, self._run_live(), "yoosee live")
        self._subs.add(q)
        self._streams += 1
        started = False
        try:
            while True:
                item = await q.get()
                if item is None:
                    return
                if isinstance(item, Exception):
                    raise item
                data, key = item
                if key is False and not started:
                    continue
                started = started or bool(key)
                self._last_use = time.monotonic()
                yield data
        finally:
            self._subs.discard(q)
            self._streams -= 1
            if not self._subs and self._live and not self._live.done():
                self._live.cancel()

    def _send(self, item) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                _LOGGER.debug("live consumer too slow, dropped")
                self._subs.discard(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)

    def _emit(self, data: bytes, key: bool | None) -> None:
        if key:
            self._gop, self._gop_size = [data], len(data)
        elif key is False and self._gop and self._gop_size + len(data) <= LIVE_GOP_MAX:
            self._gop.append(data)
            self._gop_size += len(data)
        elif key is False:
            self._gop, self._gop_size = [], 0
        self._send((data, key))

    async def _run_live(self) -> None:
        self._head, self._gop, self._gop_size = None, [], 0
        self.keyframe = None
        mux = FlvMuxer()
        enc = None
        pending = []
        end = None

        def on_aac(pts: int, aac: bytes) -> None:
            if (data := mux.feed_audio(pts, aac)) is not None:
                self._emit(data, False)

        try:
            media = await self._media()
            self.state = "streaming"
            gen = stream_live(media)
            try:
                async for unit in gen:
                    self._last_use = time.monotonic()
                    if isinstance(unit, AudioUnit):
                        if enc is None and mux.head is None:
                            enc = AacEncoder(self.ffmpeg, on_aac)
                            mux.audio = await enc.start()
                        if mux.audio:
                            enc.feed(unit)
                        continue
                    pending.append(unit)
                    if enc is None and mux.head is None and unit.pts - pending[0].pts < LIVE_AUDIO_WAIT_US:
                        continue
                    for u in pending:
                        for data, key in mux.feed(u):
                            if key is None:
                                self._head = mux.head
                            elif key:
                                self.keyframe = u.data
                                self.keyframe_seq += 1
                            self._emit(data, key)
                    pending.clear()
                    if mux.unsupported:
                        raise Unsupported(mux.unsupported)
            finally:
                await gen.aclose()
                if enc:
                    await enc.close()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            _LOGGER.debug("live ended: %s", e)
            end = e
        finally:
            subs, self._subs = self._subs, set()
            for q in subs:
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(end)
            self._head, self._gop, self._gop_size = None, [], 0
            if self._active is None:
                self.state = "ready" if self.client else "idle"

    async def snapshot_keyframe(self) -> bytes | None:
        if self.live and self.keyframe:
            return self.keyframe
        seq = self.keyframe_seq
        gen = self.live_stream()
        try:
            async with asyncio.timeout(KEYFRAME_TIMEOUT_S):
                async for _ in gen:
                    if self.keyframe_seq != seq:
                        break
        except TimeoutError:
            pass
        finally:
            await gen.aclose()
        key = self.keyframe if self.keyframe_seq != seq else None
        if self._live and not self._live.done() and not self._subs:
            await self.stop_live()
        c = self.client
        if c and c.media and not c.media.closed and not self._streams:
            await c.media.close()
        return key

    async def shutdown(self) -> None:
        await self.stop_stream()
        await self.stop_live()
        if self._idle_task:
            self._idle_task.cancel()
        if self.client:
            await self.client.stop()
            self.client = None
