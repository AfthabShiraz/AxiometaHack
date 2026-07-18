"""Rover transport: USB serial or UDP line protocol (M/S/P)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger("hub.rover")


class RoverLink:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.transport = cfg.get("transport", "serial")  # serial | udp
        self._ser = None
        self._udp = None
        self._udp_addr = None
        self.left = 0
        self.right = 0
        self.pan = 0.0
        self.tilt = 0.0
        self.last_ack = 0.0
        self.last_heartbeat = 0.0
        self.watchdog_tripped = False
        self.connected = False
        self._rx_buf = b""
        self._lock = asyncio.Lock()

    async def start(self):
        if self.transport == "serial":
            await self._open_serial()
        else:
            await self._open_udp()
        await self.stop()

    async def _open_serial(self):
        import serial

        port = self.cfg["port"]
        baud = self.cfg.get("baud", 115200)
        try:
            self._ser = serial.Serial(port, baud, timeout=0)
            self.connected = True
            log.info("Opened serial %s @ %s", port, baud)
            await asyncio.sleep(2.5)  # Arduino/Genesis reset
        except Exception as e:
            log.warning("Serial open failed (%s): %s — dry mode", port, e)
            self._ser = None
            self.connected = False

    async def _open_udp(self):
        import socket

        host = self.cfg.get("host", "192.168.4.1")
        port = self.cfg.get("udp_port", 5007)
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setblocking(False)
        self._udp_addr = (host, port)
        self.connected = True
        log.info("Rover UDP -> %s:%s", host, port)

    async def close(self):
        await self.stop()
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        if self._udp:
            try:
                self._udp.close()
            except Exception:
                pass
            self._udp = None
        self.connected = False

    def set_motors(self, left: int, right: int):
        self.left = max(-255, min(255, int(left)))
        self.right = max(-255, min(255, int(right)))

    def set_look(self, pan: float, tilt: float):
        limits = self.cfg.get("servo", {})
        pan_min = limits.get("pan_min", -90)
        pan_max = limits.get("pan_max", 90)
        tilt_min = limits.get("tilt_min", -45)
        tilt_max = limits.get("tilt_max", 45)
        self.pan = max(pan_min, min(pan_max, float(pan)))
        self.tilt = max(tilt_min, min(tilt_max, float(tilt)))

    async def stop(self):
        self.left = 0
        self.right = 0
        await self._write(b"S\n")

    async def tick(self):
        """Send motor + look at fixed rate; drain inbound acks."""
        await self._drain_rx()
        cmd = f"M,{self.left},{self.right}\n".encode()
        await self._write(cmd)
        if self.cfg.get("servos_enabled", True):
            look = f"P,{self.pan:.1f},{self.tilt:.1f}\n".encode()
            await self._write(look)

    async def _write(self, data: bytes):
        async with self._lock:
            try:
                if self._ser:
                    self._ser.write(data)
                    self.connected = True
                elif self._udp and self._udp_addr:
                    self._udp.sendto(data, self._udp_addr)
                    self.connected = True
            except Exception as e:
                log.warning("Rover write failed: %s", e)
                self.connected = False

    async def _drain_rx(self):
        if not self._ser:
            return
        try:
            chunk = self._ser.read(256)
        except Exception:
            return
        if not chunk:
            return
        self._rx_buf += chunk
        while b"\n" in self._rx_buf:
            line, self._rx_buf = self._rx_buf.split(b"\n", 1)
            line = line.strip()
            now = time.monotonic()
            if line == b"A":
                self.last_ack = now
            elif line == b"H":
                self.last_heartbeat = now
            elif line == b"W":
                self.watchdog_tripped = True
                log.warning("Rover watchdog tripped")

    def health(self) -> dict:
        now = time.monotonic()
        hb_age = (now - self.last_heartbeat) if self.last_heartbeat else None
        return {
            "connected": self.connected,
            "transport": self.transport,
            "left": self.left,
            "right": self.right,
            "pan": self.pan,
            "tilt": self.tilt,
            "heartbeat_age_s": hb_age,
            "watchdog": self.watchdog_tripped,
        }
