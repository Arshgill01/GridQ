// GridQ control panel. Plain JS, no dependencies, works offline.
// Every number on screen comes from the backend (SSE /api/stream, /api/bench, /api/meta, /api/tune).
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const SVGNS = "http://www.w3.org/2000/svg";
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const fmt = (x, d = 1) => (x == null || Number.isNaN(x) ? "—" : Number(x).toFixed(d));
const rs = (x) => (x == null || Number.isNaN(x) ? "—" : "₹" + Math.round(x).toLocaleString("en-IN"));
const rsk = (x) => (x == null ? "—" : "₹" + (x / 1000).toLocaleString("en-IN", { maximumFractionDigits: 1, minimumFractionDigits: 1 }) + "k");
const clockOf = (step) => { const sod = ((step % 96) + 96) % 96; return `${String(Math.floor(sod / 4)).padStart(2, "0")}:${String((sod % 4) * 15).padStart(2, "0")}`; };
const hm = (h) => { const H = Math.floor(h); const M = Math.round((h - H) * 60); return M === 60 ? `${H + 1}h 00m` : `${H}h ${String(M).padStart(2, "0")}m`; };
const NAMES = { clinic: "Clinic", pump: "Water pump", hostel_a: "Hostel A", hostel_b: "Hostel B", academic: "Academic", ev: "EV bay", heater: "Heater" };
const PRIO = { clinic: 1, pump: 2, hostel_a: 3, hostel_b: 3, academic: 3, ev: 4, heater: 5 };
let S = null, BENCH = null, META = null, FOLD = "winter";

function el(tag, attrs, parent, text) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (text != null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}
const store = { get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} } };

// ---------------------------------------------------------------- theme + tabs
(function theme() {
  const t = store.get("gq-theme");
  if (t) document.documentElement.dataset.theme = t;
  $("#theme").addEventListener("click", () => {
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    const cur = document.documentElement.dataset.theme || (dark ? "dark" : "light");
    const nxt = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = nxt;
    store.set("gq-theme", nxt);
    SLD.layout = null;
    if (S) render(S);
    if (BENCH) drawProof();
  });
})();
$$(".tab").forEach((b) => b.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.toggle("on", x === b));
  $$(".tabpanel").forEach((p) => p.classList.toggle("on", p.id === "tab-" + b.dataset.tab));
  store.set("gq-tab", b.dataset.tab);
  if (b.dataset.tab === "proof") loadProof();
  if (S) render(S);
}));
{ const t = store.get("gq-tab"); if (t && $(`.tab[data-tab="${t}"]`)) $(`.tab[data-tab="${t}"]`).click(); }

// ---------------------------------------------------------------- API
async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  return r.json();
}
function flash(btn) { btn.classList.add("flash"); setTimeout(() => btn.classList.remove("flash"), 450); }
$$("[data-ev]").forEach((b) => b.addEventListener("click", () => { flash(b); post("/api/events", { type: b.dataset.ev, source: "dashboard" }); }));
$("#b-kill").addEventListener("click", () => post("/api/demo/kill-brain"));
$("#b-cable").addEventListener("click", (e) => { flash(e.currentTarget); post("/api/dev/cable", {}); });
$("#b-pause").addEventListener("click", () => post(S && S.paused ? "/api/sim/play" : "/api/sim/pause"));
$("#b-step").addEventListener("click", () => post("/api/sim/step"));
$("#b-reset").addEventListener("click", (e) => {        // two-stage guard instead of a modal confirm
  const b = e.currentTarget;
  if (b.classList.contains("armed")) { b.classList.remove("armed"); b.textContent = "RESET"; post("/api/sim/reset"); return; }
  b.classList.add("armed"); b.textContent = "CONFIRM?";
  setTimeout(() => { b.classList.remove("armed"); b.textContent = "RESET"; }, 3000);
});
$$("#speeds button").forEach((b) => b.addEventListener("click", () => post("/api/sim/speed", { x: Number(b.dataset.x) })));
$("#t-sky").addEventListener("change", (e) => post("/api/live/sky", { enabled: e.target.checked }));
$("#t-heat").addEventListener("change", (e) => post("/api/live/heat", { enabled: e.target.checked }));
$("#b-replay").addEventListener("click", replay);
$$("#folds button").forEach((b) => b.addEventListener("click", () => {
  FOLD = b.dataset.fold; $$("#folds button").forEach((x) => x.classList.toggle("on", x === b)); loadProof();
}));

// A browser allows ~6 open connections per address, and each tab's stream holds one. Background tabs
// let go of theirs, so a pile of old tabs can't leave a new one hanging or frozen.
let ES = null;
function connect() {
  if (ES || document.hidden) return;
  const es = ES = new EventSource("/api/stream");
  es.addEventListener("state", (e) => { S = JSON.parse(e.data); render(S); });
  es.onerror = () => {
    es.close(); if (ES === es) ES = null;
    ANN.linkLost = true; if (S) renderAnn(S); setTimeout(connect, 1500);
  };
  es.onopen = () => { ANN.linkLost = false; };
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { if (ES) { ES.close(); ES = null; } }
  else connect();
});

// ---------------------------------------------------------------- annunciator
const WINS = [
  { id: "grid", label: "Grid lost", c: "red", on: (s) => !s.grid.up },
  { id: "cable", label: "Cable pulled", c: "red", on: (s) => s.grid.cable_present === false },
  { id: "island", label: "Islanded", c: "amber", on: (s) => s.grid.relay_islanded },
  { id: "dg", label: "DG running", c: "amber", on: (s) => s.flows.dg > 0.05 },
  { id: "shed", label: "Load shed", c: "red", on: (s) => s.feeders.some((f) => f.demand_kw != null && f.state !== "ON") },
  { id: "defer", label: "Load deferred", c: "green", on: (s) => s.feeders.some((f) => f.state === "DEFERRED") },
  { id: "battlow", label: "Battery low", c: "amber", on: (s) => s.battery.soc_frac < 0.2 },
  { id: "fault", label: "Battery fault", c: "amber", on: (s) => s.battery.fault },
  { id: "peak", label: "Peak tariff", c: "amber", on: (s) => s.band === "peak" },
  { id: "safe", label: "SAFE mode", c: "red", on: (s) => s.mode === "SAFE" },
  { id: "brain", label: "Brain down", c: "red", on: (s) => s.brain.enabled && !s.brain.alive },
  { id: "link", label: "Link lost", c: "red", on: (s) => !s.mcu.ok || ANN.linkLost },
];
const ANN = { state: {}, test: false, linkLost: false };
(function buildAnn() {
  const box = $("#ann");
  WINS.forEach((w) => { const d = document.createElement("div"); d.className = "win " + w.c; d.id = "win-" + w.id; d.textContent = w.label; box.appendChild(d); ANN.state[w.id] = { on: false, acked: true }; });
  $("#ack").addEventListener("click", () => { for (const k in ANN.state) ANN.state[k].acked = true; if (S) renderAnn(S); });
  $("#lamptest").addEventListener("click", lampTest);
})();
function lampTest() {        // as on a real annunciator: every window lights, in sequence, then returns to service
  ANN.test = true;
  WINS.forEach((w, i) => setTimeout(() => { $("#win-" + w.id).classList.add("on"); }, i * 55));
  setTimeout(() => { ANN.test = false; if (S) renderAnn(S); else WINS.forEach((w) => $("#win-" + w.id).classList.remove("on")); }, WINS.length * 55 + 900);
}
function renderAnn(s) {
  if (ANN.test) return;
  let unacked = false;
  WINS.forEach((w) => {
    const st = ANN.state[w.id];
    let on = false;
    try { on = !!w.on(s); } catch (e) {}
    if (on && !st.on) st.acked = w.c === "green";       // a new alarm flashes until acknowledged
    if (!on) st.acked = true;
    st.on = on;
    const d = $("#win-" + w.id);
    d.classList.toggle("on", on);
    d.classList.toggle("blink", on && !st.acked);
    unacked = unacked || (on && !st.acked);
  });
  $("#ack").classList.toggle("ack-needed", unacked);
}

// ---------------------------------------------------------------- single-line diagram
const SLD = { layout: null, refs: {} };
const FEEDERS = ["clinic", "pump", "hostel_a", "hostel_b", "academic", "ev", "heater"];
const speedClass = (kw) => (kw < 4 ? "s1" : kw < 12 ? "s2" : kw < 25 ? "s3" : "s4");

function buildSLD(mode) {
  const box = $("#sld");
  box.innerHTML = "";
  const R = (SLD.refs = { src: {}, fd: {} });
  const narrow = mode === "narrow";
  const W = narrow ? 360 : 1000;
  const H = narrow ? 172 + FEEDERS.length * 46 : 400;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Single-line diagram of the campus microgrid" }, box);
  const defs = el("defs", {}, svg);
  const pat = el("pattern", { id: "hatch", width: 4, height: 4, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, defs);
  el("rect", { width: 4, height: 4, fill: css("--panel") }, pat);
  el("line", { x1: 0, y1: 0, x2: 0, y2: 4, stroke: css("--ink"), "stroke-width": 2.2 }, pat);

  const SRC = [
    { k: "grid", name: "Grid", sub: "40 kW tie", color: "--grid" },
    { k: "pv", name: "Solar", sub: "30 kWp", color: "--pv" },
    { k: "batt", name: "Battery", sub: "50 kWh", color: "--batt" },
    { k: "dg", name: "Diesel", sub: "24 kW", color: "--dg" },
  ];
  const busY = narrow ? 128 : 182;
  const sx = (i) => (narrow ? 45 + i * 90 : 125 + i * 250);
  const symY = narrow ? 60 : 70;
  const brkY = narrow ? 104 : 136;
  SRC.forEach((d, i) => {
    const x = sx(i);
    const g = el("g", {}, svg);
    el("text", { x, y: narrow ? 13 : 18, "text-anchor": "middle", class: "lbl" }, g, d.name);
    if (!narrow) el("text", { x, y: 32, "text-anchor": "middle", class: "lbl-s" }, g, d.sub);
    const wire = el("path", { d: `M${x},${symY + 22} V${busY}`, class: "wire" }, g);
    const flow = el("path", { d: `M${x},${symY + 22} V${busY}`, class: "flow", stroke: css(d.color) }, g);
    drawSourceSymbol(g, d.k, x, symY, R);
    const brk = el("rect", { x: x - 7, y: brkY - 7, width: 14, height: 14, class: "brk" }, g);
    // wide: value beside the breaker; narrow: value under the name (columns are only 90 px)
    const val = narrow ? el("text", { x, y: 28, "text-anchor": "middle", class: "lbl-s" }, g, "—")
                       : el("text", { x: x + 14, y: brkY + 4, "text-anchor": "start", class: "val" }, g, "—");
    R.src[d.k] = { wire, flow, brk, val, g };
  });
  // relay tag on the grid tie (the real relay on D2)
  const gx = sx(0);
  const rt = el("g", { class: "tag-CLOSED" }, svg);
  const rtb = el("rect", { x: narrow ? gx - 30 : gx - 98, y: brkY - 9, width: 74, height: 18, class: "tag-bg" }, rt);
  const rtt = el("text", { x: (narrow ? gx - 30 : gx - 98) + 37, y: brkY + 4, "text-anchor": "middle", class: "tag" }, rt, "K1 CLOSED");
  if (narrow) { rtb.setAttribute("x", 2); rtb.setAttribute("y", busY + 8); rtt.setAttribute("x", 39); rtt.setAttribute("y", busY + 21); }
  R.relay = { g: rt, t: rtt };
  if (!narrow) el("text", { x: gx - 61, y: brkY + 22, "text-anchor": "middle", class: "lbl-s" }, svg, "relay · MCU D2");
  // bus
  R.bus = el("line", { x1: narrow ? 16 : 40, x2: W - (narrow ? 16 : 40), y1: busY, y2: busY, class: "bus" }, svg);
  el("text", { x: W - (narrow ? 16 : 40), y: narrow ? busY + 18 : busY - 10, "text-anchor": "end", class: "lbl-s" }, svg, "LV BUS · SIMULATED");

  if (!narrow) {
    const fx = (i) => 40 + (920 / FEEDERS.length) * (i + 0.5);
    FEEDERS.forEach((f, i) => {
      const x = fx(i);
      const g = el("g", {}, svg);
      const wire = el("path", { d: `M${x},${busY} V${busY + 92}`, class: "wire" }, g);
      const flow = el("path", { d: `M${x},${busY} V${busY + 92}`, class: "flow", stroke: css("--ink") }, g);
      const brk = el("rect", { x: x - 7, y: busY + 34, width: 14, height: 14, class: "brk" }, g);
      el("text", { x: x - 12, y: busY + 45, "text-anchor": "end", class: "lbl-s" }, g, `P${PRIO[f]}`);
      el("path", { d: `M${x - 11},${busY + 92} L${x + 11},${busY + 92} L${x},${busY + 110} Z`, class: "sym" }, g);
      el("text", { x, y: busY + 132, "text-anchor": "middle", class: "lbl" }, g, NAMES[f]);
      const sub = el("text", { x, y: busY + 146, "text-anchor": "middle", class: "lbl-s" }, g, "");
      const val = el("text", { x, y: busY + 166, "text-anchor": "middle", class: "val" }, g, "—");
      const tg = el("g", { class: "tag-IDLE" }, g);
      const tb = el("rect", { x: x - 40, y: busY + 176, width: 80, height: 19, class: "tag-bg" }, tg);
      const tt = el("text", { x, y: busY + 189.5, "text-anchor": "middle", class: "tag" }, tg, "—");
      R.fd[f] = { wire, flow, brk, val, sub, tg, tb, tt, x, w: 80 };
    });
  } else {
    const rx = 24, y0 = busY + 44;
    const last = y0 + (FEEDERS.length - 1) * 46;
    R.riser = el("path", { d: `M${rx},${busY} V${last}`, class: "wire" }, svg);
    R.riserFlow = el("path", { d: `M${rx},${busY} V${last}`, class: "flow", stroke: css("--ink") }, svg);
    FEEDERS.forEach((f, i) => {
      const y = y0 + i * 46;
      const g = el("g", {}, svg);
      const wire = el("path", { d: `M${rx},${y} H${rx + 54}`, class: "wire" }, g);
      const flow = el("path", { d: `M${rx},${y} H${rx + 54}`, class: "flow", stroke: css("--ink") }, g);
      const brk = el("rect", { x: rx + 20, y: y - 7, width: 14, height: 14, class: "brk" }, g);
      el("path", { d: `M${rx + 54},${y - 9} L${rx + 54},${y + 9} L${rx + 68},${y} Z`, class: "sym" }, g);
      el("text", { x: rx + 78, y: y - 3, class: "lbl" }, g, NAMES[f]);
      const val = el("text", { x: rx + 78, y: y + 13, class: "val" }, g, "—");
      const sub = el("text", { x: W - 16, y: y + 22, "text-anchor": "end", class: "lbl-s" }, g, "");
      const tg = el("g", { class: "tag-IDLE" }, g);
      const tb = el("rect", { x: W - 16 - 84, y: y - 10, width: 84, height: 19, class: "tag-bg" }, tg);
      const tt = el("text", { x: W - 16 - 42, y: y + 3.5, "text-anchor": "middle", class: "tag" }, tg, "—");
      R.fd[f] = { wire, flow, brk, val, sub, tg, tb, tt, x: W - 16 - 42, w: 84, right: W - 16, narrow: true };
    });
  }
  SLD.layout = mode;
}

function drawSourceSymbol(g, k, x, y, R) {
  if (k === "grid") {                       // transformer: two coupled windings
    el("circle", { cx: x, cy: y - 9, r: 15, class: "sym" }, g);
    el("circle", { cx: x, cy: y + 9, r: 15, class: "sym", "fill-opacity": 0.6 }, g);
  } else if (k === "pv") {                  // PV module
    el("rect", { x: x - 24, y: y - 16, width: 48, height: 32, class: "sym" }, g);
    for (let i = 1; i < 4; i++) el("line", { x1: x - 24 + i * 12, x2: x - 24 + i * 12, y1: y - 16, y2: y + 16, stroke: css("--ink"), "stroke-width": 1 }, g);
    el("line", { x1: x - 24, x2: x + 24, y1: y, y2: y, stroke: css("--ink"), "stroke-width": 1 }, g);
  } else if (k === "batt") {                // battery with live SoC fill
    el("rect", { x: x - 26, y: y - 16, width: 52, height: 32, class: "soc-bg", rx: 2 }, g);
    el("rect", { x: x + 26, y: y - 6, width: 5, height: 12, class: "ink" }, g);
    R.soc = el("rect", { x: x - 23, y: y - 13, width: 0, height: 26, class: "soc-fill" }, g);
    R.socX = x - 23; R.socW = 46;
    el("line", { x1: x - 23 + 4.6, x2: x - 23 + 4.6, y1: y - 16, y2: y + 16, class: "soc-floor" }, g);
    R.socTxt = el("text", { x, y: y + 4, "text-anchor": "middle", class: "val" }, g, "—");
  } else if (k === "dg") {                  // generator
    el("circle", { cx: x, cy: y, r: 20, class: "sym" }, g);
    el("text", { x, y: y + 5, "text-anchor": "middle", class: "lbl" }, g, "G~");
  }
}

function setFlow(path, kw, rev) {
  const live = kw > 0.05;
  path.classList.toggle("live", live);
  path.classList.toggle("rev", !!rev);
  ["s1", "s2", "s3", "s4"].forEach((c) => path.classList.toggle(c, live && c === speedClass(kw)));
}

function renderSLD(s) {
  const mode = $("#sld").clientWidth < 620 ? "narrow" : "wide";
  if (SLD.layout !== mode) buildSLD(mode);
  const R = SLD.refs, f = s.flows;
  const pvUsed = Math.max(0, f.pv - f.curt);
  const bk = s.battery.kw;
  const vals = {
    grid: [s.grid.up ? f.grid : 0, false, s.grid.up ? `${fmt(f.grid)} kW` : "DOWN"],
    pv: [pvUsed, false, `${fmt(pvUsed)} kW${f.curt > 0.05 ? " · " + fmt(f.curt) + " cut" : ""}`],
    batt: [Math.abs(bk), bk < 0, bk > 0.05 ? `${fmt(bk)} kW out` : bk < -0.05 ? `${fmt(-bk)} kW in` : "idle"],
    dg: [f.dg, false, f.dg > 0.05 ? `${fmt(f.dg)} kW · ${fmt(s.tank_l, 0)} L` : `off · ${fmt(s.tank_l, 0)} L`],
  };
  for (const k in vals) {
    const r = R.src[k], [kw, rev, txt] = vals[k];
    setFlow(r.flow, kw, rev);
    r.wire.classList.toggle("dead", k === "grid" ? !s.grid.up : kw <= 0.05);
    r.val.textContent = txt;
    const closed = k === "grid" ? !s.grid.relay_islanded : k === "batt" ? true : kw > 0.05;
    r.brk.classList.toggle("open", !closed);
  }
  if (R.soc) {
    R.soc.setAttribute("width", Math.max(0, R.socW * s.battery.soc_frac));
    R.socTxt.textContent = `${fmt(100 * s.battery.soc_frac, 0)}%`;
  }
  const isl = s.grid.relay_islanded;
  R.relay.g.setAttribute("class", isl ? "tag-OPEN" : "tag-CLOSED");
  R.relay.t.textContent = isl ? "K1 OPEN" : "K1 CLOSED";
  R.bus.classList.toggle("island", !s.grid.up);
  let total = 0;
  s.feeders.forEach((c) => {
    const r = R.fd[c.name];
    if (!r) return;
    total += c.kw;
    setFlow(r.flow, c.kw, false);
    r.wire.classList.toggle("dead", c.kw <= 0.05);
    r.brk.setAttribute("class", "brk" + (c.state === "ON" ? "" : c.state === "PARTIAL" ? " partial" : " open"));
    const label = c.state === "PARTIAL" ? `PARTIAL ${c.pct}%` : c.state;
    r.tt.textContent = label;
    const w = Math.max(58, 12 + label.length * 6.4);
    if (r.right) { r.tb.setAttribute("x", r.right - w); r.tt.setAttribute("x", r.right - w / 2); }
    else { r.tb.setAttribute("x", r.x - w / 2); }
    r.tb.setAttribute("width", w);
    r.tg.setAttribute("class", "tag-" + c.state);
    r.val.textContent = `${fmt(c.kw)} kW`;
    const extra = c.demand_kw != null ? `of ${fmt(c.demand_kw)} kW` : c.remaining_kwh > 0 ? `${fmt(c.remaining_kwh, 0)} kWh by ${c.deadline}` : "";
    r.sub.textContent = extra;
  });
  if (R.riserFlow) setFlow(R.riserFlow, total, false);
}

// ---------------------------------------------------------------- render
function render(s) {
  $("#clk").textContent = s.clock;
  $("#day").textContent = `DAY ${s.day + 1}${s.paused ? " · PAUSED" : ` · ${s.speed}×`}`;
  const band = $("#band");
  band.textContent = `${s.band} ₹${fmt(s.tariff, 2)}`;
  band.className = "band " + s.band;
  $("#v-lp").textContent = s.solve_ms != null ? `${fmt(s.solve_ms, 0)} ms` : "—";
  $("#v-hb").textContent = s.brain.alive ? `#${s.brain.hb}` : s.brain.respawn_in_s != null ? `dead · ${Math.ceil(s.brain.respawn_in_s)}s` : "—";
  $("#v-mcu").textContent = `${s.mcu.mode === "S" ? "SAFE" : "NORM"} ${String(s.mcu.applied_mask).padStart(3, " ")}`;
  $("#v-mcu").title = `bridge: ${s.mcu.bridge} · applied load mask ${s.mcu.applied_mask} (bit0 clinic … bit6 heater)`;
  renderAnn(s);
  $("#mode-line").textContent = `${s.mode} · control: ${s.control_source || "—"}`;
  if ($("#tab-live").classList.contains("on")) {
    renderSLD(s);
    const sv = $("#survival");
    sv.textContent = s.survival_batt_h >= 48 ? "> 48 h" : hm(s.survival_batt_h);
    sv.classList.toggle("low", s.survival_batt_h < 2);
    sv.classList.toggle("mid", s.survival_batt_h >= 2 && s.survival_batt_h < 6);
    $("#survival-dg").textContent = `With the diesel genset (${fmt(s.tank_l, 0)} L in tank): ${s.survival_h >= 48 ? "> 48 h" : hm(s.survival_h)}`;
    $("#price").textContent = s.shadow_price != null ? `₹${fmt(s.shadow_price, 1)}` : "—";
    $("#why-live").innerHTML = whyHtml(s.explanations, s);
    if (!hovering("#chart-hist")) histChart(s.hist);
  }
  if ($("#tab-plan").classList.contains("on") && s.plan) {
    $("#why-plan").innerHTML = whyHtml(s.explanations, s);
    if (!hovering("#chart-plan") && !hovering("#chart-soc")) planChart(s);
  }
  if ($("#tab-break").classList.contains("on")) renderBreak(s);
  const fl = [];
  if (s.data.fallback.demand) fl.push("SYNTHETIC demand: I-BLEND unavailable");
  if (s.data.fallback.solar) fl.push("synthetic solar for some days: NASA POWER missing");
  if (s.plan && s.plan.forecast_fallback) fl.push("demand forecast: seasonal-naive fallback");
  if (!s.mcu.ok) fl.push("MCU unreachable: plant on SAFE rules");
  $("#foot").innerHTML = (fl.length ? `<div class="flag">⚠ ${fl.join(" · ")}</div>` : "") +
    `<span class="mono">held-out day ${s.date} · demand: ${s.data.source || "?"} · solar: NASA POWER · bridge ${s.mcu.bridge} · forecaster ${s.plan ? s.plan.forecaster : "—"}${s.plan && s.plan.n_branches ? " · " + s.plan.n_branches + " outage branches" : ""}</span>`;
}
const hovering = (sel) => !!document.querySelector(sel + " .tip:not([hidden])");

const KIND = { price: "Price", defer: "Deferred", shed: "Trimmed", battery: "Battery", hedge: "Hedge", binding: "Limit" };
function whyHtml(items, s) {
  if (!items || !items.length) {
    return `<div class="empty">${s.mode === "SAFE" ? "Brain offline. The MCU holds the safe mask (clinic + pump) on its own until a new heartbeat arrives." : "Waiting for the first plan…"}</div>`;
  }
  return items.map((it) => {
    const t = (it.text || "").replace(/Rs ([\d,]+(\.\d+)?)(\/kWh)?/g, (m, n, _d, u) => `<span class="rs">₹${n}${u || ""}</span>`);
    return `<div class="w ${it.kind}"><span class="kind">${KIND[it.kind] || it.kind}</span><span>${t}</span></div>`;
  }).join("");
}

function renderBreak(s) {
  $("#b-outage").setAttribute("aria-pressed", String(!!s.grid.software_outage));
  $("#b-kill").classList.toggle("armed", s.brain.enabled && !s.brain.alive);
  $("#cable-ctl").hidden = s.mcu.bridge !== "mock";
  $("#b-pause").textContent = s.paused ? "PLAY" : "PAUSE";
  $$("#speeds button").forEach((b) => b.classList.toggle("on", Number(b.dataset.x) === s.speed));
  $("#t-sky").checked = s.live.sky;
  $("#t-heat").checked = s.live.heat;
  $("#sensor-last").textContent = s.live.sensor && s.live.sensor.kind
    ? `last: ${s.live.sensor.kind} = ${fmt(s.live.sensor.value, 1)} · PV ×${fmt(s.live.sky_mult, 2)} · hostels ×${fmt(s.live.heat_mult, 2)}`
    : "no sensor data from the clinic node yet";
  const ev = (s.events || []).slice().reverse();
  $("#events").innerHTML = ev.length ? ev.map((e) => `<li><span class="t">${clockOf(e.step)}</span><span class="ev">${e.type.replace("_", " ")} ${Object.entries(e.params || {}).map(([k, v]) => `${k}=${typeof v === "number" ? fmt(v, 2) : v}`).join(" ")}</span><span class="src">${e.source}</span></li>`).join("")
    : `<li><span class="empty">No events yet. Press something above.</span></li>`;
}

// ---------------------------------------------------------------- charts
// One y-axis per chart, thin marks, recessive rules on graph paper, crosshair tooltip.
function chart(box, spec) {
  const W = Math.max(300, box.clientWidth), H = box.clientHeight || 200;
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"></svg><div class="tip" hidden></div>`;
  const sv = box.firstChild, tip = box.lastChild;
  const pad = { l: 44, r: 12, t: 10, b: 22 }, n = spec.n;
  const ymin = spec.ymin || 0, ymax = spec.ymax;
  const x = (i) => pad.l + (W - pad.l - pad.r) * i / Math.max(1, n - 1);
  const y = (v) => pad.t + (H - pad.t - pad.b) * (1 - (v - ymin) / (ymax - ymin));
  const nt = spec.ticks || 4;
  for (let i = 0; i <= nt; i++) {
    const v = ymin + (ymax - ymin) * i / nt;
    el("line", { x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v), class: i === 0 ? "ax0" : "ax", "stroke-width": 1 }, sv);
    el("text", { x: pad.l - 6, y: y(v) + 3.5, "text-anchor": "end" }, sv, (spec.yfmt || ((u) => Math.round(u)))(v) + (i === nt ? spec.unit : ""));
  }
  const step = Math.max(1, Math.round(n / (W < 500 ? 4 : 8)));
  for (let i = 0; i < n; i += step) el("text", { x: x(i), y: H - 6, "text-anchor": i === 0 ? "start" : "middle" }, sv, spec.xlabel(i));
  let base = Array(n).fill(0);
  (spec.areas || []).forEach((a) => {
    const top = a.vals.map((v, i) => base[i] + Math.max(0, v || 0));
    const pts = top.map((v, i) => [x(i), y(v)]).concat(base.map((v, i) => [x(i), y(v)]).reverse());
    el("path", { d: pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join("") + "Z", fill: a.color, "fill-opacity": 0.9,
      stroke: css("--paper"), "stroke-width": 1.5, "stroke-linejoin": "round" }, sv);
    base = top;
  });
  (spec.lines || []).forEach((l) => el("path", { d: l.vals.map((v, i) => (i ? "L" : "M") + x(i).toFixed(1) + "," + y(v == null ? ymin : v).toFixed(1)).join(""),
    fill: "none", stroke: l.color, "stroke-width": l.width || 2, "stroke-dasharray": l.dash || "", "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" }, sv));
  const xh = el("line", { class: "xh", y1: pad.t, y2: H - pad.b, x1: 0, x2: 0, visibility: "hidden" }, sv);
  const hit = el("rect", { x: pad.l, y: 0, width: W - pad.l - pad.r, height: H, fill: "transparent" }, sv);
  const move = (ev) => {
    const r = sv.getBoundingClientRect();
    const px = ((ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left) * W / r.width;
    const i = Math.max(0, Math.min(n - 1, Math.round((px - pad.l) / ((W - pad.l - pad.r) / Math.max(1, n - 1)))));
    xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i)); xh.setAttribute("visibility", "visible");
    tip.innerHTML = spec.tip(i);
    tip.hidden = false;
    const left = x(i) * r.width / W;
    tip.style.left = (left + 14 + tip.offsetWidth > r.width ? left - 14 - tip.offsetWidth : left + 14) + "px";
    tip.style.top = "8px";
  };
  hit.addEventListener("mousemove", move);
  hit.addEventListener("touchmove", move, { passive: true });
  hit.addEventListener("mouseleave", () => { tip.hidden = true; xh.setAttribute("visibility", "hidden"); });
}
const swatch = (c) => `<span class="sw" style="background:${c}"></span>`;
const tipRows = (title, rows) => `<div class="t-h">${title}</div>` + rows.map(([c, l, v]) => `<div class="t-r"><span>${c ? swatch(c) : ""}${l}</span><span>${v}</span></div>`).join("");
// 4 gridlines: pick a round step so every tick label is the exact value it marks
const niceMax = (v) => 4 * [1, 2, 2.5, 5, 10, 15, 20, 25, 50, 100].find((st) => 4 * st >= v);

function histChart(hh) {
  if (!hh || !hh.k || hh.k.length < 2) return;
  const n = hh.k.length;
  const L = [["load", "Load served", "--ink", 2.2], ["pv", "Solar used", "--pv", 1.8], ["grid", "Grid import", "--grid", 1.8], ["dg", "Diesel", "--dg", 1.8]];
  chart($("#chart-hist"), {
    n, ymax: niceMax(Math.max(10, ...hh.load, ...hh.pv, ...hh.grid) * 1.1), unit: " kW", xlabel: (i) => clockOf(hh.k[i]),
    lines: L.map(([k, , c, w]) => ({ color: css(c), vals: hh[k], width: w })),
    tip: (i) => tipRows(clockOf(hh.k[i]), [...L.map(([k, l, c]) => [css(c), l, fmt(hh[k][i]) + " kW"]),
      [css("--batt"), "Battery", `${fmt(hh.soc[i], 0)} kWh · ${hh.batt[i] >= 0 ? "out " + fmt(hh.batt[i]) : "in " + fmt(-hh.batt[i])} kW`]]),
  });
  $("#hist-legend").innerHTML = L.map(([, l, c]) => `<span><i style="background:${css(c)}"></i>${l}</span>`).join("");
}

function planChart(s) {
  const h = s.plan.horizon, n = h.grid.length;
  const areas = [
    { label: "Solar used", color: css("--pv"), vals: h.pv_used },
    { label: "Battery out", color: css("--batt"), vals: h.dis },
    { label: "Grid", color: css("--grid"), vals: h.grid },
    { label: "Diesel", color: css("--dg"), vals: h.dg },
  ];
  const tot = h.grid.map((_, i) => areas.reduce((a, b) => a + Math.max(0, b.vals[i]), 0));
  chart($("#chart-plan"), {
    n, ymax: niceMax(Math.max(10, ...tot, ...h.load) * 1.1), unit: " kW", xlabel: (i) => clockOf(s.plan.step + i), areas,
    lines: [{ color: css("--ink"), vals: h.load, width: 2 }],
    tip: (i) => tipRows(`${clockOf(s.plan.step + i)} · plan`, [
      ...areas.map((a) => [a.color, a.label, fmt(a.vals[i]) + " kW"]),
      [css("--ink"), "Planned load", fmt(h.load[i]) + " kW"],
      ["", "Forecast demand P50", fmt(h.dem_p50[i]) + " kW"],
      ["", "EV · heater · pump", `${fmt(h.p_ev[i])} · ${fmt(h.p_heater[i])} · ${fmt(h.p_pump[i])}`],
      ["", "Battery charging", fmt(h.ch[i]) + " kW"]]),
  });
  $("#plan-legend").innerHTML = areas.map((a) => `<span><i style="background:${a.color}"></i>${a.label}</span>`).join("") +
    `<span><i style="background:${css("--ink")}"></i>Planned load, incl. the flexible loads it schedules</span>`;
  const cap = s.battery.cap_kwh;
  chart($("#chart-soc"), {
    n, ymax: cap, ticks: cap % 10 === 0 ? cap / 10 : 4, unit: " kWh", xlabel: (i) => clockOf(s.plan.step + i + 1),
    lines: [{ color: css("--batt"), vals: h.soc, width: 2.4 }, { color: css("--alarm"), vals: h.soc.map(() => 0.1 * cap), width: 1 }],
    tip: (i) => tipRows(clockOf(s.plan.step + i + 1), [[css("--batt"), "Battery", `${fmt(h.soc[i])} kWh · ${fmt(100 * h.soc[i] / cap, 0)}%`]]),
  });
  $("#plan-meta").textContent = `solved at ${clockOf(s.plan.step)} · ${s.plan.n_branches ? s.plan.n_branches + " outage branches · " : ""}${s.plan.forecaster}`;
}

// ---------------------------------------------------------------- proof
async function loadProof() {
  try {
    if (!BENCH || BENCH._fold !== FOLD) { const r = await fetch("/api/bench?fold=" + FOLD); BENCH = r.ok ? await r.json() : null; if (BENCH) BENCH._fold = FOLD; }
    if (!META || META._fold !== FOLD) { META = await (await fetch("/api/meta?fold=" + FOLD)).json(); META._fold = FOLD; }
  } catch (e) { BENCH = null; }
  drawProof();
}

function drawProof() {
  if (!BENCH || !BENCH.report) {
    $("#captured").textContent = "—";
    $("#proof-table").innerHTML = "<tr><td>No benchmark for this block yet: run <code>python -m gridq.bench.run_days</code></td></tr>";
    return;
  }
  const R = BENCH.report, C = BENCH.controllers, T = (c) => R[c].total_cost_rs.sum, n = R.GridQ.total_cost_rs.n;
  const bb = BENCH.best_baseline && BENCH.best_baseline.report;
  const capB1 = BENCH.captured_pct && BENCH.captured_pct.report_total_cost_rs;
  const best = bb ? bb.name : "B1";
  $("#captured").textContent = `${fmt(bb ? bb.captured_pct : capB1, 0)}%`;
  $("#captured-k").textContent = `of the achievable saving, captured vs the better baseline (${best})`;
  $("#captured-sub").innerHTML = `Over <b>${n}</b> held-out scenario-days, GridQ costs <b>${rs(T("GridQ"))}</b> against ${best}'s ${rs(T(best))}, saving <b>${rs(T(best) - T("GridQ"))}</b> ` +
    `(${fmt(100 * (T(best) - T("GridQ")) / T(best), 1)}%). The perfect-foresight Oracle bound is ${rs(T("Oracle"))}. By the spec's definition (vs B1, smart ToD rules) the figure is <b>${fmt(capB1, 0)}%</b>. ` +
    `Total cost of service = energy bill + diesel + demand charge + battery wear + unserved energy at its value of lost load.`;
  gauge(R, C, best);
  const col = { B0: css("--b0"), B1: css("--b1"), GridQ: css("--gq"), Oracle: css("--or") };
  const rows = [
    ["Total cost of service ₹/day", "total_cost_rs", 0], ["Energy bill + diesel ₹/day", "cost_rs", 0], ["Battery wear ₹/day", "battery_wear_rs", 0],
    ["Unserved energy kWh/day", "unserved_kwh_total", 1], ["Clinic uptime %", "critical_uptime_pct", 1], ["Diesel L/day", "diesel_l", 1],
    ["CO₂ kg/day", "co2_kg", 0], ["Peak import kW", "peak_grid_kw", 1], ["Solar self-use %", "solar_self_use_pct", 1],
    ["Fairness, Jain (1 = even)", "fairness_jain", 2], ["Deadline misses/day", "deadline_misses", 1],
  ];
  let h = `<tr class="h"><th>Metric</th>${C.map((c) => `<th class="${c === "GridQ" ? "gq" : ""}">${swatch(col[c])}${c}</th>`).join("")}</tr>`;
  rows.forEach(([lab, k, d]) => {
    h += `<tr><td>${lab}</td>` + C.map((c) => { const v = R[c] && R[c][k]; return v ? `<td class="num${c === "GridQ" ? " gq" : ""}">${fmt(v.median, d)}<span class="rng">${fmt(v.min, d)} – ${fmt(v.max, d)}</span></td>` : "<td>—</td>"; }).join("") + "</tr>";
  });
  const ex = BENCH.extra;
  h += `<tr><td>LP solve, median / p95</td>` + C.map((c) => `<td class="num${c === "GridQ" ? " gq" : ""}">${ex[c].solve_ms_median != null ? fmt(ex[c].solve_ms_median, 0) + " / " + fmt(ex[c].solve_ms_p95, 0) + " ms" : "rules"}</td>`).join("") + "</tr>";
  $("#proof-table").innerHTML = h;
  daysChart(col);
  tuneTable();
  forecastCard();
  const d = META || {}, fr = d.folds && d.folds[FOLD], w = BENCH.worst_gridq, o = BENCH.oracle_le_gridq;
  $("#provenance").innerHTML = `Demand: ${d.source || "?"}${fr ? `. Held out ${fr.test_range[0]} → ${fr.test_range[1]}; the forecaster for this block saw only ${fr.train_range[0]} → ${fr.train_range[1]}` : ""}. ` +
    `Solar: NASA POWER hourly at IIIT-Delhi, same dates. ${n} report scenario-days = held-out days ${BENCH.report_days ? BENCH.report_days.join("–") : "11–30"} × ${BENCH.meta.seeds} seeded outage patterns; days 1–10 were used for tuning only. ` +
    `Method after Panda et al. 2024 (rule-based vs MPC vs perfect-foresight bound). Worst GridQ day: ${w ? `${w.date}, seed ${w.seed}, ${rs(w.total_cost_rs)} with ${fmt(w.outage_steps / 4, 2)} h of outage` : "—"}. ` +
    (o ? `Oracle ≤ GridQ held in total; on ${o.exceptions}/${o.days} single days it did not (continuous runs carry battery state across days). ` : "") +
    `git ${BENCH.meta.git} · params ${BENCH.meta.params_hash} · ${BENCH.meta.date}` +
    (BENCH.meta.fallback && BENCH.meta.fallback.demand ? `<div class="flag">⚠ this benchmark ran on SYNTHETIC demand</div>` : "");
}

function gauge(R, C, best) {       // number line: worse ← cost of service → better; the captured span is solid
  const box = $("#gauge");
  const W = Math.max(320, box.clientWidth), H = 84;
  box.innerHTML = "";
  const sv = el("svg", { viewBox: `0 0 ${W} ${H}` }, box);
  const T = (c) => R[c].total_cost_rs.sum;
  const hi = Math.max(...C.map(T)), lo = T("Oracle");
  const pad = 24, x = (v) => pad + (W - 2 * pad) * (hi - v) / Math.max(1, hi - lo);
  const y = 40;
  el("line", { x1: pad, x2: W - pad, y1: y, y2: y, stroke: css("--line"), "stroke-width": 8, "stroke-linecap": "round" }, sv);
  el("line", { x1: x(T(best)), x2: x(T("GridQ")), y1: y, y2: y, stroke: css("--gq"), "stroke-width": 8 }, sv);
  el("line", { x1: x(T("GridQ")), x2: x(lo), y1: y, y2: y, stroke: css("--gq"), "stroke-width": 8, "stroke-opacity": 0.25 }, sv);
  const col = { B0: css("--b0"), B1: css("--b1"), GridQ: css("--gq"), Oracle: css("--or") };
  const placed = [];
  C.forEach((c) => {
    const xx = x(T(c));
    el("line", { x1: xx, x2: xx, y1: y - 14, y2: y + 14, stroke: col[c], "stroke-width": c === "GridQ" ? 4 : 2.5 }, sv);
    let row = 0;
    while (placed.some((p) => p.row === row && Math.abs(p.x - xx) < 92)) row++;
    placed.push({ x: xx, row });
    const top = row % 2 === 0;
    const ty = top ? y - 22 - Math.floor(row / 2) * 0 : y + 30;
    const anchor = xx < 60 ? "start" : xx > W - 60 ? "end" : "middle";
    el("text", { x: xx, y: ty, "text-anchor": anchor, class: "g-k" }, sv, c);
    el("text", { x: xx, y: ty + (top ? -13 : 13), "text-anchor": anchor }, sv, rsk(T(c)));
  });
  el("text", { x: pad, y: H - 2, "text-anchor": "start" }, sv, "← costlier");
  el("text", { x: W - pad, y: H - 2, "text-anchor": "end" }, sv, "cheaper →");
}

function daysChart(col) {
  const C = BENCH.controllers, r0 = BENCH.report_days ? BENCH.report_days[0] : 11;
  const days = {};
  C.forEach((c) => { days[c] = BENCH.per_day[c].filter((d) => d.day >= r0).sort((a, b) => a.seed - b.seed || a.day - b.day); });
  const ref = days.B1 || days[C[0]];
  const order = ref.map((_, i) => i).sort((a, b) => ref[a].total_cost_rs - ref[b].total_cost_rs);
  const all = C.flatMap((c) => days[c].map((d) => d.total_cost_rs));
  const ymin = Math.floor(Math.min(...all) * 0.95 / 1000) * 1000;
  const ymax = ymin + 4000 * Math.ceil((Math.max(...all) * 1.02 - ymin) / 4000);   // whole ₹k per gridline
  chart($("#chart-days"), {
    n: order.length, ymin, ymax, unit: "", yfmt: (v) => "₹" + (v / 1000).toFixed(0) + "k", xlabel: (i) => (i === 0 ? "cheapest day for B1 →" : ""),
    lines: C.map((c) => ({ color: col[c], vals: order.map((j) => days[c][j].total_cost_rs), width: c === "GridQ" ? 2.6 : 1.4, dash: c === "Oracle" ? "5 4" : "" })),
    tip: (i) => { const j = order[i], d = ref[j]; return tipRows(`${d.date} · seed ${d.seed} · ${fmt(d.outage_steps / 4, 2)} h outage`, C.map((c) => [col[c], c, rs(days[c][j].total_cost_rs)])); },
  });
  $("#days-legend").innerHTML = C.map((c) => `<span><i class="${c === "Oracle" ? "dash" : ""}" style="background:${col[c]};color:${col[c]}"></i>${c}${c === "Oracle" ? " (perfect foresight)" : ""}</span>`).join("") +
    `<span>${order.length} days, sorted by B1 cost</span>`;
}

async function tuneTable() {
  let t;
  try { const r = await fetch("/api/tune"); if (!r.ok) return; t = await r.json(); } catch (e) { return; }
  let h = `<tr class="h"><th>Variant</th><th>Total ₹</th><th>Captured</th><th>Unserved kWh</th><th>LP ms</th></tr>`;
  h += `<tr class="ref"><td>B1 smart rules (zero point)</td><td class="num">${rsk(t.b1)}</td><td class="num">0%</td><td></td><td></td></tr>`;
  t.rows.forEach((r) => {
    const chosen = r.name === "fan tree, terminal value 8";
    h += `<tr class="${chosen ? "chosen" : ""}"><td>${chosen ? "▶ " : ""}${r.name}${chosen ? " · shipped" : ""}</td><td class="num">${rsk(r.total_cost_rs)}</td>` +
      `<td class="num">${fmt(r.captured_pct, 1)}%</td><td class="num">${fmt(r.unserved_kwh, 0)}</td><td class="num">${fmt(r.solve_ms, 0)}</td></tr>`;
  });
  h += `<tr class="ref"><td>Oracle, perfect foresight (100 %)</td><td class="num">${rsk(t.oracle)}</td><td class="num">100%</td><td></td><td></td></tr>`;
  $("#tune-table").innerHTML = h;
}

function forecastCard() {
  const fc = META && META.forecast;
  if (!fc) { $("#forecast-card").innerHTML = "<span class='fine'>No trained forecaster: seasonal-naive in use (flagged).</span>"; return; }
  const f = fc.held_out;
  $("#forecast-card").innerHTML =
    `<div class="row"><div class="k">Demand P50 error</div><div class="v">${fmt(100 * (1 - f.p50_mae_kw / f.naive_mae_kw), 0)}% better</div>` +
    `<div class="d">MAE ${fmt(f.p50_mae_kw, 2)} kW vs "same as yesterday" ${fmt(f.naive_mae_kw, 2)} kW</div></div>` +
    `<div class="row"><div class="k">P90 coverage (target ≈ 90%)</div><div class="v">${fmt(f.p90_coverage_pct, 1)}%</div>` +
    `<div class="d">share of real demand at or below the P90; conformal-calibrated on training days only</div></div>` +
    (f.pv_mae_kw != null ? `<div class="row"><div class="k">Solar forecast error</div><div class="v">${fmt(100 * (1 - f.pv_mae_kw / f.pv_persist_mae_kw), 0)}% better</div>` +
      `<div class="d">MAE ${fmt(f.pv_mae_kw, 2)} kW vs persistence ${fmt(f.pv_persist_mae_kw, 2)} kW</div></div>` : "") +
    `<div class="fine">${fc.model}</div>`;
}

// ---------------------------------------------------------------- replay
async function replay() {
  $("#replay-out").innerHTML = "<p class='fine'>Replaying…</p>";
  const r = await post("/api/replay");
  if (r.error) { $("#replay-out").innerHTML = `<div class="flag">${r.error}</div>`; return; }
  const box = (name, m) => `<div><div class="who">${name}</div><div class="n">${rs(m.total_cost_rs)}</div><dl>` +
    `<dt>Bill + diesel</dt><dd>${rs(m.cost_rs)}</dd><dt>Battery wear</dt><dd>${rs(m.battery_wear_rs)}</dd>` +
    `<dt>Unserved energy</dt><dd>${fmt(m.unserved_kwh_total, 1)} kWh</dd><dt>Clinic uptime</dt><dd>${fmt(m.critical_uptime_pct, 1)}%</dd>` +
    `<dt>Diesel</dt><dd>${fmt(m.diesel_l, 1)} L</dd><dt>Peak import</dt><dd>${fmt(m.peak_grid_kw, 1)} kW</dd></dl></div>`;
  const d = r.B0.total_cost_rs - r.GridQ.total_cost_rs;
  $("#replay-out").innerHTML = `<div class="replay-verdict">GridQ ${d >= 0 ? "saves" : "costs"} ${rs(Math.abs(d))} on your session</div>` +
    `<p class="fine">${r.steps} steps (${fmt(r.steps / 4, 1)} h of plant time) re-run headless from the same start, with your events and outages.</p>` +
    `<div class="replay-grid">${box("B0 · inverter default", r.B0)}${box("GridQ · re-run headless", r.GridQ)}</div>` +
    `<p class="fine">As it actually ran live, including any SAFE periods: ${rs(r.live.total_cost_rs)}.</p>`;
}

// ---------------------------------------------------------------- boot
let rz;
window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { if (S) render(S); if (BENCH && $("#tab-proof").classList.contains("on")) drawProof(); }, 120); });
const Q = new URLSearchParams(location.search);
if (Q.has("theme")) document.documentElement.dataset.theme = Q.get("theme");
if (Q.has("tab") && $(`.tab[data-tab="${Q.get("tab")}"]`)) $(`.tab[data-tab="${Q.get("tab")}"]`).click();
if (Q.has("once")) {           // static snapshot (screenshots / printing): one fetch, no stream
  fetch("/api/state").then((r) => r.json()).then((s) => {
    S = s; render(s);
    if (Q.has("dbg")) {        // layout debugging: list elements wider than the viewport
      const w = document.documentElement.clientWidth, out = [];
      $$("body *").forEach((e) => { const r = e.getBoundingClientRect(); if (r.right > w + 1 && r.width > 0) out.push(`${e.tagName}.${e.className.baseVal ?? e.className}#${e.id} right=${Math.round(r.right)} w=${Math.round(r.width)}`); });
      const pre = document.createElement("pre"); pre.id = "dbg"; pre.textContent = `vw=${w}\n` + out.slice(0, 25).join("\n"); document.body.appendChild(pre);
    }
  });
} else { lampTest(); connect(); }
