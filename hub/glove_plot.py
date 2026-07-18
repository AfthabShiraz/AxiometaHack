#!/usr/bin/env python3
"""Live scrolling plot of glove roll/pitch/yaw from UDP telemetry.

Use it to verify the IMU tracks your hand accurately:
  - rotate hand left/right (roll)  -> roll trace moves, others stay put
  - tilt hand back (palm outward)  -> pitch trace moves
  - twist wrist flat left/right    -> yaw trace moves (drifts over time)
  - hand still                     -> all traces flat, near their zeros

Keys (with the plot window focused):
  c  request recalibration — glove waits 5 s, then re-zeroes (hold still)
  q  quit

The shaded band is the planned ±10° drive deadzone (FR-2): traces inside
the band will mean "stopped" once the drive mapping exists.
"""

import collections
import json
import socket
import time

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

TELEMETRY_PORT = 5005
COMMAND_PORT = 5006
WINDOW_S = 15
DEADZONE_DEG = 10

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("", TELEMETRY_PORT))
sock.setblocking(False)

t0 = time.monotonic()
hist = collections.deque()  # (t, roll, pitch, yaw)
state = {"glove_ip": None, "rate_count": 0, "rate": 0.0,
         "rate_t": t0, "calibrating": False}

fig, ax = plt.subplots(figsize=(10, 5))
fig.canvas.manager.set_window_title("Glove IMU")
(l_roll,) = ax.plot([], [], label="roll", lw=1.5)
(l_pitch,) = ax.plot([], [], label="pitch", lw=1.5)
(l_yaw,) = ax.plot([], [], label="yaw", lw=1.5)
ax.axhspan(-DEADZONE_DEG, DEADZONE_DEG, alpha=0.12, color="gray",
           label=f"deadzone ±{DEADZONE_DEG}°")
ax.axhline(0, color="gray", lw=0.5)
ax.set_xlabel("seconds")
ax.set_ylabel("degrees")
ax.set_ylim(-100, 100)
ax.legend(loc="upper left")


def drain_socket():
    while True:
        try:
            data, addr = sock.recvfrom(1024)
        except BlockingIOError:
            return
        state["glove_ip"] = addr[0]
        state["rate_count"] += 1
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            continue
        now = time.monotonic() - t0
        hist.append((now, msg.get("roll", 0.0), msg.get("pitch", 0.0),
                     msg.get("yaw", 0.0)))
        state["calibrating"] = bool(msg.get("cal"))
        while hist and hist[0][0] < now - WINDOW_S:
            hist.popleft()


def update(_frame):
    drain_socket()
    now = time.monotonic()
    if now - state["rate_t"] >= 1.0:
        state["rate"] = state["rate_count"] / (now - state["rate_t"])
        state["rate_count"] = 0
        state["rate_t"] = now

    if hist:
        ts = [h[0] for h in hist]
        l_roll.set_data(ts, [h[1] for h in hist])
        l_pitch.set_data(ts, [h[2] for h in hist])
        l_yaw.set_data(ts, [h[3] for h in hist])
        ax.set_xlim(max(0, ts[-1] - WINDOW_S), max(WINDOW_S, ts[-1]))
        r, p, y = hist[-1][1], hist[-1][2], hist[-1][3]
        status = "CALIBRATING — hold still" if state["calibrating"] else \
            f"roll {r:+6.1f}   pitch {p:+6.1f}   yaw {y:+6.1f}"
        ax.set_title(f"{status}    ({state['rate']:.0f} Hz, "
                     f"{state['glove_ip']})")
    else:
        ax.set_title("waiting for glove packets on UDP :5005 ...")
    return l_roll, l_pitch, l_yaw


def on_key(event):
    if event.key == "q":
        plt.close(fig)
    elif event.key == "c" and state["glove_ip"]:
        for _ in range(5):
            sock.sendto(b"CAL", (state["glove_ip"], COMMAND_PORT))
            time.sleep(0.02)
        print(f"CAL sent to {state['glove_ip']} — neutral pose, "
              "zeroing in 5s, then hold still ~2s")


fig.canvas.mpl_connect("key_press_event", on_key)
anim = FuncAnimation(fig, update, interval=50, cache_frame_data=False)
plt.show()
