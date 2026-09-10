/* VayuVeer Cloud GCS dashboard.  Vanilla JS; talks to the relay over one WebSocket.
   Text frames = JSON (telemetry, status, ack); binary frames = JPEG video. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);

  // ------------------------------------------------------------------ config
  const cfg = {
    get url() { return localStorage.getItem("vayuveer.url") || ""; },
    get session() { return localStorage.getItem("vayuveer.session") || ""; },
    get manualToken() { return localStorage.getItem("vayuveer.token") || ""; },
    get token() { return this.session || this.manualToken; },
    get drone() { return localStorage.getItem("vayuveer.drone") || ""; },
    get user() { try { return JSON.parse(localStorage.getItem("vayuveer.user") || "null"); } catch { return null; } },
    save(u, t, d) { localStorage.setItem("vayuveer.url", u.trim()); localStorage.setItem("vayuveer.token", t.trim()); localStorage.setItem("vayuveer.drone", d.trim()); },
    setSession(tok, user) { localStorage.setItem("vayuveer.session", tok); localStorage.setItem("vayuveer.user", JSON.stringify(user)); },
    clearSession() { localStorage.removeItem("vayuveer.session"); localStorage.removeItem("vayuveer.user"); },
  };
  function httpBase() { return cfg.url ? cfg.url.replace(/^ws/, "http").replace(/\/$/, "") : ""; }
  function wsBase() {
    if (cfg.url) return cfg.url.replace(/^http/, "ws").replace(/\/$/, "");
    return (location.protocol === "https:" ? "wss://" : "ws://") + location.host;
  }

  // ------------------------------------------------------------------ state
  const S = {
    ws: null, connected: false, droneOnline: false, tele: null, lastTeleAt: 0,
    frames: 0, fps: 0, rttDrone: null, rttServer: null,
    waypoints: [], planMode: false, follow: true, trail: [], role: "operator",
    stick: { throttle: 0, yaw: 0, pitch: 0, roll: 0 }, stickActive: false, keys: new Set(),
  };

  // ------------------------------------------------------------------ log
  const logEl = $("log");
  function log(text, cls = "") {
    const d = document.createElement("div");
    const t = document.createElement("time"); t.textContent = new Date().toLocaleTimeString([], { hour12: false });
    const s = document.createElement("span"); s.textContent = text; if (cls) s.className = cls;
    d.append(t, s); logEl.prepend(d);
    while (logEl.childElementCount > 300) logEl.lastChild.remove();
  }
  $("btn-clearlog").onclick = () => (logEl.innerHTML = "");

  // ------------------------------------------------------------------ websocket
  let reconnectTimer = null, backoff = 1000;
  function connect() {
    if (S.ws) { try { S.ws.close(); } catch { } S.ws = null; }
    if (!cfg.token || !cfg.drone) { openSettings(); return; }
    const url = `${wsBase()}/ws/client/${encodeURIComponent(cfg.drone)}?token=${encodeURIComponent(cfg.token)}`;
    setPill("pill-relay", "warn", "Relay…");
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    S.ws = ws;
    ws.onopen = () => { S.connected = true; backoff = 1000; setPill("pill-relay", "on", "Relay"); log(`Connected to relay (${cfg.drone})`, "ok"); };
    ws.onclose = e => {
      S.connected = false; S.droneOnline = false;
      setPill("pill-relay", "off", "Relay"); setPill("pill-drone", "off", "Aircraft"); setPill("pill-fc", "off", "FC");
      if (e.code === 4401) { log("Session expired or invalid – please sign in", "err"); cfg.clearSession(); openSettings("Session expired. Please sign in again."); return; }
      if (e.code === 4403) { log(`Your account cannot access ${cfg.drone}`, "err"); openSettings(`Your account cannot access aircraft "${cfg.drone}".`); return; }
      log(`Relay link closed (${e.code}) – retrying`, "err");
      clearTimeout(reconnectTimer); reconnectTimer = setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 10000);
    };
    ws.onerror = () => { };
    ws.onmessage = ev => {
      if (ev.data instanceof ArrayBuffer) return onFrame(ev.data);
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      onMessage(m);
    };
  }
  function send(obj) { if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(obj)); }
  function cmd(name, args = {}) { if (S.role !== "operator") { log("View-only account – command not sent", "err"); return; } send({ type: "cmd", name, args }); }

  function onMessage(m) {
    switch (m.type) {
      case "telemetry": S.tele = m; S.lastTeleAt = performance.now(); if (!S.droneOnline) { S.droneOnline = true; setPill("pill-drone", "on", "Aircraft"); } renderTelemetry(m); break;
      case "session": applySession(m); break;
      case "drone_online": S.droneOnline = true; setPill("pill-drone", "on", "Aircraft"); log("Drone online", "ok"); break;
      case "drone_offline": S.droneOnline = false; setPill("pill-drone", "off", "Aircraft"); setPill("pill-fc", "off", "FC"); $("vid-offline").classList.remove("hidden"); log("Drone offline", "err"); break;
      case "status": log(m.text, `s${m.severity}`); break;
      case "ack": log(`${m.cmd}: ${m.msg}`, m.ok ? "ok" : "err"); break;
      case "error": log(m.text, "err"); break;
      case "pong": S.rttServer = Date.now() - m.t; break;
      case "dpong": if (m.nonce === myNonce) { S.rttDrone = Date.now() - m.t; renderRtt(); } break;
    }
  }
  const myNonce = Math.random().toString(36).slice(2);
  setInterval(() => { if (S.connected) { send({ type: "ping", t: Date.now() }); send({ type: "dping", t: Date.now(), nonce: myNonce }); } }, 2000);
  function renderRtt() {
    const b = $("st-rtt"); b.textContent = S.rttDrone != null ? `${S.rttDrone} ms` : "—";
    b.className = S.rttDrone > 800 ? "bad" : S.rttDrone > 350 ? "warn" : "";
  }

  // ------------------------------------------------------------------ video
  const video = $("video"); let lastUrl = null, fpsCount = 0;
  function onFrame(buf) {
    const url = URL.createObjectURL(new Blob([buf], { type: "image/jpeg" }));
    video.onload = () => { if (lastUrl) URL.revokeObjectURL(lastUrl); lastUrl = url; };
    video.src = url; fpsCount++;
    $("vid-offline").classList.add("hidden");
  }
  setInterval(() => { S.fps = fpsCount; fpsCount = 0; $("st-fps").textContent = S.fps ? `${S.fps} fps` : "—"; if (!S.fps) $("vid-offline").classList.remove("hidden"); }, 1000);
  document.querySelectorAll(".video-tools [data-cam]").forEach(b => b.onclick = () => {
    document.querySelectorAll(".video-tools [data-cam]").forEach(x => x.classList.toggle("active", x === b));
    cmd("camera", { source: b.dataset.cam });
  });
  $("btn-fullscreen").onclick = () => { const p = $("video-panel"); document.fullscreenElement ? document.exitFullscreen() : p.requestFullscreen?.(); };

  // ------------------------------------------------------------------ HUD
  const hud = $("hud"), hctx = hud.getContext("2d");
  function drawHud() {
    const p = hud.parentElement; const w = p.clientWidth, h = p.clientHeight;
    if (hud.width !== w || hud.height !== h) { hud.width = w; hud.height = h; }
    hctx.clearRect(0, 0, w, h);
    const t = S.tele; if (!t) return;
    const cx = w / 2, cy = h / 2, R = Math.min(w, h) * 0.22;
    hctx.save(); hctx.strokeStyle = "rgba(57,217,138,.9)"; hctx.fillStyle = "rgba(57,217,138,.9)"; hctx.lineWidth = 1.5; hctx.font = "12px ui-monospace,Menlo,monospace";
    // horizon
    hctx.save(); hctx.translate(cx, cy); hctx.rotate(-t.roll); const pitchPx = (t.pitch * 180 / Math.PI) * (R / 30);
    hctx.beginPath(); hctx.moveTo(-R * 1.6, pitchPx); hctx.lineTo(-R * 0.35, pitchPx); hctx.moveTo(R * 0.35, pitchPx); hctx.lineTo(R * 1.6, pitchPx); hctx.stroke();
    for (const d of [-20, -10, 10, 20]) { const y = pitchPx - d * (R / 30); hctx.beginPath(); hctx.moveTo(-R * 0.5, y); hctx.lineTo(R * 0.5, y); hctx.stroke(); hctx.fillText(d, R * 0.55, y + 4); }
    hctx.restore();
    // aircraft reference
    hctx.strokeStyle = "rgba(255,176,32,.95)"; hctx.lineWidth = 2; hctx.beginPath(); hctx.moveTo(cx - R * 0.3, cy); hctx.lineTo(cx - R * 0.1, cy); hctx.lineTo(cx, cy + R * 0.06); hctx.lineTo(cx + R * 0.1, cy); hctx.lineTo(cx + R * 0.3, cy); hctx.stroke();
    // heading tape
    hctx.strokeStyle = hctx.fillStyle = "rgba(223,231,241,.9)"; hctx.lineWidth = 1; hctx.textAlign = "center";
    const hy = 34, hw = Math.min(w * 0.6, 420), hx0 = cx - hw / 2, pxPerDeg = hw / 90;
    hctx.beginPath(); hctx.moveTo(hx0, hy); hctx.lineTo(hx0 + hw, hy); hctx.stroke();
    for (let d = -45; d <= 45; d += 5) { const deg = ((Math.round(t.heading) + d) % 360 + 360) % 360; if (deg % 5) continue; const x = cx + d * pxPerDeg; const big = deg % 15 === 0; hctx.beginPath(); hctx.moveTo(x, hy); hctx.lineTo(x, hy - (big ? 8 : 4)); hctx.stroke(); if (deg % 30 === 0) hctx.fillText({ 0: "N", 90: "E", 180: "S", 270: "W" }[deg] ?? deg, x, hy - 11); }
    hctx.fillStyle = "#ffb020"; hctx.fillText(`${Math.round(t.heading)}°`, cx, hy + 14);
    // side readouts
    hctx.textAlign = "left"; hctx.fillStyle = "rgba(223,231,241,.95)"; hctx.font = "bold 16px ui-monospace,Menlo,monospace";
    hctx.fillText(`${t.groundspeed.toFixed(1)} m/s`, 14, cy - 6); hctx.font = "11px ui-monospace,Menlo,monospace"; hctx.fillText("GS", 14, cy + 10);
    hctx.textAlign = "right"; hctx.font = "bold 16px ui-monospace,Menlo,monospace"; hctx.fillText(`${t.alt_rel.toFixed(1)} m`, w - 14, cy - 6);
    hctx.font = "11px ui-monospace,Menlo,monospace"; hctx.fillText(`AGL  ${t.climb >= 0 ? "▲" : "▼"} ${Math.abs(t.climb).toFixed(1)}`, w - 14, cy + 10);
    hctx.textAlign = "left"; hctx.fillText(`${t.mode}${t.armed ? "  ARMED" : ""}${t.manual_active ? "  STICK" : ""}`, 14, h - 14);
    hctx.restore();
  }
  (function hudLoop() { drawHud(); requestAnimationFrame(hudLoop); })();

  // ------------------------------------------------------------------ pills / stats
  function setPill(id, state, text) { const e = $(id); e.dataset.state = state; if (text) e.textContent = text; }
  function renderTelemetry(t) {
    setPill("pill-fc", t.connected ? "on" : "warn", t.connected ? "FC" : "FC link");
    $("pill-mode").textContent = t.mode;
    setPill("pill-armed", t.armed ? "on" : "off", t.armed ? "Armed" : "Disarmed");
    const bp = t.battery_pct; const bb = $("st-batt");
    bb.textContent = bp >= 0 ? `${Math.round(bp)}%  ${t.battery_v.toFixed(1)}V` : `${t.battery_v.toFixed(1)}V`;
    bb.className = bp >= 0 && bp < 20 ? "bad" : bp >= 0 && bp < 35 ? "warn" : "";
    const gb = $("st-gps"); gb.textContent = `${t.gps_fix} ${t.sats}`; gb.className = t.sats < 6 ? "bad" : t.sats < 9 ? "warn" : "";
    const ft = Math.round(t.flight_time || 0); $("st-time").textContent = `${String(Math.floor(ft / 60)).padStart(2, "0")}:${String(ft % 60).padStart(2, "0")}`;
    $("vid-source").textContent = t.video?.source || "EO";
    $("btn-arm").disabled = t.armed; $("btn-disarm").disabled = !t.armed;
    $("mission-status").textContent = t.mission_count ? `on FC: ${t.mission_count} items, at #${t.mission_current}` : "";
    const rows = [["LAT", t.lat.toFixed(6)], ["LON", t.lon.toFixed(6)], ["ALT MSL", `${t.alt_msl.toFixed(1)} m`], ["ALT AGL", `${t.alt_rel.toFixed(1)} m`],
    ["HDG", `${t.heading.toFixed(0)}°`], ["GS", `${t.groundspeed.toFixed(1)} m/s`], ["CLIMB", `${t.climb.toFixed(1)} m/s`], ["THR", `${t.throttle}%`],
    ["ROLL", `${(t.roll * 57.3).toFixed(0)}°`], ["PITCH", `${(t.pitch * 57.3).toFixed(0)}°`], ["HDOP", t.hdop.toFixed(1)], ["EKF", t.ekf_ok == null ? "?" : t.ekf_ok ? "OK" : "BAD"],
    ["CURR", `${t.battery_a.toFixed(1)} A`], ["RSSI", t.rssi ?? "—"], ["PI °C", t.rpi?.cpu_temp ?? "—"], ["PI LOAD", t.rpi?.load ?? "—"]];
    $("tele-grid").innerHTML = rows.map(([k, v]) => `<div><i>${k}</i><span>${v}</span></div>`).join("");
    updateMap(t);
  }
  setInterval(() => { if (S.droneOnline && performance.now() - S.lastTeleAt > 4000) { setPill("pill-drone", "warn", "Stale"); } }, 1000);

  // ------------------------------------------------------------------ map
  const map = L.map("map", { zoomControl: false, attributionControl: false }).setView([23.0225, 72.5714], 16);
  const street = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19 });
  const sat = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { maxZoom: 19 });
  street.addTo(map); let satOn = false;
  $("btn-layer").onclick = () => { satOn = !satOn; if (satOn) { map.removeLayer(street); sat.addTo(map); } else { map.removeLayer(sat); street.addTo(map); } $("btn-layer").textContent = satOn ? "Street" : "Satellite"; };
  const droneIcon = L.divIcon({ className: "drone-icon", iconSize: [34, 34], iconAnchor: [17, 17], html: `<div id="drone-rot" style="transform:rotate(0deg)"><svg viewBox="0 0 34 34" width="34" height="34"><path d="M17 3 L24 27 L17 22 L10 27 Z" fill="#0a66c2" stroke="#fff" stroke-width="1.5"/></svg></div>` });
  const droneMarker = L.marker([0, 0], { icon: droneIcon, zIndexOffset: 1000 }).addTo(map);
  const homeMarker = L.marker([0, 0], { icon: L.divIcon({ className: "", html: '<div class="home-icon">H</div>', iconSize: [20, 20], iconAnchor: [10, 10] }) });
  const trail = L.polyline([], { color: "#0a66c2", weight: 2.5, opacity: .8 }).addTo(map);
  const wpLine = L.polyline([], { color: "#b86e00", weight: 2, dashArray: "6 6" }).addTo(map);
  const wpLayer = L.layerGroup().addTo(map);
  let gotoMarker = null, firstFix = true;
  function updateMap(t) {
    if (!t.lat && !t.lon) return;
    const ll = [t.lat, t.lon];
    droneMarker.setLatLng(ll); const rot = document.getElementById("drone-rot"); if (rot) rot.style.transform = `rotate(${t.heading}deg)`;
    if (t.home) { homeMarker.setLatLng([t.home.lat, t.home.lon]); if (!map.hasLayer(homeMarker)) homeMarker.addTo(map); }
    const last = S.trail[S.trail.length - 1];
    if (!last || Math.abs(last[0] - ll[0]) > 1e-6 || Math.abs(last[1] - ll[1]) > 1e-6) { S.trail.push(ll); if (S.trail.length > 2000) S.trail.shift(); trail.setLatLngs(S.trail); }
    if (firstFix) { map.setView(ll, 17); firstFix = false; } else if (S.follow) map.panTo(ll, { animate: false });
  }
  $("btn-center").onclick = () => { if (S.tele) map.setView([S.tele.lat, S.tele.lon], Math.max(map.getZoom(), 16)); };
  $("btn-follow").onclick = () => { S.follow = !S.follow; $("btn-follow").classList.toggle("active", S.follow); };
  $("btn-plan").onclick = () => { S.planMode = !S.planMode; $("btn-plan").classList.toggle("active", S.planMode); $("map-hint").textContent = S.planMode ? "Plan mode: tap the map to add a waypoint" : "Right-click or long-press the map to fly there"; };
  map.on("dragstart", () => { if (S.follow) $("btn-follow").click(); });
  map.on("click", e => { if (!S.planMode) return; S.waypoints.push({ lat: +e.latlng.lat.toFixed(7), lon: +e.latlng.lng.toFixed(7), alt: +$("wp-alt").value }); renderWaypoints(); });
  map.on("contextmenu", e => {
    const alt = +$("goto-alt").value;
    if (!confirm(`Fly to ${e.latlng.lat.toFixed(5)}, ${e.latlng.lng.toFixed(5)} at ${alt} m AGL?`)) return;
    cmd("goto", { lat: e.latlng.lat, lon: e.latlng.lng, alt });
    if (gotoMarker) map.removeLayer(gotoMarker);
    gotoMarker = L.circleMarker(e.latlng, { radius: 7, color: "#39d98a", fillOpacity: .6 }).addTo(map);
    log(`GOTO ${e.latlng.lat.toFixed(5)}, ${e.latlng.lng.toFixed(5)} @ ${alt} m`);
  });

  // ------------------------------------------------------------------ mission
  function renderWaypoints() {
    wpLayer.clearLayers();
    S.waypoints.forEach((w, i) => L.marker([w.lat, w.lon], { icon: L.divIcon({ className: "", html: `<div class="wp-icon">${i + 1}</div>`, iconSize: [22, 22], iconAnchor: [11, 11] }), draggable: true })
      .on("dragend", ev => { const p = ev.target.getLatLng(); w.lat = +p.lat.toFixed(7); w.lon = +p.lng.toFixed(7); renderWaypoints(); }).addTo(wpLayer));
    wpLine.setLatLngs(S.waypoints.map(w => [w.lat, w.lon]));
    const ol = $("wp-list");
    ol.innerHTML = S.waypoints.length ? S.waypoints.map((w, i) => `<li><span>${w.lat.toFixed(5)}, ${w.lon.toFixed(5)}</span><span>${w.alt} m <button data-del="${i}">✕</button></span></li>`).join("") : '<li class="muted">Turn on Plan and tap the map to add waypoints.</li>';
    ol.querySelectorAll("[data-del]").forEach(b => b.onclick = () => { S.waypoints.splice(+b.dataset.del, 1); renderWaypoints(); });
  }
  $("btn-upload").onclick = () => { if (!S.waypoints.length) return log("No waypoints to upload", "err"); cmd("mission_upload", { waypoints: S.waypoints, takeoff_alt: +$("takeoff-alt").value, rtl_at_end: $("wp-rtl").checked }); log(`Uploading ${S.waypoints.length} waypoints…`); };
  $("btn-start").onclick = () => { if (confirm("Start AUTO mission now?")) cmd("mission_start"); };
  $("btn-clearwp").onclick = () => { S.waypoints = []; renderWaypoints(); cmd("mission_clear"); };
  $("btn-savewp").onclick = () => { localStorage.setItem("vayuveer.mission", JSON.stringify(S.waypoints)); log(`Saved ${S.waypoints.length} waypoints locally`, "ok"); };
  $("btn-loadwp").onclick = () => { try { S.waypoints = JSON.parse(localStorage.getItem("vayuveer.mission") || "[]"); renderWaypoints(); log(`Loaded ${S.waypoints.length} waypoints`, "ok"); } catch { } };

  // ------------------------------------------------------------------ flight buttons
  let armTimer = null;
  $("btn-arm").onclick = function () {
    if (!this.classList.contains("confirm")) { this.classList.add("confirm"); this.textContent = "Confirm arm"; armTimer = setTimeout(() => { this.classList.remove("confirm"); this.textContent = "Arm"; }, 4000); return; }
    clearTimeout(armTimer); this.classList.remove("confirm"); this.textContent = "Arm"; cmd("arm", { confirm: true }); log("ARM requested");
  };
  $("btn-disarm").onclick = () => { cmd("disarm"); log("DISARM requested"); };
  $("btn-takeoff").onclick = () => { const alt = +$("takeoff-alt").value; if (confirm(`Take off to ${alt} m AGL?`)) { cmd("takeoff", { alt }); log(`TAKEOFF ${alt} m`); } };
  $("btn-land").onclick = () => { cmd("land"); log("LAND"); };
  $("btn-rtl").onclick = () => { cmd("rtl"); log("RTL"); };
  $("btn-loiter").onclick = () => { cmd("mode", { mode: "LOITER" }); log("HOLD (LOITER)"); };
  $("btn-sethome").onclick = () => cmd("set_home");
  $("takeoff-alt").oninput = e => ($("takeoff-alt-lbl").textContent = `${e.target.value} m`);
  $("mode-select").onchange = e => { cmd("mode", { mode: e.target.value }); log(`MODE ${e.target.value}`); };
  $("stick-gain").oninput = e => ($("stick-gain-lbl").textContent = `${Math.round(e.target.value * 100)}%`);
  // emergency stop: hold 2 s
  const kill = $("btn-kill"); let killT = null;
  const killStart = e => { e.preventDefault(); kill.classList.add("holding"); killT = setTimeout(() => { cmd("kill", { confirm: "KILL" }); log("EMERGENCY STOP SENT", "err"); killEnd(); }, 2000); };
  const killEnd = () => { kill.classList.remove("holding"); clearTimeout(killT); killT = null; };
  kill.addEventListener("pointerdown", killStart); kill.addEventListener("pointerup", killEnd); kill.addEventListener("pointerleave", killEnd); kill.addEventListener("pointercancel", killEnd);

  // ------------------------------------------------------------------ payload
  const gp = $("gimbal-pitch"), gy = $("gimbal-yaw"); let gimbalT = null;
  const sendGimbal = () => { $("gimbal-pitch-lbl").textContent = `${gp.value}°`; $("gimbal-yaw-lbl").textContent = `${gy.value}°`; clearTimeout(gimbalT); gimbalT = setTimeout(() => cmd("gimbal", { pitch: +gp.value, yaw: +gy.value }), 120); };
  gp.oninput = gy.oninput = sendGimbal;
  $("vid-fps").oninput = e => { $("vid-fps-lbl").textContent = e.target.value; clearTimeout(gimbalT); gimbalT = setTimeout(() => cmd("camera", { fps: +e.target.value }), 300); };
  $("vid-enabled").onchange = e => cmd("camera", { enabled: e.target.checked });

  // ------------------------------------------------------------------ joysticks (nipplejs) + keyboard
  function mkStick(el, onMove) {
    const m = nipplejs.create({ zone: el, mode: "static", position: { left: "50%", top: "50%" }, color: "#ffffff", size: 110, restJoystick: true });
    m.on("move", (_, d) => { const f = Math.min(d.force, 1); const a = d.angle.radian; onMove(Math.cos(a) * f, Math.sin(a) * f); });
    m.on("end", () => onMove(0, 0));
    return m;
  }
  mkStick($("stick-left"), (x, y) => { S.stick.yaw = x; S.stick.throttle = y; stickChanged(); });
  mkStick($("stick-right"), (x, y) => { S.stick.roll = x; S.stick.pitch = y; stickChanged(); });
  const keyMap = { w: ["pitch", 1], s: ["pitch", -1], a: ["roll", -1], d: ["roll", 1], q: ["yaw", -1], e: ["yaw", 1], r: ["throttle", 1], f: ["throttle", -1] };
  window.addEventListener("keydown", ev => { if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT") return; const k = ev.key.toLowerCase(); if (keyMap[k]) { S.keys.add(k); ev.preventDefault(); applyKeys(); } if (k === " ") { cmd("manual_stop"); cmd("mode", { mode: "LOITER" }); } });
  window.addEventListener("keyup", ev => { const k = ev.key.toLowerCase(); if (keyMap[k]) { S.keys.delete(k); applyKeys(); } });
  function applyKeys() { const v = { pitch: 0, roll: 0, yaw: 0, throttle: 0 }; S.keys.forEach(k => { const [axis, sign] = keyMap[k]; v[axis] += sign; }); Object.assign(S.stick, v); stickChanged(); }
  function stickChanged() {
    const active = Object.values(S.stick).some(v => Math.abs(v) > 0.05);
    if (active) { S.stickActive = true; sendManual(); }
    else if (S.stickActive) { S.stickActive = false; sendManual(); cmd("manual_stop"); }
  }
  function sendManual() { const g = +$("stick-gain").value; cmd("manual", { pitch: S.stick.pitch * g, roll: S.stick.roll * g, yaw: S.stick.yaw * g, throttle: S.stick.throttle * g }); }
  // 10 Hz sender runs in a Web Worker: page timers get throttled to 1 Hz when the tab is
  // backgrounded, which would trip the agent's stale-stick watchdog mid-flight.
  try {
    const tick = new Worker(URL.createObjectURL(new Blob(["setInterval(() => postMessage(1), 100)"], { type: "text/javascript" })));
    tick.onmessage = () => { if (S.stickActive) sendManual(); };
  } catch { setInterval(() => { if (S.stickActive) sendManual(); }, 100); }
  document.addEventListener("visibilitychange", () => { if (document.hidden && S.stickActive) { S.keys.clear(); Object.assign(S.stick, { pitch: 0, roll: 0, yaw: 0, throttle: 0 }); stickChanged(); log("Page hidden - joystick released", "s4"); } });

  // ------------------------------------------------------------------ session / login
  const dlg = $("settings"), loginErr = $("login-error");
  function applySession(m) {
    S.role = m.role || "operator";
    document.body.classList.toggle("viewer", S.role !== "operator");
    $("user-name").textContent = m.user === "master" ? "master token" : `${m.user} · ${S.role}`;
    fillDroneSelect(m.drones || []);
  }
  async function fillDroneSelect(allowed) {
    const sel = $("drone-select"); const cur = cfg.drone;
    let ids = allowed.filter(d => d !== "*");
    try { const r = await fetch(`${httpBase()}/api/drones?token=${encodeURIComponent(cfg.token)}`); if (r.ok) (await r.json()).drones.forEach(d => { if (!ids.includes(d.id)) ids.push(d.id); }); } catch { }
    if (cur && !ids.includes(cur)) ids.unshift(cur);
    sel.innerHTML = ids.map(id => `<option value="${id}" ${id === cur ? "selected" : ""}>${id}</option>`).join("") + `<option value="__other">Other…</option>`;
  }
  $("drone-select").onchange = e => {
    let id = e.target.value;
    if (id === "__other") { id = prompt("Aircraft ID:", cfg.drone) || cfg.drone; }
    if (id && id !== cfg.drone) { localStorage.setItem("vayuveer.drone", id); resetTrack(); connect(); }
    e.target.value = cfg.drone;
  };
  function resetTrack() { S.trail = []; trail.setLatLngs([]); firstFix = true; }
  function openSettings(msg = "") {
    $("cfg-url").value = cfg.url; $("cfg-token").value = cfg.manualToken; $("cfg-drone").value = cfg.drone;
    $("cfg-user").value = (cfg.user && cfg.user.user) || ""; $("cfg-pass").value = ""; loginErr.textContent = msg;
    $("btn-logout").classList.toggle("hidden", !cfg.session);
    if (!dlg.open) dlg.showModal();
  }
  $("btn-settings").onclick = () => openSettings();
  $("btn-cancel").onclick = () => dlg.close("cancel");
  $("btn-logout").onclick = () => { cfg.clearSession(); if (S.ws) S.ws.close(); $("user-name").textContent = ""; openSettings("Signed out."); };
  $("login-form").addEventListener("submit", async ev => {
    ev.preventDefault();
    const user = $("cfg-user").value.trim(), pass = $("cfg-pass").value;
    cfg.save($("cfg-url").value, $("cfg-token").value, $("cfg-drone").value);
    if (user) {
      try {
        const r = await fetch(`${httpBase()}/api/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: user, password: pass }) });
        const j = await r.json();
        if (!r.ok) { loginErr.textContent = j.error || "Sign-in failed"; return; }
        cfg.setSession(j.token, j);
        if (!cfg.drone || (j.drones.length && !j.drones.includes("*") && !j.drones.includes(cfg.drone))) localStorage.setItem("vayuveer.drone", j.drones.find(d => d !== "*") || cfg.drone || "");
      } catch { loginErr.textContent = "Cannot reach the relay server"; return; }
    } else if (!cfg.manualToken) { loginErr.textContent = "Enter a username and password"; return; }
    else { cfg.clearSession(); if (!cfg.drone) localStorage.setItem("vayuveer.drone", "vayuveer-01"); }
    dlg.close("ok"); resetTrack(); connect();
  });

  // ------------------------------------------------------------------ boot
  const qp = new URLSearchParams(location.search);
  if (qp.get("token")) { cfg.clearSession(); cfg.save(qp.get("url") || "", qp.get("token"), qp.get("drone") || cfg.drone || "vayuveer-01"); history.replaceState(null, "", location.pathname); }
  else if (qp.get("drone")) { localStorage.setItem("vayuveer.drone", qp.get("drone")); history.replaceState(null, "", location.pathname); }
  if ("serviceWorker" in navigator && location.protocol === "https:") navigator.serviceWorker.register("/sw.js").catch(() => { });
  renderWaypoints(); connect();
})();
