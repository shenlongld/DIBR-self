"""占位算法：多视角 BGR + 头部位姿 -> 左右眼 BGR。可替换为 DIBR / NeRF 等。"""
from __future__ import annotations

import math
from typing import Any

import cv2
import numpy as np


def _aggregate_multiview(
    frames: list[np.ndarray | None],
    out_w: int,
    out_h: int,
) -> np.ndarray | None:
    """将 K 路有效帧并入单幅 BGR（网格拼 thumbnail 后缩放到 out），K 任意。"""
    valid = [f for f in frames if f is not None]
    if not valid:
        return None
    k = len(valid)
    if k == 1:
        return cv2.resize(valid[0], (out_w, out_h), interpolation=cv2.INTER_AREA)

    cols = int(math.ceil(math.sqrt(k)))
    rows = int(math.ceil(k / cols))
    cell_w = max(1, out_w // cols)
    cell_h = max(1, out_h // rows)
    canvas_h, canvas_w = rows * cell_h, cols * cell_w
    canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    for i, f in enumerate(valid):
        r, c = divmod(i, cols)
        thumb = cv2.resize(f, (cell_w, cell_h), interpolation=cv2.INTER_AREA)
        y0, x0 = r * cell_h, c * cell_w
        canvas[y0 : y0 + cell_h, x0 : x0 + cell_w] = thumb
    return cv2.resize(canvas, (out_w, out_h), interpolation=cv2.INTER_AREA)


def render_stereo(
    frames: list[np.ndarray | None],
    pose: dict[str, Any] | None,
    out_w: int,
    out_h: int,
) -> tuple[np.ndarray, np.ndarray]:
    """
    pose JSON: {\"t_ns\": int, \"pos\": [x,y,z], \"quat\": [qx,qy,qz,qw]}，与 Unity 惯用一致。
    *frames* 长度与采集路数一致；任意一路为 None 时聚合时自动跳过。
    """
    base = _aggregate_multiview(frames, out_w, out_h)
    if base is None:
        blank = np.zeros((out_h, out_w, 3), dtype=np.uint8)
        return blank, blank

    shift_px = 0
    if pose and "quat" in pose:
        q = pose["quat"]
        if isinstance(q, (list, tuple)) and len(q) >= 4:
            qx, qy, qz, qw = float(q[0]), float(q[1]), float(q[2]), float(q[3])
            yaw = np.arctan2(2 * (qw * qy + qx * qz), 1 - 2 * (qy * qy + qz * qz))
            shift_px = int(np.clip(yaw * 80.0, -120.0, 120.0))

    M_left = np.float32([[1, 0, -shift_px], [0, 1, 0]])
    M_right = np.float32([[1, 0, shift_px], [0, 1, 0]])
    left = cv2.warpAffine(
        base, M_left, (out_w, out_h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT
    )
    right = cv2.warpAffine(
        base, M_right, (out_w, out_h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT
    )
    return left, right
