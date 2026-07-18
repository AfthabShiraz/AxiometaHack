"""UDP face module: push emotion/status to the Genesis Mini face board."""

from __future__ import annotations

import json
import logging
import socket
import time
from typing import Optional

log = logging.getLogger("hub.face")


class FaceLink:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enabled = cfg.get("enabled", True)
        self.host = cfg.get("host", "255.255.255.255")
        self.port = cfg.get("port", 5010)
        self._sock: Optional[socket.socket] = None
        self._last_emotion = ""
        self._last_send = 0.0

    async def start(self):
        if not self.enabled:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        self._sock = sock
        log.info("Face UDP -> %s:%s", self.host, self.port)

    async def stop(self):
        if self._sock:
            self._sock.close()
            self._sock = None

    def send(self, emotion: str, text: str = "", force: bool = False):
        if not self.enabled or not self._sock:
            return
        now = time.monotonic()
        if not force and emotion == self._last_emotion and (now - self._last_send) < 0.5:
            return
        msg = json.dumps(
            {"v": 1, "type": "face", "emotion": emotion, "text": text}
        ).encode()
        try:
            self._sock.sendto(msg, (self.host, self.port))
            self._last_emotion = emotion
            self._last_send = now
        except Exception as e:
            log.debug("Face send failed: %s", e)

    def health(self) -> dict:
        return {
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
            "last_emotion": self._last_emotion,
        }
