from __future__ import annotations

import struct
from collections.abc import Iterator

from .audio import AAC_CONFIG
from .av import VideoUnit
from .fmp4 import NAL_AUD, NAL_PPS, NAL_SPS, TIMESCALE, Timeline, avc_config, sps_dimensions, split_nals

FILE_HEADER = b"FLV\x01\x01\x00\x00\x00\x09" + bytes(4)
FILE_HEADER_AV = b"FLV\x01\x05\x00\x00\x00\x09" + bytes(4)
TAG_AUDIO = 8
TAG_VIDEO = 9
TAG_SCRIPT = 18
CODEC_H264 = 7
CODEC_AAC = 10
AAC_HEAD = 0xAF


def tag(kind: int, ts_ms: int, body: bytes) -> bytes:
    ts = ts_ms & 0xFFFFFFFF
    return (bytes((kind,)) + len(body).to_bytes(3, "big") + (ts & 0xFFFFFF).to_bytes(3, "big") + bytes((ts >> 24,))
            + bytes(3) + body + struct.pack(">I", 11 + len(body)))


def _amf_str(s: str) -> bytes:
    b = s.encode()
    return struct.pack(">H", len(b)) + b


def metadata(width: int, height: int, audio: bool = False) -> bytes:
    props = {"videocodecid": float(CODEC_H264), "width": float(width), "height": float(height)}
    if audio:
        props["audiocodecid"] = float(CODEC_AAC)
    body = (b"\x02" + _amf_str("onMetaData") + b"\x08" + struct.pack(">I", len(props))
            + b"".join(_amf_str(k) + b"\x00" + struct.pack(">d", v) for k, v in props.items()) + b"\x00\x00\x09")
    return tag(TAG_SCRIPT, 0, body)


class FlvMuxer(Timeline):

    def __init__(self) -> None:
        super().__init__()
        self.audio = False
        self.params: tuple[bytes, bytes] | None = None
        self.head: bytes | None = None
        self.unsupported: str | None = None

    def feed(self, unit: VideoUnit) -> Iterator[tuple[bytes, bool | None]]:
        if unit.codec != "h264":
            self.unsupported = unit.codec
            return
        nals = split_nals(unit.data)
        sps = next((n for n in nals if n[0] & 0x1F == NAL_SPS), None)
        pps = next((n for n in nals if n[0] & 0x1F == NAL_PPS), None)
        if self.params is None and not (unit.keyframe and sps and pps):
            return
        ts = self._dts(unit.pts) * 1000 // TIMESCALE
        if sps and pps and (sps, pps) != self.params:
            self.params = (sps, pps)
            seq = tag(TAG_VIDEO, ts, bytes((0x10 | CODEC_H264, 0, 0, 0, 0)) + avc_config(sps, pps))
            first = self.head is None
            self.head = ((FILE_HEADER_AV if self.audio else FILE_HEADER) + metadata(*sps_dimensions(sps), self.audio)
                         + seq + (tag(TAG_AUDIO, ts, bytes((AAC_HEAD, 0)) + AAC_CONFIG) if self.audio else b""))
            yield (self.head if first else seq), None
        body = b"".join(struct.pack(">I", len(n)) + n for n in nals if n[0] & 0x1F not in (NAL_SPS, NAL_PPS, NAL_AUD))
        if body:
            frame = (0x10 if unit.keyframe else 0x20) | CODEC_H264
            yield tag(TAG_VIDEO, ts, bytes((frame, 1, 0, 0, 0)) + body), unit.keyframe

    def feed_audio(self, pts_us: int, aac: bytes) -> bytes | None:
        if not self.audio or self.head is None or pts_us < self._origin_us:
            return None
        return tag(TAG_AUDIO, (pts_us - self._origin_us + self._shift_us) // 1000, bytes((AAC_HEAD, 1)) + aac)
