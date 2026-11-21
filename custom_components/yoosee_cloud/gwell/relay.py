from __future__ import annotations

import ipaddress
import struct
from dataclasses import dataclass

F_RELAY = 0x80
F_MTPCTRL = 0x40
F_FWD_MASK = 0x30
F_LOCAL = 0x08

TYPE_DIRECT_DATA = 0x11
TYPE_RELAY_DATA_OUT = 0x12
TYPE_RELAY_DATA_IN = 0x13
TYPE_TCP_CTRL_SELF = 0x18
TYPE_DIRECT_CTRL = 0x19
TYPE_RELAY_CTRL_OUT = 0x1A
TYPE_RELAY_CTRL_IN = 0x1B
TYPE_RELAY_CTRL2_OUT = 0x1C

MAX_FRAME = 2047
CHECK_WORDS = 12

_M = 0xFFFFFFFF


def _rotl16(x: int, r: int) -> int:
    r &= 15
    return ((x << r) | (x >> (16 - r))) & 0xFFFF if r else x


def check_value(frame: bytes) -> int:
    x = frame[0] | (frame[1] << 8)
    n = len(frame)
    for j in range(CHECK_WORDS):
        o = 4 + 2 * j
        if o + 2 <= n:
            v = frame[o] | (frame[o + 1] << 8)
        elif o < n:
            v = frame[o]
        else:
            v = 0
        x ^= _rotl16(v, j)
    return x


def check_ok(frame: bytes) -> bool:
    return len(frame) >= 4 and (frame[2] | (frame[3] << 8)) == check_value(frame)


def frame_type(frame: bytes) -> int:
    return frame[0] >> 3


def frame_length(hdr: bytes) -> int:
    return (hdr[1] << 3) | (hdr[0] & 7)


def is_forwarded(frame: bytes) -> bool:
    return bool(frame[0] & F_FWD_MASK)


def header_len(frame: bytes) -> int:
    return 12 if is_forwarded(frame) else 4


@dataclass(frozen=True)
class RelayFrame:
    ftype: int
    dest_id: int | None
    payload: bytes
    check_ok: bool

    @property
    def is_ctrl(self) -> bool:
        return bool((self.ftype << 3) & F_MTPCTRL)


def build(ftype: int, payload: bytes, dest_id: int | None = None) -> bytes:
    fwd = bool((ftype << 3) & F_FWD_MASK)
    if fwd != (dest_id is not None):
        raise ValueError("forwarded frame types need a destination id, direct ones must not have one")
    body = (struct.pack("<Q", dest_id) if fwd else b"") + bytes(payload)
    n = 4 + len(body)
    if n > MAX_FRAME:
        raise ValueError("relay frame too long")
    f = bytearray(4) + body
    f[0] = ((ftype << 3) & 0xF8) | (n & 7)
    f[1] = n >> 3
    struct.pack_into("<H", f, 2, check_value(f))
    return bytes(f)


def parse(frame: bytes) -> RelayFrame:
    if len(frame) < 4 or not frame[0] & F_RELAY:
        raise ValueError("not a relay frame")
    n = frame_length(frame)
    if n != len(frame):
        raise ValueError("relay frame length mismatch")
    if is_forwarded(frame):
        if n < 12:
            raise ValueError("forwarded relay frame too short")
        return RelayFrame(frame_type(frame), struct.unpack_from("<Q", frame, 4)[0], bytes(frame[12:]), check_ok(frame))
    return RelayFrame(frame_type(frame), None, bytes(frame[4:]), check_ok(frame))


def split_stream(buf: bytes) -> tuple[list[bytes], bytes]:
    out = []
    j = 0
    while j + 4 <= len(buf):
        ln = frame_length(buf[j:j + 2])
        if ln < 4:
            raise ValueError("relay stream desynchronised")
        if j + ln > len(buf):
            break
        out.append(bytes(buf[j:j + ln]))
        j += ln
    return out, bytes(buf[j:])


PING_LEN = 0x44
PING = 1
PONG = 2
MTU_PROBE_SIZE = 1400


@dataclass(frozen=True)
class Ping:
    sub: int
    link_id: int
    src_id: int
    dst_id: int
    counter: int
    clock_ms: int
    direct_flag: int = 0
    probe_size: int = 0
    rtt: int = 0
    side_tag: int = 0
    start_clock: int = 0
    tail: int = 0

    @property
    def is_ping(self) -> bool:
        return self.sub == PING


def build_ping(p: Ping) -> bytes:
    return struct.pack(
        "<BBHIIQQIQ8xIIHHII",
        0, p.sub, PING_LEN, p.link_id & _M, 1 if p.sub == PING else 0,
        p.src_id, p.dst_id, p.counter & _M, p.clock_ms & 0xFFFFFFFFFFFFFFFF,
        p.direct_flag & _M, p.probe_size & _M, p.rtt & 0xFFFF, p.side_tag & 0xFFFF,
        p.start_clock & _M, p.tail & _M,
    )


def parse_ping(inner: bytes) -> Ping | None:
    if len(inner) < PING_LEN or inner[0] != 0 or inner[1] not in (PING, PONG):
        return None
    if struct.unpack_from("<H", inner, 2)[0] != PING_LEN:
        return None
    (_, sub, _, link, _, src, dst, cnt, clk, dflag, psize, rtt, tag, sclk, tail) = struct.unpack_from(
        "<BBHIIQQIQ8xIIHHII", inner, 0)
    return Ping(sub, link, src, dst, cnt, clk, dflag, psize, rtt, tag, sclk, tail)


def pong_for(ping: Ping, src_id: int, relayed: bool) -> Ping:
    return Ping(PONG, ping.link_id, src_id, ping.src_id, ping.counter, ping.clock_ms,
                probe_size=PING_LEN if relayed else 0)


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int


@dataclass(frozen=True)
class RelayServer:
    server_id: int
    endpoint: Endpoint


@dataclass(frozen=True)
class CallParams:
    link_id: int
    caller_id: int
    callee_id: int
    caller_public: Endpoint
    callee_public: Endpoint
    relays: tuple[RelayServer, ...]

    @property
    def media_conv(self) -> int:
        return self.link_id & 0x7FFFFFFF

    @property
    def control_conv(self) -> int:
        return (self.link_id | 0x80000000) & _M


def _endpoint(body: bytes, off: int) -> Endpoint:
    port = struct.unpack_from(">H", body, off)[0]
    ip = str(ipaddress.IPv4Address(bytes(body[off + 4:off + 8])))
    return Endpoint(ip, port)


def parse_call_params(body: bytes) -> CallParams:
    if len(body) < 0x50:
        raise ValueError("call parameter body too short")
    link, caller, callee = struct.unpack_from("<IQQ", body, 0)
    n = struct.unpack_from("<H", body, 0x4E)[0]
    if len(body) < 0x50 + 16 * n:
        raise ValueError("relay table truncated")
    relays = []
    for i in range(n):
        o = 0x50 + 16 * i
        sid, _, _, port = struct.unpack_from("<IIHH", body, o)
        relays.append(RelayServer(sid, Endpoint(str(ipaddress.IPv4Address(bytes(body[o + 12:o + 16]))), port)))
    return CallParams(link, caller, callee, _endpoint(body, 0x18), _endpoint(body, 0x34), tuple(relays))
