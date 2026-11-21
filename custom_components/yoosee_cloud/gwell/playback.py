from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from . import mtp
from .audio import AacEncoder
from .av import AudioUnit, AVFrame, AVParser, AVUserData, VideoUnit, is_keyframe
from .client import GwellError, MediaSession
from .fmp4 import Fmp4Muxer

_LOGGER = logging.getLogger(__name__)

IDLE_TIMEOUT_S = 15.0
MARKER_TIMEOUT_S = 8.0
CH_PLAYBACK = 3
SUB_FILE_DATA = 1
SUB_FILE_SWITCHED = 10
AUDIO_WAIT_US = 1_500_000


class Unsupported(Exception):
    pass


async def stream_fmp4(media: MediaSession, descriptor: bytes, offset_s: int = 0, *, ffmpeg: str | None = None,
                      idle_timeout: float = IDLE_TIMEOUT_S,
                      marker_timeout: float = MARKER_TIMEOUT_S) -> AsyncIterator[bytes]:
    switched = seek_sent = offset_s == 0

    def gate(ud) -> bool:
        nonlocal switched, seek_sent
        if ud.channel != CH_PLAYBACK:
            return False
        if ud.sub_cmd == SUB_FILE_SWITCHED and ud.payload[4:12] == descriptor:
            switched = True
        elif ud.sub_cmd == SUB_FILE_DATA and switched:
            return True
        elif ud.sub_cmd == SUB_FILE_DATA and not seek_sent:
            seek_sent = True
            media.switch_file(descriptor, offset_s)
        return False

    mux = Fmp4Muxer(start_s=offset_s)
    enc = None
    pending: list[VideoUnit] = []
    out: list[bytes] = []
    gen = _pump(media, lambda: media.open_playback(descriptor), idle_timeout, gate, marker_timeout,
                "%s offset %d" % (descriptor.hex(), offset_s), bool(ffmpeg))
    try:
        async for unit in gen:
            if isinstance(unit, AudioUnit):
                if enc is None and mux.params is None:
                    enc = AacEncoder(ffmpeg, lambda pts, aac: out.extend(mux.feed_audio(pts, aac)))
                    mux.audio = await enc.start()
                if mux.audio:
                    enc.feed(unit)
            else:
                pending.append(unit)
                if ffmpeg and enc is None and mux.params is None and unit.pts - pending[0].pts < AUDIO_WAIT_US:
                    continue
                for u in pending:
                    out.extend(mux.feed(u))
                pending.clear()
            while out:
                yield out.pop(0)
            if mux.unsupported:
                raise Unsupported(mux.unsupported)
        for u in pending:
            out.extend(mux.feed(u))
        out.extend(mux.flush())
        while out:
            yield out.pop(0)
    finally:
        await gen.aclose()
        if enc:
            await enc.close()


def stream_live(media: MediaSession, definition: int = mtp.DEF_HD, *,
                idle_timeout: float = IDLE_TIMEOUT_S) -> AsyncIterator[VideoUnit | AudioUnit]:
    return _pump(media, lambda: media.open_stream(mtp.CALL_LIVE, mtp.live_param(definition)), idle_timeout, None, 0,
                 "live", True)


async def _pump(media: MediaSession, opener: Callable[[], Awaitable[None]], idle_timeout: float,
                gate: Callable | None, marker_timeout: float, label: str,
                audio: bool = False) -> AsyncIterator[VideoUnit | AudioUnit]:
    live = gate is None
    parser = AVParser()
    dropped = 0
    try:
        await opener()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + marker_timeout
        while True:
            timeout = idle_timeout
            if not live:
                left = deadline - loop.time()
                if left <= 0:
                    raise GwellError("no playback marker for " + label)
                timeout = min(timeout, left)
            data = await media.read(timeout)
            if data is None:
                if not live and loop.time() >= deadline:
                    continue
                break
            if not live:
                dropped += len(data)
            for item in parser.feed(data):
                if isinstance(item, AVUserData):
                    ud = item.data
                    _LOGGER.debug("marker ch%d/sub%d %s after %d bytes (live=%s)",
                                  ud.channel, ud.sub_cmd, ud.payload[:12].hex(), dropped, live)
                    if not live and gate(ud):
                        live = True
                    continue
                if not live or not isinstance(item, AVFrame):
                    continue
                if audio and item.slices:
                    yield AudioUnit(item.pts2, item.slices)
                if item.video:
                    yield VideoUnit(item.pts, item.video, is_keyframe(item.video, item.video_codec), item.video_codec)
    finally:
        media.close_stream()
