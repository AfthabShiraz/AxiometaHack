#!/usr/bin/env python3
"""Live viewer for the ESP32-CAM serial JPEG stream.

Reads framed JPEGs from the serial port (magic AA 55 AA 55 + uint32 LE length
+ uint32 LE byte-sum + JPEG bytes, as sent by esp32cam-stream.ino) and serves
them at http://localhost:8765 as an MJPEG stream you can watch in a browser.
Frames whose checksum fails (serial glitches) are dropped, not displayed.

Usage: python3 viewer.py [serial-port] [baud] [http-port]
"""

import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/cu.usbserial-140"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 460800
HTTP_PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8765
MAGIC = b"\xaa\x55\xaa\x55"
MAX_FRAME = 200_000  # sanity cap; VGA JPEGs are ~15-40 KB

latest = {"frame": None, "count": 0}
cond = threading.Condition()


def reader():
    while True:
        try:
            ser = serial.Serial()
            ser.port, ser.baudrate, ser.timeout = PORT, BAUD, 2
            # Deassert DTR/RTS so the auto-reset circuit doesn't put the
            # ESP32 into bootloader mode, then pulse EN for a clean run boot.
            ser.dtr = False
            ser.rts = False
            ser.open()
            try:
                ser.rts = True
                time.sleep(0.1)
                ser.rts = False
                print(f"Connected to {PORT} @ {BAUD}")
                buf = b""
                while True:
                    buf += ser.read(4096)
                    while True:
                        i = buf.find(MAGIC)
                        if i < 0:
                            # keep tail in case magic is split across reads
                            buf = buf[-3:]
                            break
                        if len(buf) < i + 12:
                            buf = buf[i:]
                            break
                        length = int.from_bytes(buf[i + 4 : i + 8], "little")
                        checksum = int.from_bytes(buf[i + 8 : i + 12], "little")
                        if not 0 < length <= MAX_FRAME:
                            buf = buf[i + 4 :]  # bad header, resync
                            continue
                        if len(buf) < i + 12 + length:
                            buf = buf[i:]
                            break
                        frame = buf[i + 12 : i + 12 + length]
                        buf = buf[i + 12 + length :]
                        if (frame[:2] == b"\xff\xd8"
                                and sum(frame) & 0xFFFFFFFF == checksum):
                            with cond:
                                latest["frame"] = frame
                                latest["count"] += 1
                                cond.notify_all()
                        else:
                            print("dropped corrupt frame")
                    # Surface camera errors sent as plain text
                    if b"CAMERA_INIT_FAILED" in buf:
                        print("ESP32 reports: camera init failed — check the "
                              "board model / ribbon cable.")
                        buf = b""
            finally:
                ser.close()
        except serial.SerialException as e:
            print(f"Serial error: {e} — retrying in 2s")
            time.sleep(2)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/stream":
            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=frame"
            )
            self.end_headers()
            last = -1
            try:
                while True:
                    with cond:
                        cond.wait_for(lambda: latest["count"] != last, timeout=5)
                        frame, last = latest["frame"], latest["count"]
                    if frame is None:
                        continue
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                    self.wfile.write(frame + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            body = (b"<!doctype html><title>ESP32-CAM</title>"
                    b"<body style='margin:0;background:#111;display:grid;"
                    b"place-items:center;height:100vh'>"
                    b"<img src='/stream' style='max-width:100%'></body>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


if __name__ == "__main__":
    threading.Thread(target=reader, daemon=True).start()
    print(f"Viewer running: open http://localhost:{HTTP_PORT}")
    ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler).serve_forever()
