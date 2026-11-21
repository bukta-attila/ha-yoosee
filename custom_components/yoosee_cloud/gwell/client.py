from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import time
from collections.abc import Callable
from datetime import date, datetime

from . import cloud, gute, kcp, mtp, relay
from .crypto import entry_pwd, gw_encode_password

_LOGGER = logging.getLogger(__name__)

RETRY_S = 1.0
MESG_TIMEOUT_S = 8.0
CALL_TIMEOUT_S = 10.0
PUNCH_TIMEOUT_S = 9.0
PING_PERIOD_S = 2.0
PING_BURST_S = (0.0, 0.030, 0.041)
START_RETRY_S = 0.06
START_TIMEOUT_S = 5.0
KCP_TICK_S = kcp.INTERVAL / 1000
AV_QUEUE = 64
PHONE_SIDE_TAG = 0x05FB
PHONE_PING_TAIL = 2
DIRECT_PING_FLAG = 3
RECORD_PAGE = 64
RECORD_MAX_PAGES = 30


class GwellError(Exception):
    pass


class SessionExpired(GwellError):
    pass


def _rand31(rnd: Callable[[int], bytes]) -> int:
    v = int.from_bytes(rnd(4), "little") & 0x7FFFFFFF
    return v or 1


def _ms() -> int:
    return int(time.monotonic() * 1000)


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, owner: GwellClient) -> None:
        self.owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            self.owner._on_datagram(data, (addr[0], addr[1]))
        except Exception:
            _LOGGER.exception("dropping datagram after handler error")

    def error_received(self, exc) -> None:
        _LOGGER.debug("udp error: %s", exc)


class GwellClient:

    def __init__(self, account: cloud.P2PAccount, dev_id: int, device_password: str, *,
                 hosts: tuple[str, ...] = cloud.P2P_HOSTS, listsrv_port: int = cloud.LISTSRV_PORT,
                 bind_port: int = 0, rand=os.urandom) -> None:
        self.account = account
        self.dev_id = dev_id
        self._entry = entry_pwd(device_password)
        self.password_enc = gw_encode_password(self._entry)
        self.hosts = hosts
        self.listsrv_port = listsrv_port
        self.bind_port = bind_port
        self._rand = rand
        self.codec = cloud.GuteCodec(account, rand)
        self.server: tuple[str, int] | None = None
        self.server_tag = b"\x00\x00"
        self.start_time = int(time.time())
        self._transport: asyncio.DatagramTransport | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._acks: dict[tuple[int, int], asyncio.Future] = {}
        self._resps: dict[tuple[int, int], asyncio.Future] = {}
        self._listsrv: asyncio.Future | None = None
        self._detect: asyncio.Future | None = None
        self._mesg_waiters: dict[int, asyncio.Future] = {}
        self._call_waiters: dict[int, asyncio.Future] = {}
        self._mesg_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self.media: MediaSession | None = None
        self.registered = False

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._transport, _ = await self._loop.create_datagram_endpoint(
            lambda: _Proto(self), local_addr=("0.0.0.0", self.bind_port), family=socket.AF_INET)
        try:
            await self._bootstrap()
        except BaseException:
            await self.stop()
            raise
        self._spawn(self._heartbeat_loop())

    async def stop(self) -> None:
        if self.media:
            await self.media.close()
            self.media = None
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            try:
                await t
            except BaseException:
                pass
        self._tasks.clear()
        for d in (self._acks, self._resps, self._mesg_waiters, self._call_waiters):
            for f in d.values():
                if not f.done():
                    f.cancel()
            d.clear()
        if self._transport:
            self._transport.close()
            self._transport = None
        self.registered = False

    @property
    def alive(self) -> bool:
        return self._transport is not None and self.registered

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._transport:
            self._transport.sendto(data, addr)

    async def _resolve(self) -> list[str]:
        out: list[str] = []
        for h in self.hosts:
            try:
                infos = await self._loop.getaddrinfo(h, self.listsrv_port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
            except OSError:
                continue
            for info in infos:
                ip = info[4][0]
                if ip not in out:
                    out.append(ip)
        if not out:
            raise GwellError("no p2p bootstrap host resolved")
        return out

    async def _bootstrap(self) -> None:
        ips = await self._resolve()
        self._listsrv = self._loop.create_future()
        frame = cloud.build_listsrv(self.codec)
        for _ in range(3):
            for ip in ips:
                self.sendto(frame, (ip, self.listsrv_port))
            try:
                body = await asyncio.wait_for(asyncio.shield(self._listsrv), RETRY_S * 2)
                break
            except asyncio.TimeoutError:
                continue
        else:
            raise GwellError("no p2p server list")
        servers = cloud.parse_listsrv_reply(body)
        self.server_tag = bytes(body[0:2])
        if not servers:
            raise GwellError("empty p2p server list")
        self._detect = self._loop.create_future()
        for _ in range(3):
            _, frame = cloud.build_detect(self.codec, int(time.time() * 1000))
            for ep in servers:
                self.sendto(frame, (ep.host, ep.port))
            try:
                self.server = await asyncio.wait_for(asyncio.shield(self._detect), RETRY_S * 2)
                break
            except asyncio.TimeoutError:
                continue
        else:
            raise GwellError("no p2p server answered")
        _LOGGER.debug("p2p server selected")
        certify = self._spawn(self._certify())
        await self._send_reliable(*cloud.build_init_info(self.codec))
        self._spawn(self._send_reliable(*cloud.build_heartbeat(self.codec, self.server_tag), need_resp=cloud.ST_HEARTBEAT_RESP))
        await certify
        self.registered = True
        self._spawn(self._send_reliable(*cloud.build_query_friends(self.codec, [self.dev_id]),
                                        need_resp=cloud.ST_QUERY_FRIENDS_RESP, tries=2))

    async def _certify(self) -> None:
        self.codec.mode = cloud.MODE_STATIC
        self.codec.session_key = None
        resp_f = await self._send_reliable(*cloud.build_certify(self.codec), need_ack=True,
                                           need_resp=cloud.ST_CERTIFY_RESP)
        resp = cloud.parse_certify_resp(resp_f)
        if resp.result in (3, 4):
            raise SessionExpired("account session rejected")
        if not resp.modulus:
            raise GwellError("CertifyResp without RSA key")
        material = cloud.build_key_material(self.account, self.start_time, int(time.time()))
        fid, frame = cloud.build_update_key(self.codec, resp, material, self._rand)
        upd = await self._send_reliable(fid, frame, need_ack=True, need_resp=cloud.ST_UPDATE_KEY_RESP)
        if upd.flags & 0xFFFF:
            raise GwellError("UpdateEncKey refused")
        self.codec.set_session_key(material)

    async def _send_reliable(self, fid: int, frame: bytes, *, need_ack: bool = False, need_resp: int | None = None,
                             tries: int = 5) -> cloud.Frame | None:
        if self.server is None:
            raise GwellError("not connected")
        sub = frame[1]
        ack_f = self._loop.create_future() if need_ack else None
        resp_f = self._loop.create_future() if need_resp else None
        if ack_f:
            self._acks[(sub, fid)] = ack_f
        if resp_f:
            self._resps[(need_resp, fid)] = resp_f
        try:
            if not ack_f and not resp_f:
                self.sendto(frame, self.server)
                return None
            for _ in range(tries):
                self.sendto(frame, self.server)
                waits = [f for f in (ack_f, resp_f) if f and not f.done()]
                if not waits:
                    break
                done, _ = await asyncio.wait(waits, timeout=RETRY_S, return_when=asyncio.ALL_COMPLETED)
                if resp_f and resp_f.done():
                    break
                if not resp_f and ack_f.done():
                    break
            if resp_f:
                if not resp_f.done():
                    raise GwellError("no response to 0x%02x" % sub)
                return resp_f.result()
            if not ack_f.done():
                raise GwellError("no ack for 0x%02x" % sub)
            return None
        finally:
            self._acks.pop((sub, fid), None)
            self._resps.pop((need_resp, fid), None)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(cloud.HEARTBEAT_INTERVAL_S)
            try:
                f = await self._send_reliable(*cloud.build_heartbeat(self.codec, self.server_tag),
                                              need_resp=cloud.ST_HEARTBEAT_RESP)
            except GwellError:
                _LOGGER.debug("heartbeat lost")
                self.registered = False
                continue
            flags = f.flags if f else 0
            try:
                if flags & 8:
                    await self._certify()
                if flags & 4:
                    await self._send_reliable(*cloud.build_init_info(self.codec))
            except GwellError:
                self.registered = False
                continue
            self.registered = True

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        if not data:
            return
        if data[0] == 0x7F:
            self._on_gute(data, addr)
        elif data[0] & 0x80 and self.media:
            self.media.on_frame(data, addr)

    def _on_gute(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < cloud.HEADER_LEN:
            return
        if data[1] == cloud.ST_DETECT_RESP:
            if cloud.parse_detect_reply(data) is not None and self._detect and not self._detect.done():
                self._detect.set_result(addr)
            return
        try:
            f = self.codec.decode(data)
        except ValueError:
            _LOGGER.debug("undecodable gute frame 0x%02x", data[1])
            return
        if self.codec.needs_ack(f):
            self.sendto(self.codec.ack(f), addr)
        if f.is_ack:
            fut = self._acks.get((f.subtype, f.frame_id))
            if fut and not fut.done():
                fut.set_result(f)
            return
        if f.is_resp:
            fut = self._resps.get((f.subtype, f.word10))
            if fut and not fut.done():
                fut.set_result(f)
            return
        if f.subtype == cloud.ST_LISTSRV_RESP:
            if self._listsrv and not self._listsrv.done():
                self._listsrv.set_result(f.body)
        elif f.subtype == cloud.ST_MESG:
            self._on_mesg(f, addr)
        elif f.subtype == cloud.ST_CALL_PARAMS:
            try:
                cp = relay.parse_call_params(f.body)
            except ValueError:
                return
            fut = self._call_waiters.get(cp.link_id)
            if fut and not fut.done():
                fut.set_result(cp)

    def _on_mesg(self, f: cloud.Frame, addr) -> None:
        try:
            m = cloud.parse_remote_msg(f)
        except ValueError:
            return
        if m.src_id == self.dev_id:
            _, ack = cloud.build_mesg_ack(self.codec, m)
            self.sendto(ack, addr)
        if m.src_id != self.dev_id or not m.payload:
            return
        fut = self._mesg_waiters.get(m.payload[0])
        if fut and not fut.done():
            fut.set_result(m.payload)

    async def _query(self, payload: bytes, reply_cmd: int) -> bytes:
        async with self._mesg_lock:
            fut = self._loop.create_future()
            self._mesg_waiters[reply_cmd] = fut
            try:
                msg_id = _rand31(self._rand)
                await self._send_reliable(*cloud.build_remote_msg(self.codec, self.dev_id, msg_id, self._entry, payload),
                                          need_ack=True, tries=3)
                return await asyncio.wait_for(fut, MESG_TIMEOUT_S)
            except asyncio.TimeoutError as e:
                raise GwellError("camera did not answer") from e
            finally:
                self._mesg_waiters.pop(reply_cmd, None)

    async def day_list(self, around: date, search_days: int = 0) -> list[date]:
        rep = await self._query(gute.build_day_list_request(around, search_days), gute.CMD_DAY_LIST)
        return gute.parse_day_list_reply(rep).days

    async def record_list(self, day: date) -> list[gute.Recording]:
        start = datetime(day.year, day.month, day.day)
        end = start.replace(hour=23, minute=59)
        found: dict[bytes, gute.Recording] = {}
        for _ in range(RECORD_MAX_PAGES):
            rep = await self._query(gute.build_record_list_request(start, end), gute.CMD_RECORD_LIST_REPLY)
            page = gute.parse_record_list_reply(rep).recordings
            fresh = [r for r in page if r.descriptor not in found]
            for r in fresh:
                found[r.descriptor] = r
            if len(page) < RECORD_PAGE or not fresh:
                break
            end = min(r.start for r in page)
        return list(found.values())

    async def call(self) -> MediaSession:
        if self.media and not self.media.closed:
            return self.media
        link = (int.from_bytes(self._rand(4), "little") & 0xFFFFFFFF) or 1
        fut = self._loop.create_future()
        self._call_waiters[link] = fut
        try:
            await self._send_reliable(*cloud.build_calling(self.codec, link, self.dev_id, self.password_enc),
                                      need_ack=True, tries=3)
            cp = await asyncio.wait_for(fut, CALL_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise GwellError("camera did not accept the call") from e
        finally:
            self._call_waiters.pop(link, None)
        media = MediaSession(self, cp)
        self.media = media
        try:
            await media.start()
        except BaseException:
            await media.close()
            self.media = None
            raise
        return media


class MediaSession:

    def __init__(self, client: GwellClient, cp: relay.CallParams) -> None:
        self.c = client
        self.cp = cp
        self.direct = (cp.callee_public.host, cp.callee_public.port)
        self.relays = [(r.endpoint.host, r.endpoint.port) for r in cp.relays]
        self.direct_ok = False
        self.relay_ok: tuple[str, int] | None = None
        self._ready = asyncio.Event()
        self.closed = False
        self._counter = 0
        self._start_clock = int(time.time() * 1000) & 0xFFFFFFFF
        self.kcp_ctrl = kcp.KCP(cp.control_conv, self._out)
        self.kcp_media = kcp.KCP(cp.media_conv, self._out)
        self._tasks: set[asyncio.Task] = set()
        self.stream_id: int | None = None
        self._stream_params: tuple[int, bytes] | None = None
        self._accept: asyncio.Future | None = None
        self.av: asyncio.Queue[bytes | None] = asyncio.Queue(AV_QUEUE + 2)
        self.markers = 0
        self.last_rx = time.monotonic()

    async def start(self) -> None:
        self._spawn(self._ping_loop())
        self._spawn(self._kcp_loop())
        try:
            await asyncio.wait_for(self._ready.wait(), PUNCH_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise GwellError("no path to the camera") from e

    def _spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        if self.closed:
            return
        if self.stream_id is not None:
            self._send_ctrl(self._close_record())
            self.kcp_ctrl.flush()
        self.closed = True
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            try:
                await t
            except BaseException:
                pass
        self._tasks.clear()
        self._drop_av()
        self.av.put_nowait(None)
        if self.c.media is self:
            self.c.media = None

    def _drop_av(self) -> None:
        while not self.av.empty():
            self.av.get_nowait()

    def _ping(self, direct: bool) -> bytes:
        p = relay.Ping(relay.PING, self.cp.link_id, self.c.account.term_id, self.c.dev_id, self._counter,
                       int(time.time() * 1000), DIRECT_PING_FLAG if direct else 0, relay.PING_LEN, 0,
                       PHONE_SIDE_TAG, self._start_clock, PHONE_PING_TAIL)
        if direct:
            return relay.build(relay.TYPE_DIRECT_CTRL, relay.build_ping(p))
        return relay.build(relay.TYPE_RELAY_CTRL_OUT, relay.build_ping(p), self.c.dev_id)

    def _ping_round(self) -> None:
        self._counter += 1
        self.c.sendto(self._ping(True), self.direct)
        for r in self.relays:
            self.c.sendto(self._ping(False), r)

    async def _ping_loop(self) -> None:
        last = 0.0
        for at in PING_BURST_S:
            await asyncio.sleep(at - last)
            last = at
            self._ping_round()
        while True:
            await asyncio.sleep(PING_PERIOD_S)
            self._ping_round()

    def _out(self, data: bytes) -> None:
        if self.direct_ok:
            self.c.sendto(relay.build(relay.TYPE_DIRECT_DATA, data), self.direct)
        elif self.relay_ok:
            self.c.sendto(relay.build(relay.TYPE_RELAY_DATA_OUT, data, self.c.dev_id), self.relay_ok)

    def on_frame(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.closed:
            return
        try:
            f = relay.parse(data)
        except ValueError:
            return
        if f.is_ctrl:
            p = relay.parse_ping(f.payload)
            if p is None or p.link_id != self.cp.link_id:
                return
            via_relay = f.dest_id is not None
            if p.is_ping:
                pong = relay.build_ping(relay.pong_for(p, self.c.account.term_id, via_relay))
                if via_relay:
                    self.c.sendto(relay.build(relay.TYPE_RELAY_CTRL_OUT, pong, self.c.dev_id), addr)
                else:
                    self.c.sendto(relay.build(relay.TYPE_DIRECT_CTRL, pong), addr)
            elif via_relay:
                if self.relay_ok is None:
                    self.relay_ok = addr
            else:
                self.direct_ok = True
            if self.direct_ok or self.relay_ok:
                self._ready.set()
            return
        if f.ftype in (relay.TYPE_DIRECT_DATA, relay.TYPE_RELAY_DATA_IN):
            self.last_rx = time.monotonic()
            self.kcp_ctrl.input(f.payload)
            self.kcp_media.input(f.payload)
            self._drain()

    async def _kcp_loop(self) -> None:
        while True:
            now = kcp_now()
            self.kcp_ctrl.update(now)
            self.kcp_media.update(now)
            self._drain()
            if self.kcp_ctrl.dead or self.kcp_media.dead:
                _LOGGER.debug("kcp link dead")
                self._spawn(self.close())
                return
            await asyncio.sleep(KCP_TICK_S)

    def _drain(self) -> None:
        while (m := self.kcp_ctrl.recv()) is not None:
            self._on_record(m, control=True)
        while self.av.qsize() < AV_QUEUE and (m := self.kcp_media.recv()) is not None:
            self._on_record(m, control=False)

    def _on_record(self, data: bytes, control: bool) -> None:
        try:
            rec = mtp.parse_record(data)
        except ValueError:
            return
        if rec.type == mtp.TYPE_STREAM_CONTROL and len(rec.body) == mtp.STREAM_CONTROL_LEN:
            sc = mtp.parse_stream_control(rec.body)
            if sc.session_id != self.stream_id:
                return
            if sc.sub_cmd == mtp.SC_ACCEPT and self._accept and not self._accept.done():
                self._accept.set_result(sc)
            elif sc.sub_cmd == mtp.SC_CLOSE and control:
                self.stream_id = None
                self.av.put_nowait(None)
        elif rec.type == mtp.TYPE_AV and not control and self.stream_id is not None and self.av.qsize() < AV_QUEUE:
            self.av.put_nowait(rec.body)

    def _send_ctrl(self, record: bytes) -> None:
        self.kcp_ctrl.send(record)

    def _close_record(self) -> bytes:
        body = struct.pack("<II", self.stream_id or 0, mtp.SC_CLOSE) + bytes(mtp.STREAM_CONTROL_LEN - 8)
        return mtp.build_record(mtp.TYPE_STREAM_CONTROL, body)

    async def open_stream(self, call_type: int, param: bytes) -> None:
        if self.stream_id is not None:
            self._send_ctrl(self._close_record())
        self._drop_av()
        self.markers = 0
        sid = int.from_bytes(self.c._rand(4), "little") or 1
        self.stream_id = sid
        self._stream_params = (call_type, param)
        start = mtp.build_record(mtp.TYPE_STREAM_CONTROL,
                                 mtp.build_stream_control(sid, mtp.SC_START, call_type, param, self.c.password_enc))
        self._accept = asyncio.get_running_loop().create_future()
        deadline = time.monotonic() + START_TIMEOUT_S
        while not self._accept.done():
            if time.monotonic() > deadline:
                self.stream_id = None
                raise GwellError("camera did not accept the stream")
            self._send_ctrl(start)
            try:
                await asyncio.wait_for(asyncio.shield(self._accept), START_RETRY_S * 2)
            except asyncio.TimeoutError:
                pass
        init = mtp.build_record(mtp.TYPE_STREAM_CONTROL,
                                mtp.build_stream_control(sid, mtp.SC_READY, call_type, param, 0))
        self.kcp_media.send(init)
        self._send_ctrl(start)

    async def open_playback(self, descriptor: bytes) -> None:
        await self.open_stream(mtp.CALL_PLAYBACK, descriptor)

    def switch_file(self, descriptor: bytes, offset_s: int) -> None:
        nonce = self.c._rand(1)[0] & 0x0F
        self.kcp_media.send(mtp.build_user_data_record(mtp.build_switch_file(descriptor, offset_s), nonce))

    def close_stream(self) -> None:
        if self.stream_id is not None and not self.closed:
            self._send_ctrl(self._close_record())
            self.stream_id = None
        self._drop_av()

    async def read(self, timeout: float) -> bytes | None:
        try:
            item = await asyncio.wait_for(self.av.get(), timeout)
        except asyncio.TimeoutError:
            return None
        self._drain()
        return item


def kcp_now() -> int:
    return _ms() & 0xFFFFFFFF
