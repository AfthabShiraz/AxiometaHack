# Product Specification: VR Search-and-Rescue Teleoperation Robot

**Version:** 1.0
**Status:** Draft for implementation
**Audience:** Implementation by Claude Code / development team. This document contains no code; it defines what to build, how components communicate, and what "done" looks like for each phase.

---

## 1. Overview

A simulated search-and-rescue (S&R) system in which a remote operator, wearing a Meta Quest headset, teleoperates a wheeled mobile robot through an obstacle course they cannot see directly. The operator drives the robot using hand gestures from an IMU-equipped glove, looks around via a head-tracked pan-tilt camera on the robot, and marks the locations of discovered "victims" on a live-updating minimap built from robot odometry.

### 1.1 Goals

- Full teleoperation loop: glove → robot motion, head → camera direction, live video → headset.
- A top-down minimap in the headset showing the robot's estimated position, breadcrumb trail, and operator-placed victim markers.
- Robust failure behavior: any loss of communication halts the robot within 300 ms.
- Modular architecture: each subsystem is independently testable and each build phase yields a working demo.

### 1.2 Non-goals (v1)

- Autonomous navigation, obstacle avoidance, or path planning.
- SLAM or lidar-based mapping. Mapping is dead-reckoning odometry only, optionally corrected by AprilTag landmarks (stretch goal).
- Multi-robot support.
- Audio streaming.
- Outdoor operation. Target environment is a single indoor room-scale course on one Wi-Fi network.

---

## 2. Hardware Inventory

| # | Component | Role | Notes |
|---|-----------|------|-------|
| H1 | Meta Quest headset | Operator interface (video display, head tracking, minimap, victim marking via controller trigger) | Runs a custom Unity app |
| H2 | Customisable mobile robot chassis, 4 DC motors | The rover | Motors driven as two paired sides (skid/tank steer) |
| H3 | Arduino + motor shield | Low-level motor controller | Receives serial commands; owns the safety watchdog |
| H4 | ESP32-CAM kit (OV2640) | Video streaming | Streams MJPEG over Wi-Fi; does nothing else |
| H5 | Glove with IMU | Driving input | Streams orientation over Wi-Fi or BLE |
| H6 | Hub computer (laptop or Raspberry Pi) | Central coordinator | Runs the hub service; all devices connect here. If a Raspberry Pi is used it may be mounted on the robot with the Arduino tethered via USB |
| H7 | Pan-tilt servo bracket (2 servos) — optional, Phase 5 | Head-tracked camera aiming | Servos driven by the Arduino, not the ESP32-CAM |
| H8 | Dedicated Wi-Fi router or hotspot | Network | All devices on one isolated network |
| H9 | AprilTag printouts — optional, Phase 7 | Localization landmarks | Known fixed positions in the course |

### 2.1 Hardware constraints the software must respect

- **ESP32-CAM GPIO scarcity:** the camera and SD interface consume nearly all pins. The ESP32-CAM must not be assigned motor, servo, or sensor duties.
- **Voltage levels:** ESP32 logic is 3.3 V; classic Arduino logic is 5 V. Any direct serial from Arduino TX to an ESP32 RX requires a voltage divider. (Avoided entirely if the Arduino talks only to the hub over USB.)
- **Video ceiling:** ESP32-CAM realistically delivers ~640×480 (VGA) at 15–20 fps MJPEG. The system must be designed around 200–500 ms glass-to-glass latency; do not assume better.
- **Power:** motors and servos must not share a regulator with logic boards; shared ground is required. Brown-outs on the ESP32-CAM cause reboots and stream drops.

---

## 3. System Architecture

### 3.1 Topology

All communication is hub-and-spoke. No device talks directly to another device; everything routes through the hub service.

```
Glove (IMU) ──Wi-Fi/BLE──►┐
ESP32-CAM  ──Wi-Fi MJPEG─►│  HUB SERVICE  ◄──Wi-Fi WebSocket──► Quest Unity app
Quest head pose ◄─────────┘  (Python)
                              │
                              └──USB serial──► Arduino ──► Motor shield ──► 4 motors
                                                      └──► Pan-tilt servos (Phase 5)
```

### 3.2 Component responsibilities

**Hub service (H6)** — single Python process (or small set of processes) that:
1. Accepts the ESP32-CAM MJPEG stream, re-serves frames to the Quest.
2. Receives glove IMU telemetry; converts orientation to differential drive commands (deadzone, scaling, e-stop gesture detection).
3. Receives Quest head pose; converts to pan-tilt servo angles.
4. Sends motor and servo commands to the Arduino over USB serial at a fixed rate (≥ 10 Hz, even when unchanged, to feed the watchdog).
5. Integrates odometry (commanded-velocity dead reckoning in v1; encoder-based if encoders are added) into a pose estimate (x, y, θ).
6. Maintains map state: breadcrumb trail (pose history) and victim markers.
7. Serves a WebSocket API to the Quest app (state down, inputs up).
8. Logs all telemetry with timestamps for post-run review.

**Arduino firmware (H3)** — deliberately dumb:
1. Parses a line-based serial protocol (Section 4.3).
2. Drives left/right motor pairs with signed PWM.
3. Drives two pan-tilt servos (Phase 5).
4. **Safety watchdog:** if no valid command has arrived in 300 ms, set all motor outputs to zero. This is the system's primary safety mechanism and must be implemented in the first firmware version.
5. Replies with an acknowledgment/heartbeat so the hub can detect a dead link.

**ESP32-CAM firmware (H4)** — video only:
1. Joins the dedicated Wi-Fi with a static IP or mDNS name.
2. Serves MJPEG at VGA, quality tuned for latency over fidelity.
3. Auto-reboots/reconnects on Wi-Fi loss.
4. Flash LED controllable via a simple HTTP endpoint (useful in a dim course; optional).

**Glove firmware/bridge (H5)** — depends on the glove's existing electronics:
1. Streams orientation (roll, pitch, yaw) at ~50 Hz to the hub (WebSocket or MQTT if Wi-Fi-capable; else BLE with a small bridge process on the hub).
2. Supports a zero/calibration trigger (Section 5.1).

**Quest Unity app (H1)**:
1. Renders the video feed on a fixed virtual screen inside a static virtual environment. The feed must **not** be head-locked or fill the field of view (motion-sickness mitigation).
2. Renders a top-down minimap panel: robot pose arrow, breadcrumb trail, victim markers, optional AprilTag landmarks.
3. Sends head yaw/pitch to the hub at ~30 Hz.
4. Controller trigger press → "mark victim at current robot pose" message to hub.
5. Displays connection status for every subsystem (camera / glove / robot serial) and a large e-stop indicator when the watchdog or e-stop is active.
6. A secondary controller button toggles a "relocate minimap / recenter view" utility.

---

## 4. Interfaces and Protocols

### 4.1 Network

- Single dedicated Wi-Fi network (H8). No internet dependency at runtime.
- All devices use static IPs or mDNS names, documented in a single config file consumed by the hub.
- Transport: WebSocket with JSON payloads for Quest and glove links; HTTP MJPEG for camera; USB serial for Arduino. MQTT is an acceptable alternative for the glove if its firmware already speaks it.

### 4.2 Hub ↔ Quest WebSocket (JSON messages)

Downstream (hub → Quest), sent at ~15–30 Hz combined:
- `state`: robot pose {x, y, theta}, trail (decimated), markers list, drive mode, e-stop flag, per-device connection health, current motor command (for HUD display).
- Video: delivered either as the hub re-serving the MJPEG endpoint (Quest fetches directly) or frames relayed over the WebSocket — implementer's choice; the requirement is a single URL/endpoint the Quest app is configured with, and ≤ 1 additional frame of added latency at the hub.

Upstream (Quest → hub):
- `head`: {yaw, pitch} degrees, ~30 Hz.
- `mark_victim`: {} — hub stamps it with current pose.
- `undo_marker`: {} — removes the most recent marker.
- `estop_toggle`: {} — operator-commanded software e-stop.

Exact field names are the implementer's choice but must be documented in the repo and versioned (`"v": 1` field in every message).

### 4.3 Hub ↔ Arduino serial protocol

- 115200 baud, newline-terminated ASCII lines. Human-typeable for debugging via Serial Monitor.
- Commands (hub → Arduino):
  - Motor: `M,<left>,<right>` where each is −255…255.
  - Stop: `S` (immediate zero).
  - Servos: `P,<pan_deg>,<tilt_deg>` (Phase 5), with firmware-side clamping to safe ranges.
- Responses (Arduino → hub):
  - Acknowledge each command or emit a periodic heartbeat `H` at ≥ 2 Hz.
  - On watchdog trip: emit `W` once so the hub can surface it in the Quest UI.
- The hub sends motor commands at a fixed ≥ 10 Hz cadence regardless of change, so silence always means failure.

### 4.4 Glove → hub telemetry

- `imu`: {roll, pitch, yaw, timestamp}, ~50 Hz.
- Optional if the glove has flex sensors or a button: `gesture`: {type} for e-stop and calibration triggers.
- If the glove is BLE-only, a hub-side bridge process translates BLE notifications into the same internal message.

---

## 5. Functional Requirements

### 5.1 Driving (glove)

- FR-1: Glove pitch maps to forward/reverse throttle; roll maps to steering, mixed into left/right differential commands.
- FR-2: A configurable deadzone (default ±10°) around the calibrated neutral pose commands zero motion.
- FR-3: Output speeds scale smoothly (linear or gentle curve) from deadzone edge to a configurable maximum; the maximum default must be conservatively slow (latency tolerance, Section 2.1).
- FR-4: A calibration action ("hold hand flat/neutral" + trigger, or dedicated gesture) re-zeroes the neutral orientation at any time. Yaw is used only relatively, never absolutely, due to IMU yaw drift.
- FR-5: An e-stop gesture (sharp shake, or fist-flex if available) and the Quest e-stop both command `S` and latch until explicitly cleared.
- FR-6: All mappings (axes, signs, deadzone, max speed, curve) live in a config file, not constants scattered in code.

### 5.2 Camera and head tracking

- FR-7: Quest head yaw/pitch drives pan/tilt servo angles with configurable scaling and clamped mechanical limits; movement is smoothed (rate-limited) to protect servos.
- FR-8: Before Phase 5 hardware exists, head tracking messages are still sent and logged (soft-launch), and the camera is fixed forward.
- FR-9: Video is displayed on a stationary virtual screen; a static virtual "operator room" surrounds it. Head-locking the feed is prohibited.

### 5.3 Mapping and victims

- FR-10: The hub maintains a 2-D pose estimate from dead reckoning (commanded velocities × time in v1; upgradeable to encoder counts without interface changes).
- FR-11: The trail records pose at ≥ 2 Hz, decimated for transmission.
- FR-12: Trigger press adds a victim marker at the current pose estimate; markers are numbered in discovery order; `undo_marker` removes the latest.
- FR-13: Map state persists for the duration of a run and can be exported (JSON) at run end for scoring/review.
- FR-14 (Phase 7, stretch): When the camera detects a known AprilTag, the hub snaps/corrects the pose estimate using the tag's configured true position. Tag detection may run on the hub against the video stream; it must not run on the ESP32-CAM.

### 5.4 Safety and robustness

- FR-15: Arduino watchdog stops motors after 300 ms of command silence (Section 3.2). Non-negotiable, present from the first firmware flash.
- FR-16: The hub independently commands `S` if glove telemetry stops for > 500 ms or the Quest link drops.
- FR-17: Every subsystem reconnects automatically; the Quest UI shows red/green health per link at all times.
- FR-18: On hub startup, motors are commanded to stop before any other traffic.

---

## 6. Build Phases and Acceptance Criteria

Each phase must be demoable on its own before the next begins.

| Phase | Deliverable | Acceptance criteria |
|-------|-------------|---------------------|
| 1 | Arduino firmware + hub serial link; keyboard driving from hub PC | Robot drives via keyboard; unplugging USB or killing the hub stops the robot within 300 ms (watchdog demo) |
| 2 | ESP32-CAM streaming firmware | Stable MJPEG stream viewable in a browser for 15 min without reboot; measured glass-to-glass latency documented (stopwatch method) |
| 3 | Glove integration | Robot driven by glove alone through a simple slalom; calibration and e-stop gestures work; deadzone tuned so a relaxed hand = stopped robot |
| 4 | Quest app v1 | Operator in another room sees live video on virtual screen, sees link-health HUD, can toggle software e-stop |
| 5 | Pan-tilt head tracking | Operator looks around the course by moving their head; servo motion smooth, limits respected |
| 6 | Odometry, minimap, victim marking | Full S&R run: operator finds and marks ≥ 3 hidden victims, exports the map; positional error acknowledged and measured, not hidden |
| 7 (stretch) | AprilTag drift correction | With 3–4 tags placed, end-of-run marker error measurably lower than Phase 6 baseline |

---

## 7. Configuration and Repo Expectations

- Single top-level config (YAML/JSON) holding: device addresses, serial port, drive mappings and limits, servo limits and scaling, deadzones, watchdog timings, tag positions.
- Repo layout: `hub/` (Python service), `firmware/arduino/`, `firmware/esp32cam/`, `firmware/glove/` (or `bridge/` if BLE), `quest/` (Unity project), `docs/` (this spec, protocol doc, wiring diagrams as they are finalized).
- A `docs/PROTOCOL.md` documenting every message with examples, kept in lockstep with implementation.
- Telemetry logs written per-run to `logs/` in a replayable format (JSON lines with timestamps).

## 8. Testing Requirements

- Serial protocol parser: unit tests including malformed lines, out-of-range values, partial lines.
- Watchdog: automated or scripted test proving stop-on-silence timing.
- Glove mapping: a hub "dry-run" mode that prints computed motor commands without a robot attached.
- Latency: a documented, repeatable measurement procedure (stopwatch-in-frame) run at Phases 2 and 4, results recorded in `docs/`.
- Soak test: full system idle-connected for 30 minutes without any link permanently dropping (auto-reconnect allowed).

## 9. Known Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Video latency causes crashes into obstacles | Conservative default max speed; latency displayed to operator; course designed with forgiving margins |
| Motion sickness | Fixed virtual screen, static environment, no head-locked video (FR-9) |
| IMU yaw drift makes steering wander | Relative yaw only; frequent re-zero gesture (FR-4) |
| Wi-Fi congestion | Dedicated router (H8); MJPEG quality tuned down before resolution |
| ESP32-CAM brown-outs | Separate/adequate 5 V supply, shared ground; documented in wiring guide |
| Odometry drift ruins the map | Set expectations: room-scale course, coarse map; Phase 7 tag correction as the fix |
| Dead-reckoning without encoders is crude | Interfaces designed so encoder odometry can replace commanded-velocity estimates without protocol changes (FR-10) |

## 10. Open Questions (resolve before Phase 3)

1. Exact glove hardware: Wi-Fi-capable ESP32 or BLE-only IMU module? Determines whether a hub-side BLE bridge is needed.
2. Hub placement: Raspberry Pi mounted on the robot (untethered, recommended) vs. laptop off-robot with a tethered Arduino (Phase 1 only) vs. adding a wireless serial link.
3. Does the chassis expose wheel encoders? If yes, Phase 6 should use them from the start.
4. Exact ESP32-CAM board variant (AI-Thinker vs. S3-based) — affects flashing procedure documentation only, not architecture.