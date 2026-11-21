from __future__ import annotations

import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from .mtp import USER_DATA_LEN, UserData, parse_user_data

MAGIC = b"\xff\xff\xff\x88"
HEADER_LEN = 0x1C
MAX_SLICES = 64
MAX_AUX = 0x200000

PTYPE_DATA = 0
PTYPE_HEADER_ONLY = 1
PTYPE_FILE_START = 2

AUDIO_SLICE_STEP_US = 20000


@dataclass(frozen=True)
class AVFrame:
    offset: int
    version: int
    ptype: int
    pts: int
    pts2: int
    slices: tuple[bytes, ...]
    slice_table2: tuple[int, ...] | None
    video: bytes

    @property
    def video_codec(self) -> str:
        return "hevc" if self.version & 0x10 else "h264"

    def slice_pts(self, i: int) -> int | None:
        if self.slice_table2 is not None:
            return None
        return self.pts2 + i * AUDIO_SLICE_STEP_US


@dataclass(frozen=True)
class AVUserData:
    offset: int
    data: UserData


class AVParser:

    def __init__(self, max_aux: int = MAX_AUX) -> None:
        self._buf = bytearray()
        self._base = 0
        self.max_aux = max_aux
        self.resyncs = 0
        self.skipped = 0

    def feed(self, data: bytes) -> list[AVFrame | AVUserData]:
        self._buf += data
        return self._parse(False)

    def close(self) -> list[AVFrame | AVUserData]:
        out = self._parse(True)
        self.skipped += len(self._buf)
        self._base += len(self._buf)
        self._buf.clear()
        return out

    def _resync(self, pos: int, start: int) -> int | None:
        q = self._buf.find(MAGIC, start)
        if q < 0:
            return None
        self.resyncs += 1
        self.skipped += q - pos
        return q

    def _parse(self, final: bool) -> list[AVFrame | AVUserData]:
        b = self._buf
        out: list[AVFrame | AVUserData] = []
        pos = 0
        while True:
            if len(b) - pos < 4:
                break
            if b[pos:pos + 4] != MAGIC:
                q = self._resync(pos, pos + 1)
                if q is None:
                    keep = max(pos, len(b) - 3)
                    self.skipped += keep - pos
                    pos = keep
                    break
                pos = q
                continue
            if pos + HEADER_LEN > len(b):
                break
            ver, pt, n, aux, pts, pts2 = struct.unpack_from("<BBHIqq", b, pos + 4)
            if pt == PTYPE_HEADER_ONLY or (pt == PTYPE_FILE_START and n > MAX_SLICES):
                out.append(AVUserData(self._base + pos, parse_user_data(bytes(b[pos:pos + USER_DATA_LEN]))))
                pos += USER_DATA_LEN
                continue
            if n > MAX_SLICES:
                self.skipped += 1
                pos += 1
                continue
            tbl = pos + HEADER_LEN
            end = tbl + 2 * n * (2 if ver & 1 else 1)
            total = None
            if end <= len(b):
                lens = struct.unpack_from("<%dH" % n, b, tbl)
                total = end - pos + sum(lens) + aux
            elif not final:
                break
            if total is None or total <= HEADER_LEN or aux > self.max_aux or (final and pos + total > len(b)):
                q = self._resync(pos, pos + 4)
                if q is None:
                    if final:
                        break
                    keep = max(pos + 1, len(b) - 3)
                    self.skipped += keep - pos
                    pos = keep
                    break
                pos = q
                continue
            if pos + total > len(b):
                break
            table2 = struct.unpack_from("<%dH" % n, b, tbl + 2 * n) if ver & 1 else None
            cur = end
            slices = []
            for ln in lens:
                slices.append(bytes(b[cur:cur + ln]))
                cur += ln
            out.append(AVFrame(
                offset=self._base + pos, version=ver, ptype=pt, pts=pts, pts2=pts2,
                slices=tuple(slices), slice_table2=table2, video=bytes(b[cur:cur + aux]),
            ))
            pos += total
        del b[:pos]
        self._base += pos
        return out


def parse_stream(chunks: Iterable[bytes]) -> Iterator[AVFrame | AVUserData]:
    p = AVParser()
    for c in chunks:
        yield from p.feed(c)
    yield from p.close()


def nal_types(au: bytes, codec: str = "h264") -> list[int]:
    out = []
    i = 0
    n = len(au)
    while True:
        i = au.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= n:
            return out
        h = au[i + 3]
        out.append((h >> 1) & 0x3F if codec == "hevc" else h & 0x1F)
        i += 3


def is_keyframe(au: bytes, codec: str = "h264") -> bool:
    t = nal_types(au, codec)
    if codec == "hevc":
        return any(16 <= x <= 21 for x in t)
    return 5 in t


@dataclass(frozen=True)
class VideoUnit:
    pts: int
    data: bytes
    keyframe: bool
    codec: str


@dataclass(frozen=True)
class AudioUnit:
    pts: int
    frames: tuple[bytes, ...]


def video_units(items: Iterable[AVFrame | AVUserData]) -> Iterator[VideoUnit]:
    for it in items:
        if isinstance(it, AVFrame) and it.video:
            yield VideoUnit(it.pts, it.video, is_keyframe(it.video, it.video_codec), it.video_codec)
