(() => {
  const params = new URLSearchParams(location.search);
  const hubParam = params.get("hub");
  if (hubParam) localStorage.setItem("rover_hub", hubParam);

  function parseHubUrl(hub) {
    const s = String(hub).trim().replace(/^wss:/i, "https:").replace(/^ws:/i, "http:");
    return new URL(s);
  }

  function hubBase() {
    const stored = localStorage.getItem("rover_hub");
    if (stored) {
      // allow wss://host or https://host; match page protocol for camera fetch
      try {
        const u = parseHubUrl(stored);
        const proto = location.protocol === "https:" ? "https:" : "http:";
        return `${proto}//${u.host}`;
      } catch (_) {}
    }
    return "";
  }

  function wsUrl() {
    const stored = localStorage.getItem("rover_hub");
    if (stored) {
      try {
        const u = parseHubUrl(stored);
        const proto = u.protocol === "https:" ? "wss:" : "ws:";
        if (u.pathname.includes("/ws")) {
          return `${proto}//${u.host}${u.pathname}${u.search}`;
        }
        return `${proto}//${u.host}/ws`;
      } catch (_) {}
    }
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}/ws`;
  }

  const cam = document.getElementById("cam");
  const base = hubBase();
  if (base) cam.src = `${base}/camera.mjpg`;
  else cam.src = "/camera.mjpg";

  let ws = null;
  let state = null;
  let driveHold = null;
  let reconnectTimer = null;

  const el = (id) => document.getElementById(id);

  function setConn(ok, text) {
    const c = el("conn");
    c.textContent = text;
    c.classList.toggle("ok", ok);
    c.classList.toggle("bad", !ok);
  }

  function connect() {
    if (ws) try { ws.close(); } catch (_) {}
    setConn(false, "CONNECTING…");
    ws = new WebSocket(wsUrl());
    ws.onopen = () => setConn(true, "CONNECTED");
    ws.onclose = () => {
      setConn(false, "RECONNECTING…");
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, 1200);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (ev) => {
      try { state = JSON.parse(ev.data); } catch (_) { return; }
      render(state);
    };
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ v: 1, ...obj }));
  }

  function render(s) {
    el("mode-pill").textContent = s.mode || "—";
    const banner = el("estop-banner");
    banner.classList.toggle("hidden", !s.estop);

    const L = s.links || {};
    el("s-rover").textContent = L.rover?.connected ? `OK L${s.motors?.left} R${s.motors?.right}` : "DOWN";
    el("s-glove").textContent = L.glove?.connected
      ? `OK r${(L.glove.roll || 0).toFixed(0)} p${(L.glove.pitch || 0).toFixed(0)}`
      : "DOWN";
    el("s-cam").textContent = L.camera?.connected ? `OK #${L.camera.frame_count}` : "DOWN";
    el("s-motors").textContent = `${s.motors?.left ?? 0} / ${s.motors?.right ?? 0}`;
    el("s-look").textContent = `pan ${(s.look?.pan ?? 0).toFixed(0)} tilt ${(s.look?.tilt ?? 0).toFixed(0)}`;

    el("novideo").classList.toggle("hidden", !!L.camera?.connected);
    drawMap(s);
  }

  function drawMap(s) {
    const c = el("map");
    const ctx = c.getContext("2d");
    const W = c.width, H = c.height;
    ctx.fillStyle = "#0a1018";
    ctx.fillRect(0, 0, W, H);
    ctx.strokeStyle = "#243044";
    for (let i = 0; i < W; i += 28) {
      ctx.beginPath(); ctx.moveTo(i, 0); ctx.lineTo(i, H); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, i); ctx.lineTo(W, i); ctx.stroke();
    }
    const scale = 80; // px per meter
    const cx = W / 2, cy = H / 2;
    const to = (x, y) => [cx + x * scale, cy - y * scale];

    const trail = s.trail || [];
    if (trail.length > 1) {
      ctx.strokeStyle = "#3dd6c6";
      ctx.lineWidth = 2;
      ctx.beginPath();
      trail.forEach((p, i) => {
        const [px, py] = to(p.x, p.y);
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      });
      ctx.stroke();
    }
    (s.markers || []).forEach((m) => {
      const [px, py] = to(m.x, m.y);
      ctx.fillStyle = "#ff4d6d";
      ctx.beginPath();
      ctx.arc(px, py, 6, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#fff";
      ctx.font = "10px sans-serif";
      ctx.fillText(String(m.id), px + 8, py + 3);
    });
    if (s.pose) {
      const [px, py] = to(s.pose.x, s.pose.y);
      ctx.save();
      ctx.translate(px, py);
      ctx.rotate(-s.pose.theta);
      ctx.fillStyle = "#5ce08a";
      ctx.beginPath();
      ctx.moveTo(10, 0);
      ctx.lineTo(-7, 6);
      ctx.lineTo(-7, -6);
      ctx.closePath();
      ctx.fill();
      ctx.restore();
    }
  }

  // controls
  el("btn-estop").onclick = () => send({ type: "estop_toggle" });
  el("btn-arm").onclick = () => send({ type: "estop", on: false });
  el("btn-mark").onclick = () => send({ type: "mark_victim" });
  el("btn-undo").onclick = () => send({ type: "undo_marker" });
  el("btn-cal").onclick = () => send({ type: "calibrate_glove" });

  document.querySelectorAll("[data-mode]").forEach((b) => {
    b.onclick = () => send({ type: "set_mode", mode: b.dataset.mode });
  });

  function setDrive(v, omega) {
    driveHold = { v, omega };
    send({ type: "drive", v, omega });
  }

  document.querySelectorAll("[data-drive]").forEach((b) => {
    const [v, o] = b.dataset.drive.split(",").map(Number);
    const go = (e) => { e.preventDefault(); setDrive(v, o); };
    const stop = (e) => { e.preventDefault(); setDrive(0, 0); };
    b.addEventListener("pointerdown", go);
    b.addEventListener("pointerup", stop);
    b.addEventListener("pointerleave", stop);
  });

  // keep driving while held
  setInterval(() => {
    if (driveHold && (driveHold.v || driveHold.omega)) {
      send({ type: "drive", ...driveHold });
    }
  }, 100);

  const keys = {};
  window.addEventListener("keydown", (e) => {
    keys[e.key.toLowerCase()] = true;
    if (e.key === " ") { e.preventDefault(); send({ type: "estop_toggle" }); }
  });
  window.addEventListener("keyup", (e) => { keys[e.key.toLowerCase()] = false; });
  setInterval(() => {
    let v = 0, o = 0;
    if (keys.w) v += 1;
    if (keys.s) v -= 1;
    if (keys.a) o -= 1;
    if (keys.d) o += 1;
    if (v || o) setDrive(v, o);
    else if (!driveHold || (!driveHold.v && !driveHold.omega)) {
      /* idle */
    } else if (!Object.values(keys).some(Boolean)) {
      // only clear if pad not held — pad uses pointer
    }
  }, 100);

  el("pan").oninput = el("tilt").oninput = () => {
    send({
      type: "look",
      pan: Number(el("pan").value),
      tilt: Number(el("tilt").value),
    });
  };

  // cam error overlay
  cam.onerror = () => el("novideo").classList.remove("hidden");
  cam.onload = () => el("novideo").classList.add("hidden");

  connect();
})();
