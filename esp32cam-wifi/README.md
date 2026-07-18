# ESP32-CAM WiFi live stream

Live MJPEG stream served by the board itself. USB is only power once
this firmware is flashed.

- **View**: `http://<board-ip>/` (currently `http://10.50.33.51/`), or
  `http://esp32cam.local/` via mDNS. Works for anyone on the network —
  there is no authentication.
- `/status` — JSON diagnostics: PSRAM, heap, capture health, WiFi RSSI.
- `/stream` — raw MJPEG endpoint (usable from OpenCV etc.).
- `wifi_secrets.h` — network credentials (compiled in). Do not commit
  this file if the repo goes public.

## Behavior

- Joins the network in `wifi_secrets.h`, retrying every 20s forever, and
  prints `STA_IP <ip>` / `STA_RETRY` on serial at 115200.
- Always also broadcasts its own hotspot `ESP32CAM` / `esp32cam123`
  (stream reachable at `192.168.4.1`) as a fallback.

## Venue-network workaround (important)

The current network drops large packets to/from the board (measured:
800-byte pings lose 66%, 1200+ bytes lose 100%; RSSI ~-79 dBm). The
stream therefore sends JPEGs in paced 512-byte chunks (`send_paced`) —
do not "optimize" this back to whole-buffer writes; that is what made
the stream silently hang. If the board gets a stronger signal (closer to
an AP, better antenna), raise `FRAMESIZE_QVGA` to `FRAMESIZE_VGA` and
increase `CHUNK`.

## Reflash

```sh
arduino-cli compile --fqbn esp32:esp32:esp32cam .
arduino-cli upload  --fqbn esp32:esp32:esp32cam -p /dev/cu.usbserial-140 .
```

The serial-streaming variant (no WiFi needed) lives in
`../esp32cam-stream/`.

## Power-bank findings (2026-07-18)

The camera power bank cannot sustain WiFi transmit load. Symptoms and
progression, all verified by test:

- Bank + thin cable: chip never boots (red LED lies; use the boot-blink
  self-test — 3 white flashes at power-on).
- Bank + dongle on other port: boots, idle link perfect, but any
  sustained TX collapses (VGA 13% loss; QVGA ~1 fps; paced ~0.2 fps).
- Sharing the bank with the Axiometa board makes everything worse.
- Even OTA pushes die after ~200 KB on bank power (at any rate limit);
  OTA works perfectly on USB power (1.1 MB in 6 s).

Real fix is hardware: 470-1000 uF electrolytic across 5V/GND at the
board, or a stiffer 5V source (wall charger / rover 5V rail).

## OTA (HTTP push — espota does NOT work on the phone hotspot)

The hotspot blocks device->laptop TCP, which espota needs. Use the
/update endpoint instead (requires PartitionScheme=min_spiffs):

```sh
arduino-cli compile --fqbn esp32:esp32:esp32cam \
  --board-options "PartitionScheme=min_spiffs" --build-path /tmp/b .
curl -X POST --data-binary @/tmp/b/esp32cam-wifi.ino.bin \
  "http://10.61.237.18/update?pass=esp32cam123"
```

## Working battery setup (2026-07-18, second power bank)

The replacement power bank holds the rail: 640x480 at ~11.6 fps
sustained on battery, OTA pushes complete in ~10 s. All throttling
removed (full-MSS chunks, no pacing, VGA). If the camera ever moves to
a weaker supply again, the boot self-test is 3 white flash-LED blinks,
and the pacing knobs are CHUNK/PACE_MS in the sketch.
