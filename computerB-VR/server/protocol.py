"""双目 JPEG 打包协议（little-endian），供 Python 与 Unity 共用。"""
from __future__ import annotations

import struct
import time
from typing import Tuple

MAGIC = b"DIBR"
VERSION = 1
_HEADER = struct.Struct("<4sHHQQQQII")  # magic,ver,res,seq,pad,tsL,tsR,lenL,lenR


def pack_stereo_jpeg(
    left_jpeg: bytes,
    right_jpeg: bytes,
    *,
    frame_seq: int,
    ts_left_ns: int | None = None,
    ts_right_ns: int | None = None,
) -> bytes:
    tl = ts_left_ns if ts_left_ns is not None else time.time_ns()
    tr = ts_right_ns if ts_right_ns is not None else tl
    return _HEADER.pack(
        MAGIC,
        VERSION,
        0,
        frame_seq,
        0,
        tl,
        tr,
        len(left_jpeg),
        len(right_jpeg),
    ) + left_jpeg + right_jpeg


def unpack_stereo_jpeg(data: memoryview | bytes) -> Tuple[int, bytes, bytes, int, int]:
    """返回 (frame_seq, left_jpeg, right_jpeg, ts_left_ns, ts_right_ns)"""
    mv = data if isinstance(data, memoryview) else memoryview(data)
    if len(mv) < _HEADER.size:
        raise ValueError("packet too short")
    magic, ver, _res, frame_seq, _pad, ts_left, ts_right, ll, rl = _HEADER.unpack_from(
        mv, 0
    )
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r}")
    if ver != VERSION:
        raise ValueError(f"unsupported version {ver}")
    off = _HEADER.size
    if off + ll + rl != len(mv):
        raise ValueError("length mismatch")
    left = bytes(mv[off : off + ll])
    off += ll
    right = bytes(mv[off : off + rl])
    return frame_seq, left, right, ts_left, ts_right
