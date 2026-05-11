"""
PC 演示：连接渲染服务器 WebSocket，发送模拟/简单位姿，接收并显示双目 JPEG。"""
from __future__ import annotations

import argparse
import json
import math
import struct
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml
from websocket import WebSocketApp

# 与 server.protocol 保持一致的头部解析，避免以 -m 方式运行时的导入路径问题
_HEADER = struct.Struct("<4sHHQQQQII")


def _unpack(data: bytes) -> tuple[int, bytes, bytes]:
    if len(data) < _HEADER.size:
        raise ValueError("short")
    magic, ver, _r, seq, _p, _tl, _tr, ll, rl = _HEADER.unpack_from(data, 0)
    if magic != b"DIBR" or ver != 1:
        raise ValueError("bad hdr")
    o = _HEADER.size
    left = data[o : o + ll]
    o += ll
    right = data[o : o + rl]
    return seq, left, right


class Demo:
    def __init__(self, url: str, *, yaw_amp: float = 0.35) -> None:
        self._url = url
        self._yaw_amp = yaw_amp
        self._ws: WebSocketApp | None = None
        self._last: tuple[np.ndarray | None, np.ndarray | None] = (None, None)
        self._lock = threading.Lock()
        self._running = True

    def _on_msg(self, _ws: Any, message: bytes) -> None:
        if not isinstance(message, bytes):
            return
        try:
            _seq, lj, rj = _unpack(message)
        except ValueError:
            return
        limg = cv2.imdecode(np.frombuffer(lj, np.uint8), cv2.IMREAD_COLOR)
        rimg = cv2.imdecode(np.frombuffer(rj, np.uint8), cv2.IMREAD_COLOR)
        with self._lock:
            self._last = (limg, rimg)

    def _pose_sender(self) -> None:
        t0 = time.time()
        while self._running and self._ws:
            t = time.time() - t0
            yaw = self._yaw_amp * math.sin(t * 0.7)
            half = math.sin(yaw * 0.5)
            qx, qy, qz, qw = 0.0, half, 0.0, math.cos(yaw * 0.5)
            payload = json.dumps(
                {
                    "t_ns": time.time_ns(),
                    "pos": [0.0, 1.6, 0.0],
                    "quat": [qx, qy, qz, qw],
                }
            )
            try:
                self._ws.send(payload)
            except Exception:
                break
            time.sleep(1 / 60)

    def run(self) -> None:
        def on_open(ws: Any) -> None:
            self._ws = ws
            threading.Thread(target=self._pose_sender, daemon=True).start()

        self._ws = WebSocketApp(
            self._url,
            on_open=on_open,
            on_message=self._on_msg,
        )
        threading.Thread(target=self._ws.run_forever, kwargs={"ping_interval": 20}, daemon=True).start()
        print("按 N 退出预览窗口")
        while self._running:
            with self._lock:
                L, R = self._last
            if L is not None and R is not None:
                show = np.hstack([L, R])
                cv2.imshow("DIBR VR preview (L | R)", show)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("n") or key == ord("q"):
                break
            time.sleep(0.01)
        self._running = False
        if self._ws:
            self._ws.close()
        cv2.destroyAllWindows()


def main() -> None:
    ap = argparse.ArgumentParser(description="PC 端 VR 协议演示客户端")
    ap.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="读取 ws_listen_port 拼 ws://127.0.0.1:port",
    )
    ap.add_argument(
        "-u",
        "--url",
        default="",
        help="完整 WebSocket URL，非空则覆盖 config",
    )
    args = ap.parse_args()
    url = args.url.strip()
    if not url:
        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
        p = int(cfg.get("ws_listen_port", 8765))
        url = f"ws://127.0.0.1:{p}/"
    Demo(url).run()


if __name__ == "__main__":
    main()
