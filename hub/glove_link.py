"""Shared glove telemetry link for hub scripts.

The venue AP drops broadcast UDP, so each consumer opens its own
ephemeral-port socket and sends "SUB" to the glove's command port every
~2 s; the glove unicasts telemetry back to that exact socket. A second
socket still listens on the broadcast port as a fallback (and to discover
the glove's IP if mDNS is unavailable).

Usage:
    link = GloveLink()
    ...
    for msg in link.poll():   # call frequently; handles SUB keepalive
        print(msg["roll"], msg["pitch"], msg["yaw"], msg.get("cal"))
    link.send_cal()           # request the 5 s re-zero
    link.sockets              # for select()
"""

import json
import socket
import time

GLOVE_HOST = "glove.local"
TELEMETRY_PORT = 5005
COMMAND_PORT = 5006
SUB_INTERVAL_S = 2.0
RESOLVE_RETRY_S = 5.0


class GloveLink:
    def __init__(self):
        # private socket: SUB keepalives out, unicast telemetry in
        self.usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.usock.bind(("", 0))
        self.usock.setblocking(False)
        # shared broadcast socket: fallback + IP discovery
        self.bsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.bsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            self.bsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self.bsock.bind(("", TELEMETRY_PORT))
        self.bsock.setblocking(False)

        self.sockets = [self.usock, self.bsock]
        self.glove_ip = None
        self._last_sub = 0.0
        self._last_resolve = 0.0

    def _resolve(self):
        now = time.monotonic()
        if now - self._last_resolve < RESOLVE_RETRY_S:
            return
        self._last_resolve = now
        try:
            self.glove_ip = socket.getaddrinfo(
                GLOVE_HOST, None, socket.AF_INET)[0][4][0]
        except OSError:
            pass  # keep trying; broadcast fallback may still find it

    def _keepalive(self):
        now = time.monotonic()
        if now - self._last_sub < SUB_INTERVAL_S:
            return
        if self.glove_ip is None:
            self._resolve()
        if self.glove_ip is not None:
            self._last_sub = now
            try:
                self.usock.sendto(b"SUB", (self.glove_ip, COMMAND_PORT))
            except OSError:
                pass

    def poll(self):
        """Send keepalive if due; return list of decoded telemetry dicts."""
        self._keepalive()
        msgs = []
        for sock in (self.usock, self.bsock):
            while True:
                try:
                    data, addr = sock.recvfrom(1024)
                except (BlockingIOError, OSError):
                    break
                self.glove_ip = addr[0]
                try:
                    msgs.append(json.loads(data))
                except json.JSONDecodeError:
                    continue
        return msgs

    def send_cal(self):
        """Request the glove's 5 s re-zero. Returns True if sent."""
        if self.glove_ip is None:
            self._resolve()
        if self.glove_ip is None:
            return False
        for _ in range(5):  # copies in case of loss
            self.usock.sendto(b"CAL", (self.glove_ip, COMMAND_PORT))
            time.sleep(0.02)
        return True
