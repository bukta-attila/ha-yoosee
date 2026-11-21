import asyncio
import random
import socket
import struct
from datetime import datetime, timedelta

from _support import DEV_ID
from gwell import cloud, gute, kcp, mtp, relay
from gwell.crypto import RC5, gute_decrypt, gute_encrypt

RECORDS = [(datetime(2026, 11, 17, 9, 33, 10), 60), (datetime(2026, 11, 17, 8, 46, 51), 236)]


def _prime(bits, rnd):
    while True:
        n = rnd.getrandbits(bits) | (1 << (bits - 1)) | 1
        if n % 3 != 2:
            continue
        if all(pow(a, n - 1, n) == 1 for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)):
            return n


def rsa_key(rnd):
    p, q = _prime(256, rnd), _prime(256, rnd)
    n = p * q
    return n, pow(3, -1, (p - 1) * (q - 1))


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, cb):
        self.cb = cb
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.cb(data, addr)


class FakeServer:

    def __init__(self, account, camera):
        self.acc = account
        self.camera = camera
        self.rnd = random.Random(1)
        self.n, self.d = rsa_key(self.rnd)
        self.key = None
        self.seen = []
        self.mesg_payloads = []
        self.calls = 0
        self.records = list(RECORDS)

    async def start(self):
        loop = asyncio.get_running_loop()
        self.tr, self.p = await loop.create_datagram_endpoint(lambda: _Udp(self.on), local_addr=("127.0.0.1", 0),
                                                              family=socket.AF_INET)
        self.port = self.tr.get_extra_info("sockname")[1]

    def close(self):
        self.tr.close()

    def _frame(self, sub, body, *, opt, word10=None, fid=None, mode=1):
        plain = gute.build_plain_frame(sub, body, sender_id=self.acc.term_id, frame_id=fid or self.rnd.getrandbits(32),
                                       opt=opt | mode, chk=word10)
        return gute_encrypt(plain, RC5(self.key) if self.key and mode == 2 else None)

    def resp(self, req, body, addr, mode=1):
        self.tr.sendto(self._frame(req[1] + 1, body, opt=0x20 | 0x04, word10=struct.unpack_from("<I", req, 0x0C)[0],
                                   mode=mode), addr)

    def ack(self, plain, addr):
        p = bytearray(24)
        p[0:2] = plain[0:2]
        struct.pack_into("<H", p, 2, 24)
        p[4:16] = plain[4:16]
        p[0x14] = (plain[0x14] & 3) | 0x10
        struct.pack_into("<I", p, 0x10, gute.checksum(p))
        self.tr.sendto(gute_encrypt(bytes(p), RC5(self.key) if self.key else None), addr)

    def on(self, data, addr):
        sub = data[1]
        self.seen.append(sub)
        if sub == cloud.ST_LISTSRV:
            body = b"\x50\x47" + bytes(4) + b"\x01\x01" + bytes(4) + bytes((127, 0, 0, 1)) + struct.pack(">HH", self.port, self.port) + bytes(4)
            self.tr.sendto(self._frame(cloud.ST_LISTSRV_RESP, body, opt=0, mode=1), addr)
            return
        if sub == cloud.ST_DETECT:
            p = gute_decrypt(data)
            out = bytearray(24) + p[0x18:0x20] + b"\x06\x00\x06\x00"
            out[0], out[1] = 0x7F, cloud.ST_DETECT_RESP
            struct.pack_into("<H", out, 2, len(out))
            out[0x14] = 0x20
            self.tr.sendto(bytes(out), addr)
            return
        p = gute_decrypt(data, RC5(self.key) if self.key else None)
        if p[0x14] & 0x10:
            return
        if (p[0x14] >> 2) & 1:
            self.ack(p, addr)
        body = p[0x18:struct.unpack_from("<H", p, 2)[0]]
        if sub == cloud.ST_CERTIFY:
            assert body[:8] == self.acc.code_bytes and body[8:16] == self.acc.session_bytes
            self.resp(p, struct.pack("<IHH", 0xAC71AC69, 64, 512) + self.n.to_bytes(64, "big"), addr)
        elif sub == cloud.ST_UPDATE_KEY:
            tok, h, klen = struct.unpack_from("<IIH", body, 0)
            m = pow(int.from_bytes(body[10:10 + klen], "big"), self.d, self.n).to_bytes(64, "big")
            assert m[:2] == b"\x00\x02"
            material = m[m.index(0, 2) + 1:]
            assert len(material) == 32 and cloud.material_hash(material) == h and tok == 0xAC71AC69
            self.resp(p, b"", addr)
            self.key = material
        elif sub == cloud.ST_HEARTBEAT:
            self.resp(p, bytes(20), addr, mode=2 if self.key else 1)
        elif sub == cloud.ST_QUERY_FRIENDS:
            self.resp(p, struct.pack("<II", DEV_ID, 1) + bytes(8), addr, mode=2)
        elif sub == cloud.ST_MESG:
            dest, src, _, _, msg_id = struct.unpack_from("<IIHHI", body, 0)
            assert dest == DEV_ID and p[0x16] & 1
            payload = body[20:]
            self.mesg_payloads.append((body[16:20], payload))
            if payload[0] == gute.CMD_RECORD_LIST:
                start, end = gute.parse_record_list_request(payload)
                end += timedelta(minutes=1)
                page = sorted((r for r in self.records if start <= r[0] < end), reverse=True)[:64]
                descs = b"".join(gute.build_descriptor(t) for t, _ in page)
                rep = bytes((4, 1, 0, len(page))) + descs + b"".join(struct.pack("<H", d) for _, d in page)
            else:
                rep = bytes((0xFA, 25, 1, 2)) + struct.pack("<HBBHBB", 2026, 11, 16, 2026, 11, 17) + bytes(12)
            out = struct.pack("<IIHHI", src, DEV_ID, 0, 0x48, 0x473C) + rep
            self.tr.sendto(self._frame(cloud.ST_MESG, out, opt=0x04), addr)
        elif sub == cloud.ST_CALLING:
            self.calls += 1
            link, caller, callee = struct.unpack_from("<IQQ", body, 0)
            assert callee == DEV_ID and struct.unpack_from("<I", body, 0x50)[0] == self.camera.password_enc
            self.camera.link = link
            cb = bytearray(0x50 + 16)
            struct.pack_into("<IQQ", cb, 0, link, caller, callee)
            struct.pack_into(">H", cb, 0x34, self.camera.port)
            cb[0x38:0x3C] = bytes((127, 0, 0, 1))
            struct.pack_into("<H", cb, 0x4E, 1)
            struct.pack_into("<IIHH", cb, 0x50, 5101, 0, 7, self.camera.port + 1)
            cb[0x5C:0x60] = bytes((127, 0, 0, 1))
            self.tr.sendto(self._frame(cloud.ST_CALL_PARAMS, bytes(cb), opt=0x04), addr)


class FakeCamera:

    def __init__(self, password_enc, av_source):
        self.password_enc = password_enc
        self.av = av_source
        self.link = None
        self.peer = None
        self.k_ctrl = self.k_media = None
        self.records = []
        self.closed_streams = 0
        self.sent = 0
        self.task = None

    async def start(self):
        loop = asyncio.get_running_loop()
        self.tr, _ = await loop.create_datagram_endpoint(lambda: _Udp(self.on), local_addr=("127.0.0.1", 0),
                                                         family=socket.AF_INET)
        self.port = self.tr.get_extra_info("sockname")[1]
        self.task = asyncio.ensure_future(self.tick())

    def close(self):
        if self.task:
            self.task.cancel()
        self.tr.close()

    def out(self, data):
        if self.peer:
            self.tr.sendto(relay.build(relay.TYPE_DIRECT_DATA, data), self.peer)

    def on(self, data, addr):
        f = relay.parse(data)
        if f.is_ctrl:
            p = relay.parse_ping(f.payload)
            if p and p.is_ping:
                self.peer = addr
                if self.k_ctrl is None:
                    self.k_ctrl = kcp.KCP(p.link_id | 0x80000000, self.out)
                    self.k_media = kcp.KCP(p.link_id & 0x7FFFFFFF, self.out)
                self.tr.sendto(relay.build(relay.TYPE_DIRECT_CTRL, relay.build_ping(relay.pong_for(p, DEV_ID, False))), addr)
            return
        self.k_ctrl.input(f.payload)
        self.k_media.input(f.payload)

    async def tick(self):
        now = 0
        while True:
            await asyncio.sleep(0.005)
            now += 5
            if self.k_ctrl is None:
                continue
            for k, ctrl in ((self.k_ctrl, True), (self.k_media, False)):
                while (m := k.recv()) is not None:
                    self.handle(mtp.parse_record(m), ctrl)
            if self.stream and len(self.k_media.snd_queue) < 32:
                chunk = self.av.read(1000)
                if chunk:
                    self.k_media.send(mtp.build_record(mtp.TYPE_AV, chunk))
                    self.sent += len(chunk)
            self.k_ctrl.update(now)
            self.k_media.update(now)

    stream = None

    def handle(self, rec, ctrl):
        if rec.type == mtp.TYPE_STREAM_CONTROL:
            sc = mtp.parse_stream_control(rec.body)
            self.records.append(("sc", sc.sub_cmd, sc.call_type, sc.param, sc.password_enc))
            if sc.sub_cmd == mtp.SC_START and ctrl:
                acc = mtp.build_stream_control(sc.session_id, mtp.SC_ACCEPT, sc.call_type, b"\0" * 4 + b"d\0\0\0", 0)
                self.k_ctrl.send(mtp.build_record(mtp.TYPE_STREAM_CONTROL, acc))
                if self.stream is None:
                    self.stream = sc.session_id
                    self.av.open(None if sc.call_type == mtp.CALL_LIVE else sc.param, 0)
            elif sc.sub_cmd == mtp.SC_CLOSE:
                self.closed_streams += 1
                self.stream = None
        elif rec.type == mtp.TYPE_AV:
            ud = mtp.parse_user_data(rec.body)
            desc, off = mtp.parse_switch_file(ud)
            self.records.append(("switch", desc, off))
            self.av.open(desc, off)


class AvSource:

    def __init__(self, frames_blob):
        from gwell import av
        offs = [it.offset for it in av.parse_stream([frames_blob])] + [len(frames_blob)]
        self.units = [frames_blob[a:b] for a, b in zip(offs, offs[1:])]
        self.idx = 0
        self.cur = b""
        self.pending = b""

    def open(self, desc, off):
        self.idx = 0
        if desc is None:
            self.pending = b""
            return
        marks = b""
        if off:
            marks += mtp.build_user_data(3, 10, struct.pack("<I", 1) + desc)
        self.pending = marks + mtp.build_user_data(3, 1, bytes(20))
        self.idx = 0

    def read(self, n):
        if not self.cur:
            if self.pending:
                self.cur, self.pending = self.pending, b""
            elif self.idx < len(self.units):
                self.cur = self.units[self.idx]
                self.idx += 1
        out, self.cur = self.cur[:n], self.cur[n:]
        return out
