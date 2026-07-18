#!/usr/bin/env python3
"""Glove teleoperation console (plan.md §5.1, Phase 3).

Gestures (after calibrating with a fist, palm down):
  flat fist (neutral)            -> robot cruises forward
  tilt hand back (palm outward)  -> brakes: slowing from brake_start_deg,
                                    fully stopped by brake_full_deg
  rotate hand left / right       -> turn left / right

The robot MOVES BY DEFAULT when armed: the console starts with the
e-stop latched, so press space once to start driving.

Keys (always active — keyboard overrides the glove):
  space   E-STOP: latched stop; press space again to arm/clear (FR-5)
  c       recalibrate glove (5 s countdown, then hold still ~2 s)
  k       toggle keyboard mode (glove ignored; w/s/a/d drive, x stops)
  q       quit (stops robot first)

Safety (plan.md §5.4):
  - streams commands to the Arduino at a fixed 20 Hz so its 300 ms
    watchdog is fed only while this process is alive (FR-15)
  - if glove telemetry stops for > timeout_s, commands stop (FR-16)
  - stop is commanded on startup before anything else (FR-18)
  - while the glove is calibrating, the robot is held stopped

All tunables live in config.json (FR-6). Run with --dry-run to print
computed commands without opening the serial port (plan.md §8).
"""

import argparse
import json
import pathlib
import select
import sys
import termios
import time
import tty

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from hub.glove_link import GloveLink
from hub.mapping import mix, scale_axis, throttle_from_tilt
from hub.rover_link import DryRunLink, make_rover_link

CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config.json"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print commands instead of driving the Arduino")
    args = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text())
    g, d, s = cfg["glove"], cfg["drive"], cfg["serial"]

    rover = DryRunLink() if args.dry_run else make_rover_link(cfg)
    rover.send("S\n")  # FR-18

    link = GloveLink()

    last_packet = 0.0
    pkt_count = 0
    roll = pitch = 0.0
    calibrating = False
    estop = True  # robot moves by default once armed: start latched safe
    keyboard_mode = False
    kb_left = kb_right = 0
    watchdog_note = ""

    old_attrs = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin.fileno())
    print("Glove drive console. space=e-stop  c=calibrate  "
          "k=keyboard mode  q=quit")
    try:
        next_send = time.monotonic()
        while True:
            readable, _, _ = select.select(
                link.sockets + [sys.stdin], [], [], 0.005)

            if sys.stdin in readable:
                key = sys.stdin.read(1)
                if key == "q":
                    break
                elif key == " ":
                    estop = not estop  # latched until pressed again (FR-5)
                elif key == "c":
                    link.send_cal()
                elif key == "k":
                    keyboard_mode = not keyboard_mode
                    kb_left = kb_right = 0
                elif keyboard_mode and key in "wsadx":
                    sp = d["keyboard_speed"]
                    kb_left, kb_right = {
                        "w": (sp, sp), "s": (-sp, -sp),
                        "a": (-sp, sp), "d": (sp, -sp),
                        "x": (0, 0)}[key]

            for msg in link.poll():
                last_packet = time.monotonic()
                pkt_count += 1
                roll = msg.get("roll", 0.0)
                pitch = msg.get("pitch", 0.0)
                calibrating = bool(msg.get("cal"))

            now = time.monotonic()
            glove_fresh = (now - last_packet) < g["timeout_s"]

            # --- decide command ---
            if estop or calibrating:
                left = right = 0
                mode = "E-STOP" if estop else "CAL"
                state = "STOPPED (press space to arm)" if estop \
                    else "STOPPED (calibrating)"
            elif keyboard_mode:
                left, right = kb_left, kb_right
                mode = "KEYBD"
                state = "DRIVING" if (left or right) else "STOPPED"
            elif not glove_fresh:
                left = right = 0  # FR-16
                mode = "NO GLOVE"
                state = "STOPPED (no telemetry)"
            else:
                tilt_back = g["pitch_sign"] * pitch
                throttle = throttle_from_tilt(tilt_back,
                                              g["brake_start_deg"],
                                              g["brake_full_deg"])
                steer = scale_axis(g["roll_sign"] * roll,
                                   g["deadzone_deg"], g["full_speed_deg"])
                left, right = mix(throttle, steer, d["max_speed"],
                                  d["min_pwm"], d.get("turn_gain", 2.0))
                mode = "GLOVE"
                if left == 0 and right == 0:
                    state = "STOPPED (tilted back)"
                elif throttle < 1.0:
                    state = "BRAKING"
                elif steer > 0.05:
                    state = "TURNING RIGHT"
                elif steer < -0.05:
                    state = "TURNING LEFT"
                else:
                    state = "DRIVING"

            # --- rover receive (heartbeat / watchdog) ---
            for line in rover.poll_lines():
                if line == "W":
                    watchdog_note = "WATCHDOG "

            # --- fixed-rate send ---
            if now >= next_send:
                next_send = now + 1.0 / s["send_hz"]
                rover.send(f"M,{left},{right}\n")
                age = time.monotonic() - last_packet if pkt_count else -1
                sys.stdout.write(
                    f"\r{watchdog_note}[{mode:8s}] "
                    f"L{left:+4d} R{right:+4d}  {state:28s} "
                    f"roll {roll:+6.1f} pitch {pitch:+6.1f}  "
                    f"pkts {pkt_count} age {age:4.2f}s   ")
                sys.stdout.flush()
                watchdog_note = ""
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)
        try:
            rover.send("S\n")
        except Exception:
            pass
        rover.close()
        print("\nStopped.")


if __name__ == "__main__":
    main()
