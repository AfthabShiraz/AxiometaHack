"""Rover transport: USB serial or UDP line protocol (M/S/P)."""

from __future__ import annotations

import asyncio
import logging
import socket
import time

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
        host = self.cfg.get("host", self.cfg.get("bridge_host", "192.168.4.1"))
        port = self.cfg.get("udp_port", self.cfg.get("bridge_port", 5007))
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


class SerialLink:
    def __init__(self, port, baud):
        import serial
        self.ser = serial.Serial(port, baud, timeout=0)
        self.buf = b""
        print(f"Opened {port}. Waiting for Arduino reset...")
        time.sleep(2.5)  # opening the port resets the Uno via DTR

    def send(self, line):
        self.ser.write(line.encode())

    def poll_lines(self):
        self.buf += self.ser.read(256)
        lines = []
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            raw = raw.strip()
            if raw:
                lines.append(raw.decode(errors="replace"))
        return lines

    def close(self):
        try:
            self.ser.flush()
        except Exception:
            pass
        self.ser.close()


class UdpBridgeLink:
    """Sync UDP bridge used by the standalone glove console."""

    HELLO_PORT = 5011
    RESOLVE_RETRY_S = 5.0

    def __init__(self, host, port):
        self.host = host      # mDNS fallback only
        self.port = port
        self.addr = None      # (ip, port) learned from HELLO
        self._last_resolve = 0.0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", self.HELLO_PORT))
        self.sock.setblocking(False)
        print(f"Rover bridge: waiting for rover HELLO on :{self.HELLO_PORT} "
              f"(mDNS fallback {host}:{port})")

    def _resolve_fallback(self):
        now = time.monotonic()
        if now - self._last_resolve < self.RESOLVE_RETRY_S:
            return
        self._last_resolve = now
        try:
            ip = socket.getaddrinfo(self.host, None, socket.AF_INET)[0][4][0]
            self.addr = (ip, self.port)
        except OSError:
            pass

    def send(self, line):
        if self.addr is None:
            self._resolve_fallback()
            if self.addr is None:
                return
        try:
            self.sock.sendto(line.encode(), self.addr)
        except OSError:
            pass

    def poll_lines(self):
        lines = []
        while True:
            try:
                data, addr = self.sock.recvfrom(256)
            except (BlockingIOError, OSError):
                break
            for raw in data.split(b"\n"):
                raw = raw.strip()
                if not raw:
                    continue
                if raw.startswith(b"HELLO"):
                    # "HELLO rover <cmd_port>" advertises the command port.
                    parts = raw.split()
                    try:
                        cmd_port = int(parts[2])
                    except (IndexError, ValueError):
                        cmd_port = self.port
                    new_addr = (addr[0], cmd_port)
                    if self.addr != new_addr:
                        print(f"\nrover announced: commands to "
                              f"{new_addr[0]}:{new_addr[1]}")
                    self.addr = new_addr
                    continue
                lines.append(raw.decode(errors="replace"))
        return lines

    def close(self):
        self.sock.close()


class DryRunLink:
    def send(self, line):
        pass

    def poll_lines(self):
        return []

    def close(self):
        pass


def make_rover_link(cfg):
    transport = cfg.get("rover", {}).get("transport", "serial")
    if transport == "udp":
        r = cfg["rover"]
        host = r.get("bridge_host", r.get("host", "192.168.4.1"))
        port = r.get("bridge_port", r.get("udp_port", 5007))
        return UdpBridgeLink(host, port)
    s = cfg["serial"]
    return SerialLink(s["port"], s["baud"])
