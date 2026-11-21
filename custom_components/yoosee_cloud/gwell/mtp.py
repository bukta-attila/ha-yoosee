from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .crypto import mtp_decrypt, mtp_encrypt

HEADER_LEN = 4
TYPE_STREAM_CONTROL = 3
TYPE_AV = 4
FLAG_ENCRYPTED = 0x01

_M = 0xFFFFFFFF


@dataclass(frozen=True)
class MtpRecord:
    type: int
    flag: int
    body: bytes

    @property
    def encrypted(self) -> bool:
        return bool(self.flag & FLAG_ENCRYPTED)


def build_record(rtype: int, body: bytes, flag: int = 0) -> bytes:
    n = HEADER_LEN + len(body)
    if n > 0xFFFF:
        raise ValueError("MTP record too long")
    return mtp_encrypt(struct.pack("<BBH", rtype, flag, n) + bytes(body))


def parse_record(data: bytes) -> MtpRecord:
    if len(data) < HEADER_LEN:
        raise ValueError("MTP record shorter than its header")
    n = struct.unpack_from("<H", data, 2)[0]
    if n < HEADER_LEN or n > len(data):
        raise ValueError("MTP record length mismatch")
    plain = mtp_decrypt(bytes(data[:n]))
    return MtpRecord(plain[0], plain[1], plain[HEADER_LEN:n])


def split_records(buf: bytes) -> tuple[list[MtpRecord], bytes]:
    out = []
    pos = 0
    while pos + HEADER_LEN <= len(buf):
        n = struct.unpack_from("<H", buf, pos + 2)[0]
        if n < HEADER_LEN:
            raise ValueError("MTP record length below header size")
        if pos + n > len(buf):
            break
        out.append(parse_record(buf[pos:pos + n]))
        pos += n
    return out, bytes(buf[pos:])


def user_data_flag(nonce: int) -> int:
    return ((nonce & 0x0F) << 4) | FLAG_ENCRYPTED


STREAM_CONTROL_LEN = 48

SC_START = 1
SC_ACCEPT = 2
SC_CLOSE = 4
SC_READY = 6

CALL_LIVE = 1
CALL_PLAYBACK = 2

SC_OPEN_WORD = 0x01010101


@dataclass(frozen=True)
class StreamControl:
    session_id: int
    sub_cmd: int
    call_type: int
    word10: int
    param: bytes
    password_enc: int = field(repr=False)
    word08: int = 0
    tail: bytes = bytes(16)


def build_stream_control(
    session_id: int,
    sub_cmd: int,
    call_type: int,
    param: bytes,
    password_enc: int,
    word10: int = SC_OPEN_WORD,
    word08: int = 0,
    tail: bytes = bytes(16),
) -> bytes:
    if len(param) != 8 or len(tail) != 16:
        raise ValueError("param must be 8 bytes, tail 16 bytes")
    return (
        struct.pack("<5I", session_id & _M, sub_cmd & _M, word08 & _M, call_type & _M, word10 & _M)
        + bytes(param)
        + struct.pack("<I", password_enc & _M)
        + bytes(tail)
    )


def parse_stream_control(body: bytes) -> StreamControl:
    if len(body) != STREAM_CONTROL_LEN:
        raise ValueError("stream-control body must be 48 bytes")
    sid, sub, w08, ctype, w10 = struct.unpack_from("<5I", body, 0)
    return StreamControl(
        session_id=sid, sub_cmd=sub, call_type=ctype, word10=w10,
        param=bytes(body[0x14:0x1C]),
        password_enc=struct.unpack_from("<I", body, 0x1C)[0],
        word08=w08, tail=bytes(body[0x20:0x30]),
    )


DEF_LD = 0
DEF_HD = 1
DEF_SD = 2


def live_param(definition: int = DEF_HD) -> bytes:
    return struct.pack("<II", definition, 0)


USER_DATA_MAGIC = b"\xff\xff\xff\x88"
USER_DATA_LEN = 28
USER_DATA_PAYLOAD_LEN = 20

CHANNEL_PLAYBACK = 3
SUBCMD_SWITCH_FILE = 9


@dataclass(frozen=True)
class UserData:
    builder: int
    channel: int
    sub_cmd: int
    payload: bytes
    reserved: int = 0


def build_user_data(channel: int, sub_cmd: int, payload: bytes, builder: int = 2) -> bytes:
    if len(payload) > USER_DATA_PAYLOAD_LEN:
        raise ValueError("user-data payload is at most 20 bytes")
    return USER_DATA_MAGIC + bytes((0, builder, channel, sub_cmd)) + bytes(payload).ljust(USER_DATA_PAYLOAD_LEN, b"\0")


def parse_user_data(body: bytes) -> UserData:
    if len(body) < USER_DATA_LEN or body[:4] != USER_DATA_MAGIC:
        raise ValueError("not a user-data block")
    return UserData(body[5], body[6], body[7], bytes(body[8:USER_DATA_LEN]), body[4])


def build_switch_file(descriptor: bytes, offset_s: int = 0) -> bytes:
    if len(descriptor) != 8:
        raise ValueError("descriptor must be 8 bytes")
    return build_user_data(CHANNEL_PLAYBACK, SUBCMD_SWITCH_FILE, bytes(descriptor) + struct.pack("<I", offset_s & _M))


def parse_switch_file(ud: UserData) -> tuple[bytes, int]:
    if ud.channel != CHANNEL_PLAYBACK or ud.sub_cmd != SUBCMD_SWITCH_FILE:
        raise ValueError("not a switch-file user-data block")
    return ud.payload[:8], struct.unpack_from("<I", ud.payload, 8)[0]


def build_user_data_record(user_data: bytes, nonce: int) -> bytes:
    return build_record(TYPE_AV, user_data, user_data_flag(nonce))
