# Axiometa Rover — telepresence hub

4WD FPV teleop rover: wrist IMU + Quest WebXR passthrough + ESP32-CAM, glued by a Python hub.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Terminal 1 — hub (works without hardware; rover/camera show DOWN until present)
python -m hub.server

# Terminal 2 — optional public tunnel for Quest / Vercel
chmod +x scripts/tunnel.sh
./scripts/tunnel.sh 8000
```

Open:

- Landing: http://localhost:8000/
- Dashboard: http://localhost:8000/dashboard/
- Health: http://localhost:8000/api/health
- Camera proxy: http://localhost:8000/camera.mjpg

## WebXR (Quest)

```bash
cd webxr && npm install && npm run build
# hub will serve the build at /xr/
# or: npm run dev  (Vite on :5173) with ?hub=http://<laptop-ip>:8000
```

On Quest Browser: open the Vercel URL (or hub `/xr/`) with  
`?hub=https://YOUR-TUNNEL.trycloudflare.com` once — it is saved. Tap **Enter Rover View**.

**Controls:** thumbstick drive · trigger mark victim · squeeze = e-stop · hold squeeze ~1s = recenter panels · glove overrides stick when streaming.

**Casting:** Meta Horizon app (or castr.meta.com) on a laptop before the demo.

## Config

[`config.json`](config.json) — serial/UDP rover, glove ports, camera URL, face UDP, hub port.

See [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for WebSocket and line protocols.

## Teammate firmware (existing)

- `firmware/arduino/rover/` — motors (`M,l,r` / `S`)
- `firmware/glove/` — IMU UDP :5005
- `esp32cam-wifi/` — MJPEG `/stream`

Legacy consoles still work: `hub/drive_glove.py`, `hub/drive_keyboard.py`.
