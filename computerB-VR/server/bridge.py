"""WebSocket：VR 客户端上传位姿；服务器下传打包后的双目 JPEG。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

import websockets
from websockets.server import WebSocketServerProtocol

log = logging.getLogger(__name__)


def _parse_pose_text(text: str) -> dict[str, Any] | None:
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    return d


class VrBridgeServer:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        on_pose: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._on_pose = on_pose
        self._clients: set[WebSocketServerProtocol] = set()
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast_bin(self, data: bytes) -> None:
        async with self._lock:
            dead: list[WebSocketServerProtocol] = []
            for ws in self._clients:
                try:
                    await ws.send(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)

    async def _handler(self, ws: WebSocketServerProtocol) -> None:
        async with self._lock:
            self._clients.add(ws)
        log.info("VR 客户端已连接，当前 %s", len(self._clients))
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    continue
                pose = _parse_pose_text(message)
                if pose is not None and self._on_pose:
                    self._on_pose(pose)
        finally:
            async with self._lock:
                self._clients.discard(ws)
            log.info("VR 客户端断开，当前 %s", len(self._clients))

    async def run(self) -> Coroutine[Any, Any, None]:
        async with websockets.serve(self._handler, self._host, self._port, max_size=None):
            log.info("WebSocket 监听 ws://%s:%s", self._host, self._port)
            await asyncio.Future()
