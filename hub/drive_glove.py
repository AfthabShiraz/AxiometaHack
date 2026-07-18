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
import socket
import sys
import termios
import time
import tty

CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config.json"


def scale_axis(angle, deadzone, full_scale):
    """Angle in degrees -> -1..1 with deadzone (FR-2, FR-3)."""
    mag = abs(angle)
    if mag <= deadzone:
        return 0.0
    frac = min(1.0, (mag - deadzone) / (full_scale - deadzone))
    return frac if angle > 0 else -frac


def throttle_from_tilt(tilt_back, brake_start, brake_full):
    """Neutral = full cruise; tilting back brakes progressively to 0."""
    if tilt_back <= brake_start:
        return 1.0
    if tilt_back >= brake_full:
        return 0.0
    return 1.0 - (tilt_back - brake_start) / (brake_full - brake_start)


def mix(throttle, steer, max_speed, min_pwm):
    """throttle/steer in -1..1 -> (left, right) PWM with stall floor."""
    left = throttle + steer
    right = throttle - steer
    biggest = max(1.0, abs(left), abs(right))
    left, right = left / biggest, right / biggest

    def to_pwm(v):
        if abs(v) < 1e-3:
            return 0
        span = max_speed - min_pwm
        return int((min_pwm + span * abs(v)) * (1 if v > 0 else -1))

    return to_pwm(left), to_pwm(right)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print commands instead of driving the Arduino")
    args = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text())
    g, d, s = cfg["glove"], cfg["drive"], cfg["serial"]

    ser = None
    if not args.dry_run:
        import serial
        ser = serial.Serial(s["port"], s["baud"], timeout=0)
        print(f"Opened {s['port']}. Waiting for Arduino reset...")
        time.sleep(2.5)
        ser.write(b"S\n")  # FR-18

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):  # let plot/listener run concurrently
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", g["telemetry_port"]))
    sock.setblocking(False)

    glove_ip = None
    last_packet = 0.0
    pkt_count = 0
    roll = pitch = 0.0
    calibrating = False
    estop = True  # robot moves by default once armed: start latched safe
    keyboard_mode = False
    kb_left = kb_right = 0
    watchdog_note = ""
    rx_buf = b""

    old_attrs = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin.fileno())
    print("Glove drive console. space=e-stop  c=calibrate  "
          "k=keyboard mode  q=quit")
    try:
        next_send = time.monotonic()
        while True:
            readable, _, _ = select.select([sock, sys.stdin], [], [], 0.005)

            if sys.stdin in readable:
                key = sys.stdin.read(1)
                if key == "q":
                    break
                elif key == " ":
                    estop = not estop  # latched until pressed again (FR-5)
                elif key == "c" and glove_ip:
                    for _ in range(5):
                        sock.sendto(b"CAL", (glove_ip, g["command_port"]))
                        time.sleep(0.02)
                elif key == "k":
                    keyboard_mode = not keyboard_mode
                    kb_left = kb_right = 0
                elif keyboard_mode and key in "wsadx":
                    sp = d["keyboard_speed"]
                    kb_left, kb_right = {
                        "w": (sp, sp), "s": (-sp, -sp),
                        "a": (-sp, sp), "d": (sp, -sp),
                        "x": (0, 0)}[key]

            if sock in readable:
                while True:
                    try:
                        data, addr = sock.recvfrom(1024)
                    except BlockingIOError:
                        break
                    try:
                        msg = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    glove_ip = addr[0]
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
            elif keyboard_mode:
                left, right = kb_left, kb_right
                mode = "KEYBD"
            elif not glove_fresh:
                left = right = 0  # FR-16
                mode = "NO GLOVE"
            else:
                tilt_back = g["pitch_sign"] * pitch
                throttle = throttle_from_tilt(tilt_back,
                                              g["brake_start_deg"],
                                              g["brake_full_deg"])
                steer = scale_axis(g["roll_sign"] * roll,
                                   g["deadzone_deg"], g["full_speed_deg"])
                left, right = mix(throttle, steer, d["max_speed"],
                                  d["min_pwm"])
                mode = "GLOVE"

            # --- serial receive (heartbeat / watchdog) ---
            if ser:
                rx_buf += ser.read(256)
                while b"\n" in rx_buf:
                    line, rx_buf = rx_buf.split(b"\n", 1)
                    if line.strip() == b"W":
                        watchdog_note = "WATCHDOG "

            # --- fixed-rate send ---
            if now >= next_send:
                next_send = now + 1.0 / s["send_hz"]
                cmd = f"M,{left},{right}\n"
                if ser:
                    ser.write(cmd.encode())
                age = time.monotonic() - last_packet if pkt_count else -1
                sys.stdout.write(
                    f"\r{watchdog_note}[{mode:8s}] "
                    f"roll {roll:+6.1f} pitch {pitch:+6.1f} -> "
                    f"M,{left:>4},{right:>4}  "
                    f"pkts {pkt_count} age {age:4.2f}s   ")
                sys.stdout.flush()
                watchdog_note = ""
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)
        if ser:
            try:
                ser.write(b"S\n")
                ser.flush()
            except Exception:
                pass
            ser.close()
        print("\nStopped.")


if __name__ == "__main__":
    main()
