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
