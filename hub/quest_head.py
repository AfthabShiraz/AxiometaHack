#!/usr/bin/env python3
"""Quest head tracking -> pan/tilt servos (plan.md FR-7, Phase 5 soft-launch).

Serves a WebXR page over HTTPS; the Quest browser opens it, enters VR,
and streams head yaw/pitch over a WebSocket back to this process, which
maps them to smoothed P,<pan>,<tilt> commands for the rover.

Self-contained on purpose: no imports from the hub package (which is
being refactored in parallel) — rover UDP transport is inlined.

Run:  python3 hub/quest_head.py
Quest (same WiFi/hotspot): open  https://10.61.237.1:8443
  -> accept the self-signed-certificate warning (Advanced -> proceed)
  -> ENTER VR. Controller trigger (or pinch) re-centers "forward".

Servo behavior: first head sample defines center; angles are scaled,
clamped, and slew-rate limited (FR-7). If the Quest stops sending for
0.5 s the servos hold their last position (motors are untouched — the
rover watchdog is fed only by M/S commands, never by P).
"""

import asyncio
import json
import math
import pathlib
import socket
import ssl
import subprocess
import time

import aiohttp
from aiohttp import web, WSMsgType

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
QCFG = CONFIG.get("quest", {})

HTTPS_PORT = QCFG.get("port", 8443)
HELLO_PORT = 5011
SEND_HZ = 20
PAN_SIGN = QCFG.get("pan_sign", 1)
TILT_SIGN = QCFG.get("tilt_sign", 1)
PAN_SCALE = QCFG.get("pan_scale", 1.0)
TILT_SCALE = QCFG.get("tilt_scale", 1.0)
PAN_RANGE = QCFG.get("pan_range", 70)    # deg each side of center
TILT_RANGE = QCFG.get("tilt_range", 45)
PAN_CENTER = QCFG.get("pan_center", 90)   # servo angle = straight ahead
TILT_CENTER = QCFG.get("tilt_center", 90)
SLEW_DPS = QCFG.get("slew_deg_per_s", 180)  # servo speed limit (FR-7)
CAM_URL = CONFIG.get("camera", {}).get("stream_url",
                                       "http://esp32cam.local/stream")
RCFG = CONFIG.get("rover", {})
ROVER_MDNS = RCFG.get("bridge_host", RCFG.get("host", "rover.local"))
ROVER_PORT = RCFG.get("bridge_port", RCFG.get("udp_port", 5006))

CERT_DIR = ROOT / "hub" / "certs"


def ensure_cert():
    CERT_DIR.mkdir(exist_ok=True)
    crt, key = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"
    if not (crt.exists() and key.exists()):
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(crt), "-days", "365",
             "-subj", "/CN=rescue-hub"], check=True, capture_output=True)
        print("generated self-signed cert in hub/certs/")
    return crt, key


class RoverUdp:
    """Minimal rover link: learn address from HELLO, send P commands."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):  # coexist with drive console
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self.sock.bind(("", HELLO_PORT))
        self.sock.setblocking(False)
        self.addr = None

    def poll(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(256)
            except (BlockingIOError, OSError):
                return
            if data.startswith(b"HELLO"):
                parts = data.split()
                try:
                    port = int(parts[2])
                except (IndexError, ValueError):
                    port = 5006
                if self.addr != (addr[0], port):
                    print(f"rover at {addr[0]}:{port}")
                self.addr = (addr[0], port)

    def send_look(self, pan, tilt):
        if self.addr:
            try:
                self.sock.sendto(f"P,{pan:.0f},{tilt:.0f}\n".encode(),
                                 self.addr)
            except OSError:
                pass


class HeadState:
    def __init__(self):
        self.yaw = 0.0        # latest raw, degrees
        self.pitch = 0.0
        self.center_yaw = None
        self.center_pitch = None
        self.last_msg = 0.0
        self.pan_out = float(PAN_CENTER)   # slew-limited servo state
        self.tilt_out = float(TILT_CENTER)

    def update(self, yaw, pitch):
        self.yaw, self.pitch = yaw, pitch
        self.last_msg = time.monotonic()
        if self.center_yaw is None:
            self.recenter()

    def recenter(self):
        self.center_yaw, self.center_pitch = self.yaw, self.pitch
        print(f"\nre-centered at yaw={self.yaw:.0f} pitch={self.pitch:.0f}")

    def targets(self):
        if self.center_yaw is None:
            return float(PAN_CENTER), float(TILT_CENTER)
        rel_yaw = ((self.yaw - self.center_yaw + 540) % 360) - 180
        rel_pitch = self.pitch - self.center_pitch
        pan = PAN_CENTER + PAN_SIGN * max(
            -PAN_RANGE, min(PAN_RANGE, rel_yaw * PAN_SCALE))
        tilt = TILT_CENTER + TILT_SIGN * max(
            -TILT_RANGE, min(TILT_RANGE, rel_pitch * TILT_SCALE))
        return pan, tilt


HEAD = HeadState()
ROVER = RoverUdp()

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Rescue Head Tracking</title>
<style>
 body{margin:0;background:#111;color:#eee;font-family:system-ui;
      display:grid;place-items:center;height:100vh;text-align:center}
 button{font-size:2em;padding:.6em 1.2em;border-radius:.4em;border:0;
        background:#2a7;color:#fff}
 #s{margin-top:1em;font-size:1.2em;color:#8cf}
</style>
<div>
  <h1>Rescue Robot — Head Tracking</h1>
  <button id="go">ENTER VR</button>
  <div id="s">not connected</div>
</div>
<script>
const S = document.getElementById('s');
let ws, xrSession, refSpace, lastSend = 0;
let gl, prog, uMVP, uHasTex, tex, texReady = false;
const TV_DIST = 2.0;                // metres in front of the eyes

// MJPEG camera feed, proxied same-origin by the hub (/cam).
// Retry with a cache-buster whenever the stream drops or 502s.
const cam = new Image();
cam.onerror = () => setTimeout(
    () => { texReady = false; cam.src = '/cam?' + Date.now(); }, 2000);
cam.src = '/cam';

function connectWS() {
  ws = new WebSocket('wss://' + location.host + '/ws');
  ws.onopen  = () => S.textContent = 'hub connected';
  ws.onclose = () => { S.textContent = 'hub lost, retrying...';
                       setTimeout(connectWS, 1000); };
}
connectWS();

// Diagnostics: show whether this device/browser can do WebXR at all.
const diag = document.createElement('div');
diag.style.cssText = 'margin-top:1em;font-size:.9em;color:#aaa';
diag.textContent =
  'secure:' + window.isSecureContext +
  '  xr:' + (navigator.xr ? 'yes' : 'MISSING') +
  '  ua:' + navigator.userAgent.slice(0, 60);
document.querySelector('div').appendChild(diag);
if (navigator.xr) {
  navigator.xr.isSessionSupported('immersive-ar').then(ok => {
    diag.textContent += '  passthrough:' + (ok ? 'supported' : 'NO');
  });
}

function yawPitchFromQuat(q) {
  const {x, y, z, w} = q;
  const fx = -(2*(x*z + w*y));
  const fy = -(2*(y*z - w*x));
  const fz = -(1 - 2*(x*x + y*y));
  const yaw = Math.atan2(-fx, -fz) * 180/Math.PI;   // + = looking left
  const pitch = Math.asin(Math.max(-1, Math.min(1, fy))) * 180/Math.PI;
  return [yaw, pitch];
}

// --- minimal WebGL: one textured quad ---------------------------------
function mul(a, b) {               // column-major 4x4: out = a * b
  const o = new Float32Array(16);
  for (let c = 0; c < 4; c++)
    for (let r = 0; r < 4; r++)
      o[c*4+r] = a[r]*b[c*4] + a[4+r]*b[c*4+1] +
                 a[8+r]*b[c*4+2] + a[12+r]*b[c*4+3];
  return o;
}

function makeModel(px, py, pz, yaw, hw, hh) {  // unit quad, half-extents
  const c = Math.cos(yaw), s = Math.sin(yaw);
  return new Float32Array([c*hw,0,-s*hw,0, 0,hh,0,0, s,0,c,0,
                           px,py,pz,1]);
}

function initGL() {
  const canvas = document.createElement('canvas');
  gl = canvas.getContext('webgl', {xrCompatible: true, alpha: true});
  const vs = gl.createShader(gl.VERTEX_SHADER);
  gl.shaderSource(vs,
    'attribute vec4 aPos; attribute vec2 aUV; uniform mat4 uMVP;' +
    'varying vec2 vUV;' +
    'void main(){ gl_Position = uMVP * aPos; vUV = aUV; }');
  gl.compileShader(vs);
  const fs = gl.createShader(gl.FRAGMENT_SHADER);
  gl.shaderSource(fs,
    'precision mediump float; varying vec2 vUV;' +
    'uniform sampler2D uTex; uniform int uHasTex;' +
    'void main(){ gl_FragColor = uHasTex == 1 ?' +
    '  texture2D(uTex, vUV) : vec4(0.08, 0.08, 0.1, 1.0); }');
  gl.compileShader(fs);
  prog = gl.createProgram();
  gl.attachShader(prog, vs); gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  uMVP = gl.getUniformLocation(prog, 'uMVP');
  uHasTex = gl.getUniformLocation(prog, 'uHasTex');

  const buf = gl.createBuffer();          // x,y,u,v  (triangle strip)
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  // UVs rotate the feed 90 deg clockwise: the ESP32-CAM is mounted
  // sideways. Straight-mount UVs would be 0,0 / 0,1 / 1,0 / 1,1.
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
    -1,  1, 0, 1,   -1, -1, 1, 1,    1,  1, 0, 0,    1, -1, 1, 0,
  ]), gl.STATIC_DRAW);
  const aPos = gl.getAttribLocation(prog, 'aPos');
  const aUV = gl.getAttribLocation(prog, 'aUV');
  gl.enableVertexAttribArray(aPos);
  gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 16, 0);
  gl.enableVertexAttribArray(aUV);
  gl.vertexAttribPointer(aUV, 2, gl.FLOAT, false, 16, 8);

  tex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
}

function onFrame(t, frame) {
  xrSession.requestAnimationFrame(onFrame);
  const pose = frame.getViewerPose(refSpace);
  if (!pose) return;

  const q = pose.transform.orientation;
  const now = performance.now();
  if (now - lastSend >= 33) {           // ~30 Hz to servos
    lastSend = now;
    const [yaw, pitch] = yawPitchFromQuat(q);
    if (ws && ws.readyState === 1)
      ws.send(JSON.stringify({v:1, yaw:yaw, pitch:pitch}));
  }

  if (cam.naturalWidth > 0) {           // latest MJPEG frame -> texture
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, cam);
    texReady = true;
  }

  const layer = xrSession.renderState.baseLayer;
  gl.bindFramebuffer(gl.FRAMEBUFFER, layer.framebuffer);
  gl.clearColor(0, 0, 0, 0);            // transparent -> passthrough
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  gl.useProgram(prog);
  gl.uniform1i(uHasTex, texReady ? 1 : 0);
  // Head-locked screen: the quad rides on the head pose, so it stays
  // dead-center in view while the same head motion steers the servos.
  // Rotated feed -> portrait TV: width follows the sensor's height.
  const hw = texReady ? cam.naturalHeight / cam.naturalWidth : 0.75;
  const model = mul(pose.transform.matrix,
                    makeModel(0, 0, -TV_DIST, 0, hw, 1.0));
  for (const view of pose.views) {
    const vp = layer.getViewport(view);
    gl.viewport(vp.x, vp.y, vp.width, vp.height);
    gl.uniformMatrix4fv(uMVP, false,
        mul(view.projectionMatrix, mul(view.transform.inverse.matrix,
                                       model)));
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }
}

document.getElementById('go').onclick = async () => {
  try {
    let mode = 'immersive-ar';          // passthrough with floating TV
    try {
      xrSession = await navigator.xr.requestSession(mode);
    } catch (e) {
      mode = 'immersive-vr';            // fallback: black void + TV
      xrSession = await navigator.xr.requestSession(mode);
    }
    refSpace = await xrSession.requestReferenceSpace('local');
    initGL();
    await xrSession.updateRenderState(
        {baseLayer: new XRWebGLLayer(xrSession, gl)});
    xrSession.addEventListener('select', () => {
      if (ws && ws.readyState === 1)
        ws.send(JSON.stringify({v:1, recenter:true}));
    });
    xrSession.requestAnimationFrame(onFrame);
    S.textContent = 'in ' + mode + ' — trigger/pinch = re-center';
  } catch (e) {
    S.textContent = 'XR failed: ' + e;
  }
};
</script>
"""


async def page(request):
    return web.Response(text=PAGE, content_type="text/html")


async def cam_proxy(request):
    """Re-serve the ESP32-CAM MJPEG stream same-origin over HTTPS.

    The Quest page can't load the camera's plain-HTTP stream directly
    (mixed content + a cross-origin image would taint the WebGL canvas).
    """
    try:
        timeout = aiohttp.ClientTimeout(sock_connect=3, sock_read=15)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(CAM_URL) as upstream:
                resp = web.StreamResponse(status=upstream.status)
                resp.headers["Content-Type"] = upstream.headers.get(
                    "Content-Type", "multipart/x-mixed-replace")
                await resp.prepare(request)
                print(f"\ncamera relay -> {request.remote}")
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
                return resp
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        print(f"\ncamera relay ended: {e}")
        return web.Response(status=502, text=f"camera unreachable: {e}")


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=5)
    await ws.prepare(request)
    print(f"\nQuest connected from {request.remote}")
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            continue
        if data.get("recenter"):
            HEAD.recenter()
        elif "yaw" in data:
            HEAD.update(float(data["yaw"]), float(data["pitch"]))
    print("\nQuest disconnected")
    return ws


async def resolve_rover_fallback():
    """mDNS fallback: when drive_glove.py shares port 5011, the rover's
    unicast HELLOs may all land on its socket, so resolve rover.local
    ourselves until a HELLO (which always wins) reaches us."""
    loop = asyncio.get_running_loop()
    while True:
        if ROVER.addr is None:
            try:
                infos = await loop.getaddrinfo(ROVER_MDNS, None,
                                               family=socket.AF_INET)
                if ROVER.addr is None:
                    ROVER.addr = (infos[0][4][0], ROVER_PORT)
                    print(f"\nrover via mDNS fallback: "
                          f"{ROVER.addr[0]}:{ROVER_PORT}")
            except OSError:
                pass
        await asyncio.sleep(5)


async def servo_loop():
    interval = 1.0 / SEND_HZ
    max_step = SLEW_DPS * interval
    while True:
        ROVER.poll()
        pan_t, tilt_t = HEAD.targets()
        HEAD.pan_out += max(-max_step, min(max_step, pan_t - HEAD.pan_out))
        HEAD.tilt_out += max(-max_step, min(max_step, tilt_t - HEAD.tilt_out))
        fresh = (time.monotonic() - HEAD.last_msg) < 0.5
        if HEAD.last_msg and fresh:
            ROVER.send_look(HEAD.pan_out, HEAD.tilt_out)
        age = time.monotonic() - HEAD.last_msg if HEAD.last_msg else -1
        print(f"\ryaw {HEAD.yaw:+6.1f} pitch {HEAD.pitch:+6.1f} -> "
              f"P,{HEAD.pan_out:5.1f},{HEAD.tilt_out:5.1f} "
              f"{'LIVE ' if fresh else 'STALE'} age {age:4.1f}s  ",
              end="", flush=True)
        await asyncio.sleep(interval)


async def main():
    crt, key = ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/cam", cam_proxy)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTPS_PORT, ssl_context=ctx)
    await site.start()

    ip = socket.gethostbyname(socket.gethostname())
    print(f"Quest head-tracking hub up.")
    print(f"On the Quest browser open:  https://10.61.237.1:{HTTPS_PORT}")
    print("(accept the certificate warning, then ENTER VR)")
    asyncio.create_task(resolve_rover_fallback())
    await servo_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
