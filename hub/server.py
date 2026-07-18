#!/usr/bin/env python3
"""Hub service: message glue for glove, Quest WebXR, dashboard, rover, face.

Run from repo root:
  python -m hub.server
  # or
  uvicorn hub.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time
from contextlib import asynccontextmanager
from typing import Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from hub.camera import BOUNDARY, CameraProxy
from hub.face_link import FaceLink
from hub.glove_input import GloveInput
from hub.mapping import v_omega_to_pwm
from hub.rover_link import RoverLink
from hub.state import RobotState

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
WEB_DIR = ROOT / "web"
WEBXR_DIR = ROOT / "webxr" / "dist"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hub")


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())
    # defaults for new sections
    cfg.setdefault(
        "rover",
        {
            "transport": "serial",
            "port": cfg.get("serial", {}).get("port", "/dev/cu.usbserial-130"),
            "baud": cfg.get("serial", {}).get("baud", 115200),
            "send_hz": cfg.get("serial", {}).get("send_hz", 20),
            "servos_enabled": True,
            "servo": {
                "pan_min": -90,
                "pan_max": 90,
                "tilt_min": -45,
                "tilt_max": 45,
                "yaw_scale": 1.0,
                "pitch_scale": 1.0,
            },
        },
    )
    # merge serial into rover if only serial present
    if "serial" in cfg and cfg["rover"].get("transport") == "serial":
        cfg["rover"].setdefault("port", cfg["serial"]["port"])
        cfg["rover"].setdefault("baud", cfg["serial"]["baud"])
        cfg["rover"].setdefault("send_hz", cfg["serial"]["send_hz"])
    cfg.setdefault(
        "camera",
        {"stream_url": "http://esp32cam.local/stream"},
    )
    cfg.setdefault(
        "face",
        {"enabled": True, "host": "255.255.255.255", "port": 5010},
    )
    cfg.setdefault("hub", {"host": "0.0.0.0", "port": 8000, "state_hz": 15})
    return cfg


class Hub:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state = RobotState()
        self.rover = RoverLink(cfg["rover"])
        self.glove = GloveInput(cfg["glove"], cfg["drive"])
        self.camera = CameraProxy(cfg["camera"])
        self.face = FaceLink(cfg["face"])
        self.clients: Set[WebSocket] = set()
        self._tasks: list[asyncio.Task] = []

    async def start(self):
        await self.rover.start()
        await self.glove.start()
        await self.camera.start()
        await self.face.start()
        self._tasks = [
            asyncio.create_task(self._command_loop()),
            asyncio.create_task(self._broadcast_loop()),
        ]
        log.info("Hub started")

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.rover.close()
        await self.glove.stop()
        await self.camera.stop()
        await self.face.stop()

    def links(self) -> dict:
        return {
            "rover": self.rover.health(),
            "glove": self.glove.health(),
            "camera": self.camera.health(),
            "face": self.face.health(),
            "clients": len(self.clients),
        }

    async def _command_loop(self):
        hz = self.cfg["rover"].get("send_hz", 20)
        period = 1.0 / hz
        max_speed = self.cfg["drive"]["max_speed"]
        min_pwm = self.cfg["drive"]["min_pwm"]
        servo = self.cfg["rover"].get("servo", {})
        yaw_scale = servo.get("yaw_scale", 1.0)
        pitch_scale = servo.get("pitch_scale", 1.0)

        while True:
            t0 = time.monotonic()
            st = self.state
            left = right = 0

            if st.estop:
                left = right = 0
            else:
                # priority: glove > fresh manual (quest/dashboard)
                glove_pwm = self.glove.pwm()
                now = time.monotonic()
                if glove_pwm is not None:
                    left, right = glove_pwm
                elif now < st.manual_until:
                    left, right = v_omega_to_pwm(
                        st.manual_v, st.manual_omega, max_speed, min_pwm
                    )

            st.left, st.right = left, right
            self.rover.set_motors(left, right)

            # look: quest head or held look targets
            if time.monotonic() < st.look_until:
                pan = st.look_pan * yaw_scale
                tilt = st.look_tilt * pitch_scale
                self.rover.set_look(pan, tilt)

            await self.rover.tick()
            st.integrate_odometry(left, right, max_speed)

            # emotion from motion / estop
            if st.estop:
                st.emotion = "worried"
            elif left or right:
                st.emotion = "happy"
            elif st.mode == "companion":
                st.emotion = st.emotion if st.emotion != "worried" else "neutral"
            else:
                if st.emotion in ("happy", "worried"):
                    st.emotion = "neutral"
            self.face.send(st.emotion)

            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.001, period - elapsed))

    async def _broadcast_loop(self):
        hz = self.cfg["hub"].get("state_hz", 15)
        period = 1.0 / hz
        while True:
            msg = self.state.snapshot(self.links())
            raw = json.dumps(msg)
            dead = []
            for ws in list(self.clients):
                try:
                    await ws.send_text(raw)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)
            await asyncio.sleep(period)

    async def handle_message(self, data: dict):
        typ = data.get("type")
        st = self.state
        if typ == "head":
            # yaw/pitch degrees from headset
            st.set_look(float(data.get("yaw", 0)), float(data.get("pitch", 0)))
        elif typ == "look":
            st.set_look(float(data.get("pan", 0)), float(data.get("tilt", 0)))
        elif typ == "drive":
            st.set_manual_drive(
                float(data.get("v", 0)), float(data.get("omega", 0))
            )
        elif typ == "estop_toggle":
            st.toggle_estop()
            if st.estop:
                await self.rover.stop()
        elif typ == "estop":
            st.set_estop(bool(data.get("on", True)))
            if st.estop:
                await self.rover.stop()
        elif typ == "set_mode":
            st.set_mode(str(data.get("mode", "teleop")))
        elif typ == "set_emotion":
            st.emotion = str(data.get("emotion", "neutral"))
            self.face.send(st.emotion, force=True)
        elif typ == "mark_victim":
            st.mark_victim()
        elif typ == "undo_marker":
            st.undo_marker()
        elif typ == "calibrate_glove":
            await self.glove.send_cal()
        elif typ == "reset_pose":
            st.reset_pose()
        elif typ == "ping":
            pass
        else:
            log.debug("Unknown message type: %s", typ)


hub: Optional[Hub] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global hub
    cfg = load_config()
    hub = Hub(cfg)
    await hub.start()
    yield
    await hub.stop()
    hub = None


app = FastAPI(title="Axiometa Rover Hub", lifespan=lifespan)


@app.get("/api/health")
async def api_health():
    assert hub
    return {"ok": True, "links": hub.links(), "mode": hub.state.mode, "estop": hub.state.estop}


@app.get("/api/state")
async def api_state():
    assert hub
    return hub.state.snapshot(hub.links())


@app.get("/camera.mjpg")
async def camera_mjpeg():
    assert hub

    async def gen():
        async for part in hub.camera.mjpeg_generator():
            yield part

    return StreamingResponse(
        gen(),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY.decode()}",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    assert hub
    await ws.accept()
    hub.clients.add(ws)
    log.info("WS client connected (%d)", len(hub.clients))
    try:
        # immediate state
        await ws.send_text(json.dumps(hub.state.snapshot(hub.links())))
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await hub.handle_message(data)
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)
        log.info("WS client gone (%d)", len(hub.clients))


@app.get("/")
async def root():
    landing = WEB_DIR / "landing" / "index.html"
    if landing.is_file():
        return RedirectResponse("/site/")
    return RedirectResponse("/dashboard/")


# Static sites (mount after API routes)
if WEB_DIR.is_dir():
    app.mount(
        "/dashboard",
        StaticFiles(directory=WEB_DIR / "dashboard", html=True),
        name="dashboard",
    )
    landing = WEB_DIR / "landing"
    if landing.is_dir():
        app.mount("/site", StaticFiles(directory=landing, html=True), name="landing")

if WEBXR_DIR.is_dir():
    app.mount("/xr", StaticFiles(directory=WEBXR_DIR, html=True), name="webxr")


def main():
    import uvicorn

    cfg = load_config()
    host = cfg["hub"].get("host", "0.0.0.0")
    port = int(cfg["hub"].get("port", 8000))
    uvicorn.run("hub.server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
