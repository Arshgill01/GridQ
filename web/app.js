// GridQ dashboard. Plain JS, no dependencies, works offline. Every number shown is computed by the backend.
"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const NAMES = { clinic: "Clinic", pump: "Water pump", hostel_a: "Hostel A", hostel_b: "Hostel B", academic: "Academic", ev: "EV / e-rickshaw", heater: "Water heater" };
const PRIO = { clinic: 1, pump: 2, hostel_a: 3, hostel_b: 3, academic: 3, ev: 4, heater: 5 };
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const fmt = (x, d = 1) => (x == null || Number.isNaN(x) ? "—" : Number(x).toFixed(d));
const rs = (x) => (x == null ? "—" : "₹" + Math.round(x).toLocaleString("en-IN"));
let S = null;
let BENCH = null;
let META = null;

// ---------------------------------------------------------------- tabs
$$(".tab").forEach((b) => b.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.toggle("on", x === b));
  $$(".panel").forEach((p) => p.classList.toggle("on", p.id === "tab-" + b.dataset.tab));
  try { localStorage.setItem("gq-tab", b.dataset.tab); } catch (e) {}
  if (b.dataset.tab === "proof") loadProof();
  if (S) render(S);
}));
try { const t = localStorage.getItem("gq-tab"); if (t) $(`.tab[data-tab="${t}"]`)?.click(); } catch (e) {}

// ---------------------------------------------------------------- API
async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  return r.json();
}
$$("[data-ev]").forEach((b) => b.addEventListener("click", () => post("/api/events", { type: b.dataset.ev, source: "dashboard" })));
$("#b-kill").addEventListener("click", () => post("/api/demo/kill-brain"));
$("#b-cable").addEventListener("click", () => post("/api/dev/cable", {}));
$("#b-pause").addEventListener("click", () => post(S && S.paused ? "/api/sim/play" : "/api/sim/pause"));
$("#b-step").addEventListener("click", () => post("/api/sim/step"));
$("#b-reset").addEventListener("click", () => post("/api/sim/reset"));
$$("#speeds button").forEach((b) => b.addEventListener("click", () => post("/api/sim/speed", { x: Number(b.dataset.x) })));
$("#t-sky").addEventListener("change", (e) => post("/api/live/sky", { enabled: e.target.checked }));
$("#t-heat").addEventListener("change", (e) => post("/api/live/heat", { enabled: e.target.checked }));
$("#b-replay").addEventListener("click", replay);

function connect() {
  const es = new EventSource("/api/stream");
  es.addEventListener("state", (e) => { S = JSON.parse(e.data); render(S); });
  es.onerror = () => { $("#h-brain").textContent = "link lost…"; es.close(); setTimeout(connect, 1500); };
}
connect();

// ---------------------------------------------------------------- render
function render(s) {
  // header
  $("#h-clock").textContent = `Day ${s.day + 1} · ${s.clock}${s.paused ? " ⏸" : ""} · ${s.speed}×`;
  $("#h-band").textContent = `${s.band} ₹${fmt(s.tariff, 1)}/kWh`;
  const m = $("#h-mode");
  m.textContent = s.mode;
  m.className = "pill mode " + (s.mode === "SAFE" ? "bad" : s.mode === "CONSTRAINED" ? "warn" : "ok");
  const g = $("#h-grid");
  g.textContent = s.grid.up ? "grid connected" : (s.grid.cable_present ? "islanded" : "grid cable PULLED");
  g.className = "pill " + (s.grid.up ? "ok" : "bad");
  const br = $("#h-brain");
  if (!s.brain.enabled) { br.textContent = "brain disabled"; br.className = "pill warn"; }
  else if (s.brain.alive) { br.textContent = `brain alive · hb ${s.brain.hb}`; br.className = "pill ok"; }
  else if (s.brain.respawn_in_s != null) { br.textContent = `brain DEAD · respawn ${Math.ceil(s.brain.respawn_in_s)}s`; br.className = "pill bad"; }
  else { br.textContent = "brain starting…"; br.className = "pill warn"; }
  $("#h-solve").textContent = s.solve_ms != null ? `LP ${fmt(s.solve_ms, 0)} ms` : "LP —";
  $("#b-cable").hidden = s.mcu.bridge !== "mock";
  $("#b-outage").classList.toggle("active", s.grid.software_outage);
  $("#b-pause").textContent = s.paused ? "Play" : "Pause";
  $$("#speeds button").forEach((b) => b.classList.toggle("on", Number(b.dataset.x) === s.speed));
  $("#t-sky").checked = s.live.sky; $("#t-heat").checked = s.live.heat;
  $("#sensor-last").textContent = s.live.sensor && s.live.sensor.kind ? `last sensor: ${s.live.sensor.kind} = ${fmt(s.live.sensor.value, 1)} · PV ×${fmt(s.live.sky_mult, 2)} · hostel ×${fmt(s.live.heat_mult, 2)}` : "no sensor data yet";

  // live
  $("#survival").textContent = s.survival_h >= 48 ? "> 48 h" : hm(s.survival_h);
  $("#price").textContent = s.shadow_price != null ? `₹${fmt(s.shadow_price, 1)}/kWh` : "—";
  const f = s.flows;
  setSrc("#src-pv", `${fmt(f.pv - f.curt)} kW` + (f.curt > 0.05 ? ` (${fmt(f.curt)} curtailed)` : ""), f.pv > 0.05);
  setSrc("#src-grid", s.grid.up ? `${fmt(f.grid)} kW import` : "DOWN", s.grid.up);
  const bk = s.battery.kw;
  setSrc("#src-batt", `${fmt(100 * s.battery.soc_frac, 0)}% · ${bk > 0.05 ? "→ " + fmt(bk) + " kW out" : bk < -0.05 ? "← " + fmt(-bk) + " kW in" : "idle"}${s.battery.fault ? " · FAULT" : ""}`, true);
  $("#socfill").style.width = `${100 * s.battery.soc_frac}%`;
  setSrc("#src-dg", f.dg > 0.05 ? `${fmt(f.dg)} kW · ${fmt(s.tank_l, 0)} L left` : `off · ${fmt(s.tank_l, 0)} L`, f.dg > 0.05);
  $("#feeders").innerHTML = s.feeders.map(chip).join("");
  const why = whyHtml(s.explanations, s);
  $("#why-live").innerHTML = why;
  $("#why-plan").innerHTML = why;
  if ($("#tab-live").classList.contains("on")) histChart(s.hist);
  if ($("#tab-plan").classList.contains("on") && s.plan) planChart(s);
  $("#events").innerHTML = (s.events || []).slice().reverse().map((e) =>
    `<li><b>${e.type}</b> ${Object.entries(e.params || {}).map(([k, v]) => `${k}=${typeof v === "number" ? fmt(v, 2) : v}`).join(" ")} <span class="muted">· ${e.source} · ${clockOf(e.step)}</span></li>`).join("");
  const fl = [];
  if (s.data.fallback.demand) fl.push("SYNTHETIC demand (I-BLEND unavailable)");
  if (s.data.fallback.solar) fl.push("synthetic solar for some days (NASA POWER missing)");
  if (s.plan && s.plan.forecast_fallback) fl.push("demand forecast: seasonal-naive fallback");
  if (!s.mcu.ok) fl.push("MCU unreachable: plant running SAFE rules");
  $("#foot").innerHTML = (fl.length ? `<div class="flag">⚠ ${fl.join(" · ")}</div>` : "") +
    `Live day: held-out ${s.date} · bridge: ${s.mcu.bridge} · MCU ${s.mcu.mode === "S" ? "SAFE" : "NORMAL"} mask ${s.mcu.applied_mask} · control: ${s.control_source || "—"}`;
}

function setSrc(sel, txt, on) { const e = $(sel); e.querySelector(".v").textContent = txt; e.classList.toggle("off", !on); }
function hm(h) { const H = Math.floor(h); const M = Math.round((h - H) * 60); return `${H}h ${String(M).padStart(2, "0")}m`; }
function clockOf(step) { const sod = ((step % 96) + 96) % 96; return `${String(Math.floor(sod / 4)).padStart(2, "0")}:${String((sod % 4) * 15).padStart(2, "0")}`; }

function chip(c) {
  let st = c.state;
  let label = st === "PARTIAL" ? `PARTIAL ${c.pct}%` : st;
  let kw = c.demand_kw != null ? `${fmt(c.kw)} / ${fmt(c.demand_kw)} kW` :
    (c.remaining_kwh > 0 ? `${fmt(c.kw)} kW · ${fmt(c.remaining_kwh)} kWh left${c.deadline ? " by " + c.deadline : ""}` : `${fmt(c.kw)} kW`);
  return `<div class="chip"><div class="n">${NAMES[c.name]}<span class="prio">P${PRIO[c.name]}</span></div>` +
    `<span class="s ${st}">${label}</span><div class="kw">${kw}</div></div>`;
}

function whyHtml(items, s) {
  if (!items || !items.length) return `<div class="muted">${s.mode === "SAFE" ? "Brain offline: the MCU is holding the safe mask (clinic + pump). No optimisation until it returns." : "Waiting for the first plan…"}</div>`;
  return items.map((it) => {
    let t = it.text || "";
    t = t.replace(/(Rs [\d,]+(\.\d)?(\/kWh)?)/g, '<span class="rs">$1</span>').replace(/Rs /g, "₹");
    return `<div class="w ${it.kind}">${t}</div>`;
  }).join("");
}

// ---------------------------------------------------------------- SVG charts
function svg(el, W, H) { el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"></svg>`; return el.firstChild; }
function path(pts) { return pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join(""); }
function add(s, tag, attrs) { const e = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const k in attrs) e.setAttribute(k, attrs[k]); s.appendChild(e); return e; }

function axes(s, W, H, pad, ymax, unit, xlabels) {
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (H - pad.t - pad.b) * (1 - i / 4);
    add(s, "line", { x1: pad.l, x2: W - pad.r, y1: y, y2: y, class: "ax", "stroke-width": 0.5 });
    add(s, "text", { x: pad.l - 4, y: y + 3, "text-anchor": "end" }).textContent = Math.round(ymax * i / 4) + (i === 4 ? unit : "");
  }
  xlabels.forEach(([x, t]) => { add(s, "text", { x, y: H - 4, "text-anchor": "middle" }).textContent = t; });
}

function planChart(s) {
  const el = $("#chart-plan");
  const W = Math.max(320, el.clientWidth), H = el.clientHeight || 260;
  const sv = svg(el, W, H);
  const h = s.plan.horizon, n = h.grid.length, pad = { l: 34, r: 34, t: 8, b: 18 };
  const series = [["pv_used", css("--pv"), "PV used"], ["grid", css("--grid"), "Grid"], ["dis", css("--batt"), "Battery out"], ["dg", css("--dg"), "Diesel"]];
  const stack = Array(n).fill(0);
  const tops = series.map(([k]) => h[k].map((v, i) => (stack[i] += Math.max(0, v))));
  const ymax = Math.max(10, ...stack, ...h.load) * 1.08;
  const x = (i) => pad.l + (W - pad.l - pad.r) * i / (n - 1);
  const y = (v) => pad.t + (H - pad.t - pad.b) * (1 - v / ymax);
  const xl = [];
  for (let i = 0; i < n; i += 16) xl.push([x(i), clockOf(s.plan.step + i)]);
  axes(sv, W, H, pad, ymax, " kW", xl);
  let prev = Array(n).fill(0);
  series.forEach(([k, col], j) => {
    const top = tops[j];
    const pts = top.map((v, i) => [x(i), y(v)]).concat(prev.map((v, i) => [x(i), y(v)]).reverse());
    add(sv, "path", { d: path(pts) + "Z", fill: col, opacity: 0.75 });
    prev = top;
  });
  add(sv, "path", { d: path(h.load.map((v, i) => [x(i), y(v)])), fill: "none", stroke: css("--load"), "stroke-width": 1.6 });
  add(sv, "path", { d: path(h.dem_p50.map((v, i) => [x(i), y(v)])), fill: "none", stroke: css("--load"), "stroke-width": 1, "stroke-dasharray": "3 3", opacity: 0.6 });
  // SoC on right axis (0..cap)
  const cap = s.battery.cap_kwh;
  const ys = (v) => pad.t + (H - pad.t - pad.b) * (1 - v / cap);
  add(sv, "path", { d: path(h.soc.map((v, i) => [x(i), ys(v)])), fill: "none", stroke: css("--soc"), "stroke-width": 2 });
  add(sv, "text", { x: W - 2, y: pad.t + 8, "text-anchor": "end" }).textContent = "SoC";
  // tariff peak band shading
  $("#plan-legend").innerHTML = series.map(([, c, l]) => `<span><i style="background:${c}"></i>${l}</span>`).join("") +
    `<span><i style="background:${css("--load")}"></i>planned load</span><span>‒ ‒ forecast demand (P50)</span><span><i style="background:${css("--soc")}"></i>battery (right axis, 0–${fmt(cap, 0)} kWh)</span>`;
  $("#plan-meta").textContent = `solved at ${clockOf(s.plan.step)} · ${s.plan.n_branches ? s.plan.n_branches + " outage branches · " : ""}forecaster: ${s.plan.forecaster}`;
}

function histChart(hh) {
  const el = $("#chart-hist");
  if (!hh || !hh.k || hh.k.length < 2) return;
  const W = Math.max(320, el.clientWidth), H = el.clientHeight || 180;
  const sv = svg(el, W, H);
  const n = hh.k.length, pad = { l: 34, r: 8, t: 8, b: 18 };
  const ymax = Math.max(10, ...hh.load, ...hh.pv, ...hh.grid) * 1.1;
  const x = (i) => pad.l + (W - pad.l - pad.r) * i / Math.max(1, n - 1);
  const y = (v) => pad.t + (H - pad.t - pad.b) * (1 - Math.max(0, v) / ymax);
  const xl = [];
  for (let i = 0; i < n; i += Math.max(1, Math.round(n / 6))) xl.push([x(i), clockOf(hh.k[i])]);
  axes(sv, W, H, pad, ymax, " kW", xl);
  [["load", "--load"], ["pv", "--pv"], ["grid", "--grid"], ["dg", "--dg"]].forEach(([k, c]) =>
    add(sv, "path", { d: path(hh[k].map((v, i) => [x(i), y(v)])), fill: "none", stroke: css(c), "stroke-width": k === "load" ? 1.8 : 1.3 }));
}

// ---------------------------------------------------------------- proof
async function loadProof() {
  try {
    if (!BENCH) { const r = await fetch("/api/bench"); BENCH = r.ok ? await r.json() : null; }
    if (!META) META = await (await fetch("/api/meta")).json();
  } catch (e) { BENCH = null; }
  if (!BENCH || !BENCH.report) { $("#captured").textContent = "—"; $("#proof-table").innerHTML = "<tr><td>No benchmark yet: run <code>python -m gridq.bench.run_days</code></td></tr>"; return; }
  const R = BENCH.report, C = BENCH.controllers;
  const cap = BENCH.captured_pct && BENCH.captured_pct.report_total_cost_rs;
  $("#captured").textContent = cap != null ? `${fmt(cap, 0)}%` : "—";
  const sumT = (c) => R[c].total_cost_rs.sum;
  $("#captured-sub").innerHTML = `of the achievable saving captured: GridQ ${rs(sumT("GridQ"))} vs B1 ${rs(sumT("B1"))} vs Oracle bound ${rs(sumT("Oracle"))} over ${R.GridQ.total_cost_rs.n || ""} held-out report scenario-days (total cost of service incl. unserved energy)`;
  const rows = [
    ["Total cost of service ₹/day", "total_cost_rs", 0], ["Energy bill + diesel ₹/day", "cost_rs", 0], ["Battery wear ₹/day", "battery_wear_rs", 0],
    ["Unserved energy kWh/day", "unserved_kwh_total", 1], ["Clinic uptime %", "critical_uptime_pct", 2], ["Diesel L/day", "diesel_l", 1],
    ["CO₂ kg/day", "co2_kg", 0], ["Peak import kW", "peak_grid_kw", 1], ["Solar self-use %", "solar_self_use_pct", 1], ["Fairness (Jain)", "fairness_jain", 2],
    ["Deadline misses/day", "deadline_misses", 2],
  ];
  let html = `<tr><th>metric</th>${C.map((c) => `<th>${c === "GridQ" ? "<b>GridQ</b>" : c}</th>`).join("")}</tr>`;
  rows.forEach(([lab, k, d]) => {
    html += `<tr><td>${lab}</td>` + C.map((c) => { const v = R[c] && R[c][k]; return v ? `<td class="num">${fmt(v.median, d)}<span class="rng">${fmt(v.min, d)} – ${fmt(v.max, d)}</span></td>` : "<td>—</td>"; }).join("") + "</tr>";
  });
  const ex = BENCH.extra;
  html += `<tr><td>LP solve ms (median / p95)</td>` + C.map((c) => `<td class="num">${ex[c].solve_ms_median != null ? fmt(ex[c].solve_ms_median, 0) + " / " + fmt(ex[c].solve_ms_p95, 0) : "—"}</td>`).join("") + "</tr>";
  $("#proof-table").innerHTML = html;
  daysChart();
  const w = BENCH.worst_gridq;
  const fc = META && META.forecast;
  $("#forecast-card").innerHTML = fc ? forecastHtml(fc) : "<span class='muted'>No trained forecaster (seasonal-naive in use).</span>";
  const d = META || {};
  $("#provenance").innerHTML = `Demand: ${d.source || "?"}${d.test_range ? `, held-out ${d.test_range[0]} → ${d.test_range[1]}` : ""} · Solar: NASA POWER (${d.solar_site || "site"}) · ` +
    `${(BENCH.meta.n_days - BENCH.meta.n_days + 20) * BENCH.meta.seeds} report scenario-days (days ${BENCH.meta.n_days ? "11–30" : ""} × ${BENCH.meta.seeds} outage seeds) · method after Panda et al. 2024 · ` +
    `worst GridQ day: ${w ? `${w.date} seed ${w.seed}, ${rs(w.total_cost_rs)} (${w.outage_steps / 4} h outage)` : "—"} · git ${BENCH.meta.git} · params ${BENCH.meta.params_hash} · ${BENCH.meta.date}` +
    (BENCH.meta.fallback && BENCH.meta.fallback.demand ? `<div class="flag">⚠ benchmark ran on SYNTHETIC demand</div>` : "");
}

function forecastHtml(fc) {
  const f = fc.held_out || fc;
  return `<div>P50 error (MAE): <b>${fmt(f.p50_mae_kw, 2)} kW</b> vs "same as yesterday" ${fmt(f.naive_mae_kw, 2)} kW (${fmt(100 * (1 - f.p50_mae_kw / f.naive_mae_kw), 0)}% better)</div>` +
    `<div>P90 coverage: <b>${fmt(f.p90_coverage_pct, 1)}%</b> of actuals ≤ P90 (target ≈ 90%) · pinball ${fmt(f.p90_pinball, 3)}</div>` +
    (f.pv_mae_kw != null ? `<div>Solar: MAE ${fmt(f.pv_mae_kw, 2)} kW vs persistence ${fmt(f.pv_persist_mae_kw, 2)} kW</div>` : "") +
    `<div class="muted">${fc.model || ""}</div>`;
}

function daysChart() {
  const el = $("#chart-days");
  const W = Math.max(320, el.clientWidth), H = el.clientHeight || 260;
  const sv = svg(el, W, H);
  const C = BENCH.controllers, r0 = 11;
  const days = {};
  C.forEach((c) => { days[c] = BENCH.per_day[c].filter((d) => d.day >= r0).sort((a, b) => a.seed - b.seed || a.day - b.day); });
  const ref = days.B1 || days[C[0]];
  const order = ref.map((_, i) => i).sort((a, b) => ref[a].total_cost_rs - ref[b].total_cost_rs);
  const n = order.length, pad = { l: 46, r: 8, t: 8, b: 18 };
  const all = C.flatMap((c) => days[c].map((d) => d.total_cost_rs));
  const ymin = Math.min(...all) * 0.95, ymax = Math.max(...all) * 1.02;
  const x = (i) => pad.l + (W - pad.l - pad.r) * i / Math.max(1, n - 1);
  const y = (v) => pad.t + (H - pad.t - pad.b) * (1 - (v - ymin) / (ymax - ymin));
  for (let i = 0; i <= 4; i++) { const v = ymin + (ymax - ymin) * i / 4; add(sv, "line", { x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v), class: "ax", "stroke-width": 0.5 }); add(sv, "text", { x: pad.l - 4, y: y(v) + 3, "text-anchor": "end" }).textContent = "₹" + Math.round(v / 1000) + "k"; }
  add(sv, "text", { x: (W + pad.l) / 2, y: H - 4, "text-anchor": "middle" }).textContent = `${n} report scenario-days, sorted by B1 cost`;
  const col = { B0: "--b0", B1: "--b1", GridQ: "--gq", Oracle: "--or" };
  C.forEach((c) => add(sv, "path", { d: path(order.map((j, i) => [x(i), y(days[c][j].total_cost_rs)])), fill: "none", stroke: css(col[c]), "stroke-width": c === "GridQ" ? 2.2 : 1.3, "stroke-dasharray": c === "Oracle" ? "4 3" : "" }));
  $("#days-legend").innerHTML = C.map((c) => `<span><i style="background:${css(col[c])}"></i>${c}${c === "Oracle" ? " (perfect foresight bound)" : ""}</span>`).join("");
}

// ---------------------------------------------------------------- replay
async function replay() {
  $("#replay-out").innerHTML = "<p class='muted'>Replaying…</p>";
  const r = await post("/api/replay");
  if (r.error) { $("#replay-out").innerHTML = `<p class="flag">${r.error}</p>`; return; }
  const box = (name, m) => `<div><div class="muted">${name}</div><div class="big-n">${rs(m.total_cost_rs)}</div>` +
    `<div>bill+diesel ${rs(m.cost_rs)} · wear ${rs(m.battery_wear_rs)}</div><div>unserved ${fmt(m.unserved_kwh_total, 1)} kWh · clinic uptime ${fmt(m.critical_uptime_pct, 1)}%</div>` +
    `<div>diesel ${fmt(m.diesel_l, 1)} L · peak ${fmt(m.peak_grid_kw, 1)} kW</div></div>`;
  const d = r.B0.total_cost_rs - r.GridQ.total_cost_rs;
  $("#replay-out").innerHTML = `<p>${r.steps} steps (${fmt(r.steps / 4, 1)} h of sim time) replayed. GridQ ${d >= 0 ? "saves" : "costs"} <b>${rs(Math.abs(d))}</b> vs the inverter default on your session.</p>` +
    `<div class="replay-grid">${box("B0 inverter default", r.B0)}${box("GridQ (headless re-run)", r.GridQ)}</div>` +
    `<p class="muted">Live session as it actually ran (incl. SAFE periods): ${rs(r.live.total_cost_rs)}.</p>`;
}
window.addEventListener("resize", () => S && render(S));
