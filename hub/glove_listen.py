#!/usr/bin/env python3
"""Listen for glove telemetry and report rate + contents.

Subscribes to the glove (unicast; broadcast fallback) via GloveLink.
Prints every 25th packet plus a once-per-second summary of receive rate
and sequence gaps (lost packets).

Keys:
  c  request recalibration — the glove waits 5 s (get your hand into the
     neutral pose and hold still), then re-zeroes roll/pitch/yaw (FR-4)
  q  quit
"""

import select
import sys
import termios
import time
import tty

from glove_link import GloveLink

link = GloveLink()
print("Subscribing to glove ... ('c' = recalibrate in 5s, 'q' = quit)")

count = 0
lost = 0
last_seq = None
window_start = time.monotonic()

old_attrs = termios.tcgetattr(sys.stdin)
tty.setcbreak(sys.stdin.fileno())
try:
    while True:
        readable, _, _ = select.select(link.sockets + [sys.stdin], [], [], 1.0)

        if sys.stdin in readable:
            key = sys.stdin.read(1)
            if key == "q":
                break
            elif key == "c":
                if link.send_cal():
                    print(f"\rCAL sent to {link.glove_ip} — hold neutral "
                          "pose, zeroing in 5s (then ~2s still for gyro)")
                else:
                    print("\rglove not found yet, can't calibrate")

        for msg in link.poll():
            count += 1
            seq = msg.get("seq")
            if last_seq is not None and seq is not None and seq > last_seq + 1:
                lost += seq - last_seq - 1
            last_seq = seq
            if count % 25 == 0:
                tag = "  [CALIBRATING]" if msg.get("cal") else ""
                print(f"  {link.glove_ip}  {msg}{tag}")

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
