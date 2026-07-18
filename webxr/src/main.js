import * as THREE from "three";
import { VRButton } from "three/addons/webxr/VRButton.js";

const STORAGE_KEY = "rover_hub";

function resolveHub() {
  const params = new URLSearchParams(location.search);
  const q = params.get("hub");
  if (q) {
    localStorage.setItem(STORAGE_KEY, q);
    return q;
  }
  return localStorage.getItem(STORAGE_KEY) || "";
}

function httpOrigin(hub) {
  if (!hub) return location.origin;
  const u = new URL(hub.replace(/^ws/i, "http"));
  return u.origin;
}

function makeWsUrl(hub) {
  if (!hub) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}/ws`;
  }
  if (hub.includes("/ws")) return hub.replace(/^http/, "ws");
  const u = new URL(hub.replace(/^ws/i, "http"));
  const proto = u.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${u.host}/ws`;
}

const hubStored = resolveHub();
const hubInput = document.getElementById("hub-input");
const enterBtn = document.getElementById("enter");
const statusEl = document.getElementById("status");
const uiEl = document.getElementById("ui");
const overlayHud = document.getElementById("overlay-hud");
const hudMsg = document.getElementById("hud-msg");

hubInput.value = hubStored;

let hubUrl = hubStored;
let ws = null;
let state = null;
let reconnectTimer = null;
let lastHeadSend = 0;
let sessionActive = false;

// --- Three / XR ---
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.xr.enabled = true;
renderer.setClearColor(0x000000, 0); // transparent for passthrough
document.getElementById("app").appendChild(renderer.domElement);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(
  70,
  window.innerWidth / window.innerHeight,
  0.01,
  50
);
camera.position.set(0, 1.5, 0);

const light = new THREE.HemisphereLight(0xffffff, 0x222233, 1.0);
scene.add(light);

// FPV panel (world-locked after spawn)
const videoImg = new Image();
videoImg.crossOrigin = "anonymous";
const videoCanvas = document.createElement("canvas");
videoCanvas.width = 640;
videoCanvas.height = 480;
const vctx = videoCanvas.getContext("2d");
const videoTex = new THREE.CanvasTexture(videoCanvas);
videoTex.colorSpace = THREE.SRGBColorSpace;

const fpvMat = new THREE.MeshBasicMaterial({
  map: videoTex,
  side: THREE.DoubleSide,
  transparent: true,
});
const fpv = new THREE.Mesh(new THREE.PlaneGeometry(1.4, 1.05), fpvMat);
fpv.position.set(0, 1.45, -1.6);
scene.add(fpv);

// Frame around FPV for cast readability
const frame = new THREE.Mesh(
  new THREE.PlaneGeometry(1.48, 1.13),
  new THREE.MeshBasicMaterial({ color: 0x0a120e, side: THREE.DoubleSide })
);
frame.position.copy(fpv.position);
frame.position.z += 0.01;
scene.add(frame);
fpv.position.z -= 0.005;

// Status plane (canvas texture) — center of view for casting
const hudCanvas = document.createElement("canvas");
hudCanvas.width = 512;
hudCanvas.height = 128;
const hctx = hudCanvas.getContext("2d");
const hudTex = new THREE.CanvasTexture(hudCanvas);
const hudPlane = new THREE.Mesh(
  new THREE.PlaneGeometry(1.1, 0.275),
  new THREE.MeshBasicMaterial({
    map: hudTex,
    transparent: true,
    depthTest: false,
  })
);
hudPlane.position.set(0, 2.05, -1.55);
scene.add(hudPlane);

function paintHud() {
  hctx.clearRect(0, 0, 512, 128);
  hctx.fillStyle = "rgba(8,14,12,0.75)";
  hctx.fillRect(0, 0, 512, 128);
  const estop = state?.estop;
  const camOk = state?.links?.camera?.connected;
  const roverOk = state?.links?.rover?.connected;
  const gloveOk = state?.links?.glove?.connected;
  let line = "ROVER VIEW";
  let color = "#5ce08a";
  if (estop) {
    line = "E-STOP ACTIVE";
    color = "#ff4d6d";
  } else if (!camOk) {
    line = "NO VIDEO";
    color = "#ffc857";
  } else if (!roverOk) {
    line = "ROVER LINK DOWN";
    color = "#ffc857";
  }
  hctx.fillStyle = color;
  hctx.font = "bold 42px system-ui,sans-serif";
  hctx.textAlign = "center";
  hctx.fillText(line, 256, 55);
  hctx.fillStyle = "#c8d5cc";
  hctx.font = "22px system-ui,sans-serif";
  const mode = (state?.mode || "teleop").toUpperCase();
  hctx.fillText(
    `${mode}  ·  CAM ${camOk ? "OK" : "--"}  ·  GLOVE ${gloveOk ? "OK" : "--"}  ·  ROVER ${roverOk ? "OK" : "--"}`,
    256,
    95
  );
  hudTex.needsUpdate = true;

  // 2D overlay for flat / cast browser chrome
  if (sessionActive) {
    overlayHud.style.display = "block";
    hudMsg.textContent = line;
    hudMsg.className = "big " + (estop ? "bad" : camOk && roverOk ? "ok" : "warn");
  }
}

function placePanelsInFront() {
  const xrCam = renderer.xr.getCamera();
  const pos = new THREE.Vector3();
  const quat = new THREE.Quaternion();
  xrCam.getWorldPosition(pos);
  xrCam.getWorldQuaternion(quat);
  const forward = new THREE.Vector3(0, 0, -1).applyQuaternion(quat);
  forward.y = 0;
  if (forward.lengthSq() < 1e-4) forward.set(0, 0, -1);
  forward.normalize();
  const target = pos.clone().add(forward.multiplyScalar(1.6));
  target.y = pos.y - 0.1;
  fpv.position.copy(target);
  frame.position.copy(target);
  frame.position.z = target.z; // will lookAt
  hudPlane.position.copy(target).add(new THREE.Vector3(0, 0.55, 0));
  fpv.lookAt(pos);
  frame.lookAt(pos);
  hudPlane.lookAt(pos);
}

// Video pump via img tag (MJPEG)
function startVideo() {
  const origin = httpOrigin(hubUrl);
  const src = `${origin}/camera.mjpg?t=${Date.now()}`;
  videoImg.onload = () => {
    try {
      vctx.drawImage(videoImg, 0, 0, videoCanvas.width, videoCanvas.height);
      videoTex.needsUpdate = true;
    } catch (_) {}
    // keep pumping — for MJPEG, browsers may only fire onload once;
    // use a second Image that reloads periodically as fallback
  };
  videoImg.src = src;
}

// Periodic redraw: some browsers update MJPEG img continuously
setInterval(() => {
  if (videoImg.complete && videoImg.naturalWidth) {
    try {
      vctx.drawImage(videoImg, 0, 0, videoCanvas.width, videoCanvas.height);
      videoTex.needsUpdate = true;
    } catch (_) {}
  }
}, 66);

// MJPEG fallback: reload img every few seconds if stalled
let lastFrameCheck = 0;
setInterval(() => {
  if (!hubUrl && location.protocol === "file:") return;
  if (Date.now() - lastFrameCheck > 4000) {
    lastFrameCheck = Date.now();
    // nudge by resetting src if disconnected in state
    if (state && !state.links?.camera?.connected) startVideo();
  }
}, 2000);

function setStatus(ok, text) {
  statusEl.textContent = text;
  statusEl.classList.toggle("ok", ok);
  statusEl.classList.toggle("bad", !ok);
}

function connectWs() {
  clearTimeout(reconnectTimer);
  const url = makeWsUrl(hubUrl);
  setStatus(false, "CONNECTING…");
  try {
    if (ws) ws.close();
  } catch (_) {}
  ws = new WebSocket(url);
  ws.onopen = () => {
    setStatus(true, "HUB CONNECTED");
    enterBtn.disabled = !navigator.xr;
    if (!navigator.xr) {
      // still allow flat fallback
      enterBtn.disabled = false;
      enterBtn.textContent = "Open Flat FPV";
    } else {
      enterBtn.textContent = "Enter Rover View";
      enterBtn.disabled = false;
    }
  };
  ws.onclose = () => {
    setStatus(false, "RECONNECTING…");
    reconnectTimer = setTimeout(connectWs, 1200);
  };
  ws.onerror = () => {
    try {
      ws.close();
    } catch (_) {}
  };
  ws.onmessage = (ev) => {
    try {
      state = JSON.parse(ev.data);
      paintHud();
    } catch (_) {}
  };
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ v: 1, ...obj }));
  }
}

hubInput.addEventListener("change", () => {
  hubUrl = hubInput.value.trim();
  if (hubUrl) localStorage.setItem(STORAGE_KEY, hubUrl);
  startVideo();
  connectWs();
});

// VR button styling — we use our own Enter button
const vrButton = VRButton.createButton(renderer);
vrButton.style.display = "none";
document.body.appendChild(vrButton);

enterBtn.addEventListener("click", async () => {
  hubUrl = hubInput.value.trim() || hubUrl;
  if (hubUrl) localStorage.setItem(STORAGE_KEY, hubUrl);
  startVideo();
  if (!ws || ws.readyState !== WebSocket.OPEN) connectWs();

  if (navigator.xr) {
    try {
      // Prefer immersive-ar for passthrough on Quest Browser
      const okAr = await navigator.xr.isSessionSupported("immersive-ar");
      const mode = okAr ? "immersive-ar" : "immersive-vr";
      const session = await navigator.xr.requestSession(mode, {
        requiredFeatures: ["local-floor"],
        optionalFeatures: ["hand-tracking", "layers"],
      });
      await renderer.xr.setSession(session);
      sessionActive = true;
      uiEl.style.display = "none";
      overlayHud.style.display = "block";
      // clear e-stop for demo convenience? No — safety: leave as-is; show hint
      setTimeout(placePanelsInFront, 200);
      session.addEventListener("end", () => {
        sessionActive = false;
        uiEl.style.display = "flex";
        overlayHud.style.display = "none";
      });
    } catch (e) {
      console.warn("XR session failed, flat mode", e);
      startFlatMode();
    }
  } else {
    startFlatMode();
  }
});

function startFlatMode() {
  sessionActive = true;
  uiEl.style.display = "none";
  overlayHud.style.display = "block";
  camera.position.set(0, 1.5, 0.5);
  fpv.position.set(0, 1.45, -1.6);
  frame.position.copy(fpv.position);
  hudPlane.position.set(0, 2.05, -1.55);
}

// Controllers
const controllers = [];
for (let i = 0; i < 2; i++) {
  const c = renderer.xr.getController(i);
  c.userData = { selecting: false };
  c.addEventListener("selectstart", () => {
    c.userData.selecting = true;
    send({ type: "mark_victim" });
  });
  c.addEventListener("selectend", () => {
    c.userData.selecting = false;
  });
  // squeeze = e-stop toggle / recenter long press handled in loop
  c.addEventListener("squeezestart", () => {
    c.userData.squeezeAt = performance.now();
  });
  c.addEventListener("squeezeend", () => {
    const held = performance.now() - (c.userData.squeezeAt || 0);
    if (held > 800) placePanelsInFront();
    else send({ type: "estop_toggle" });
  });
  scene.add(c);
  controllers.push(c);
}

const dummy = new THREE.Object3D();

function sampleHead() {
  if (!renderer.xr.isPresenting) return;
  const xrCam = renderer.xr.getCamera();
  xrCam.getWorldQuaternion(dummy.quaternion);
  const e = new THREE.Euler().setFromQuaternion(dummy.quaternion, "YXZ");
  const yaw = THREE.MathUtils.radToDeg(e.y);
  const pitch = THREE.MathUtils.radToDeg(e.x);
  const now = performance.now();
  if (now - lastHeadSend > 33) {
    lastHeadSend = now;
    send({ type: "head", yaw, pitch });
  }
}

// Hand / controller drive: right stick style via thumbstick if available,
// else use controller orientation relative to head for simple drive
const gamepadDrive = { v: 0, omega: 0 };

function pollGamepads() {
  const session = renderer.xr.getSession();
  if (!session) return;
  for (const source of session.inputSources) {
    const gp = source.gamepad;
    if (!gp) continue;
    // Quest: axes[2], axes[3] often thumbstick
    let ax = 0,
      ay = 0;
    if (gp.axes.length >= 4) {
      ax = gp.axes[2];
      ay = gp.axes[3];
    } else if (gp.axes.length >= 2) {
      ax = gp.axes[0];
      ay = gp.axes[1];
    }
    if (Math.abs(ax) > 0.15 || Math.abs(ay) > 0.15) {
      gamepadDrive.v = -ay;
      gamepadDrive.omega = ax;
      send({ type: "drive", v: gamepadDrive.v, omega: gamepadDrive.omega });
      return;
    }
  }
  if (gamepadDrive.v || gamepadDrive.omega) {
    gamepadDrive.v = 0;
    gamepadDrive.omega = 0;
    send({ type: "drive", v: 0, omega: 0 });
  }
}

// Keyboard flat fallback
const keys = {};
window.addEventListener("keydown", (e) => {
  keys[e.key.toLowerCase()] = true;
  if (e.key === " ") send({ type: "estop_toggle" });
  if (e.key.toLowerCase() === "r") placePanelsInFront();
});
window.addEventListener("keyup", (e) => {
  keys[e.key.toLowerCase()] = false;
});

function pollKeys() {
  if (renderer.xr.isPresenting) return;
  let v = 0,
    o = 0;
  if (keys.w) v += 1;
  if (keys.s) v -= 1;
  if (keys.a) o -= 1;
  if (keys.d) o += 1;
  if (v || o) send({ type: "drive", v, omega: o });
}

window.addEventListener("resize", () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

renderer.setAnimationLoop(() => {
  sampleHead();
  pollGamepads();
  pollKeys();
  paintHud();
  renderer.render(scene, camera);
});

// boot
startVideo();
connectWs();

// Prefer AR session hint on VRButton for browsers that use it
if (navigator.xr) {
  navigator.xr.isSessionSupported("immersive-ar").then((ok) => {
    if (ok) console.info("immersive-ar supported (passthrough)");
  });
}
