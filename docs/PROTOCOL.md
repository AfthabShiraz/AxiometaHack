# Rover Hub Protocol

All JSON messages include `"v": 1`.

## WebSocket (`/ws`)

### Hub → clients (`type: "state"`, ~15 Hz)

```json
{
  "v": 1,
  "type": "state",
  "t": 1710000000.0,
  "mode": "teleop",
  "estop": true,
  "emotion": "neutral",
  "pose": { "x": 0.0, "y": 0.0, "theta": 0.0 },
  "trail": [{ "x": 0.0, "y": 0.0 }],
  "markers": [{ "id": 1, "x": 0.5, "y": -0.2, "t": 1710000000.0 }],
  "motors": { "left": 0, "right": 0 },
  "look": { "pan": 0.0, "tilt": 0.0 },
  "links": {
    "rover": { "connected": true, "left": 0, "right": 0, "watchdog": false },
    "glove": { "connected": false, "roll": 0, "pitch": 0 },
    "camera": { "connected": true, "frame_count": 120 },
    "face": { "enabled": true, "last_emotion": "neutral" },
    "clients": 1
  }
}
```

### Clients → hub

| type | fields | meaning |
|------|--------|---------|
| `head` | `yaw`, `pitch` (deg) | Headset orientation → pan/tilt |
| `look` | `pan`, `tilt` (deg) | Explicit look |
| `drive` | `v`, `omega` (−1…1) | Manual drive (Quest/dashboard); glove wins if fresh |
| `estop_toggle` | — | Latch software e-stop |
| `estop` | `on` bool | Set e-stop |
| `set_mode` | `mode`: `teleop` \| `rescue` \| `companion` \| `manual` | |
| `set_emotion` | `emotion` | Push to face module |
| `mark_victim` | — | Marker at current pose |
| `undo_marker` | — | Remove latest marker |
| `calibrate_glove` | — | UDP `CAL` to glove |
| `reset_pose` | — | Zero odometry |
| `ping` | — | no-op |

## Hub → rover (serial or UDP line protocol)

Same as Phase 1 firmware:

- `M,<left>,<right>` — PWM −255…255, sent at 20 Hz
- `S` — immediate stop
- `P,<pan>,<tilt>` — servo degrees (Phase 5)

Responses: `A` ack, `H` heartbeat, `W` watchdog trip.

## Glove → hub (UDP :5005)

```json
{"v":1,"seq":123,"t":45678,"roll":1.2,"pitch":-3.4,"yaw":10.5,"cal":0}
```

Hub → glove `CAL` on UDP :5006.

## Hub → face (UDP :5010)

```json
{"v":1,"type":"face","emotion":"happy","text":""}
```

Emotions: `neutral`, `happy`, `worried`, `sleepy`, `scan`, `talking`.

## HTTP

| path | purpose |
|------|---------|
| `/camera.mjpg` | Proxied MJPEG from ESP32-CAM |
| `/api/health` | Link health JSON |
| `/api/state` | Latest state snapshot |
| `/dashboard/` | Backup control UI |
| `/xr/` | Built WebXR app (after `npm run build`) |
| `/` | Landing page |

## Tunnel

```bash
./scripts/tunnel.sh 8000
```

Use the printed `https://….trycloudflare.com` as `?hub=` for the WebXR / dashboard apps. WebSocket path is `/ws` (`wss://….trycloudflare.com/ws`).
