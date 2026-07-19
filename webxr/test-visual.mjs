// Visual test: drive the real page in headless chromium, draw a course in
// flat rescue mode, capture screenshots into shots/.
// Requires: npm i --no-save playwright-core  (uses the ms-playwright browser cache)
// Run: npm run dev -- --port 5199 --strictPort   then   node test-visual.mjs
import { chromium } from "playwright-core";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const cacheDir = path.join(os.homedir(), "Library/Caches/ms-playwright");
const shells = fs
  .readdirSync(cacheDir)
  .filter((d) => d.startsWith("chromium_headless_shell-"))
  .sort();
const shell = path.join(
  cacheDir,
  shells[shells.length - 1],
  "chrome-headless-shell-mac-arm64/chrome-headless-shell"
);
console.log("using", shell);

const browser = await chromium.launch({
  executablePath: shell,
  args: ["--enable-unsafe-swiftshader", "--use-angle=swiftshader"],
});
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
page.on("console", (m) => {
  if (m.type() === "error") console.log("[page error]", m.text());
});
page.on("pageerror", (e) => console.log("[pageerror]", e.message));

await page.goto("http://localhost:5199/", { waitUntil: "networkidle" });
await page.waitForTimeout(600);
await page.screenshot({ path: "shots/01-connect.png" });

await page.click("#enter");
await page.waitForTimeout(300);
await page.screenshot({ path: "shots/02-menu.png" });

await page.click("#mode-rescue");
await page.waitForTimeout(800);
await page.screenshot({ path: "shots/03-draw-phase.png" });

// Draw an S-shaped course with the mouse
const pts = [];
for (let i = 0; i <= 60; i++) {
  const t = i / 60;
  const x = 280 + t * 720;
  const y = 500 + Math.sin(t * Math.PI * 2) * 110 - t * 60;
  pts.push([x, y]);
}
await page.mouse.move(pts[0][0], pts[0][1]);
await page.mouse.down();
for (const [x, y] of pts) {
  await page.mouse.move(x, y);
  await page.waitForTimeout(8);
}
await page.screenshot({ path: "shots/04-drawing.png" });
await page.mouse.up();

await page.waitForTimeout(1400); // rise animation
await page.screenshot({ path: "shots/05-course.png" });

// Mark victim found
await page.mouse.click(640, 400);
await page.waitForTimeout(500);
await page.screenshot({ path: "shots/06-found.png" });

// Clear with keyboard
await page.keyboard.press("c");
await page.waitForTimeout(300);
await page.screenshot({ path: "shots/07-cleared.png" });

const hud = await page.evaluate(() => document.getElementById("hud-msg").textContent);
console.log("HUD after clear:", hud);

await browser.close();
console.log("VISUAL TEST DONE");
