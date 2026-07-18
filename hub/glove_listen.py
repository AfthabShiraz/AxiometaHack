#!/usr/bin/env python3
"""Listen for glove UDP telemetry (port 5005) and report rate + contents.

Prints every 25th packet plus a once-per-second summary of receive rate,
sequence gaps (lost packets), and sender address.

Keys:
  c  request recalibration — the glove waits 5 s (get your hand into the
     neutral pose and hold still), then re-zeroes roll/pitch/yaw (FR-4)
  q  quit
"""

import json
import select
import socket
import sys
import termios
import time
import tty

TELEMETRY_PORT = 5005
COMMAND_PORT = 5006

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("", TELEMETRY_PORT))
sock.setblocking(False)
print(f"Listening on UDP :{TELEMETRY_PORT} ... "
      "('c' = recalibrate in 5s, 'q' = quit)")

count = 0
lost = 0
last_seq = None
glove_addr = None
window_start = time.monotonic()

old_attrs = termios.tcgetattr(sys.stdin)
tty.setcbreak(sys.stdin.fileno())
try:
    while True:
        readable, _, _ = select.select([sock, sys.stdin], [], [], 1.0)

        if sys.stdin in readable:
            key = sys.stdin.read(1)
            if key == "q":
                break
            elif key == "c":
                if glove_addr is None:
                    print("\rno glove seen yet, can't calibrate")
                else:
                    for _ in range(5):  # a few copies in case of loss
                        sock.sendto(b"CAL", (glove_addr, COMMAND_PORT))
                        time.sleep(0.02)
                    print(f"\rCAL sent to {glove_addr} — hold neutral pose, "
                          "zeroing in 5s (then ~2s still for gyro)")

        if sock in readable:
            data, addr = sock.recvfrom(1024)
            glove_addr = addr[0]
            count += 1
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                print(f"bad packet from {addr[0]}: {data[:80]!r}")
                continue
            seq = msg.get("seq")
            if last_seq is not None and seq is not None and seq > last_seq + 1:
                lost += seq - last_seq - 1
            last_seq = seq
            if count % 25 == 0:
                tag = "  [CALIBRATING]" if msg.get("cal") else ""
                print(f"  {addr[0]}  {msg}{tag}")

        now = time.monotonic()
        if now - window_start >= 1.0:
            if count == 0:
                print("no packets in the last second")
            else:
                print(f"rate: {count / (now - window_start):.1f} Hz, "
                      f"lost this window: {lost}")
            count = 0
            lost = 0
            window_start = now
finally:
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)
