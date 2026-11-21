from __future__ import annotations

import asyncio
import bisect
import logging
from collections.abc import Callable

from .av import AUDIO_SLICE_STEP_US, AudioUnit

_LOGGER = logging.getLogger(__name__)

AMR_HEADER = b"#!AMR\n"
AMR_NO_DATA = b"\x7c"
SAMPLE_RATE = 16000
AAC_FRAME_US = 1024 * 1_000_000 // SAMPLE_RATE
AAC_CONFIG = bytes((0x14, 0x08))
MAX_FILL = 750


class AacEncoder:

    def __init__(self, ffmpeg: str, on_frame: Callable[[int, bytes], None]) -> None:
        self.ffmpeg = ffmpeg
        self.on_frame = on_frame
        self.proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._base: int | None = None
        self._fed = 0
        self._segs: list[int] = []
        self._bases: list[int] = []

    async def start(self) -> bool:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-probesize", "32", "-analyzeduration", "0",
                "-f", "amr", "-i", "pipe:0", "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "aac",
                "-flush_packets", "1", "-f", "adts", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except OSError as e:
            _LOGGER.debug("audio encoder unavailable: %s", e)
            return False
        self.proc.stdin.write(AMR_HEADER)
        self._reader = asyncio.ensure_future(self._read())
        return True

    def feed(self, unit: AudioUnit) -> None:
        if self.proc is None or self.proc.returncode is not None or self.proc.stdin.is_closing():
            return
        if self._base is None:
            self._rebase(unit.pts)
        out = []
        for i, f in enumerate(unit.frames):
            gap = unit.pts + i * AUDIO_SLICE_STEP_US - self._base - self._fed * AUDIO_SLICE_STEP_US
            if abs(gap) > MAX_FILL * AUDIO_SLICE_STEP_US:
                self._rebase(self._base + gap)
                gap = 0
            if gap < -AUDIO_SLICE_STEP_US // 2:
                continue
            fill = (gap + AUDIO_SLICE_STEP_US // 2) // AUDIO_SLICE_STEP_US
            out.append(AMR_NO_DATA * fill + f)
            self._fed += fill + 1
        self.proc.stdin.write(b"".join(out))

    def _rebase(self, base: int) -> None:
        self._base = base
        self._segs.append(self._fed)
        self._bases.append(base)

    def _pts(self, n: int) -> int:
        t = n * AAC_FRAME_US
        return self._bases[bisect.bisect_right(self._segs, t // AUDIO_SLICE_STEP_US) - 1] + t

    async def _read(self) -> None:
        out = self.proc.stdout
        n = 0
        try:
            while True:
                head = await out.readexactly(7)
                size = (head[3] & 3) << 11 | head[4] << 3 | head[5] >> 5
                body = await out.readexactly(size - 7)
                skip = 0 if head[1] & 1 else 2
                if n:
                    self.on_frame(self._pts(n - 1), body[skip:])
                n += 1
        except (asyncio.IncompleteReadError, ConnectionError):
            _LOGGER.debug("audio encoder ended after %d frames", n)

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
