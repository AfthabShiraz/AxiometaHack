"""Async UDP glove ingest -> throttle/steer PWM."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from typing import Optional, Tuple

from hub.mapping import mix, scale_axis, throttle_from_tilt

log = logging.getLogger("hub.glove")


class GloveInput:
    def __init__(self, glove_cfg: dict, drive_cfg: dict):
        self.cfg = glove_cfg
        self.drive = drive_cfg
        self.roll = 0.0
        self.pitch = 0.0
        self.yaw = 0.0
        self.calibrating = False
        self.last_packet = 0.0
        self.pkt_count = 0
        self.glove_ip: Optional[str] = None
        self._sock: Optional[socket.socket] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", self.cfg["telemetry_port"]))
        sock.setblocking(False)
        self._sock = sock
        self._task = asyncio.create_task(self._loop())
        log.info("Glove listening UDP :%s", self.cfg["telemetry_port"])

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._sock:
            self._sock.close()
            self._sock = None

    async def _loop(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                data, addr = await asyncio.wait_for(
                    loop.run_in_executor(None, self._recv), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.01)
                continue
            if data is None:
                await asyncio.sleep(0.005)
                continue
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            self.glove_ip = addr[0]
            self.last_packet = time.monotonic()
            self.pkt_count += 1
            self.roll = float(msg.get("roll", 0.0))
            self.pitch = float(msg.get("pitch", 0.0))
            self.yaw = float(msg.get("yaw", 0.0))
            self.calibrating = bool(msg.get("cal"))

    def _recv(self):
        try:
            return self._sock.recvfrom(1024)
        except BlockingIOError:
            return None, None

    def fresh(self) -> bool:
        if self.last_packet <= 0:
            return False
        return (time.monotonic() - self.last_packet) < self.cfg["timeout_s"]

    def pwm(self) -> Optional[Tuple[int, int]]:
        """Return (left, right) if glove should drive, else None."""
        if self.calibrating or not self.fresh():
            return None
        g = self.cfg
        tilt_back = g["pitch_sign"] * self.pitch
        throttle = throttle_from_tilt(
            tilt_back, g["brake_start_deg"], g["brake_full_deg"]
        )
        steer = scale_axis(
            g["roll_sign"] * self.roll, g["deadzone_deg"], g["full_speed_deg"]
        )
        return mix(throttle, steer, self.drive["max_speed"], self.drive["min_pwm"])

    async def send_cal(self):
        if not self._sock or not self.glove_ip:
            return
        port = self.cfg["command_port"]
        for _ in range(5):
            try:
                self._sock.sendto(b"CAL", (self.glove_ip, port))
            except Exception:
                pass
            await asyncio.sleep(0.02)

    def health(self) -> dict:
        age = (time.monotonic() - self.last_packet) if self.last_packet else None
        return {
            "connected": self.fresh(),
            "calibrating": self.calibrating,
            "pkt_count": self.pkt_count,
            "age_s": age,
            "roll": self.roll,
            "pitch": self.pitch,
            "yaw": self.yaw,
            "ip": self.glove_ip,
        }
