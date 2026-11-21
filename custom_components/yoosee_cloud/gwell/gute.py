from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .crypto import RC5, gute_decrypt, gute_encrypt

MAGIC = 0x7F
HEADER_LEN = 0x18
SUBTYPE_MESG = 0x27

OPT_MODE_MASK = 0x03
OPT_DATA = 0x04
OPT_ACK = 0x10
OPT_RESP = 0x20

MODE_PLAIN = 0
MODE_STATIC = 1
MODE_SESSION = 2

_M = 0xFFFFFFFF


def checksum(frame: bytes) -> int:
    w = struct.unpack_from("<6I", frame, 0)
    return (w[0] + w[1] + w[2] + w[3] + w[5]) & _M


def checksum_checked(frame: bytes) -> bool:
    return not frame[0x14] & OPT_RESP


def checksum_ok(plain: bytes) -> bool:
    return struct.unpack_from("<I", plain, 0x10)[0] == checksum(plain)


@dataclass(frozen=True)
class GuteHeader:
    subtype: int
    length: int
    sender_id: int
    w2: int
    frame_id: int
    checksum: int
    opt: int
    opt_tail: bytes
    magic: int = MAGIC

    @property
    def mode(self) -> int:
        return self.opt & OPT_MODE_MASK


@dataclass(frozen=True)
class GuteFrame:
    header: GuteHeader
    body: bytes
    checksum_ok: bool | None


def parse_header(plain: bytes) -> GuteHeader:
    if len(plain) < HEADER_LEN:
        raise ValueError("gute frame shorter than its header")
    sender_id, w2, frame_id, chk = struct.unpack_from("<4I", plain, 4)
    return GuteHeader(
        subtype=plain[1],
        length=struct.unpack_from("<H", plain, 2)[0],
        sender_id=sender_id,
        w2=w2,
        frame_id=frame_id,
        checksum=chk,
        opt=plain[0x14],
        opt_tail=bytes(plain[0x15:0x18]),
        magic=plain[0],
    )


def decode_frame(frame: bytes, session_key: bytes | RC5 | None = None) -> GuteFrame:
    plain = gute_decrypt(frame, session_key)
    hdr = parse_header(plain)
    ok = checksum_ok(plain) if checksum_checked(plain) else None
    return GuteFrame(hdr, bytes(plain[HEADER_LEN:]), ok)


def build_plain_frame(
    subtype: int,
    body: bytes,
    *,
    sender_id: int,
    frame_id: int,
    opt: int = OPT_DATA | MODE_STATIC,
    opt_tail: bytes = b"\x00\x00\x00",
    w2: int = 0,
    chk: int | None = None,
    magic: int = MAGIC,
) -> bytes:
    if len(opt_tail) != 3:
        raise ValueError("opt_tail must be 3 bytes")
    total = HEADER_LEN + len(body)
    if total > 0xFFFF:
        raise ValueError("gute frame too long")
    f = bytearray(HEADER_LEN) + bytes(body)
    f[0] = magic
    f[1] = subtype
    struct.pack_into("<HIII", f, 2, total, sender_id & _M, w2 & _M, frame_id & _M)
    f[0x14] = opt
    f[0x15:0x18] = opt_tail
    struct.pack_into("<I", f, 0x10, checksum(f) if chk is None else chk & _M)
    return bytes(f)


def build_frame(
    subtype: int,
    body: bytes,
    *,
    sender_id: int,
    frame_id: int,
    opt: int = OPT_DATA | MODE_STATIC,
    opt_tail: bytes = b"\x00\x00\x00",
    w2: int = 0,
    chk: int | None = None,
    magic: int = MAGIC,
    session_key: bytes | RC5 | None = None,
) -> bytes:
    plain = build_plain_frame(
        subtype, body, sender_id=sender_id, frame_id=frame_id, opt=opt,
        opt_tail=opt_tail, w2=w2, chk=chk, magic=magic,
    )
    return gute_encrypt(plain, session_key)


MESG_ENVELOPE_LEN = 20
MESG_REPLY_HEADER_LEN = 16

CMD_RECORD_LIST = 3
CMD_RECORD_LIST_REPLY = 4
SUBCMD_RECORD_LIST = 1
CMD_DAY_LIST = 0xFA
DAY_LIST_VERSION = 24
DAY_LIST_SLOTS = 5


@dataclass(frozen=True)
class MesgEnvelope:
    dev_id: int
    password: int = field(repr=False)
    msg_id: int
    flag: int
    length: int


def build_mesg(dev_id: int, password_int: int, msg_id: int, payload: bytes, flag: int = 0) -> bytes:
    return struct.pack("<5I", dev_id & _M, password_int & _M, msg_id & _M, flag & _M, len(payload)) + bytes(payload)


def parse_mesg(body: bytes) -> tuple[MesgEnvelope, bytes]:
    if len(body) < MESG_ENVELOPE_LEN:
        raise ValueError("MESG body shorter than its envelope")
    env = MesgEnvelope(*struct.unpack_from("<5I", body, 0))
    return env, bytes(body[MESG_ENVELOPE_LEN:MESG_ENVELOPE_LEN + env.length])


@dataclass(frozen=True)
class MesgReplyHeader:
    dest_id: int
    dev_id: int
    flag: int
    msg_id: int


def parse_mesg_reply(body: bytes) -> tuple[MesgReplyHeader, bytes]:
    if len(body) < MESG_REPLY_HEADER_LEN + 4:
        raise ValueError("MESG reply too short")
    hdr = MesgReplyHeader(*struct.unpack_from("<4I", body, 0))
    return hdr, bytes(body[MESG_REPLY_HEADER_LEN:])


def build_record_list_request(start: datetime, end: datetime) -> bytes:
    return struct.pack(
        "<BBHhBBBBhBBBB",
        CMD_RECORD_LIST, SUBCMD_RECORD_LIST, 0,
        start.year, start.month, start.day, start.hour, start.minute,
        end.year, end.month, end.day, end.hour, end.minute,
    )


def build_day_record_list_request(day: date) -> bytes:
    d = datetime(day.year, day.month, day.day)
    return build_record_list_request(d, d.replace(hour=23, minute=59))


def parse_record_list_request(payload: bytes) -> tuple[datetime, datetime]:
    f = struct.unpack_from("<BBHhBBBBhBBBB", payload, 0)
    if f[0] != CMD_RECORD_LIST:
        raise ValueError("not a record-list request")
    return datetime(*f[3:8]), datetime(*f[8:13])


DESCRIPTOR_LEN = 8
DESCRIPTOR_MONTH_FLAGS = 0x10


@dataclass(frozen=True)
class Recording:
    start: datetime
    duration: int
    rec_type: str
    descriptor: bytes

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration)


def build_descriptor(start: datetime, rec_type: str = "A", month_flags: int = DESCRIPTOR_MONTH_FLAGS) -> bytes:
    return struct.pack(
        "<HBBBBBB", start.year, (start.month & 0x0F) | month_flags,
        start.day, start.hour, start.minute, start.second, ord(rec_type),
    )


def parse_descriptor(desc: bytes) -> tuple[datetime, str]:
    if len(desc) != DESCRIPTOR_LEN:
        raise ValueError("descriptor must be 8 bytes")
    y, m, d, hh, mm, ss, t = struct.unpack("<HBBBBBB", desc)
    return datetime(y, m & 0x0F, d, hh, mm, ss), chr(t)


@dataclass(frozen=True)
class RecordList:
    sub_cmd: int
    option: int
    recordings: list[Recording]


def parse_record_list_reply(payload: bytes) -> RecordList:
    if len(payload) < 4 or payload[0] != CMD_RECORD_LIST_REPLY:
        raise ValueError("not a record-list reply")
    n = payload[3]
    if len(payload) < 4 + 10 * n:
        raise ValueError("record-list reply truncated")
    durs = struct.unpack_from("<%dH" % n, payload, 4 + 8 * n)
    recs = []
    for i in range(n):
        desc = bytes(payload[4 + 8 * i:12 + 8 * i])
        start, t = parse_descriptor(desc)
        recs.append(Recording(start, durs[i], t, desc))
    return RecordList(payload[1], payload[2], recs)


def build_day_list_request(day: date, search_days: int, version: int = DAY_LIST_VERSION) -> bytes:
    return struct.pack("<BBBBHBB", CMD_DAY_LIST, version, 0, search_days, day.year, day.month, day.day)


@dataclass(frozen=True)
class DayList:
    sub_cmd: int
    option: int
    days: list[date]


def parse_day_list_reply(payload: bytes) -> DayList:
    if len(payload) < 4 or payload[0] != CMD_DAY_LIST:
        raise ValueError("not a day-list reply")
    n = payload[3]
    if len(payload) < 4 + 4 * n:
        raise ValueError("day-list reply truncated")
    days = []
    for i in range(n):
        y, m, d = struct.unpack_from("<HBB", payload, 4 + 4 * i)
        days.append(date(y, m, d))
    return DayList(payload[1], payload[2], days)
