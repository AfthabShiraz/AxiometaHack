"""Central robot state: modes, e-stop, odometry, markers, emotion."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


MODES = ("teleop", "rescue", "companion", "manual")


@dataclass
class Marker:
    id: int
    x: float
    y: float
    t: float


@dataclass
class RobotState:
    mode: str = "teleop"
    estop: bool = True
    emotion: str = "neutral"
    # pose (meters, radians)
    x: float = 0.0
    y: float = 0.0
    theta: float = 0.0
    trail: List[Tuple[float, float]] = field(default_factory=list)
    markers: List[Marker] = field(default_factory=list)
    _next_marker_id: int = 1
    _last_odom: float = field(default_factory=time.monotonic)
    # commanded drive for HUD
    left: int = 0
    right: int = 0
    # manual / quest drive override (normalized -1..1), expires if stale
    manual_v: float = 0.0
    manual_omega: float = 0.0
    manual_until: float = 0.0
    # look from quest
    look_pan: float = 0.0
    look_tilt: float = 0.0
    look_until: float = 0.0
    # wheel model for dead reckoning (rough TT chassis)
    wheelbase_m: float = 0.14
    max_mps: float = 0.35  # at max_speed PWM

    def set_mode(self, mode: str):
        if mode in MODES:
            self.mode = mode

    def toggle_estop(self) -> bool:
        self.estop = not self.estop
        if self.estop:
            self.emotion = "worried"
        return self.estop

    def set_estop(self, on: bool):
        self.estop = on
        if on:
            self.emotion = "worried"

    def set_manual_drive(self, v: float, omega: float, ttl: float = 0.35):
        self.manual_v = max(-1.0, min(1.0, v))
        self.manual_omega = max(-1.0, min(1.0, omega))
        self.manual_until = time.monotonic() + ttl

    def set_look(self, pan: float, tilt: float, ttl: float = 0.5):
        self.look_pan = pan
        self.look_tilt = tilt
        self.look_until = time.monotonic() + ttl

    def mark_victim(self) -> Optional[Marker]:
        m = Marker(self._next_marker_id, self.x, self.y, time.time())
        self._next_marker_id += 1
        self.markers.append(m)
        self.emotion = "happy"
        return m

    def undo_marker(self) -> bool:
        if not self.markers:
            return False
        self.markers.pop()
        return True

    def integrate_odometry(self, left_pwm: int, right_pwm: int, max_speed: int):
        now = time.monotonic()
        dt = now - self._last_odom
        self._last_odom = now
        if dt <= 0 or dt > 1.0 or self.estop:
            return
        if max_speed <= 0:
            return
        vl = (left_pwm / max_speed) * self.max_mps
        vr = (right_pwm / max_speed) * self.max_mps
        v = 0.5 * (vl + vr)
        omega = (vr - vl) / self.wheelbase_m
        self.theta += omega * dt
        self.x += v * math.cos(self.theta) * dt
        self.y += v * math.sin(self.theta) * dt
        # trail at ~2 Hz
        if not self.trail or (
            (self.x - self.trail[-1][0]) ** 2 + (self.y - self.trail[-1][1]) ** 2
            > 0.02**2
        ):
            self.trail.append((self.x, self.y))
            if len(self.trail) > 500:
                self.trail = self.trail[-500:]

    def reset_pose(self):
        self.x = self.y = self.theta = 0.0
        self.trail.clear()

    def snapshot(self, links: dict) -> dict:
        trail = self.trail[:: max(1, len(self.trail) // 80)] if self.trail else []
        return {
            "v": 1,
            "type": "state",
            "t": time.time(),
            "mode": self.mode,
            "estop": self.estop,
            "emotion": self.emotion,
            "pose": {"x": self.x, "y": self.y, "theta": self.theta},
            "trail": [{"x": a, "y": b} for a, b in trail],
            "markers": [
                {"id": m.id, "x": m.x, "y": m.y, "t": m.t} for m in self.markers
            ],
            "motors": {"left": self.left, "right": self.right},
            "look": {"pan": self.look_pan, "tilt": self.look_tilt},
            "links": links,
        }
