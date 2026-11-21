from __future__ import annotations

import hashlib
import base64
import binascii
import struct

_M = 0xFFFFFFFF
_P32 = 0xB7E15163
_Q32 = 0x9E3779B9

ROUNDS = 6
GUTE_BASE_KEY = b"www.gwell.cc"
GUTE_HEADER_LEN = 0x18
MTP_CRYPT_OFFSET = 8


def _rotl(x: int, n: int) -> int:
    n &= 31
    return ((x << n) | (x >> (32 - n))) & _M if n else x


def _rotr(x: int, n: int) -> int:
    n &= 31
    return ((x >> n) | (x << (32 - n))) & _M if n else x


class RC5:

    __slots__ = ("rounds", "_s")

    def __init__(self, key: bytes, rounds: int = ROUNDS) -> None:
        self.rounds = rounds
        t = 2 * (rounds + 1)
        b = len(key)
        c = max(1, (b + 3) // 4)
        L = [0] * c
        for i in range(b - 1, -1, -1):
            L[i // 4] = ((L[i // 4] << 8) + key[i]) & _M
        S = [0] * t
        S[0] = _P32
        for i in range(1, t):
            S[i] = (S[i - 1] + _Q32) & _M
        i = j = A = B = 0
        for _ in range(3 * max(t, c)):
            A = S[i] = _rotl((S[i] + A + B) & _M, 3)
            B = L[j] = _rotl((L[j] + A + B) & _M, (A + B) & 31)
            i = (i + 1) % t
            j = (j + 1) % c
        self._s = S

    def encrypt_block(self, block: bytes) -> bytes:
        A, B = struct.unpack("<II", block)
        S = self._s
        A = (A + S[0]) & _M
        B = (B + S[1]) & _M
        for i in range(1, self.rounds + 1):
            A = (_rotl(A ^ B, B) + S[2 * i]) & _M
            B = (_rotl(B ^ A, A) + S[2 * i + 1]) & _M
        return struct.pack("<II", A, B)

    def decrypt_block(self, block: bytes) -> bytes:
        A, B = struct.unpack("<II", block)
        S = self._s
        for i in range(self.rounds, 0, -1):
            B = _rotr((B - S[2 * i + 1]) & _M, A) ^ A
            A = _rotr((A - S[2 * i]) & _M, B) ^ B
        B = (B - S[1]) & _M
        A = (A - S[0]) & _M
        return struct.pack("<II", A, B)

    def _apply(self, buf: bytearray, start: int, nblocks: int, fn) -> None:
        for k in range(nblocks):
            o = start + 8 * k
            buf[o:o + 8] = fn(bytes(buf[o:o + 8]))

    def encrypt_ecb(self, data: bytes) -> bytes:
        out = bytearray(data)
        self._apply(out, 0, len(data) // 8, self.encrypt_block)
        return bytes(out)

    def decrypt_ecb(self, data: bytes) -> bytes:
        out = bytearray(data)
        self._apply(out, 0, len(data) // 8, self.decrypt_block)
        return bytes(out)


_BASE = RC5(GUTE_BASE_KEY)


def _as_rc5(key: bytes | RC5) -> RC5:
    return key if isinstance(key, RC5) else RC5(bytes(key))


def gute_subkey(frame: bytes) -> bytes:
    return bytes(frame[0:4]) + bytes(frame[0x14:0x18])


def _gute_crypt(frame: bytes, session_key: bytes | RC5 | None, encrypt: bool) -> bytes:
    f = bytearray(frame)
    if len(f) < GUTE_HEADER_LEN:
        raise ValueError("gute frame shorter than its header")
    mode = f[0x14] & 3
    if mode == 0:
        return bytes(f)
    if mode == 1:
        ctx = RC5(gute_subkey(f))
    elif mode == 2:
        if session_key is None:
            raise ValueError("gute mode 2 needs the session key")
        ctx = _as_rc5(session_key)
    else:
        raise ValueError("unknown gute encryption mode 3")
    n = struct.unpack_from("<H", f, 2)[0]
    nb = max(0, (min(n, len(f)) - GUTE_HEADER_LEN) >> 3)
    base = _BASE.encrypt_block if encrypt else _BASE.decrypt_block
    blk = ctx.encrypt_block if encrypt else ctx.decrypt_block
    f[4:12] = base(bytes(f[4:12]))
    f[0x0C:0x14] = blk(bytes(f[0x0C:0x14]))
    ctx._apply(f, GUTE_HEADER_LEN, nb, blk)
    return bytes(f)


def gute_decrypt(frame: bytes, session_key: bytes | RC5 | None = None) -> bytes:
    return _gute_crypt(frame, session_key, False)


def gute_encrypt(frame: bytes, session_key: bytes | RC5 | None = None) -> bytes:
    return _gute_crypt(frame, session_key, True)


def _mtp_crypt(record: bytes, encrypt: bool) -> bytes:
    if len(record) < 4 or not record[1] & 1:
        return bytes(record)
    n = min(struct.unpack_from("<H", record, 2)[0], len(record))
    if n < MTP_CRYPT_OFFSET:
        return bytes(record)
    ctx = RC5(bytes(record[0:8]))
    out = bytearray(record)
    ctx._apply(out, MTP_CRYPT_OFFSET, (n - MTP_CRYPT_OFFSET) >> 3,
               ctx.encrypt_block if encrypt else ctx.decrypt_block)
    return bytes(out)


def mtp_decrypt(record: bytes) -> bytes:
    return _mtp_crypt(record, False)


def mtp_encrypt(record: bytes) -> bytes:
    return _mtp_crypt(record, True)


_XORTAB = (
    0x177BCE1F, 0x4208ABFB, 0xBF50695E, 0x5C04BB9A, 0x13ECF425,
    0x76C479AD, 0x5B63C382, 0xAC4217BE, 0x8567656A, 0x568CAAE0,
)
_GWC = (-0x5D1C6027) & _M


def _mod999(x: int) -> int:
    return (x & _M) % 999999999


def _is_weak(value: int) -> bool:
    s = "%u" % (value & _M)
    if len(s) < 6:
        return True
    base = ord(s[0]) - ord(s[1])
    return all(ord(s[i]) - ord(s[i + 1]) == base for i in range(1, len(s) - 1))


def encrypt_gw1(password: str) -> int:
    if not password:
        return 0
    h = hashlib.md5(password.encode("utf-8")).hexdigest()
    w = [int(h[i * 8:(i + 1) * 8], 16) for i in range(4)]
    x = _mod999(w[0] ^ w[1] ^ w[2] ^ w[3])
    i = 0
    while i < 10 and _is_weak(_mod999(x)):
        x = (x ^ _XORTAB[i]) & _M
        i += 1
    return _mod999(x)


def is_need_encrypt(password: str | None) -> bool:
    if password is None:
        return False
    n = len(password)
    if n < 6 or n > 30:
        return False
    return not (password.isascii() and password.isdigit() and n < 10)


def entry_pwd(password: str) -> int:
    if is_need_encrypt(password):
        return encrypt_gw1(password)
    return int(password) if password else 0


def gw_encode_password(value: int) -> int:
    v = value & _M
    for i in range(0x11):
        v = (v * _GWC + i) & _M
    return v


def gw_decode_password(value: int) -> int:
    k = gw_encode_password(0)
    a_inv = pow(pow(_GWC, 0x11, 1 << 32), -1, 1 << 32)
    return ((value - k) * a_inv) & _M


_DES_KEY = bytes.fromhex("8a0b0557397e9ac1")
_GW5_XOR_SEED = bytes.fromhex("8d0d0a37497c9dcd")
_DES_IP = (58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
           62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
           57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
           61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7)
_DES_FP = (40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
           38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
           36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
           34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25)
_DES_E = (32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13,
          12, 13, 14, 15, 16, 17, 16, 17, 18, 19, 20, 21, 20, 21, 22, 23,
          24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1)
_DES_P = (16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
          2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25)
_DES_PC1 = (57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2,
            59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36, 63, 55, 47, 39,
            31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37,
            29, 21, 13, 5, 28, 20, 12, 4)
_DES_PC2 = (14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4,
            26, 8, 16, 7, 27, 20, 13, 2, 41, 52, 31, 37, 47, 55, 30, 40,
            51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32)
_DES_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
_DES_SBOX = (
    ((14,4,13,1,2,15,11,8,3,10,6,12,5,9,0,7),(0,15,7,4,14,2,13,1,10,6,12,11,9,5,3,8),(4,1,14,8,13,6,2,11,15,12,9,7,3,10,5,0),(15,12,8,2,4,9,1,7,5,11,3,14,10,0,6,13)),
    ((15,1,8,14,6,11,3,4,9,7,2,13,12,0,5,10),(3,13,4,7,15,2,8,14,12,0,1,10,6,9,11,5),(0,14,7,11,10,4,13,1,5,8,12,6,9,3,2,15),(13,8,10,1,3,15,4,2,11,6,7,12,0,5,14,9)),
    ((10,0,9,14,6,3,15,5,1,13,12,7,11,4,2,8),(13,7,0,9,3,4,6,10,2,8,5,14,12,11,15,1),(13,6,4,9,8,15,3,0,11,1,2,12,5,10,14,7),(1,10,13,0,6,9,8,7,4,15,14,3,11,5,2,12)),
    ((7,13,14,3,0,6,9,10,1,2,8,5,11,12,4,15),(13,8,11,5,6,15,0,3,4,7,2,12,1,10,14,9),(10,6,9,0,12,11,7,13,15,1,3,14,5,2,8,4),(3,15,0,6,10,1,13,8,9,4,5,11,12,7,2,14)),
    ((2,12,4,1,7,10,11,6,8,5,3,15,13,0,14,9),(14,11,2,12,4,7,13,1,5,0,15,10,3,9,8,6),(4,2,1,11,10,13,7,8,15,9,12,5,6,3,0,14),(11,8,12,7,1,14,2,13,6,15,0,9,10,4,5,3)),
    ((12,1,10,15,9,2,6,8,0,13,3,4,14,7,5,11),(10,15,4,2,7,12,9,5,6,1,13,14,0,11,3,8),(9,14,15,5,2,8,12,3,7,0,4,10,1,13,11,6),(4,3,2,12,9,5,15,10,11,14,1,7,6,0,8,13)),
    ((4,11,2,14,15,0,8,13,3,12,9,7,5,10,6,1),(13,0,11,7,4,9,1,10,14,3,5,12,2,15,8,6),(1,4,11,13,12,3,7,14,10,15,6,8,0,5,9,2),(6,11,13,8,1,4,10,7,9,5,0,15,14,2,3,12)),
    ((13,2,8,4,6,15,11,1,10,9,3,14,5,0,12,7),(1,15,13,8,10,3,7,4,12,5,6,11,0,14,9,2),(7,11,4,1,9,12,14,2,0,6,10,13,15,3,5,8),(2,1,14,7,4,10,8,13,15,12,9,0,3,5,6,11)),
)


def _des_permute(value: int, width: int, table: tuple[int, ...]) -> int:
    out = 0
    for position in table:
        out = (out << 1) | ((value >> (width - position)) & 1)
    return out


def _des_keys(key: bytes) -> tuple[int, ...]:
    key56 = _des_permute(int.from_bytes(key, "big"), 64, _DES_PC1)
    c, d = key56 >> 28, key56 & 0xFFFFFFF
    keys = []
    for shift in _DES_SHIFTS:
        c = ((c << shift) | (c >> (28 - shift))) & 0xFFFFFFF
        d = ((d << shift) | (d >> (28 - shift))) & 0xFFFFFFF
        keys.append(_des_permute((c << 28) | d, 56, _DES_PC2))
    return tuple(keys)


def _des_block(block: bytes, keys: tuple[int, ...], decrypt: bool) -> bytes:
    bits = _des_permute(int.from_bytes(block, "big"), 64, _DES_IP)
    left, right = bits >> 32, bits & _M
    for key in reversed(keys) if decrypt else keys:
        expanded = _des_permute(right, 32, _DES_E) ^ key
        s_out = 0
        for i, box in enumerate(_DES_SBOX):
            six = (expanded >> (42 - 6 * i)) & 0x3F
            row = ((six & 0x20) >> 4) | (six & 1)
            col = (six >> 1) & 0xF
            s_out = (s_out << 4) | box[row][col]
        left, right = right, left ^ _des_permute(s_out, 32, _DES_P)
    out = _des_permute((right << 32) | left, 64, _DES_FP)
    return out.to_bytes(8, "big")


def _gw5_password_key(user_id: int) -> bytes:
    seed = user_id & _M
    for i in range(32):
        seed = (seed * 0xDF7B0817 + i * 3 + 0x98E59) & _M
    key = bytearray(_GW5_XOR_SEED)
    for i in range(2):
        seed = (seed * 0x44087152 + i * 2) & _M
        word = int.from_bytes(key[i * 4:i * 4 + 4], "little") ^ seed
        key[i * 4:i * 4 + 4] = word.to_bytes(4, "little")
    return bytes(key)


def decrypt_gw5(user_id: int | str, secret_key: str, *, limit: int = 128) -> str:
    if limit < 8:
        raise ValueError("GW5 output limit must be at least 8")
    try:
        seed = int(user_id) | 0x80000000
        encoded = base64.b64decode(secret_key, validate=True)
    except (TypeError, ValueError, binascii.Error) as e:
        raise ValueError("invalid GW5 user id or base64 secret key") from e
    if not encoded or len(encoded) % 8:
        raise ValueError("GW5 secret key must decode to whole DES blocks")
    xor_key = _gw5_password_key(seed)
    keys = _des_keys(_DES_KEY)
    plain = bytearray()
    for offset in range(0, min(len(encoded), limit), 8):
        block = bytes(a ^ b for a, b in zip(encoded[offset:offset + 8], xor_key))
        plain.extend(_des_block(block, keys, False))
    nul = plain.find(0)
    if nul >= 0:
        plain = plain[:nul]
    try:
        return plain.decode("ascii").strip()
    except UnicodeDecodeError as e:
        raise ValueError("GW5 secret key did not decode to an ASCII password") from e
