#!/usr/bin/env python3
"""Phase 1 keyboard driving (plan.md §6 Phase 1).

Streams motor commands to the rover Arduino at 20 Hz — continuously, even
when unchanged — so the firmware watchdog is fed only while this process is
alive and connected. Killing this script (Ctrl+C) or unplugging USB must
stop the robot within 300 ms: that is the Phase 1 acceptance demo.

Controls (toggle style — press once, robot keeps doing it):
  w / s   forward / reverse
  a / d   spin left / spin right
  space   stop
  [ / ]   speed down / up (steps of 25)
  q       quit (sends S first)

Usage:
  python3 hub/drive_keyboard.py [--port /dev/cu.usbserial-130]
"""

import argparse
import select
import sys
import termios
import time
import tty

import serial

SEND_HZ = 20
DEFAULT_PORT = "/dev/cu.usbserial-130"
BAUD = 115200
SPEED_STEP = 25
SPEED_MIN = 75    # below this the motors stall anyway
SPEED_MAX = 255
SPEED_DEFAULT = 150


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default=DEFAULT_PORT)
    args = ap.parse_args()

    ser = serial.Serial(args.port, BAUD, timeout=0)
    print(f"Opened {args.port}. Waiting for Arduino reset...")
    time.sleep(2.5)  # opening the port resets the Uno via DTR

    speed = SPEED_DEFAULT
    left, right = 0, 0
    last_heartbeat = time.monotonic()
    watchdog_note = ""
    rx_buf = b""

    old_attrs = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin.fileno())
    try:
        ser.write(b"S\n")  # FR-18: stop before anything else
        next_send = time.monotonic()
        while True:
            # --- keyboard ---
            if select.select([sys.stdin], [], [], 0)[0]:
                key = sys.stdin.read(1)
                if key == "q":
                    break
                elif key == "w":
                    left, right = speed, speed
                elif key == "s":
                    left, right = -speed, -speed
                elif key == "a":
                    left, right = -speed, speed
                elif key == "d":
                    left, right = speed, -speed
                elif key == " ":
                    left, right = 0, 0
                elif key == "[":
                    speed = max(SPEED_MIN, speed - SPEED_STEP)
                elif key == "]":
                    speed = min(SPEED_MAX, speed + SPEED_STEP)
                # moving keys re-apply the (possibly new) speed
                if key in "wsad" and (left, right) != (0, 0):
                    sign_l = 1 if left > 0 else -1
                    sign_r = 1 if right > 0 else -1
                    left, right = sign_l * speed, sign_r * speed

            # --- serial receive ---
            rx_buf += ser.read(256)
            while b"\n" in rx_buf:
                line, rx_buf = rx_buf.split(b"\n", 1)
                line = line.strip().decode(errors="replace")
                if line == "H":
                    last_heartbeat = time.monotonic()
                elif line == "W":
                    watchdog_note = "WATCHDOG TRIPPED "

            # --- fixed-rate command send ---
            now = time.monotonic()
            if now >= next_send:
                next_send = now + 1.0 / SEND_HZ
                ser.write(f"M,{left},{right}\n".encode())
                hb_age = now - last_heartbeat
                hb = "OK" if hb_age < 1.5 else f"LOST {hb_age:.1f}s"
                sys.stdout.write(
                    f"\r{watchdog_note}cmd M,{left:>4},{right:>4}  "
                    f"speed {speed:>3}  heartbeat {hb}   "
                )
                sys.stdout.flush()
                watchdog_note = ""

            time.sleep(0.005)
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)
        try:
            ser.write(b"S\n")
            ser.flush()
        except serial.SerialException:
            pass
        ser.close()
        print("\nStopped and closed port.")


if __name__ == "__main__":
    main()
