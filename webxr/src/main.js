import * as THREE from "three";
import { createRescue, intersectGround } from "./rescue.js";

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

/** Normalize hub URL (http/https/ws/wss) to an http(s) URL for parsing. */
function parseHubUrl(hub) {
  const s = String(hub).trim().replace(/^wss:/i, "https:").replace(/^ws:/i, "http:");
  return new URL(s);
}

function httpOrigin(hub) {
  if (!hub) return location.origin;
  return parseHubUrl(hub).origin;
}

function makeWsUrl(hub) {
  if (!hub) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}/ws`;
  }
  const u = parseHubUrl(hub);
  const proto = u.protocol === "https:" ? "wss:" : "ws:";
  if (u.pathname.includes("/ws")) {
    return `${proto}//${u.host}${u.pathname}${u.search}`;
  }
  return `${proto}//${u.host}/ws`;
}

const hubStored = resolveHub();
const hubInput = document.getElementById("hub-input");
const enterBtn = document.getElementById("enter");
const statusEl = document.getElementById("status");
const uiEl = document.getElementById("ui");
const stepConnect = document.getElementById("step-connect");
const stepMode = document.getElementById("step-mode");
const exploreBtn = document.getElementById("mode-explore");
const rescueBtn = document.getElementById("mode-rescue");
const backBtn = document.getElementById("mode-back");
const overlayHud = document.getElementById("overlay-hud");
const hudMsg = document.getElementById("hud-msg");
const hudSub = document.getElementById("hud-sub");
const exitBtn = document.getElementById("exit-btn");

hubInput.value = hubStored;

let hubUrl = hubStored;
let ws = null;
let state = null;
let reconnectTimer = null;
let lastHeadSend = 0;
let sessionActive = false;
let appMode = "explore"; // "explore" | "rescue"
let rescueHint = ""; // transient HUD hint
let rescueHintUntil = 0;
let scanMode = false; // rescue: robot parked, head drives the camera gimbal

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

// FPV panel: world-locked billboard in explore, head-locked corner in rescue
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

// --- Rescue mission -------------------------------------------------------
const rescue = createRescue({
  scene,
  groundY: 0,
  onPhaseChange: (phase) => {
    if (phase === "too_short") {
      rescueHint = "PATH TOO SHORT — DRAW AGAIN";
      rescueHintUntil = performance.now() + 2200;
    } else if (phase === "found") {
      rescueHint = "";
    }
    if (phase === "draw") scanMode = false;
  },
});

// Faint grid for flat-mode rescue drawing
const flatGrid = new THREE.GridHelper(4, 40, 0x2f8f5b, 0x14311f);
flatGrid.position.y = 0.0005;
flatGrid.material.transparent = true;
flatGrid.material.opacity = 0.5;
flatGrid.visible = false;
scene.add(flatGrid);

/** Set the largest font size (<= max) that fits the text into maxWidth px. */
function fitText(ctx, text, maxPx, maxWidth, weight) {
  let px = maxPx;
  do {
    ctx.font = `${weight}${px}px system-ui,sans-serif`;
    if (ctx.measureText(text).width <= maxWidth) return;
    px -= 2;
  } while (px > 14);
}

function rescueStatusLine() {
  if (rescueHint && performance.now() < rescueHintUntil) {
    return { line: rescueHint, color: "#ffc857" };
  }
  if (scanMode) return { line: "SCANNING — LOOK AROUND", color: "#c8f542" };
  switch (rescue.phase) {
    case "draw":
      return { line: "DRAW THE ROUTE", color: "#c8f542" };
    case "built":
      return { line: "DRIVE TO THE VICTIM", color: "#5ce08a" };
    case "found":
      return { line: "VICTIM LOCATED", color: "#5ce08a" };
    default:
      return { line: "RESCUE MISSION", color: "#5ce08a" };
  }
}

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
  let sub = "";
  if (appMode === "rescue") {
    // Rescue instructions own the main line; link problems go to the sub line
    const rs = rescueStatusLine();
    line = rs.line;
    color = rs.color;
    if (estop) {
      line = "E-STOP ACTIVE";
      color = "#ff4d6d";
    } else if (rescue.phase === "draw") {
      sub = "hold trigger / pinch and drag on the floor";
    } else if (scanMode) {
      sub = "robot parked · head moves camera · trigger: drive";
    } else if (rescue.phase === "built") {
      sub = "trigger: park + scan · hold trigger: redraw";
    }
  } else if (estop) {
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
  hctx.textAlign = "center";
  fitText(hctx, line, 42, 490, "bold ");
  hctx.fillText(line, 256, 55);
  hctx.fillStyle = "#c8d5cc";
  const mode = appMode === "rescue" ? "RESCUE" : (state?.mode || "teleop").toUpperCase();
  const subLine =
    sub ||
    `${mode}  ·  CAM ${camOk ? "OK" : "--"}  ·  GLOVE ${gloveOk ? "OK" : "--"}  ·  ROVER ${roverOk ? "OK" : "--"}`;
  fitText(hctx, subLine, 22, 490, "");
  hctx.fillText(subLine, 256, 95);
  hudTex.needsUpdate = true;

  // 2D overlay for flat / cast browser chrome
  if (sessionActive) {
    overlayHud.style.display = "block";
    hudMsg.textContent = line;
    hudMsg.className = "big " + (estop ? "bad" : camOk && roverOk ? "ok" : "warn");
    hudSub.textContent = sub;
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

// Head-locked corner panels for rescue mode (smoothed follow)
const FPV_SCALE_RESCUE = 0.24;
const cornerTmp = {
  pos: new THREE.Vector3(),
  quat: new THREE.Quaternion(),
  target: new THREE.Vector3(),
  offset: new THREE.Vector3(),
};

function followCorner(cam, obj, offsetX, offsetY, offsetZ, lerp) {
  cam.getWorldPosition(cornerTmp.pos);
  cam.getWorldQuaternion(cornerTmp.quat);
  cornerTmp.offset.set(offsetX, offsetY, offsetZ).applyQuaternion(cornerTmp.quat);
  cornerTmp.target.copy(cornerTmp.pos).add(cornerTmp.offset);
  obj.position.lerp(cornerTmp.target, lerp);
  obj.quaternion.slerp(cornerTmp.quat, lerp);
}

function updateRescuePanels() {
  const cam = renderer.xr.isPresenting ? renderer.xr.getCamera() : camera;
  followCorner(cam, fpv, -0.3, 0.22, -1.0, 0.3);
  followCorner(cam, frame, -0.3, 0.22, -1.004, 0.3);
  followCorner(cam, hudPlane, -0.3, 0.05, -1.0, 0.3);
}

function applyModeLayout() {
  if (appMode === "rescue") {
    fpv.scale.setScalar(FPV_SCALE_RESCUE);
    frame.scale.setScalar(FPV_SCALE_RESCUE);
    hudPlane.scale.setScalar(FPV_SCALE_RESCUE * 1.05);
  } else {
    fpv.scale.setScalar(1);
    frame.scale.setScalar(1);
    hudPlane.scale.setScalar(1);
  }
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
    enterBtn.disabled = false;
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

// --- Menu flow: connect step -> mode step -> session -----------------------
enterBtn.addEventListener("click", () => {
  hubUrl = hubInput.value.trim() || hubUrl;
  if (hubUrl) localStorage.setItem(STORAGE_KEY, hubUrl);
  startVideo();
  if (!ws || ws.readyState !== WebSocket.OPEN) connectWs();
  stepConnect.style.display = "none";
  stepMode.style.display = "flex";
});

backBtn.addEventListener("click", () => {
  stepMode.style.display = "none";
  stepConnect.style.display = "flex";
});

exploreBtn.addEventListener("click", () => startExperience("explore"));
rescueBtn.addEventListener("click", () => startExperience("rescue"));

async function startExperience(mode) {
  appMode = mode;
  applyModeLayout();
  flatGrid.visible = false;
  if (mode === "rescue") rescue.enter();
  else rescue.exit();

  if (navigator.xr) {
    try {
      // Prefer immersive-ar for passthrough on Quest Browser
      const okAr = await navigator.xr.isSessionSupported("immersive-ar");
      const xrMode = okAr ? "immersive-ar" : "immersive-vr";
      const session = await navigator.xr.requestSession(xrMode, {
        requiredFeatures: ["local-floor"],
        optionalFeatures: ["hand-tracking", "layers"],
      });
      await renderer.xr.setSession(session);
      sessionActive = true;
      uiEl.style.display = "none";
      overlayHud.style.display = "block";
      if (appMode === "explore") setTimeout(placePanelsInFront, 200);
      if (appMode === "rescue") send({ type: "head", yaw: 0, pitch: 0 });
      session.addEventListener("end", returnToMenu);
    } catch (e) {
      console.warn("XR session failed, flat mode", e);
      startFlatMode();
    }
  } else {
    startFlatMode();
  }
}

function returnToMenu() {
  sessionActive = false;
  scanMode = false;
  send({ type: "drive", v: 0, omega: 0 });
  rescue.exit();
  flatGrid.visible = false;
  uiEl.style.display = "flex";
  stepConnect.style.display = "none";
  stepMode.style.display = "flex";
  overlayHud.style.display = "none";
}

// Back to menu from anywhere: ends the XR session or leaves flat mode
exitBtn.addEventListener("click", () => {
  const session = renderer.xr.getSession();
  if (session) session.end();
  else returnToMenu();
});

function startFlatMode() {
  sessionActive = true;
  uiEl.style.display = "none";
  overlayHud.style.display = "block";
  if (appMode === "rescue") {
    camera.position.set(0, 1.7, 1.4);
    camera.lookAt(0, 0, -0.4);
    flatGrid.visible = true;
    // corner panels are placed each frame by updateRescuePanels()
  } else {
    camera.position.set(0, 1.5, 0.5);
    camera.lookAt(0, 1.45, -1.6);
    fpv.position.set(0, 1.45, -1.6);
    frame.position.copy(fpv.position);
    frame.position.z += 0.01;
    hudPlane.position.set(0, 2.05, -1.55);
    fpv.rotation.set(0, 0, 0);
    frame.rotation.set(0, 0, 0);
    hudPlane.rotation.set(0, 0, 0);
  }
}

// --- Controllers (also fired by hand pinch on Quest) ------------------------
const rayOrigin = new THREE.Vector3();
const rayDir = new THREE.Vector3();
const rayQuat = new THREE.Quaternion();

function controllerRay(c) {
  c.getWorldPosition(rayOrigin);
  c.getWorldQuaternion(rayQuat);
  rayDir.set(0, 0, -1).applyQuaternion(rayQuat);
  return { origin: rayOrigin, dir: rayDir };
}

let drawController = null;

const controllers = [];
for (let i = 0; i < 2; i++) {
  const c = renderer.xr.getController(i);
  c.userData = { selecting: false, selectAt: 0 };
  c.addEventListener("selectstart", () => {
    c.userData.selecting = true;
    c.userData.selectAt = performance.now();
    if (appMode === "rescue") {
      if (rescue.phase === "draw") {
        const { origin, dir } = controllerRay(c);
        const hit = rescue.pointerMove(origin, dir);
        if (rescue.beginStroke(hit)) drawController = c;
      }
      return;
    }
    send({ type: "mark_victim" });
  });
  c.addEventListener("selectend", () => {
    c.userData.selecting = false;
    if (appMode !== "rescue") return;
    const held = performance.now() - (c.userData.selectAt || 0);
    if (rescue.isDrawing && drawController === c) {
      drawController = null;
      rescue.endStroke();
    } else if (rescue.phase === "built" || rescue.phase === "found") {
      // Short press = park + scan toggle; long hold (hands-friendly) = redraw
      if (held >= 1200) {
        rescue.clear();
      } else {
        scanMode = !scanMode;
        send({ type: "drive", v: 0, omega: 0 });
        if (!scanMode) send({ type: "head", yaw: 0, pitch: 0 });
      }
    }
  });
  // squeeze: short = e-stop, long = recenter (explore) / redraw (rescue)
  c.addEventListener("squeezestart", () => {
    c.userData.squeezeAt = performance.now();
  });
  c.addEventListener("squeezeend", () => {
    const held = performance.now() - (c.userData.squeezeAt || 0);
    if (held > 800) {
      if (appMode === "rescue") rescue.clear();
      else placePanelsInFront();
    } else {
      send({ type: "estop_toggle" });
    }
  });
  scene.add(c);
  controllers.push(c);
}

function updateRescuePointer() {
  if (!renderer.xr.isPresenting) return;
  if (rescue.phase !== "draw" && !rescue.isDrawing) return;
  const source = drawController || controllers.find((c) => {
    const { origin, dir } = controllerRay(c);
    return !!intersectGround(origin, dir, 0);
  }) || controllers[0];
  if (!source) return;
  const { origin, dir } = controllerRay(source);
  rescue.pointerMove(origin, dir);
}

const dummy = new THREE.Object3D();

function sampleHead() {
  if (!renderer.xr.isPresenting) return;
  const now = performance.now();
  if (appMode === "rescue" && !scanMode) {
    // Driving: gimbal locked level for a stable FPV reference
    if (now - lastHeadSend > 500) {
      lastHeadSend = now;
      send({ type: "head", yaw: 0, pitch: 0 });
    }
    return;
  }
  // Exploration, or rescue scan mode: head drives the camera gimbal
  const xrCam = renderer.xr.getCamera();
  xrCam.getWorldQuaternion(dummy.quaternion);
  const e = new THREE.Euler().setFromQuaternion(dummy.quaternion, "YXZ");
  const yaw = THREE.MathUtils.radToDeg(e.y);
  const pitch = THREE.MathUtils.radToDeg(e.x);
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
  if (appMode === "rescue" && scanMode) return; // parked: no drive input
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

// A/X button marks the victim as located (rescue only)
let markBtnLatch = false;
function pollMarkButton() {
  const session = renderer.xr.getSession();
  if (!session) return;
  let any = false;
  for (const source of session.inputSources) {
    const b = source.gamepad?.buttons;
    if (b && (b[4]?.pressed || b[5]?.pressed)) any = true;
  }
  if (any && !markBtnLatch && rescue.markFound()) send({ type: "mark_victim" });
  markBtnLatch = any;
}

// --- Flat-mode mouse drawing (rescue testing without a headset) -------------
const mouseRaycaster = new THREE.Raycaster();
const mouseNdc = new THREE.Vector2();
let mouseDrawing = false;

function mouseGroundRay(ev) {
  mouseNdc.set(
    (ev.clientX / window.innerWidth) * 2 - 1,
    -(ev.clientY / window.innerHeight) * 2 + 1
  );
  mouseRaycaster.setFromCamera(mouseNdc, camera);
  return { origin: mouseRaycaster.ray.origin, dir: mouseRaycaster.ray.direction };
}

renderer.domElement.addEventListener("pointerdown", (ev) => {
  if (renderer.xr.isPresenting || !sessionActive || appMode !== "rescue") return;
  const { origin, dir } = mouseGroundRay(ev);
  const hit = rescue.pointerMove(origin, dir);
  if (rescue.phase === "draw") {
    if (rescue.beginStroke(hit)) mouseDrawing = true;
  } else if (rescue.phase === "built" && rescue.markFound()) {
    send({ type: "mark_victim" });
  }
});
renderer.domElement.addEventListener("pointermove", (ev) => {
  if (renderer.xr.isPresenting || !sessionActive || appMode !== "rescue") return;
  const { origin, dir } = mouseGroundRay(ev);
  rescue.pointerMove(origin, dir);
});
window.addEventListener("pointerup", () => {
  if (mouseDrawing) {
    mouseDrawing = false;
    rescue.endStroke();
  }
});

// Keyboard flat fallback
const keys = {};
window.addEventListener("keydown", (e) => {
  keys[e.key.toLowerCase()] = true;
  if (e.key === " ") send({ type: "estop_toggle" });
  if (e.key.toLowerCase() === "r") placePanelsInFront();
  if (e.key.toLowerCase() === "c" && appMode === "rescue") rescue.clear();
  if (e.key.toLowerCase() === "v" && appMode === "rescue" && rescue.markFound())
    send({ type: "mark_victim" });
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

renderer.setAnimationLoop((timeMs) => {
  sampleHead();
  pollGamepads();
  pollKeys();
  if (appMode === "rescue") {
    updateRescuePointer();
    updateRescuePanels();
    pollMarkButton();
    rescue.tick(timeMs);
  }
  paintHud();
  renderer.render(scene, camera);
});

// boot
startVideo();
connectWs();
// Menu is always reachable; hub state is communicated by the status pill.
enterBtn.disabled = false;

// Prefer AR session hint on VRButton for browsers that use it
if (navigator.xr) {
  navigator.xr.isSessionSupported("immersive-ar").then((ok) => {
    if (ok) console.info("immersive-ar supported (passthrough)");
  });
}
