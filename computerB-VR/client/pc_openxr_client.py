"""
PC OpenXR 客户端：头显经 OpenXR 回传位姿到渲染服务器，并将服务器返回的双目 RGB（JPEG）
显示在头显中（每眼一块世界空间平面）。

依赖（与 Unity 无关，纯 Python）:
  pip install -r requirements-pc-openxr.txt

环境: 已安装 PC VR 运行时（Windows Mixed Reality / SteamVR OpenXR / Oculus PC 等）。
运行前请先启动 computerB-VR 的 `python -m server.main`。

用法:
  cd computerB-VR
  pip install -r requirements-pc-openxr.txt
  python client/pc_openxr_client.py -c config.yaml
  或: python client/pc_openxr_client.py -u ws://127.0.0.1:8765/
"""
from __future__ import annotations

import argparse
import ctypes
import inspect
import json
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    import cv2
    import numpy as np
    import websocket
    from OpenGL import GL
    from OpenGL.GL import shaders
    import xr
    from xr.utils import GraphicsAPI, Matrix4x4f
    from xr.utils.gl import ContextObject
    from xr.utils.gl.glfw_util import GLFWOffscreenContextProvider
except ImportError as e:
    print(
        "缺少依赖或 DLL。请先安装:\n"
        "  cd computerB-VR\n"
        "  pip install -r requirements-pc-openxr.txt\n"
        "并确保系统已安装可运行的 OpenXR 运行时与最新显卡驱动。\n"
        f"原始错误: {e}",
        file=sys.stderr,
    )
    raise SystemExit(2) from e

# 与 server.protocol / pc_pose_demo 一致
_HEADER = struct.Struct("<4sHHQQQQII")

try:
    from xr.utils import Eye as _XrEye
except ImportError:
    _XrEye = None  # type: ignore[misc,assignment]


def _unpack_stereo(data: bytes) -> tuple[int, bytes, bytes]:
    if len(data) < _HEADER.size:
        raise ValueError("short")
    magic, ver, _r, seq, _p, _tl, _tr, ll, rl = _HEADER.unpack_from(data, 0)
    if magic != b"DIBR" or ver != 1:
        raise ValueError("bad hdr")
    o = _HEADER.size
    return seq, data[o : o + ll], data[o + ll : o + ll + rl]


def quat_rotate_vec(
    ox: float, oy: float, oz: float, ow: float, vx: float, vy: float, vz: float
) -> tuple[float, float, float]:
    """单位四元数 (x,y,z,w) 旋转矢量 v（列向量约定）。"""
    xx, yy, zz = ox * ox, oy * oy, oz * oz
    xy, xz, yz = ox * oy, ox * oz, oy * oz
    wx, wy, wz = ow * ox, ow * oy, ow * oz
    tx = (1.0 - 2.0 * (yy + zz)) * vx + 2.0 * (xy - wz) * vy + 2.0 * (xz + wy) * vz
    ty = 2.0 * (xy + wz) * vx + (1.0 - 2.0 * (xx + zz)) * vy + 2.0 * (yz - wx) * vz
    tz = 2.0 * (xz - wy) * vx + 2.0 * (yz + wx) * vy + (1.0 - 2.0 * (xx + yy)) * vz
    return tx, ty, tz


def _decode_jpeg_u8_rgb(jpeg: bytes) -> np.ndarray | None:
    if not jpeg:
        return None
    bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        return None
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    return np.ascontiguousarray(np.flipud(rgb))


class _WsRx:
    """在独立线程中非阻塞拉取 WebSocket 二进制帧（主线程只做 OpenXR 帧循环时可避免卡死）。"""

    def __init__(self, url: str) -> None:
        self._url = url
        self._lock = threading.Lock()
        self._latest: tuple[np.ndarray | None, np.ndarray | None] = (None, None)
        self._aspect = 16.0 / 9.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: websocket.WebSocket | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # 等待连接
        for _ in range(100):
            if self._ws is not None or self._stop.is_set():
                break
            time.sleep(0.02)
        if self._ws is None and not self._stop.is_set():
            raise RuntimeError(f"WebSocket 连接超时: {self._url}")

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)

    def snapshot(self) -> tuple[np.ndarray | None, np.ndarray | None, float]:
        with self._lock:
            return self._latest[0], self._latest[1], self._aspect

    def send_pose(self, payload: str) -> None:
        ws = self._ws
        if ws:
            try:
                ws.send(payload)
            except Exception:
                pass

    def _run(self) -> None:
        try:
            ws = websocket.create_connection(self._url, timeout=15)
            self._ws = ws
            ws.settimeout(0.5)
            while not self._stop.is_set():
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception:
                    break
                if isinstance(msg, bytes):
                    try:
                        _, lj, rj = _unpack_stereo(msg)
                    except ValueError:
                        continue
                    L = _decode_jpeg_u8_rgb(lj)
                    R = _decode_jpeg_u8_rgb(rj)
                    with self._lock:
                        self._latest = (L, R)
                        if L is not None and L.ndim == 3:
                            h, w = L.shape[0], L.shape[1]
                            if h > 0:
                                self._aspect = float(w) / float(h)
        finally:
            self._ws = None


def _compile_gl_textured_quad() -> tuple[Any, int, int, int]:
    vert = """
    #version 430
    layout(location=0) in vec2 in_pos;
    layout(location=1) in vec2 in_uv;
    layout(location=0) uniform mat4 Projection;
    layout(location=4) uniform mat4 View;
    layout(location=8) uniform mat4 Model;
    out vec2 uv;
    void main() {
        uv = in_uv;
        gl_Position = Projection * View * Model * vec4(in_pos, 0.0, 1.0);
    }
    """
    frag = """
    #version 430
    in vec2 uv;
    out vec4 FragColor;
    uniform sampler2D tex0;
    void main() {
        FragColor = texture(tex0, uv);
    }
    """
    vs = shaders.compileShader(inspect.cleandoc(vert), GL.GL_VERTEX_SHADER)
    fs = shaders.compileShader(inspect.cleandoc(frag), GL.GL_FRAGMENT_SHADER)
    prog = shaders.compileProgram(vs, fs)
    quad = np.array(
        [
            -1.0,
            -1.0,
            0.0,
            0.0,
            1.0,
            -1.0,
            1.0,
            0.0,
            -1.0,
            1.0,
            0.0,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
        ],
        dtype=np.float32,
    )
    vbo = int(GL.glGenBuffers(1))
    GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vbo)
    GL.glBufferData(GL.GL_ARRAY_BUFFER, quad.nbytes, quad, GL.GL_STATIC_DRAW)
    vao = int(GL.glGenVertexArrays(1))
    GL.glBindVertexArray(vao)
    stride = 16
    GL.glEnableVertexAttribArray(0)
    GL.glVertexAttribPointer(
        0, 2, GL.GL_FLOAT, GL.GL_FALSE, stride, ctypes.c_void_p(0)
    )
    GL.glEnableVertexAttribArray(1)
    GL.glVertexAttribPointer(
        1, 2, GL.GL_FLOAT, GL.GL_FALSE, stride, ctypes.c_void_p(8)
    )
    tex_l = int(GL.glGenTextures(1))
    tex_r = int(GL.glGenTextures(1))
    for tid in (tex_l, tex_r):
        GL.glBindTexture(GL.GL_TEXTURE_2D, tid)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
    return prog, vao, tex_l, tex_r


def _upload_rgb(tex_id: int, image: np.ndarray | None) -> None:
    GL.glBindTexture(GL.GL_TEXTURE_2D, tex_id)
    if image is None or image.size == 0:
        return
    h, w = image.shape[0], image.shape[1]
    GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
    GL.glTexImage2D(
        GL.GL_TEXTURE_2D,
        0,
        GL.GL_RGB8,
        w,
        h,
        0,
        GL.GL_RGB,
        GL.GL_UNSIGNED_BYTE,
        image,
    )


def run(url: str, *, screen_height_m: float = 1.2, depth_m: float = 2.0) -> None:
    rx = _WsRx(url)
    rx.start()

    prog, vao, tex_l, tex_r = _compile_gl_textured_quad()

    context_provider = GLFWOffscreenContextProvider()
    instance_info = xr.InstanceCreateInfo(
        enabled_extension_names=[
            xr.KHR_OPENGL_ENABLE_EXTENSION_NAME,
        ],
    )

    try:
        with ContextObject(
            instance_create_info=instance_info,
            context_provider=context_provider,
        ) as context:
            GL.glEnable(GL.GL_DEPTH_TEST)
            GL.glClearDepth(1.0)

            for _frame_index, frame_state in enumerate(context.frame_loop()):
                left_rgb, right_rgb, aspect = rx.snapshot()
                _upload_rgb(tex_l, left_rgb)
                _upload_rgb(tex_r, right_rgb)

                view_state, views = xr.locate_views(
                    session=context.session,
                    view_locate_info=xr.ViewLocateInfo(
                        view_configuration_type=context.view_configuration_type,
                        display_time=frame_state.predicted_display_time,
                        space=context.space,
                    ),
                )
                flags = xr.ViewStateFlags(view_state.view_state_flags)
                if flags & xr.ViewStateFlags.POSITION_VALID_BIT:
                    left_slot = getattr(_XrEye, "LEFT", 0) if _XrEye else 0
                    v0 = views[left_slot]
                    p = v0.pose.position
                    o = v0.pose.orientation
                    payload = json.dumps(
                        {
                            "t_ns": time.time_ns(),
                            "pos": [float(p.x), float(p.y), float(p.z)],
                            "quat": [
                                float(o.x),
                                float(o.y),
                                float(o.z),
                                float(o.w),
                            ],
                        }
                    )
                    rx.send_pose(payload)

                for view_index, view in enumerate(context.view_loop(frame_state)):
                    projection = Matrix4x4f.create_projection_fov(
                        graphics_api=GraphicsAPI.OPENGL,
                        fov=view.fov,
                        near_z=0.05,
                        far_z=100.0,
                    )
                    to_view = Matrix4x4f.create_translation_rotation_scale(
                        translation=view.pose.position,
                        rotation=view.pose.orientation,
                        scale=(1, 1, 1),
                    )
                    view_mat = Matrix4x4f.invert_rigid_body(to_view)

                    fx, fy, fz = quat_rotate_vec(
                        float(view.pose.orientation.x),
                        float(view.pose.orientation.y),
                        float(view.pose.orientation.z),
                        float(view.pose.orientation.w),
                        0.0,
                        0.0,
                        -float(depth_m),
                    )
                    cx = float(view.pose.position.x) + fx
                    cy = float(view.pose.position.y) + fy
                    cz = float(view.pose.position.z) + fz
                    half_h = float(screen_height_m) * 0.5
                    half_w = half_h * float(aspect)
                    model = Matrix4x4f.create_translation_rotation_scale(
                        translation=(cx, cy, cz),
                        rotation=(
                            float(view.pose.orientation.x),
                            float(view.pose.orientation.y),
                            float(view.pose.orientation.z),
                            float(view.pose.orientation.w),
                        ),
                        scale=(half_w, half_h, 1.0),
                    )

                    GL.glUseProgram(prog)
                    GL.glUniformMatrix4fv(
                        0,
                        1,
                        GL.GL_FALSE,
                        projection.as_numpy().astype(np.float32).flatten("F"),
                    )
                    GL.glUniformMatrix4fv(
                        4,
                        1,
                        GL.GL_FALSE,
                        view_mat.as_numpy().astype(np.float32).flatten("F"),
                    )
                    GL.glUniformMatrix4fv(
                        8,
                        1,
                        GL.GL_FALSE,
                        model.as_numpy().astype(np.float32).flatten("F"),
                    )
                    tid = tex_l if view_index == 0 else tex_r
                    GL.glActiveTexture(GL.GL_TEXTURE0)
                    GL.glBindTexture(GL.GL_TEXTURE_2D, tid)
                    loc = GL.glGetUniformLocation(prog, "tex0")
                    if loc >= 0:
                        GL.glUniform1i(loc, 0)
                    GL.glBindVertexArray(vao)
                    GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
                    GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
    finally:
        rx.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description="PC OpenXR ↔ 渲染服务器 WebSocket")
    ap.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="默认读取 ws_listen_port 拼 ws://127.0.0.1:port",
    )
    ap.add_argument("-u", "--url", default="", help="完整 ws:// URL，非空则覆盖 config")
    ap.add_argument(
        "--screen-height",
        type=float,
        default=1.2,
        help="虚拟屏幕高度（米），宽度按图像宽高比",
    )
    ap.add_argument(
        "--depth",
        type=float,
        default=2.0,
        help="屏幕中心相对头部的距离（米）",
    )
    args = ap.parse_args()
    url = args.url.strip()
    if not url:
        import yaml

        cfg_path = Path(args.config)
        if not cfg_path.is_file():
            raise SystemExit(f"未找到 {cfg_path}，请用 -u 指定 WebSocket URL")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        p = int(cfg.get("ws_listen_port", 8765))
        url = f"ws://127.0.0.1:{p}/"
    run(
        url,
        screen_height_m=float(args.screen_height),
        depth_m=float(args.depth),
    )


if __name__ == "__main__":
    main()
