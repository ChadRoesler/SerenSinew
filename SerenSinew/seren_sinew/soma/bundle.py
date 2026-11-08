"""A built face as one blob, the way the robot's face_push.cpp unpacks it.

    for each file, by name:  [u8 name length][name, utf-8][u32 size, little-endian][bytes]

It goes to POST /api/face with ``X-Face-Crc32: <crc32_hex(bundle)>``; the robot
writes it to a spare partition, checks the CRC, and swaps it in on the next boot.
"""
from __future__ import annotations

import struct
import zlib

MAX_BUNDLE = 2 * 1024 * 1024   # what the robot will load (firmware hal_sdcard.cpp)
MAX_NAME = 255


def pack(files: dict[str, bytes]) -> bytes:
    """files by name -> one bundle, in name order so the same face is the same bytes."""
    out = bytearray()
    for name in sorted(files):
        raw = name.encode("utf-8")
        if not raw or len(raw) > MAX_NAME or "/" in name or "\\" in name:
            raise ValueError(f"not a file name the robot takes: {name!r}")
        data = files[name]
        out += struct.pack("<B", len(raw)) + raw + struct.pack("<I", len(data)) + data
    if len(out) > MAX_BUNDLE:
        raise ValueError(f"the face is {len(out) // 1024} KB, and the robot holds {MAX_BUNDLE // 1024} KB")
    return bytes(out)


def unpack(blob: bytes) -> dict[str, bytes]:
    """The other way, for tests and for looking at what a robot was sent."""
    files, off = {}, 0
    while off < len(blob):
        n = blob[off]
        off += 1
        name = blob[off:off + n].decode("utf-8")
        off += n
        (size,) = struct.unpack_from("<I", blob, off)
        off += 4
        if off + size > len(blob):
            raise ValueError(f"{name} runs past the end of the bundle")
        files[name] = blob[off:off + size]
        off += size
    return files


def crc32_hex(blob: bytes) -> str:
    """The X-Face-Crc32 header's value."""
    return f"{zlib.crc32(blob) & 0xFFFFFFFF:08x}"
