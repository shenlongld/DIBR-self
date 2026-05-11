"""从 mediamtx 拉多路 RTSP，线程安全保留最新帧。"""
from __future__ import annotations

import os
import threading
import time

import cv2
import numpy as np


class MultiRtspIngest:
    def __init__(self, urls: list[str], rtsp_transport: str = "tcp") -> None:
        self._urls = urls
        self._rtsp_transport = rtsp_transport
        self._caps: list[cv2.VideoCapture | None] = [None] * len(urls)
        self._frames: list[np.ndarray | None] = [None] * len(urls)
        self._locks = [threading.Lock() for _ in urls]
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def start(self) -> None:
        if self._rtsp_transport == "tcp":
            # OpenCV/FFmpeg：强制 RTSP over TCP，跨机拉流更稳
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        for i, url in enumerate(self._urls):
            t = threading.Thread(target=self._loop, args=(i, url), daemon=True)
            self._threads.append(t)
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        for c in self._caps:
            if c is not None:
                c.release()
        self._caps = [None] * len(self._urls)

    def latest(self) -> list[np.ndarray | None]:
        """按相机顺序返回 BGR；可能为 None 若尚未收到帧。"""
        out: list[np.ndarray | None] = []
        for i, lk in enumerate(self._locks):
            with lk:
                f = self._frames[i]
                out.append(None if f is None else f.copy())
        return out

    def _loop(self, index: int, url: str) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                time.sleep(min(backoff, 10.0))
                backoff *= 1.5
                continue
            backoff = 1.0
            self._caps[index] = cap
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                with self._locks[index]:
                    self._frames[index] = frame
            cap.release()
            self._caps[index] = None
            time.sleep(0.5)


def build_rtsp_urls(host: str, port: int, paths: list[str], _transport: str) -> list[str]:
    return [f"rtsp://{host}:{port}/{p}" for p in paths]
