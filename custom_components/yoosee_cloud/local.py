from __future__ import annotations

import asyncio
from urllib.parse import quote

from .const import DISCOVERY_PORT, DISCOVERY_TIMEOUT_S, RTSP_PORT

PROBE = b"\x01\x00\x00\x00"
REPLY = b"\x02\x00\x00\x00"


class _Listener(asyncio.DatagramProtocol):
    def __init__(self, found: dict[int, str], want: int | None, done: asyncio.Event) -> None:
        self.found = found
        self.want = want
        self.done = done

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) >= 20 and data[:4] == REPLY:
            dev = int.from_bytes(data[16:20], "little")
            self.found[dev] = addr[0]
            if dev == self.want:
                self.done.set()


async def discover(want: int | None = None, timeout: float = DISCOVERY_TIMEOUT_S,
                   target: tuple[str, int] = ("255.255.255.255", DISCOVERY_PORT)) -> dict[int, str]:
    found: dict[int, str] = {}
    done = asyncio.Event()
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: _Listener(found, want, done), local_addr=("0.0.0.0", 0), allow_broadcast=True)
    try:
        transport.sendto(PROBE, target)
        try:
            await asyncio.wait_for(done.wait(), timeout)
        except TimeoutError:
            pass
    finally:
        transport.close()
    return found


def rtsp_url(host: str, password: str) -> str:
    return f"rtsp://admin:{quote(password, safe='')}@{host}:{RTSP_PORT}/onvif1"
