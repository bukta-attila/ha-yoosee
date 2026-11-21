from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

from .audio import AAC_CONFIG, SAMPLE_RATE
from .av import VideoUnit

TIMESCALE = 90000
MAX_SAMPLES_PER_FRAGMENT = 8
DEFAULT_DURATION = TIMESCALE // 15
AAC_SAMPLES = 1024
MAX_GAP_US = 5_000_000

NAL_SLICE = 1
NAL_IDR = 5
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9


def split_nals(au: bytes) -> list[bytes]:
    out = []
    i = au.find(b"\x00\x00\x01")
    while i >= 0:
        s = i + 3
        j = au.find(b"\x00\x00\x01", s)
        end = len(au) if j < 0 else j
        nal = au[s:end]
        while nal.endswith(b"\x00"):
            nal = nal[:-1]
        if nal:
            out.append(bytes(nal))
        i = j
    return out


def _unescape(nal: bytes) -> bytes:
    return nal.replace(b"\x00\x00\x03", b"\x00\x00")


class _Bits:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self.p = 0

    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self.d[self.p >> 3] if (self.p >> 3) < len(self.d) else 0
            v = (v << 1) | ((byte >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self) -> int:
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 31:
                raise ValueError("bad exp-golomb")
        return (1 << z) - 1 + self.u(z)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def _skip_scaling(b: _Bits, n: int) -> None:
    last = nxt = 8
    for _ in range(n):
        if nxt:
            nxt = (last + b.se() + 256) % 256
        last = nxt or last


def sps_dimensions(sps: bytes) -> tuple[int, int]:
    b = _Bits(_unescape(sps[1:]))
    profile = b.u(8)
    b.u(16)
    b.ue()
    chroma = 1
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = b.ue()
        if chroma == 3:
            b.u(1)
        b.ue()
        b.ue()
        b.u(1)
        if b.u(1):
            for i in range(8 if chroma != 3 else 12):
                if b.u(1):
                    _skip_scaling(b, 16 if i < 6 else 64)
    b.ue()
    poc = b.ue()
    if poc == 0:
        b.ue()
    elif poc == 1:
        b.u(1)
        b.se()
        b.se()
        for _ in range(b.ue()):
            b.se()
    b.ue()
    b.u(1)
    w_mbs = b.ue() + 1
    h_map = b.ue() + 1
    frame_mbs_only = b.u(1)
    if not frame_mbs_only:
        b.u(1)
    b.u(1)
    w = w_mbs * 16
    h = h_map * 16 * (2 - frame_mbs_only)
    if b.u(1):
        cl, cr, ct, cb = b.ue(), b.ue(), b.ue(), b.ue()
        cx = 1 if chroma == 0 else (2 if chroma in (1, 2) else 1)
        cy = (1 if chroma == 0 else (2 if chroma == 1 else 1)) * (2 - frame_mbs_only)
        w -= (cl + cr) * cx
        h -= (ct + cb) * cy
    return w, h


def codec_string(sps: bytes) -> str:
    return "avc1.%02X%02X%02X" % (sps[1], sps[2], sps[3])


def codec_from_init(init: bytes) -> str | None:
    i = init.find(b"avcC")
    if i < 0 or i + 8 > len(init):
        return None
    return "avc1.%02X%02X%02X" % (init[i + 5], init[i + 6], init[i + 7]) + (",mp4a.40.2" if b"mp4a" in init else "")


def _box(kind: bytes, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I", 8 + len(body)) + kind + body


def _full(kind: bytes, version: int, flags: int, *parts: bytes) -> bytes:
    return _box(kind, struct.pack(">I", (version << 24) | flags), *parts)


_MATRIX = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)


def avc_config(sps: bytes, pps: bytes) -> bytes:
    return bytes((1, sps[1], sps[2], sps[3], 0xFF, 0xE1)) + struct.pack(">H", len(sps)) + sps + b"\x01" + struct.pack(">H", len(pps)) + pps


def _desc(tag: int, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return bytes((tag, len(body))) + body


def _trak(track: int, w: int, h: int, timescale: int, handler: bytes, name: bytes, mhd: bytes, entry: bytes) -> bytes:
    stbl = _box(b"stbl", _full(b"stsd", 0, 0, struct.pack(">I", 1), entry),
                _full(b"stts", 0, 0, bytes(4)), _full(b"stsc", 0, 0, bytes(4)),
                _full(b"stsz", 0, 0, bytes(8)), _full(b"stco", 0, 0, bytes(4)))
    dinf = _box(b"dinf", _full(b"dref", 0, 0, struct.pack(">I", 1), _full(b"url ", 0, 1)))
    minf = _box(b"minf", mhd, dinf, stbl)
    mdia = _box(b"mdia", _full(b"mdhd", 0, 0, bytes(8), struct.pack(">II", timescale, 0), struct.pack(">HH", 0x55C4, 0)),
                _full(b"hdlr", 0, 0, bytes(4), handler, bytes(12), name), minf)
    tkhd = _full(b"tkhd", 0, 3, bytes(8), struct.pack(">I", track), bytes(4), struct.pack(">I", 0), bytes(8),
                 struct.pack(">hhhH", 0, 0, 0 if w else 0x100, 0), _MATRIX, struct.pack(">II", w << 16, h << 16))
    return _box(b"trak", tkhd, mdia)


def init_segment(sps: bytes, pps: bytes, audio: bool = False) -> bytes:
    w, h = sps_dimensions(sps)
    avcc = _box(b"avcC", avc_config(sps, pps))
    avc1 = _box(b"avc1", bytes(6), struct.pack(">H", 1), bytes(16), struct.pack(">HH", w, h),
                struct.pack(">II", 0x480000, 0x480000), bytes(4), struct.pack(">H", 1), bytes(32),
                struct.pack(">Hh", 0x18, -1), avcc)
    traks = [_trak(1, w, h, TIMESCALE, b"vide", b"Video\x00", _full(b"vmhd", 0, 1, bytes(8)), avc1)]
    trex = [_full(b"trex", 0, 0, struct.pack(">IIIII", 1, 1, 0, 0, 0))]
    if audio:
        esds = _full(b"esds", 0, 0, _desc(3, struct.pack(">HB", 2, 0),
                                          _desc(4, bytes((0x40, 0x15)), bytes(11), _desc(5, AAC_CONFIG)), _desc(6, b"\x02")))
        mp4a = _box(b"mp4a", bytes(6), struct.pack(">H", 1), bytes(8), struct.pack(">HHHHI", 1, 16, 0, 0, SAMPLE_RATE << 16),
                    esds)
        traks.append(_trak(2, 0, 0, SAMPLE_RATE, b"soun", b"Audio\x00", _full(b"smhd", 0, 0, bytes(4)), mp4a))
        trex.append(_full(b"trex", 0, 0, struct.pack(">IIIII", 2, 1, 0, 0, 0)))
    mvhd = _full(b"mvhd", 0, 0, bytes(8), struct.pack(">II", 1000, 0), struct.pack(">IH", 0x10000, 0x100),
                 bytes(10), _MATRIX, bytes(24), struct.pack(">I", len(traks) + 1))
    ftyp = _box(b"ftyp", b"isom", struct.pack(">I", 0x200), b"isomiso6avc1mp41")
    return ftyp + _box(b"moov", mvhd, *traks, _box(b"mvex", *trex))


@dataclass
class _Sample:
    data: bytes
    dts: int
    key: bool


def fragment(seq: int, samples: list[_Sample], durations: list[int], track: int = 1) -> bytes:
    n = len(samples)
    trun_flags = 0x000001 | 0x000100 | 0x000200 | 0x000400
    entries = b"".join(
        struct.pack(">III", d, len(s.data), 0x02000000 if s.key else 0x01010000) for s, d in zip(samples, durations)
    )
    tfhd = _full(b"tfhd", 0, 0x020000, struct.pack(">I", track))
    tfdt = _full(b"tfdt", 1, 0, struct.pack(">Q", samples[0].dts))
    trun_len = 12 + 4 + 4 + 12 * n
    moof_len = 8 + 16 + 8 + len(tfhd) + len(tfdt) + trun_len
    trun = _full(b"trun", 0, trun_flags, struct.pack(">Ii", n, moof_len + 8), entries)
    moof = _box(b"moof", _full(b"mfhd", 0, 0, struct.pack(">I", seq)), _box(b"traf", tfhd, tfdt, trun))
    return moof + _box(b"mdat", *(s.data for s in samples))


class Timeline:

    def __init__(self, start_s: float = 0.0) -> None:
        self.start = int(round(start_s * TIMESCALE))
        self._origin_us: int | None = None
        self._last_us: int | None = None
        self._shift_us = 0
        self._last_dts: int | None = None

    def _dts(self, pts_us: int) -> int:
        if self._origin_us is None:
            self._origin_us = pts_us
        if self._last_us is not None and (pts_us <= self._last_us or pts_us - self._last_us > MAX_GAP_US):
            self._shift_us += self._last_us - pts_us + DEFAULT_DURATION * 1_000_000 // TIMESCALE
        self._last_us = pts_us
        rel = pts_us - self._origin_us + self._shift_us
        dts = self.start + rel * TIMESCALE // 1_000_000
        if self._last_dts is not None and dts <= self._last_dts:
            dts = self._last_dts + 1
        self._last_dts = dts
        return dts


class Fmp4Muxer(Timeline):

    def __init__(self, start_s: float = 0.0, max_samples: int = MAX_SAMPLES_PER_FRAGMENT) -> None:
        super().__init__(start_s)
        self.max_samples = max_samples
        self.codec: str | None = None
        self.audio = False
        self.params: tuple[bytes, bytes] | None = None
        self._pending: list[_Sample] = []
        self._audio: list[_Sample] = []
        self._seq = 0
        self.unsupported: str | None = None

    def feed(self, unit: VideoUnit) -> Iterator[bytes]:
        if unit.codec != "h264":
            self.unsupported = unit.codec
            return
        nals = split_nals(unit.data)
        sps = next((n for n in nals if n[0] & 0x1F == NAL_SPS), None)
        pps = next((n for n in nals if n[0] & 0x1F == NAL_PPS), None)
        if self.params is None:
            if not (unit.keyframe and sps and pps):
                return
        elif sps and pps and (sps, pps) != self.params:
            yield from self.flush()
        if sps and pps and (sps, pps) != self.params:
            self.params = (sps, pps)
            self.codec = codec_string(sps)
            yield init_segment(sps, pps, self.audio)
        body = b"".join(struct.pack(">I", len(n)) + n for n in nals if n[0] & 0x1F not in (NAL_SPS, NAL_PPS, NAL_AUD))
        if not body:
            return
        dts = self._dts(unit.pts)
        if unit.keyframe or len(self._pending) >= self.max_samples:
            yield from self._emit(self._pending, dts)
        self._pending.append(_Sample(body, dts, unit.keyframe))

    def feed_audio(self, pts_us: int, aac: bytes) -> Iterator[bytes]:
        if not self.audio or self.params is None or pts_us < self._origin_us:
            return
        dts = self.start * SAMPLE_RATE // TIMESCALE + (pts_us - self._origin_us + self._shift_us) * SAMPLE_RATE // 1_000_000
        if self._audio:
            dts = max(dts, self._audio[-1].dts + AAC_SAMPLES)
        if len(self._audio) >= self.max_samples:
            yield from self._emit(self._audio, dts, 2)
        self._audio.append(_Sample(aac, dts, True))

    def _emit(self, s: list[_Sample], next_dts: int | None, track: int = 1) -> Iterator[bytes]:
        if not s:
            return
        durs = [max(1, b.dts - a.dts) for a, b in zip(s, s[1:])]
        if next_dts is not None and next_dts > s[-1].dts:
            durs.append(next_dts - s[-1].dts)
        else:
            durs.append(durs[-1] if durs else DEFAULT_DURATION if track == 1 else AAC_SAMPLES)
        self._seq += 1
        out = fragment(self._seq, s, durs, track)
        s.clear()
        yield out

    def flush(self) -> Iterator[bytes]:
        yield from self._emit(self._pending, None)
        yield from self._emit(self._audio, None, 2)
