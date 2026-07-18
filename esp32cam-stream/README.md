# ESP32-CAM USB serial live viewer

Live camera view from an AI-Thinker ESP32-CAM over the USB serial link —
no WiFi needed.

- `esp32cam-stream.ino` — firmware. Captures QVGA (320x240) JPEG frames and
  writes them to serial at 460800 baud, framed as
  `AA 55 AA 55 | uint32 LE length | uint32 LE byte-sum | JPEG`.
- `viewer.py` — laptop side. Reads the serial stream, drops corrupt frames
  via the checksum, and serves live MJPEG in the browser.

## Run

```sh
python3 viewer.py            # then open http://localhost:8765
# or: python3 viewer.py /dev/cu.usbserial-140 460800 8765
```

## Reflash the firmware

```sh
arduino-cli compile --fqbn esp32:esp32:esp32cam .
arduino-cli upload  --fqbn esp32:esp32:esp32cam -p /dev/cu.usbserial-140 .
```

## Notes

- ~11 fps at QVGA, measured ~1% corrupt frames (dropped by checksum).
  The bottleneck is the serial link: 460800 baud is the reliable ceiling
  for the macOS built-in CH340 driver (921600 corrupts everything).
  For higher resolution/framerate, switch to the WiFi `CameraWebServer`
  approach instead.
- Frame size / quality are set in `setup()` (`FRAMESIZE_QVGA`,
  `jpeg_quality = 15`). VGA works too, at ~2 fps.
- The viewer deasserts DTR/RTS when opening the port — opening it naively
  (e.g. with `screen`) resets the board into the bootloader.
