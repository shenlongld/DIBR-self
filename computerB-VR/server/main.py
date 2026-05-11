"""
渲染服务器入口：拉取多路 RTSP -> 算法 stub -> WebSocket 推送双目 RGB (JPEG)。

用法:
  cd computerB-VR
  pip install -r requirements.txt
  copy config.example.yaml config.yaml  # 按需改 mediamtx_host / stream_paths
  python -m server.main
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any

import cv2
import yaml

from .algorithm_stub import render_stereo
from .bridge import VrBridgeServer
from .ingest import MultiRtspIngest, build_rtsp_urls
from .protocol import pack_stereo_jpeg

log = logging.getLogger(__name__)

_latest_pose: dict[str, Any] = {"pos": [0, 0, 0], "quat": [0, 0, 0, 1]}


def _resolve_stream_paths(cfg: dict[str, Any]) -> list[str]:
    """显式 stream_paths优先；否则用 stream_count + stream_prefix 生成 cam0..camN-1。"""
    raw = cfg.get("stream_paths")
    if isinstance(raw, list) and len(raw) > 0:
        return [str(p).strip() for p in raw if str(p).strip()]
    n = cfg.get("stream_count")
    if n is None:
        raise ValueError("请在 config 中设置 stream_paths（列表）或 stream_count（整数≥1）")
    k = int(n)
    if k < 1:
        raise ValueError("stream_count 须 >= 1")
    prefix = str(cfg.get("stream_prefix", "cam")).strip() or "cam"
    return [f"{prefix}{i}" for i in range(k)]


def _on_pose(p: dict[str, Any]) -> None:
    global _latest_pose
    _latest_pose = p


async def _render_loop(
    ingest: MultiRtspIngest,
    bridge: VrBridgeServer,
    *,
    out_w: int,
    out_h: int,
    fps: float,
    jpeg_q: int,
) -> None:
    period = 1.0 / max(fps, 1.0)
    seq = 0
    params = [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_q)]
    while True:
        t0 = asyncio.get_event_loop().time()
        frames = ingest.latest()
        left, right = render_stereo(frames, _latest_pose, out_w, out_h)
        ok_l, buf_l = cv2.imencode(".jpg", left, params)
        ok_r, buf_r = cv2.imencode(".jpg", right, params)
        if ok_l and ok_r and bridge.client_count > 0:
            packet = pack_stereo_jpeg(
                buf_l.tobytes(),
                buf_r.tobytes(),
                frame_seq=seq,
            )
            await bridge.broadcast_bin(packet)
        seq += 1
        elapsed = asyncio.get_event_loop().time() - t0
        await asyncio.sleep(max(0.0, period - elapsed))


async def _main_async(cfg: dict[str, Any]) -> None:
    host = str(cfg["mediamtx_host"])
    port = int(cfg["mediamtx_port"])
    transport = str(cfg.get("rtsp_transport", "tcp"))
    paths = _resolve_stream_paths(cfg)
    log.info("拉取 %d 路 RTSP: %s", len(paths), paths)
    urls = build_rtsp_urls(host, port, paths, transport)
    ingest = MultiRtspIngest(urls, rtsp_transport=transport)
    ingest.start()

    ws_h = str(cfg.get("ws_listen_host", "0.0.0.0"))
    ws_p = int(cfg.get("ws_listen_port", 8765))
    bridge = VrBridgeServer(ws_h, ws_p, on_pose=_on_pose)

    out_w = int(cfg.get("out_width", 1280))
    out_h = int(cfg.get("out_height", 720))
    fps = float(cfg.get("target_fps", 30))
    jpeg_q = int(cfg.get("jpeg_quality", 85))

    render_task = asyncio.create_task(
        _render_loop(
            ingest,
            bridge,
            out_w=out_w,
            out_h=out_h,
            fps=fps,
            jpeg_q=jpeg_q,
        )
    )
    try:
        await bridge.run()
    finally:
        render_task.cancel()
        try:
            await render_task
        except asyncio.CancelledError:
            pass
        ingest.stop()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser(description="渲染服务器 + VR WebSocket 桥")
    ap.add_argument("-c", "--config", type=Path, default=Path("config.yaml"))
    args = ap.parse_args()
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    try:
        asyncio.run(_main_async(cfg))
    except ValueError as e:
        log.error("%s", e)
        raise SystemExit(2) from e
    except KeyboardInterrupt:
        log.info("退出")


if __name__ == "__main__":
    main()
