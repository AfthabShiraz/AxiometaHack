import * as THREE from "three";

// ---------------------------------------------------------------------------
// Rescue Mission: draw a route on the floor, holographic low-poly terrain
// rises around it, drive the (real) robot down the road to the victim marker.
// Everything lives in a root group added to the provided scene.
// ---------------------------------------------------------------------------

// Palette (Axiometa greens)
const LIME = 0xc8f542;
const GREEN = 0x5ce08a;
const MOUNTAIN_LOW = 0x0d2e1c;
const MOUNTAIN_HIGH = 0x86f5a8;
const TREE_GREEN = 0x3fd47f;
const ALERT_RED = 0xff4d6d;

// Course dimensions, sized for the ~18 cm 4WD rover (10-12 cm tall)
const ROAD_HALF_W = 0.19; // 38 cm road, clear of all obstacles
const SAMPLE_SPACING = 0.025; // resampled centerline step (m)
const MIN_PATH_LEN = 0.45; // reject strokes shorter than this (m)
const RIDGE_BASE = 0.16; // min peak height (m)
const RIDGE_VAR = 0.22; // extra noise-driven height (m)
const RISE_DURATION = 0.9; // build animation (s)
const MARKER_BASE_H = 0.14; // "!" hover height, clears the ~12 cm robot

// Mountain cross-section: offsets from centerline and height factors per row.
// Row 0 starts a small shoulder past the road edge (keeps the drive line
// clear), row 2 is the ridge line, row 4 is the outer skirt at ground level.
const ROW_OFFSETS = [ROAD_HALF_W + 0.03, ROAD_HALF_W + 0.12, ROAD_HALF_W + 0.24, ROAD_HALF_W + 0.37, ROAD_HALF_W + 0.48];
const ROW_HEIGHT_FACTORS = [0, 0.45, 1, 0.35, 0];

/** Intersect a ray with the horizontal plane y = groundY. Returns Vector3 or null. */
export function intersectGround(origin, dir, groundY = 0) {
  if (dir.y > -0.02) return null; // must point downward
  const t = (groundY - origin.y) / dir.y;
  if (t < 0.05 || t > 8) return null;
  return new THREE.Vector3(origin.x + dir.x * t, groundY, origin.z + dir.z * t);
}

/** Cheap 1D value noise with smooth interpolation. */
function makeNoise1D(step) {
  const vals = new Array(64).fill(0).map(() => Math.random());
  return (s) => {
    const x = s / step;
    const i0 = Math.floor(x);
    const f = x - i0;
    const a = vals[((i0 % 64) + 64) % 64];
    const b = vals[(((i0 + 1) % 64) + 64) % 64];
    const u = f * f * (3 - 2 * f);
    return a + (b - a) * u;
  };
}

const clamp01 = (v) => Math.max(0, Math.min(1, v));
const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);

/** Flat ribbon following a polyline of ground points (y ignored, set to `y`). */
function ribbonGeometry(pts, halfW, y) {
  const n = pts.length;
  const pos = new Float32Array(n * 2 * 3);
  const idx = [];
  const d = new THREE.Vector3();
  for (let i = 0; i < n; i++) {
    const p0 = pts[Math.max(0, i - 1)];
    const p1 = pts[Math.min(n - 1, i + 1)];
    d.set(p1.x - p0.x, 0, p1.z - p0.z).normalize();
    const lx = d.z * halfW;
    const lz = -d.x * halfW;
    const p = pts[i];
    pos[i * 6 + 0] = p.x + lx;
    pos[i * 6 + 1] = y;
    pos[i * 6 + 2] = p.z + lz;
    pos[i * 6 + 3] = p.x - lx;
    pos[i * 6 + 4] = y;
    pos[i * 6 + 5] = p.z - lz;
    if (i < n - 1) {
      const a = i * 2;
      idx.push(a, a + 1, a + 2, a + 1, a + 3, a + 2);
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  g.setIndex(idx);
  return g;
}

/** Push a flat quad (two triangles) into position/index arrays. */
function pushQuad(posArr, idxArr, a, b, c, d) {
  const base = posArr.length / 3;
  posArr.push(a.x, a.y, a.z, b.x, b.y, b.z, c.x, c.y, c.z, d.x, d.y, d.z);
  idxArr.push(base, base + 1, base + 2, base + 1, base + 3, base + 2);
}

function geometryFromArrays(posArr, idxArr) {
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(posArr), 3));
  g.setIndex(idxArr);
  return g;
}

function disposeGroup(group) {
  group.traverse((o) => {
    if (o.geometry) o.geometry.dispose();
    if (o.material) {
      if (o.material.map) o.material.map.dispose();
      o.material.dispose();
    }
  });
}

function makeGlowSprite(colorCss, size) {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const ctx = c.getContext("2d");
  const grad = ctx.createRadialGradient(64, 64, 4, 64, 64, 62);
  grad.addColorStop(0, colorCss);
  grad.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, 128, 128);
  const tex = new THREE.CanvasTexture(c);
  const mat = new THREE.SpriteMaterial({
    map: tex,
    transparent: true,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.setScalar(size);
  return sprite;
}

export function createRescue({ scene, groundY = 0, onPhaseChange = () => {} }) {
  const root = new THREE.Group();
  root.visible = false;
  scene.add(root);

  let phase = "idle"; // idle | draw | built | found
  let course = null; // group holding the generated environment
  let riseStart = 0;
  let foundAt = 0;

  const anim = {
    victim: null,
    check: null,
    pulseRing: null,
    pulseMat: null,
    burstRing: null,
    burstMat: null,
  };

  // --- Reticle (draw-phase pointer) -------------------------------------
  const reticle = new THREE.Group();
  {
    const ringMat = new THREE.MeshBasicMaterial({
      color: LIME,
      transparent: true,
      opacity: 0.9,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      depthTest: false,
      side: THREE.DoubleSide,
    });
    const ring = new THREE.Mesh(new THREE.RingGeometry(0.022, 0.028, 28), ringMat);
    ring.rotation.x = -Math.PI / 2;
    const dot = new THREE.Mesh(new THREE.CircleGeometry(0.005, 12), ringMat.clone());
    dot.rotation.x = -Math.PI / 2;
    dot.position.y = 0.001;
    reticle.add(ring, dot);
    reticle.renderOrder = 10;
    reticle.visible = false;
    root.add(reticle);
  }

  // --- Live stroke line ---------------------------------------------------
  const strokeMat = new THREE.MeshBasicMaterial({
    color: LIME,
    transparent: true,
    opacity: 0.9,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    side: THREE.DoubleSide,
  });
  let strokePts = [];
  let strokeMesh = null;
  let drawing = false;

  function refreshStrokeMesh() {
    if (strokeMesh) {
      root.remove(strokeMesh);
      strokeMesh.geometry.dispose();
      strokeMesh = null;
    }
    if (strokePts.length >= 2) {
      strokeMesh = new THREE.Mesh(ribbonGeometry(strokePts, 0.006, groundY + 0.004), strokeMat);
      strokeMesh.renderOrder = 9;
      root.add(strokeMesh);
    }
  }

  // --- Course generation ----------------------------------------------------
  function buildMountainSide(P, L, side, N, ridgeAt) {
    const jitter = makeNoise1D(0.11);
    const rows = ROW_OFFSETS.length;
    const positions = new Float32Array((N + 1) * rows * 3);
    const colors = new Float32Array((N + 1) * rows * 3);
    const idx = [];
    const cLow = new THREE.Color(MOUNTAIN_LOW);
    const cHigh = new THREE.Color(MOUNTAIN_HIGH);
    const tmp = new THREE.Color();

    for (let i = 0; i <= N; i++) {
      const s = i * SAMPLE_SPACING;
      const ridge = ridgeAt(i);
      for (let r = 0; r < rows; r++) {
        const k = (i * rows + r) * 3;
        const lateralJitter = r === 0 || r === rows - 1 ? 0 : (jitter(s * 1.7 + r * 5.3) - 0.5) * 0.05;
        const off = (ROW_OFFSETS[r] + lateralJitter) * side;
        const hJitter = 0.8 + 0.4 * jitter(s * 2.3 + r * 11.7);
        const h = ridge * ROW_HEIGHT_FACTORS[r] * hJitter;
        positions[k] = P[i].x + L[i].x * off;
        positions[k + 1] = groundY + h;
        positions[k + 2] = P[i].z + L[i].z * off;
        tmp.lerpColors(cLow, cHigh, clamp01(h / (RIDGE_BASE + RIDGE_VAR)));
        colors[k] = tmp.r;
        colors[k + 1] = tmp.g;
        colors[k + 2] = tmp.b;
      }
      if (i < N) {
        for (let r = 0; r < rows - 1; r++) {
          const a = i * rows + r;
          const b = a + rows;
          idx.push(a, b, a + 1, b, b + 1, a + 1);
        }
      }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    g.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    g.setIndex(idx);

    const fill = new THREE.Mesh(
      g,
      new THREE.MeshBasicMaterial({
        vertexColors: true,
        transparent: true,
        opacity: 0.26,
        depthWrite: false,
        side: THREE.DoubleSide,
      })
    );
    const wire = new THREE.Mesh(
      g,
      new THREE.MeshBasicMaterial({
        vertexColors: true,
        wireframe: true,
        transparent: true,
        opacity: 0.4,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      })
    );
    const group = new THREE.Group();
    group.add(fill, wire);
    return { group, geometry: g };
  }

  function scatterTrees(P, L, N, ridgeAt, side, parent) {
    const count = Math.max(4, Math.min(20, Math.round((N * SAMPLE_SPACING) / 0.16)));
    const cone = new THREE.ConeGeometry(0.02, 0.055, 5);
    cone.translate(0, 0.0275, 0);
    const fillMat = new THREE.MeshBasicMaterial({
      color: TREE_GREEN,
      transparent: true,
      opacity: 0.35,
      depthWrite: false,
    });
    const wireMat = new THREE.MeshBasicMaterial({
      color: LIME,
      wireframe: true,
      transparent: true,
      opacity: 0.45,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    const fill = new THREE.InstancedMesh(cone, fillMat, count);
    const wire = new THREE.InstancedMesh(cone, wireMat, count);
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    const up = new THREE.Vector3(0, 1, 0);
    let placed = 0;
    let guard = 0;
    while (placed < count && guard++ < count * 12) {
      const i = 4 + Math.floor(Math.random() * Math.max(1, N - 8));
      const t = 0.15 + Math.random() * 0.7;
      const ridge = ridgeAt(i);
      const h = ridge * (ROW_HEIGHT_FACTORS[1] + (ROW_HEIGHT_FACTORS[2] - ROW_HEIGHT_FACTORS[1]) * t) * 0.9;
      if (h > 0.2 || h < 0.015) continue; // keep trees below the "treeline"
      const off = (ROW_OFFSETS[1] + (ROW_OFFSETS[2] - ROW_OFFSETS[1]) * t) * side;
      const x = P[i].x + L[i].x * off;
      const z = P[i].z + L[i].z * off;
      q.setFromAxisAngle(up, Math.random() * Math.PI * 2);
      const s = 0.7 + Math.random() * 0.6;
      m.compose(new THREE.Vector3(x, groundY + h, z), q, new THREE.Vector3(s, s, s));
      fill.setMatrixAt(placed, m);
      wire.setMatrixAt(placed, m);
      placed++;
    }
    fill.count = placed;
    wire.count = placed;
    parent.add(fill, wire);
  }

  function buildMarkers(P, N, parent) {
    // Start: green ring + dot
    const startMat = new THREE.MeshBasicMaterial({
      color: GREEN,
      transparent: true,
      opacity: 0.85,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      side: THREE.DoubleSide,
    });
    const startRing = new THREE.Mesh(new THREE.RingGeometry(0.04, 0.048, 32), startMat);
    startRing.rotation.x = -Math.PI / 2;
    startRing.position.copy(P[0]).y = groundY + 0.004;
    const startDot = new THREE.Mesh(new THREE.CircleGeometry(0.018, 20), startMat.clone());
    startDot.rotation.x = -Math.PI / 2;
    startDot.position.copy(P[0]).y = groundY + 0.005;
    parent.add(startRing, startDot);

    // Victim: bobbing "!" + glow + pulsing ground ring, swap to check on found.
    // Placed just past the route end so nothing sits on the robot's line.
    const endDir = P[N].clone().sub(P[Math.max(0, N - 3)]).setY(0).normalize();
    if (!Number.isFinite(endDir.x) || endDir.lengthSq() < 0.5) endDir.set(0, 0, -1);
    const end = P[N].clone().addScaledVector(endDir, 0.12);
    const victim = new THREE.Group();
    const redMat = new THREE.MeshBasicMaterial({ color: ALERT_RED, transparent: true, opacity: 0.95 });
    const bar = new THREE.Mesh(new THREE.CapsuleGeometry(0.008, 0.045, 4, 10), redMat);
    bar.position.y = 0.055;
    const dot = new THREE.Mesh(new THREE.SphereGeometry(0.0095, 12, 10), redMat);
    dot.position.y = 0.0;
    const halo = makeGlowSprite("rgba(255,77,109,0.85)", 0.13);
    halo.position.y = 0.035;
    victim.add(bar, dot, halo);
    victim.position.set(end.x, groundY + MARKER_BASE_H, end.z);
    parent.add(victim);

    const check = new THREE.Group();
    const greenMat = new THREE.MeshBasicMaterial({ color: GREEN, transparent: true, opacity: 0.95 });
    const shortArm = new THREE.Mesh(new THREE.CapsuleGeometry(0.007, 0.02, 4, 10), greenMat);
    shortArm.position.set(-0.016, 0.008, 0);
    shortArm.rotation.z = -Math.PI / 4;
    const longArm = new THREE.Mesh(new THREE.CapsuleGeometry(0.007, 0.05, 4, 10), greenMat);
    longArm.position.set(0.012, 0.022, 0);
    longArm.rotation.z = Math.PI / 4;
    const checkHalo = makeGlowSprite("rgba(92,224,138,0.85)", 0.15);
    checkHalo.position.y = 0.02;
    check.add(shortArm, longArm, checkHalo);
    check.position.set(end.x, groundY + MARKER_BASE_H, end.z);
    check.visible = false;
    parent.add(check);

    const pulseMat = new THREE.MeshBasicMaterial({
      color: ALERT_RED,
      transparent: true,
      opacity: 0.7,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      side: THREE.DoubleSide,
    });
    const pulseRing = new THREE.Mesh(new THREE.RingGeometry(0.05, 0.056, 36), pulseMat);
    pulseRing.rotation.x = -Math.PI / 2;
    pulseRing.position.set(end.x, groundY + 0.004, end.z);
    parent.add(pulseRing);

    const burstMat = pulseMat.clone();
    burstMat.color = new THREE.Color(GREEN);
    burstMat.opacity = 0;
    const burstRing = new THREE.Mesh(new THREE.RingGeometry(0.05, 0.06, 36), burstMat);
    burstRing.rotation.x = -Math.PI / 2;
    burstRing.position.copy(pulseRing.position);
    parent.add(burstRing);

    anim.victim = victim;
    anim.check = check;
    anim.pulseRing = pulseRing;
    anim.pulseMat = pulseMat;
    anim.burstRing = burstRing;
    anim.burstMat = burstMat;
  }

  function buildCourse(rawPts) {
    const curve = new THREE.CatmullRomCurve3(
      rawPts.map((p) => new THREE.Vector3(p.x, groundY, p.z)),
      false,
      "centripetal",
      0.5
    );
    const len = curve.getLength();
    const N = Math.max(24, Math.min(480, Math.round(len / SAMPLE_SPACING)));
    const P = curve.getSpacedPoints(N).map((p) => new THREE.Vector3(p.x, groundY, p.z));

    // Smoothed tangents -> left normals
    const dirs = [];
    for (let i = 0; i <= N; i++) {
      const p0 = P[Math.max(0, i - 2)];
      const p1 = P[Math.min(N, i + 2)];
      dirs.push(new THREE.Vector3(p1.x - p0.x, 0, p1.z - p0.z).normalize());
    }
    const L = dirs.map((d) => new THREE.Vector3(d.z, 0, -d.x));

    course = new THREE.Group();

    // Ridge height field per side (independent noise), tapered at both ends
    const taper = (i) => {
      const edge = Math.min(i, N - i) / 8;
      const t = clamp01(edge);
      return t * t * (3 - 2 * t);
    };
    for (const side of [1, -1]) {
      const nA = makeNoise1D(0.38);
      const nB = makeNoise1D(0.13);
      const ridgeAt = (i) => {
        const s = i * SAMPLE_SPACING;
        return (RIDGE_BASE + RIDGE_VAR * (0.65 * nA(s) + 0.35 * nB(s))) * taper(i);
      };
      const { group } = buildMountainSide(P, L, side, N, ridgeAt);
      course.add(group);
      scatterTrees(P, L, N, ridgeAt, side, course);
    }

    // Road fill
    const road = new THREE.Mesh(
      ribbonGeometry(P, ROAD_HALF_W, groundY + 0.002),
      new THREE.MeshBasicMaterial({
        color: 0x2f8f5b,
        transparent: true,
        opacity: 0.16,
        depthWrite: false,
        side: THREE.DoubleSide,
      })
    );
    course.add(road);

    // Edge rails + tick whiskers + center dashes (merged bright quads)
    const railMat = new THREE.MeshBasicMaterial({
      color: LIME,
      transparent: true,
      opacity: 0.9,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      side: THREE.DoubleSide,
    });
    for (const side of [1, -1]) {
      const edgePts = P.map((p, i) => p.clone().addScaledVector(L[i], ROAD_HALF_W * side));
      const rail = new THREE.Mesh(ribbonGeometry(edgePts, 0.005, groundY + 0.005), railMat);
      course.add(rail);
    }
    {
      const pos = [];
      const idx = [];
      const yLine = groundY + 0.005;
      const everyTick = Math.max(1, Math.round(0.15 / SAMPLE_SPACING));
      const everyDash = Math.max(1, Math.round(0.07 / SAMPLE_SPACING));
      const dashHalfLen = 0.011;
      for (let i = 2; i < N - 1; i += everyDash) {
        const d = dirs[i];
        const p = P[i];
        const a = new THREE.Vector3(p.x - d.x * dashHalfLen + L[i].x * 0.0035, yLine, p.z - d.z * dashHalfLen + L[i].z * 0.0035);
        const b = new THREE.Vector3(p.x - d.x * dashHalfLen - L[i].x * 0.0035, yLine, p.z - d.z * dashHalfLen - L[i].z * 0.0035);
        const c = new THREE.Vector3(p.x + d.x * dashHalfLen + L[i].x * 0.0035, yLine, p.z + d.z * dashHalfLen + L[i].z * 0.0035);
        const e = new THREE.Vector3(p.x + d.x * dashHalfLen - L[i].x * 0.0035, yLine, p.z + d.z * dashHalfLen - L[i].z * 0.0035);
        pushQuad(pos, idx, a, b, c, e);
      }
      for (let i = 0; i <= N; i += everyTick) {
        for (const side of [1, -1]) {
          const d = dirs[i];
          const o0 = ROAD_HALF_W * side;
          const o1 = (ROAD_HALF_W + 0.016) * side;
          const p = P[i];
          const a = new THREE.Vector3(p.x + L[i].x * o0 - d.x * 0.002, yLine, p.z + L[i].z * o0 - d.z * 0.002);
          const b = new THREE.Vector3(p.x + L[i].x * o0 + d.x * 0.002, yLine, p.z + L[i].z * o0 + d.z * 0.002);
          const c = new THREE.Vector3(p.x + L[i].x * o1 - d.x * 0.002, yLine, p.z + L[i].z * o1 - d.z * 0.002);
          const e = new THREE.Vector3(p.x + L[i].x * o1 + d.x * 0.002, yLine, p.z + L[i].z * o1 + d.z * 0.002);
          pushQuad(pos, idx, a, b, c, e);
        }
      }
      const marks = new THREE.Mesh(geometryFromArrays(pos, idx), railMat.clone());
      marks.material.opacity = 0.55;
      course.add(marks);
    }

    buildMarkers(P, N, course);

    course.scale.y = 0.001;
    riseStart = performance.now();
    root.add(course);
  }

  // --- Public API -----------------------------------------------------------

  function setPhase(next) {
    if (phase === next) return;
    phase = next;
    onPhaseChange(phase);
  }

  function enter() {
    root.visible = true;
    setPhase(course ? "built" : "draw");
    reticle.visible = false;
  }

  function exit() {
    clear();
    root.visible = false;
    setPhase("idle");
  }

  function clear() {
    if (course) {
      root.remove(course);
      disposeGroup(course);
      course = null;
    }
    Object.keys(anim).forEach((k) => (anim[k] = null));
    strokePts = [];
    drawing = false;
    refreshStrokeMesh();
    if (root.visible) setPhase("draw");
  }

  /** Update pointer ray (world-space origin/direction). Returns ground hit or null. */
  function pointerMove(origin, dir) {
    const hit = intersectGround(origin, dir, groundY);
    if (phase === "draw") {
      reticle.visible = !!hit;
      if (hit) reticle.position.copy(hit);
    } else {
      reticle.visible = false;
    }
    if (drawing && hit) extendStroke(hit);
    return hit;
  }

  function beginStroke(hit) {
    if (phase !== "draw" || !hit) return false;
    drawing = true;
    strokePts = [hit.clone()];
    return true;
  }

  function extendStroke(hit) {
    if (!drawing) return;
    const last = strokePts[strokePts.length - 1];
    if (last.distanceTo(hit) >= 0.03) {
      strokePts.push(hit.clone());
      refreshStrokeMesh();
    }
  }

  function endStroke() {
    if (!drawing) return "draw";
    drawing = false;
    const pts = strokePts;
    strokePts = [];
    refreshStrokeMesh();
    let len = 0;
    for (let i = 1; i < pts.length; i++) len += pts[i].distanceTo(pts[i - 1]);
    if (pts.length < 4 || len < MIN_PATH_LEN) {
      setPhase("draw");
      onPhaseChange("too_short");
      return "too_short";
    }
    buildCourse(pts);
    reticle.visible = false;
    setPhase("built");
    return "built";
  }

  /** Mark the victim as located. Returns true if this call changed state. */
  function markFound() {
    if (phase !== "built" || !anim.victim) return false;
    anim.victim.visible = false;
    anim.check.visible = true;
    anim.pulseMat.color = new THREE.Color(GREEN);
    anim.burstMat.opacity = 0.9;
    anim.burstRing.scale.setScalar(1);
    foundAt = performance.now();
    setPhase("found");
    return true;
  }

  function tick(timeMs) {
    const t = timeMs / 1000;
    if (reticle.visible) reticle.rotation.y = t * 1.2;

    if (course) {
      const rise = clamp01((timeMs - riseStart) / (RISE_DURATION * 1000));
      course.scale.y = Math.max(0.001, easeOutCubic(rise));

      const marker = anim.check?.visible ? anim.check : anim.victim;
      if (marker) {
        marker.position.y = groundY + MARKER_BASE_H + Math.sin(t * 2.4) * 0.012;
        marker.rotation.y = t * 0.9;
      }
      if (anim.pulseRing) {
        const cycle = (t % 1.4) / 1.4;
        anim.pulseRing.scale.setScalar(1 + cycle * 1.6);
        anim.pulseMat.opacity = 0.7 * (1 - cycle);
      }
      if (anim.burstMat && anim.burstMat.opacity > 0) {
        const bt = clamp01((timeMs - foundAt) / 900);
        anim.burstRing.scale.setScalar(1 + bt * 7);
        anim.burstMat.opacity = 0.9 * (1 - bt);
      }
    }
  }

  return {
    get phase() {
      return phase;
    },
    get isDrawing() {
      return drawing;
    },
    enter,
    exit,
    clear,
    pointerMove,
    beginStroke,
    endStroke,
    markFound,
    tick,
  };
}
