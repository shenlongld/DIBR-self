"""
教室端：多路采集 + H.264（优先硬件编码）推 RTSP 至 mediamtx。
计算机 B 通过 rtsp://<mediamtx_host>:8554/<path> 拉流或依赖 mediamtx 录制文件。

依赖本机已安装 ffmpeg，且 PATH 可调用 ffmpeg。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml


def _pick_encoder(preferred: str) -> str:
    if preferred != "auto":
        return preferred
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，请先安装并加入 PATH")

    # 轻量探测：尝试各编码器是否被编译进 ffmpeg
    encoders = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    for name in ("h264_nvenc", "h264_qsv", "h264_amf"):
        if name in encoders:
            return name
    return "libx264"


def _build_input_args(stream: dict) -> list[str]:
    dtype = stream["device_type"]
    if dtype == "dshow":
        dev = stream["device"]
        w, h = int(stream["width"]), int(stream["height"])
        fps = int(stream["fps"])
        name = dev if dev.startswith("video=") else f"video={dev}"
        return [
            "-f",
            "dshow",
            "-video_size",
            f"{w}x{h}",
            "-framerate",
            str(fps),
            "-rtbufsize",
            "100M",
            "-i",
            name,
        ]
    if dtype == "v4l2":
        dev = stream["device"]
        fmt = stream.get("input_format", "yuyv422")
        w, h = int(stream["width"]), int(stream["height"])
        fps = int(stream["fps"])
        args = ["-f", "v4l2", "-input_format", fmt]
        if w and h:
            args += ["-video_size", f"{w}x{h}"]
        args += ["-framerate", str(fps), "-i", dev]
        return args
    if dtype == "file":
        path = stream["file"]
        return ["-re", "-stream_loop", "-1", "-i", str(Path(path).resolve())]
    if dtype == "rtsp_source":
        url = stream["url"]
        return ["-rtsp_transport", "tcp", "-i", url]
    raise ValueError(f"未知 device_type: {dtype}")


def _build_video_encode(encoder: str, bitrate_kbps: int, fps: int) -> list[str]:
    br = f"{int(bitrate_kbps)}k"
    if encoder == "h264_nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p4",
            "-tune",
            "ll",
            "-rc",
            "cbr",
            "-b:v",
            br,
            "-maxrate",
            br,
            "-bufsize",
            f"{int(bitrate_kbps * 2)}k",
            "-g",
            str(fps * 2),
            "-pix_fmt",
            "yuv420p",
        ]
    if encoder == "h264_qsv":
        return [
            "-c:v",
            "h264_qsv",
            "-b:v",
            br,
            "-maxrate",
            br,
            "-bufsize",
            f"{int(bitrate_kbps * 2)}k",
            "-g",
            str(fps * 2),
            "-pix_fmt",
            "yuv420p",
        ]
    if encoder == "h264_amf":
        return [
            "-c:v",
            "h264_amf",
            "-usage",
            "lowlatency",
            "-quality",
            "speed",
            "-b:v",
            br,
            "-g",
            str(fps * 2),
            "-pix_fmt",
            "yuv420p",
        ]
    # libx264 软件回退
    return [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-b:v",
        br,
        "-maxrate",
        br,
        "-bufsize",
        f"{int(bitrate_kbps * 2)}k",
        "-g",
        str(fps * 2),
        "-pix_fmt",
        "yuv420p",
    ]


def _launch_stream(
    host: str,
    port: int,
    stream: dict,
    encoder: str,
    rtsp_transport: str,
) -> subprocess.Popen:
    path = stream["path"]
    fps = int(stream["fps"])
    bitrate = int(stream["bitrate_kbps"])
    url = f"rtsp://{host}:{port}/{path}"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        *_build_input_args(stream),
        *_build_video_encode(encoder, bitrate, fps),
        "-an",
        "-f",
        "rtsp",
        "-rtsp_transport",
        rtsp_transport,
        url,
    ]
    print(f"[{path}] ffmpeg -> {url}")
    print(" ", " ".join(cmd))
    return subprocess.Popen(cmd)


def main() -> None:
    ap = argparse.ArgumentParser(description="多路采集推流至 mediamtx")
    ap.add_argument("-c", "--config", type=Path, default=Path("config.yaml"))
    args = ap.parse_args()
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    host = str(cfg["mediamtx_host"])
    port = int(cfg["mediamtx_port"])
    transport = str(cfg.get("rtsp_transport", "tcp"))
    encoder = _pick_encoder(str(cfg.get("video_encoder", "auto")))
    streams = cfg.get("streams")
    if not isinstance(streams, list) or len(streams) == 0:
        raise SystemExit("config 中 streams 必须为非空列表（路数 K≥1 任意）")
    seen: set[str] = set()
    for s in streams:
        p = str(s.get("path", "")).strip()
        if not p:
            raise SystemExit("streams 某项缺少 path")
        if p in seen:
            raise SystemExit(f"重复的 stream path: {p}")
        seen.add(p)

    procs: list[subprocess.Popen] = []
    try:
        for stream in streams:
            procs.append(_launch_stream(host, port, stream, encoder, transport))
        print(
            f"已启动 {len(procs)} 路推流（paths={sorted(seen)}），encoder={encoder}。Ctrl+C 结束。"
        )
        while True:
            time.sleep(1)
            for p in procs:
                if p.poll() is not None:
                    print("子进程异常退出，code=", p.returncode, file=sys.stderr)
                    raise SystemExit(1)
    except KeyboardInterrupt:
        print("正在停止…")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
