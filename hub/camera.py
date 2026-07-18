"""MJPEG proxy: one upstream ESP32-CAM connection, fan-out to N clients."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Set

import httpx

log = logging.getLogger("hub.camera")

BOUNDARY = b"frame"


class CameraProxy:
    def __init__(self, cfg: dict):
        self.url = cfg.get("stream_url", "http://esp32cam.local/stream")
        self._latest: Optional[bytes] = None
        self._latest_ts = 0.0
        self._clients: Set[asyncio.Queue] = set()
        self._task: Optional[asyncio.Task] = None
        self.connected = False
        self.frame_count = 0
        self.last_error = ""

    async def start(self):
        self._task = asyncio.create_task(self._upstream_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.connected = False

    async def _upstream_loop(self):
        while True:
            try:
                await self._read_stream()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                self.last_error = str(e)
                log.warning("Camera upstream error: %s — retry in 2s", e)
                await asyncio.sleep(2.0)

    async def _read_stream(self):
        timeout = httpx.Timeout(10.0, read=None)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", self.url) as resp:
                resp.raise_for_status()
                self.connected = True
                self.last_error = ""
                log.info("Camera upstream connected: %s", self.url)
                buf = b""
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    while True:
                        # Find JPEG SOI/EOI inside multipart stream
                        start = buf.find(b"\xff\xd8")
                        if start < 0:
                            buf = buf[-2:]
                            break
                        end = buf.find(b"\xff\xd9", start + 2)
                        if end < 0:
                            buf = buf[start:]
                            break
                        jpeg = buf[start : end + 2]
                        buf = buf[end + 2 :]
                        await self._publish(jpeg)

    async def _publish(self, jpeg: bytes):
        self._latest = jpeg
        self._latest_ts = time.monotonic()
        self.frame_count += 1
        dead = []
        for q in self._clients:
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(jpeg)
            except Exception:
                dead.append(q)
        for q in dead:
            self._clients.discard(q)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        if self._latest:
            q.put_nowait(self._latest)
        self._clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._clients.discard(q)

    def health(self) -> dict:
        age = (time.monotonic() - self._latest_ts) if self._latest_ts else None
        return {
            "connected": self.connected and age is not None and age < 3.0,
            "url": self.url,
            "frame_count": self.frame_count,
            "age_s": age,
            "clients": len(self._clients),
            "error": self.last_error,
        }

    async def mjpeg_generator(self):
        q = self.subscribe()
        try:
            while True:
                try:
                    jpeg = await asyncio.wait_for(q.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    # keep connection alive with a tiny comment-like part
                    yield (
                        b"--" + BOUNDARY + b"\r\n"
                        b"Content-Type: text/plain\r\n"
                        b"Content-Length: 0\r\n\r\n"
                    )
                    continue
                yield (
                    b"--" + BOUNDARY + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                    + jpeg + b"\r\n"
                )
        finally:
            self.unsubscribe(q)
