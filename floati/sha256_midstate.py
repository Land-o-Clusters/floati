"""Persistable SHA-256 append state; finalization never mutates that state."""
from __future__ import annotations

import struct

_INITIAL = (0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
            0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19)
_K = (
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2,
)
_MASK = 0xffffffff


def _ror(value, count):
    return ((value >> count) | (value << (32 - count))) & _MASK


class SHA256:
    def __init__(self, *, state=_INITIAL, length=0, tail=b""):
        if (len(state) != 8 or any(type(value) is not int or not 0 <= value <= _MASK for value in state)
                or type(length) is not int or not 0 <= length < (1 << 61)
                or type(tail) is not bytes or len(tail) != length % 64):
            raise ValueError("invalid SHA-256 midstate")
        self.state, self.length, self.tail = tuple(state), length, tail

    def update(self, data):
        if type(data) is not bytes or self.length + len(data) >= (1 << 61):
            raise ValueError("invalid SHA-256 input")
        self.length += len(data)
        data = self.tail + data
        stop = len(data) - len(data) % 64
        for offset in range(0, stop, 64):
            words = list(struct.unpack(">16I", data[offset:offset + 64]))
            for index in range(16, 64):
                left, right = words[index - 15], words[index - 2]
                small0 = _ror(left, 7) ^ _ror(left, 18) ^ (left >> 3)
                small1 = _ror(right, 17) ^ _ror(right, 19) ^ (right >> 10)
                words.append((words[index - 16] + small0 + words[index - 7] + small1) & _MASK)
            a,b,c,d,e,f,g,h = self.state
            for constant, word in zip(_K, words):
                large1 = _ror(e, 6) ^ _ror(e, 11) ^ _ror(e, 25)
                first = (h + large1 + ((e & f) ^ (~e & g)) + constant + word) & _MASK
                large0 = _ror(a, 2) ^ _ror(a, 13) ^ _ror(a, 22)
                second = (large0 + ((a & b) ^ (a & c) ^ (b & c))) & _MASK
                a,b,c,d,e,f,g,h = (first + second) & _MASK,a,b,c,(d + first) & _MASK,e,f,g
            self.state = tuple((left + right) & _MASK
                for left, right in zip(self.state, (a,b,c,d,e,f,g,h)))
        self.tail = data[stop:]
        return self

    def hexdigest(self):
        final = SHA256(state=self.state, length=self.length, tail=self.tail)
        final.update(b"\x80" + bytes((55 - self.length) % 64) + struct.pack(">Q", self.length * 8))
        return struct.pack(">8I", *final.state).hex()
