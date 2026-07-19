// Headless smoke test for the rescue course generation (no renderer needed).
import * as THREE from "three";

globalThis.document = {
  createElement: (tag) => {
    if (tag !== "canvas") throw new Error("unexpected element " + tag);
    return {
      width: 0,
      height: 0,
      getContext: () => ({
        createRadialGradient: () => ({ addColorStop() {} }),
        fillRect() {},
        clearRect() {},
        set fillStyle(_) {},
      }),
    };
  },
};

const { createRescue, intersectGround } = await import("./src/rescue.js");

// intersectGround sanity
const hit = intersectGround(new THREE.Vector3(0, 1.5, 0), new THREE.Vector3(0, -0.7, -0.7).normalize(), 0);
console.assert(hit && Math.abs(hit.y) < 1e-9 && Math.abs(hit.z + 1.5) < 1e-6, "intersectGround", hit);
console.assert(intersectGround(new THREE.Vector3(0, 1.5, 0), new THREE.Vector3(0, 0.5, -0.5), 0) === null, "upward ray must miss");

const scene = new THREE.Scene();
const phases = [];
const rescue = createRescue({ scene, groundY: 0, onPhaseChange: (p) => phases.push(p) });

rescue.enter();
console.assert(rescue.phase === "draw", "phase after enter", rescue.phase);

// Too-short stroke is rejected
rescue.beginStroke(new THREE.Vector3(0, 0, 0));
rescue.pointerMove(new THREE.Vector3(0.1, 1.5, 0), new THREE.Vector3(0, -1, 0.0001).normalize());
const r1 = rescue.endStroke();
console.assert(r1 === "too_short", "short stroke rejected", r1);

// Draw an S-shaped ~1.6 m stroke via synthetic ray hits
rescue.beginStroke(new THREE.Vector3(0, 0, -0.3));
for (let i = 1; i <= 40; i++) {
  const t = i / 40;
  const x = Math.sin(t * Math.PI * 1.5) * 0.35;
  const z = -0.3 - t * 1.3;
  // emulate a ray from head height through the target point
  const origin = new THREE.Vector3(0, 1.5, 0);
  const dir = new THREE.Vector3(x, 0, z).sub(origin).normalize();
  rescue.pointerMove(origin, dir);
}
const r2 = rescue.endStroke();
console.assert(r2 === "built", "stroke built", r2);
console.assert(rescue.phase === "built", "phase built");

// Validate all generated geometry: no NaNs
let meshes = 0;
let bad = 0;
scene.traverse((o) => {
  if (!o.geometry?.attributes?.position) return;
  meshes++;
  for (const v of o.geometry.attributes.position.array) if (!Number.isFinite(v)) bad++;
});
console.log(`meshes=${meshes} NaNs=${bad}`);
console.assert(bad === 0, "geometry contains NaN/Inf");

// Animate past the rise, then check world-space bounds of the terrain meshes
for (let t = 0; t < 3000; t += 100) rescue.tick(t);
scene.updateMatrixWorld(true);
const box = new THREE.Box3();
const p = new THREE.Vector3();
scene.traverse((o) => {
  if (!o.isMesh || !o.geometry?.attributes?.position || o.isInstancedMesh) return;
  const posAttr = o.geometry.attributes.position;
  for (let i = 0; i < posAttr.count; i++) {
    box.expandByPoint(p.fromBufferAttribute(posAttr, i).applyMatrix4(o.matrixWorld));
  }
});
console.log("world bounds min", box.min.toArray().map((v) => v.toFixed(3)));
console.log("world bounds max", box.max.toArray().map((v) => v.toFixed(3)));
console.assert(box.min.y >= -0.001, "nothing below ground", box.min.y);
console.assert(box.max.y > 0.12 && box.max.y < 0.55, "ridge peaks 12-55cm", box.max.y);
console.assert(rescue.markFound() === true, "markFound");
console.assert(rescue.phase === "found", "phase found");
rescue.tick(1600);

// Clear resets to draw
rescue.clear();
console.assert(rescue.phase === "draw", "phase after clear");

console.log("phases seen:", phases.join(" -> "));
console.log("SMOKE TEST OK");
