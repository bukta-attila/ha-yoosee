from __future__ import annotations

import os

GUTE_EXPONENT = 3


def pkcs1_v15_encrypt(data: bytes, modulus: bytes, exponent: int = GUTE_EXPONENT, rand=os.urandom) -> bytes:
    k = len(modulus)
    if len(data) > k - 11:
        raise ValueError("message too long for the modulus")
    ps = bytearray()
    while len(ps) < k - 3 - len(data):
        ps += bytes(b for b in rand(k) if b)
    block = b"\x00\x02" + bytes(ps[:k - 3 - len(data)]) + b"\x00" + bytes(data)
    n = int.from_bytes(modulus, "big")
    if n == 0:
        raise ValueError("empty modulus")
    c = pow(int.from_bytes(block, "big"), exponent, n)
    return c.to_bytes(k, "big")
