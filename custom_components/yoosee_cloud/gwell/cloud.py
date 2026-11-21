from __future__ import annotations

import ipaddress
import os
import struct
from dataclasses import dataclass, field

from .crypto import RC5, gute_decrypt, gute_encrypt
from .gute import HEADER_LEN, OPT_ACK, OPT_RESP, build_plain_frame, checksum, parse_header
from .relay import Endpoint
from .rsa import pkcs1_v15_encrypt

ST_DETECT = 0x01
ST_DETECT_RESP = 0x02
ST_CERTIFY = 0x03
ST_CERTIFY_RESP = 0x04
ST_UPDATE_KEY = 0x05
ST_UPDATE_KEY_RESP = 0x06
ST_HEARTBEAT = 0x21
ST_HEARTBEAT_RESP = 0x22
ST_CALL_PARAMS = 0x24
ST_CALLING = 0x25
ST_MESG = 0x27
ST_MESG_ACK = 0x28
ST_LISTSRV = 0x2B
ST_LISTSRV_RESP = 0x2C
ST_QUERY_FRIENDS = 0x2D
ST_QUERY_FRIENDS_RESP = 0x2E
ST_SRV_FORWARD = 0x3A
ST_INIT_INFO = 0x81

PROC_NONE = 0
PROC_ACK = 1
PROC_RESP = 2
PROC_BOTH = 3

MODE_STATIC = 1
MODE_SESSION = 2

LISTSRV_PORT = 51701
P2P_HOSTS = (
    "p2p1.cloudlinks.cn", "p2p2.cloudlinks.cn", "p2p3.cloud-links.net", "p2p4.cloud-links.net",
    "p2p5.cloudlinks.cn", "p2p6.cloudlinks.cn", "p2p7.cloudlinks.cn", "p2p8.cloudlinks.cn",
    "p2p9.cloudlinks.cn", "p2p10.cloudlinks.cn",
)
HEARTBEAT_INTERVAL_S = 50.0
EMPTY_PUBKEY_HASH = 0xD4D79C1A

INIT_INFO_BODY = bytes.fromhex(
    "2f062e000106030000000000000002000300000000000000000000002500000000000000000000000000000000000000"
)

_M = 0xFFFFFFFF


def material_hash(data: bytes) -> int:
    h = 0x4E67C6A7
    for b in data:
        h = (h ^ ((h * 32 + b + (h >> 2)) & _M)) & _M
    return h


@dataclass(frozen=True)
class P2PAccount:

    user_id: int
    session_id1: int = field(repr=False)
    session_id2: int = field(repr=False)
    code1: int = field(repr=False)
    code2: int = field(repr=False)
    cloud_user_id: str | None = field(default=None, repr=False)
    cloud_session_id: str | None = field(default=None, repr=False)
    access_id: str | None = field(default=None, repr=False)
    access_token: str | None = field(default=None, repr=False)
    area: str | None = field(default=None, repr=False)
    reg_region: str | None = field(default=None, repr=False)

    @property
    def legacy_user_id(self) -> str | None:
        if self.cloud_user_id is None:
            return None
        raw = self.cloud_user_id
        if not raw or not raw.lstrip("-").isdigit():
            return raw
        n = int(raw)
        if not -(1 << 31) <= n < (1 << 31):
            return raw
        return "0" + str(n & 0x7FFFFFFF)

    @property
    def term_id(self) -> int:
        return self.user_id & _M

    @property
    def code_bytes(self) -> bytes:
        return struct.pack("<II", self.code1 & _M, self.code2 & _M)

    @property
    def session_bytes(self) -> bytes:
        return struct.pack("<II", self.session_id1 & _M, self.session_id2 & _M)


@dataclass(frozen=True)
class Frame:
    subtype: int
    term_id: int
    frame_id: int
    opt: int
    b15: int
    flags: int
    word10: int
    body: bytes
    raw_plain: bytes

    @property
    def mode(self) -> int:
        return self.opt & 3

    @property
    def proc(self) -> int:
        return (self.opt >> 2) & 3

    @property
    def is_ack(self) -> bool:
        return bool(self.opt & OPT_ACK)

    @property
    def is_resp(self) -> bool:
        return bool(self.opt & OPT_RESP)


class GuteCodec:

    def __init__(self, account: P2PAccount, rand=os.urandom) -> None:
        self.account = account
        self._rand = rand
        self.next_id = int.from_bytes(rand(4), "little") & 0x3FFFFFFF
        self.mode = MODE_STATIC
        self.session_key: RC5 | None = None
        self.material: bytes | None = None

    def _rnd6(self) -> int:
        return self._rand(1)[0] & 0x3F

    def take_id(self) -> int:
        self.next_id = (self.next_id + 1) & _M
        return self.next_id

    def set_session_key(self, material: bytes) -> None:
        self.material = bytes(material)
        self.session_key = RC5(self.material)
        self.mode = MODE_SESSION

    def build(self, subtype: int, body: bytes, *, proc: int, flags: int = 0, mode: int | None = None,
              frame_id: int | None = None, trailer: bool | None = None, b15: int | None = None,
              term_id: int | None = None) -> tuple[int, bytes]:
        mode = self.mode if mode is None else mode
        if trailer is None:
            trailer = mode == MODE_STATIC and subtype != ST_CERTIFY
        if trailer:
            body = bytes(body) + self.account.code_bytes
        fid = self.take_id() if frame_id is None else frame_id
        if b15 is None:
            b15 = (self._rnd6() << 2) | (1 if trailer else 0)
        plain = build_plain_frame(
            subtype, body, sender_id=self.account.term_id if term_id is None else term_id, frame_id=fid,
            opt=(mode & 3) | ((proc & 3) << 2), opt_tail=bytes((b15 & 0xFF, flags & 0xFF, (flags >> 8) & 0xFF)),
        )
        return fid, gute_encrypt(plain, self.session_key)

    def decode(self, wire: bytes) -> Frame:
        if len(wire) < HEADER_LEN or wire[0] != 0x7F:
            raise ValueError("not a gute frame")
        plain = gute_decrypt(wire, self.session_key) if wire[0x14] & 3 else bytes(wire)
        if not plain[0x14] & OPT_RESP and struct.unpack_from("<I", plain, 0x10)[0] != checksum(plain):
            raise ValueError("gute checksum mismatch")
        h = parse_header(plain)
        return Frame(h.subtype, h.sender_id, h.frame_id, h.opt, plain[0x15],
                     plain[0x16] | (plain[0x17] << 8), h.checksum, bytes(plain[HEADER_LEN:h.length]), plain)

    def ack(self, f: Frame, result: int = 0) -> bytes:
        mode = 1 if f.mode == 1 else self.mode
        p = bytearray(HEADER_LEN)
        p[0] = 0x7F
        p[1] = f.subtype
        struct.pack_into("<H", p, 2, HEADER_LEN)
        p[4:12] = f.raw_plain[4:12]
        struct.pack_into("<I", p, 0x0C, f.frame_id)
        struct.pack_into("<H", p, 0x14, (mode & 3) | OPT_ACK)
        struct.pack_into("<H", p, 0x16, result & 0xFFFF)
        struct.pack_into("<I", p, 0x10, checksum(p))
        return gute_encrypt(bytes(p), self.session_key)

    def needs_ack(self, f: Frame) -> bool:
        if f.is_ack:
            return False
        return f.proc != 0 if f.is_resp else f.proc in (PROC_ACK, PROC_BOTH)

    def response_to(self, f: Frame) -> int | None:
        return f.word10 if f.is_resp else None


def build_listsrv(codec: GuteCodec) -> bytes:
    return codec.build(ST_LISTSRV, bytes(12), proc=PROC_NONE, mode=MODE_STATIC, frame_id=0, trailer=False, b15=0)[1]


def parse_listsrv_reply(body: bytes) -> list[Endpoint]:
    if len(body) < 12:
        raise ValueError("listsrv reply too short")
    n = body[6]
    out = []
    for i in range(n):
        o = 12 + 12 * i
        if o + 8 > len(body):
            break
        ip = str(ipaddress.IPv4Address(bytes(body[o:o + 4])))
        port = struct.unpack_from(">H", body, o + 4)[0]
        out.append(Endpoint(ip, port))
    return out


def build_detect(codec: GuteCodec, tick_ms: int) -> tuple[int, bytes]:
    return codec.build(ST_DETECT, struct.pack("<Q", tick_ms & 0xFFFFFFFFFFFFFFFF), proc=PROC_NONE,
                       mode=MODE_STATIC, trailer=True, b15=0)


def parse_detect_reply(wire: bytes) -> int | None:
    if len(wire) < HEADER_LEN + 8 or wire[0] != 0x7F or wire[1] != ST_DETECT_RESP:
        return None
    return struct.unpack_from("<Q", wire, HEADER_LEN)[0]


def build_certify(codec: GuteCodec, pubkey_hash: int = EMPTY_PUBKEY_HASH) -> tuple[int, bytes]:
    body = codec.account.code_bytes + codec.account.session_bytes + struct.pack("<I", pubkey_hash & _M)
    return codec.build(ST_CERTIFY, body, proc=PROC_BOTH, mode=MODE_STATIC, flags=1, trailer=False)


@dataclass(frozen=True)
class CertifyResp:
    result: int
    token: int
    bits: int
    modulus: bytes = field(repr=False)


def parse_certify_resp(f: Frame) -> CertifyResp:
    b = f.body
    if len(b) < 8:
        raise ValueError("CertifyResp too short")
    token, klen, bits = struct.unpack_from("<IHH", b, 0)
    if klen > 128 or len(b) < 8 + klen:
        raise ValueError("CertifyResp key truncated")
    return CertifyResp(f.flags & 0xFFFF, token, bits, bytes(b[8:8 + klen]))


def build_key_material(account: P2PAccount, start_time: int, now: int) -> bytes:
    return (struct.pack("<IIII", start_time & _M, (now // 7200) & _M, account.term_id, 0)
            + account.code_bytes + bytes(8))


def build_update_key(codec: GuteCodec, resp: CertifyResp, material: bytes, rand=os.urandom) -> tuple[int, bytes]:
    ct = pkcs1_v15_encrypt(material, resp.modulus, rand=rand)
    body = struct.pack("<IIH", resp.token & _M, material_hash(material), len(ct)) + ct
    return codec.build(ST_UPDATE_KEY, body, proc=PROC_BOTH, mode=MODE_STATIC, flags=1)


def build_heartbeat(codec: GuteCodec, server_tag: bytes = b"\x00\x00", net_type: int = 4) -> tuple[int, bytes]:
    body = b"\x00" + bytes(server_tag[:2]).ljust(2, b"\0") + b"\x00" + bytes((net_type & 0xFF,)) + bytes(15)
    return codec.build(ST_HEARTBEAT, body, proc=PROC_RESP)


def build_init_info(codec: GuteCodec) -> tuple[int, bytes]:
    return codec.build(ST_INIT_INFO, INIT_INFO_BODY, proc=PROC_NONE)


def build_query_friends(codec: GuteCodec, dev_ids: list[int]) -> tuple[int, bytes]:
    body = bytearray(8 + 8 * len(dev_ids))
    struct.pack_into("<H", body, 6, len(dev_ids))
    for i, d in enumerate(dev_ids):
        struct.pack_into("<I", body, 8 + 8 * i, d & _M)
    return codec.build(ST_QUERY_FRIENDS, bytes(body), proc=PROC_RESP, flags=1)


@dataclass(frozen=True)
class FriendStatus:
    dev_id: int
    online: bool
    raw: bytes = field(repr=False)


def parse_query_friends_resp(f: Frame) -> list[FriendStatus]:
    b = f.body
    if len(b) < 8:
        return []
    dev, n = struct.unpack_from("<II", b, 0)
    return [FriendStatus(dev, n != 0, bytes(b))]


def build_calling(codec: GuteCodec, link_id: int, dev_id: int, password_enc: int | None) -> tuple[int, bytes]:
    body = bytearray(0x78 - HEADER_LEN)
    struct.pack_into("<IQQ", body, 0, link_id & _M, codec.account.term_id, dev_id & _M)
    body[0x2E - HEADER_LEN] |= 1
    if password_enc is not None:
        struct.pack_into("<I", body, 0x68 - HEADER_LEN, password_enc & _M)
    return codec.build(ST_CALLING, bytes(body), proc=PROC_ACK, flags=2)


def build_remote_msg(codec: GuteCodec, dev_id: int, msg_id: int, password_int: int, payload: bytes) -> tuple[int, bytes]:
    body = struct.pack("<IIHHI", dev_id & _M, codec.account.term_id, 0, 0, msg_id & _M)
    flags = 0
    if password_int:
        body += struct.pack("<I", password_int & _M)
        flags = 1
    return codec.build(ST_MESG, body + bytes(payload), proc=PROC_ACK, flags=flags)


@dataclass(frozen=True)
class RemoteMsg:
    dest_id: int
    src_id: int
    w20: int
    w22: int
    msg_id: int
    payload: bytes


def parse_remote_msg(f: Frame) -> RemoteMsg:
    if len(f.body) < 16:
        raise ValueError("MESG body too short")
    d, s, a, b, m = struct.unpack_from("<IIHHI", f.body, 0)
    return RemoteMsg(d, s, a, b, m, bytes(f.body[16:]))


def build_mesg_ack(codec: GuteCodec, msg: RemoteMsg, lan_bit: int = 0, result: int = 0) -> tuple[int, bytes]:
    body = struct.pack("<IIHHII", msg.src_id, msg.dest_id, msg.w22, msg.w20, msg.msg_id, result & _M)
    fid, wire = codec.build(ST_MESG_ACK, body, proc=PROC_ACK, term_id=msg.dest_id)
    return fid, wire
