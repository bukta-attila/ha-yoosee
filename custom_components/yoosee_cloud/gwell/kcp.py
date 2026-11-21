from __future__ import annotations

import struct
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

CMD_PUSH = 81
CMD_ACK = 82
CMD_WASK = 83
CMD_WINS = 84

HEADER = struct.Struct("<IBBHIIII")
OVERHEAD = HEADER.size

MTU = 1032
MSS = MTU - OVERHEAD
WND = 64
INTERVAL = 15
RTO_MIN = 30
RTO_DEF = 200
RTO_MAX = 60000
FAST_RESEND = 2
DEAD_LINK = 20

_M = 0xFFFFFFFF


def _diff(a: int, b: int) -> int:
    d = (a - b) & _M
    return d - (1 << 32) if d & 0x80000000 else d


@dataclass
class Segment:
    conv: int
    cmd: int
    frg: int
    wnd: int
    ts: int
    sn: int
    una: int
    data: bytes
    resendts: int = 0
    rto: int = 0
    fastack: int = 0
    xmit: int = 0

    def encode(self) -> bytes:
        return HEADER.pack(self.conv, self.cmd, self.frg, self.wnd, self.ts & _M, self.sn & _M,
                           self.una & _M, len(self.data)) + self.data


def iter_segments(buf: bytes):
    pos = 0
    n = len(buf)
    while pos + OVERHEAD <= n:
        conv, cmd, frg, wnd, ts, sn, una, ln = HEADER.unpack_from(buf, pos)
        pos += OVERHEAD
        if ln > n - pos or cmd not in (CMD_PUSH, CMD_ACK, CMD_WASK, CMD_WINS):
            return
        yield conv, cmd, frg, wnd, ts, sn, una, bytes(buf[pos:pos + ln])
        pos += ln


def first_conv(buf: bytes) -> int | None:
    return struct.unpack_from("<I", buf, 0)[0] if len(buf) >= OVERHEAD else None


class KCP:

    def __init__(self, conv: int, output: Callable[[bytes], None], *, mtu: int = MTU,
                 snd_wnd: int = WND, rcv_wnd: int = WND, rcv_queue_limit: int = WND) -> None:
        self.conv = conv & _M
        self.output = output
        self.mtu = mtu
        self.mss = mtu - OVERHEAD
        self.snd_wnd = snd_wnd
        self.rcv_wnd = rcv_wnd
        self.rmt_wnd = WND
        self.rcv_queue_limit = rcv_queue_limit
        self.snd_una = 0
        self.snd_nxt = 0
        self.rcv_nxt = 0
        self.snd_queue: deque[bytes] = deque()
        self.snd_buf: list[Segment] = []
        self.rcv_buf: dict[int, bytes] = {}
        self.rcv_queue: deque[bytes] = deque()
        self.acklist: list[tuple[int, int]] = []
        self.rx_srtt = 0
        self.rx_rttval = 0
        self.rx_rto = RTO_DEF
        self.current = 0
        self.ts_flush = 0
        self.dead = False
        self.duplicates = 0
        self.probe_wins = False

    def send(self, msg: bytes) -> None:
        if len(msg) > self.mss:
            raise ValueError("message larger than one KCP segment")
        self.snd_queue.append(bytes(msg))

    def recv(self) -> bytes | None:
        if not self.rcv_queue:
            return None
        full = len(self.rcv_queue) >= self.rcv_wnd
        msg = self.rcv_queue.popleft()
        self._move_to_queue()
        if full and len(self.rcv_queue) < self.rcv_wnd:
            self.probe_wins = True
        return msg

    def pending_messages(self) -> int:
        return len(self.rcv_queue)

    def _move_to_queue(self) -> None:
        while self.rcv_nxt in self.rcv_buf and len(self.rcv_queue) < self.rcv_queue_limit:
            self.rcv_queue.append(self.rcv_buf.pop(self.rcv_nxt))
            self.rcv_nxt = (self.rcv_nxt + 1) & _M

    def _wnd_unused(self) -> int:
        return max(0, self.rcv_wnd - len(self.rcv_queue))

    def _update_ack(self, rtt: int) -> None:
        if self.rx_srtt == 0:
            self.rx_srtt = rtt
            self.rx_rttval = rtt // 2
        else:
            delta = abs(rtt - self.rx_srtt)
            self.rx_rttval = (3 * self.rx_rttval + delta) // 4
            self.rx_srtt = max(1, (7 * self.rx_srtt + rtt) // 8)
        rto = self.rx_srtt + max(INTERVAL, 4 * self.rx_rttval)
        self.rx_rto = min(max(RTO_MIN, rto), RTO_MAX)

    def _parse_una(self, una: int) -> None:
        self.snd_buf = [s for s in self.snd_buf if _diff(una, s.sn) <= 0]
        self._shrink()

    def _shrink(self) -> None:
        self.snd_una = self.snd_buf[0].sn if self.snd_buf else self.snd_nxt

    def _parse_ack(self, sn: int) -> None:
        if _diff(sn, self.snd_una) < 0 or _diff(sn, self.snd_nxt) >= 0:
            return
        for i, s in enumerate(self.snd_buf):
            if s.sn == sn:
                del self.snd_buf[i]
                break
            if _diff(sn, s.sn) > 0:
                s.fastack += 1
        self._shrink()

    def input(self, data: bytes) -> int:
        n = 0
        for conv, cmd, frg, wnd, ts, sn, una, payload in iter_segments(data):
            if conv != self.conv:
                continue
            n += 1
            self.rmt_wnd = wnd
            self._parse_una(una)
            if cmd == CMD_ACK:
                rtt = _diff(self.current, ts)
                if rtt >= 0:
                    self._update_ack(rtt)
                self._parse_ack(sn)
            elif cmd == CMD_PUSH:
                if _diff(sn, (self.rcv_nxt + self.rcv_wnd) & _M) < 0:
                    self.acklist.append((sn, ts))
                    if _diff(sn, self.rcv_nxt) >= 0 and sn not in self.rcv_buf:
                        self.rcv_buf[sn] = payload
                        self._move_to_queue()
                    else:
                        self.duplicates += 1
            elif cmd == CMD_WASK:
                self.probe_wins = True
        return n

    def _seg(self, cmd: int, sn: int = 0, ts: int = 0, data: bytes = b"") -> Segment:
        return Segment(self.conv, cmd, 0, self._wnd_unused(), ts, sn, self.rcv_nxt, data)

    def flush(self) -> None:
        buf = bytearray()

        def emit(raw: bytes) -> None:
            nonlocal buf
            if len(buf) + len(raw) > self.mtu and buf:
                self.output(bytes(buf))
                buf = bytearray()
            buf += raw

        for sn, ts in self.acklist:
            emit(self._seg(CMD_ACK, sn, ts).encode())
        self.acklist.clear()
        if self.probe_wins:
            emit(self._seg(CMD_WINS).encode())
            self.probe_wins = False

        cwnd = min(self.snd_wnd, self.rmt_wnd)
        while self.snd_queue and _diff(self.snd_nxt, (self.snd_una + cwnd) & _M) < 0:
            s = self._seg(CMD_PUSH, self.snd_nxt, data=self.snd_queue.popleft())
            self.snd_nxt = (self.snd_nxt + 1) & _M
            self.snd_buf.append(s)

        for s in self.snd_buf:
            send = False
            if s.xmit == 0:
                send = True
                s.rto = self.rx_rto
                s.resendts = self.current + s.rto
            elif _diff(self.current, s.resendts) >= 0:
                send = True
                s.rto += self.rx_rto // 2
                s.resendts = self.current + s.rto
            elif s.fastack >= FAST_RESEND:
                send = True
                s.fastack = 0
                s.resendts = self.current + s.rto
            if send:
                s.xmit += 1
                s.ts = self.current
                s.wnd = self._wnd_unused()
                s.una = self.rcv_nxt
                emit(s.encode())
                if s.xmit >= DEAD_LINK:
                    self.dead = True
        if buf:
            self.output(bytes(buf))

    def update(self, now_ms: int) -> None:
        self.current = now_ms & _M
        if _diff(self.current, self.ts_flush) >= 0 or _diff(self.current, self.ts_flush) < -10000:
            self.ts_flush = (self.current + INTERVAL) & _M
            self.flush()
