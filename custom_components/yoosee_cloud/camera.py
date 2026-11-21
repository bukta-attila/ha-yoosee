from __future__ import annotations

import asyncio
import contextvars
import logging
import time

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.stream import CONF_RTSP_TRANSPORT, Stream
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import live_url
from .const import CONF_DEVICE_ID, DOMAIN, RTSP_SNAPSHOT_TTL_S, SNAPSHOT_TTL_S, SOURCE_LOCAL
from .controller import Controller, camera_subentries
from .gwell.client import GwellError
from .login import LoginUnavailable

_LOGGER = logging.getLogger(__name__)
_HA_STREAM = contextvars.ContextVar("yoosee_ha_stream", default=False)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    account = hass.data[DOMAIN][entry.entry_id]
    binary = get_ffmpeg_manager(hass).binary
    for sid, subentry in camera_subentries(entry).items():
        c = account.cameras.get(int(subentry.data[CONF_DEVICE_ID]))
        if c is None:
            continue
        c.ffmpeg = binary
        async_add_entities([YooseeCamera(c)], config_subentry_id=sid)


async def _jpeg(ffmpeg: str, source: list[str], data: bytes | None = None) -> bytes | None:
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-loglevel", "error", *source, "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg",
        "pipe:1", stdin=asyncio.subprocess.PIPE if data else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(data), 10)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    return out or None


async def to_jpeg(ffmpeg: str, h264: bytes) -> bytes | None:
    return await _jpeg(ffmpeg, ["-f", "h264", "-i", "pipe:0"], h264)


async def rtsp_jpeg(ffmpeg: str, url: str) -> bytes | None:
    return await _jpeg(ffmpeg, ["-rtsp_transport", "udp", "-timeout", "3000000", "-analyzeduration", "500000", "-probesize", "32768",
                                "-allowed_media_types", "video", "-i", url])


def go2rtc_source(url: str) -> str:
    return f"ffmpeg:{url}#input=rtsp/udp"


class YooseeCamera(Camera):
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_has_entity_name = True

    def __init__(self, ctrl: Controller) -> None:
        super().__init__()
        self._ctrl = ctrl
        self._attr_unique_id = f"{ctrl.entry.entry_id}_{ctrl.dev_id}"
        self._attr_name = None
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, self._attr_unique_id)}, name=ctrl.name,
                                            manufacturer="Yoosee")
        self._image: bytes | None = None
        self._image_at = 0.0
        self._image_seq = -1
        self._image_lock = asyncio.Lock()
        self.stream_options = {CONF_RTSP_TRANSPORT: "udp"}

    async def stream_source(self) -> str | None:
        url = await self._ctrl.rtsp_url(fresh=True)
        if url is None and self._ctrl.source == SOURCE_LOCAL:
            return None
        plain = url or live_url(self.hass, self._ctrl)
        if _HA_STREAM.get():
            return plain
        if self.stream and self.stream.source != plain:
            self.stream.update_source(plain)
        return go2rtc_source(url) if url else plain

    async def async_create_stream(self) -> Stream | None:
        token = _HA_STREAM.set(True)
        try:
            return await super().async_create_stream()
        finally:
            _HA_STREAM.reset(token)

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        async with self._image_lock:
            c = self._ctrl
            if url := await c.rtsp_url():
                if self._image and time.monotonic() - self._image_at < RTSP_SNAPSHOT_TTL_S:
                    return self._image
                ffmpeg = get_ffmpeg_manager(self.hass).binary
                image = await rtsp_jpeg(ffmpeg, url)
                if not image and await c.rtsp_moved() and (url := await c.rtsp_url()):
                    image = await rtsp_jpeg(ffmpeg, url)
                if image:
                    c.rtsp_ok()
                    self._image, self._image_at = image, time.monotonic()
                    return image
                c.rtsp_broken()
            if c.source == SOURCE_LOCAL:
                return self._image
            fresh = c.keyframe_seq == self._image_seq if c.live else time.monotonic() - self._image_at < SNAPSHOT_TTL_S
            if self._image and fresh:
                return self._image
            try:
                key = await c.snapshot_keyframe()
            except (GwellError, LoginUnavailable, OSError) as e:
                _LOGGER.debug("snapshot failed: %s", e)
                return self._image
            if key and (image := await to_jpeg(get_ffmpeg_manager(self.hass).binary, key)):
                self._image, self._image_at, self._image_seq = image, time.monotonic(), c.keyframe_seq
            return self._image

    async def async_will_remove_from_hass(self) -> None:
        if self.stream:
            await self.stream.stop()
        await super().async_will_remove_from_hass()
