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
let FOLD = "winter";
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
$$("#folds button").forEach((b) => b.addEventListener("click", () => {
  FOLD = b.dataset.fold; $$("#folds button").forEach((x) => x.classList.toggle("on", x === b)); loadProof();
}));

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
  if ($("#tab-live").classList.contains("on") && !$("#chart-hist .tip:not([hidden])")) histChart(s.hist);
  if ($("#tab-plan").classList.contains("on") && s.plan && !planHover()) planChart(s);
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

function planHover() { return !!document.querySelector("#chart-plan .tip:not([hidden]), #chart-soc .tip:not([hidden])"); }
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
// One y-axis per chart (no dual axes), thin marks, recessive grid, crosshair tooltip on hover.
function add(s, tag, attrs) { const e = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const k in attrs) e.setAttribute(k, attrs[k]); s.appendChild(e); return e; }
function path(pts) { return pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join(""); }

// spec: {n, ymin, ymax, unit, xlabel(i), areas:[{key,label,color,vals}], lines:[{label,color,vals,width,dash}], tip(i)->html}
function chart(el, spec) {
  const W = Math.max(300, el.clientWidth), H = el.clientHeight || 200;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}"></svg><div class="tip" hidden></div>`;
  const sv = el.firstChild, tip = el.lastChild;
  const pad = { l: 40, r: 10, t: 8, b: 20 }, n = spec.n;
  const ymin = spec.ymin || 0, ymax = spec.ymax;
  const x = (i) => pad.l + (W - pad.l - pad.r) * i / Math.max(1, n - 1);
  const y = (v) => pad.t + (H - pad.t - pad.b) * (1 - (v - ymin) / (ymax - ymin));
  for (let i = 0; i <= 4; i++) {
    const v = ymin + (ymax - ymin) * i / 4;
    add(sv, "line", { x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v), class: "ax", "stroke-width": 0.5 });
    add(sv, "text", { x: pad.l - 5, y: y(v) + 3.5, "text-anchor": "end" }).textContent = (spec.yfmt || ((u) => Math.round(u)))(v) + (i === 4 ? spec.unit : "");
  }
  const step = Math.max(1, Math.round(n / 6));
  for (let i = 0; i < n; i += step) add(sv, "text", { x: x(i), y: H - 5, "text-anchor": "middle" }).textContent = spec.xlabel(i);
  let base = Array(n).fill(0);
  (spec.areas || []).forEach((a) => {
    const top = a.vals.map((v, i) => base[i] + Math.max(0, v || 0));
    const pts = top.map((v, i) => [x(i), y(v)]).concat(base.map((v, i) => [x(i), y(v)]).reverse());
    add(sv, "path", { d: path(pts) + "Z", fill: a.color, "fill-opacity": 0.85, stroke: css("--panel"), "stroke-width": 1 });
    base = top;
  });
  (spec.lines || []).forEach((l) => add(sv, "path", { d: path(l.vals.map((v, i) => [x(i), y(v == null ? ymin : v)])), fill: "none",
    stroke: l.color, "stroke-width": l.width || 2, "stroke-dasharray": l.dash || "", "stroke-linejoin": "round" }));
  const xh = add(sv, "line", { class: "xh", y1: pad.t, y2: H - pad.b, x1: -10, x2: -10 });
  const hit = add(sv, "rect", { x: pad.l, y: 0, width: W - pad.l - pad.r, height: H, fill: "transparent" });
  const move = (ev) => {
    const r = sv.getBoundingClientRect();
    const px = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
    const i = Math.max(0, Math.min(n - 1, Math.round((px * W / r.width - pad.l) / ((W - pad.l - pad.r) / Math.max(1, n - 1)))));
    xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i));
    tip.innerHTML = spec.tip(i);
    tip.hidden = false;
    const left = x(i) * r.width / W;
    tip.style.left = Math.min(Math.max(0, left + 12), r.width - tip.offsetWidth - 4) + "px";
    tip.style.top = "6px";
  };
  hit.addEventListener("mousemove", move);
  hit.addEventListener("touchmove", move, { passive: true });
  hit.addEventListener("mouseleave", () => { tip.hidden = true; xh.setAttribute("x1", -10); xh.setAttribute("x2", -10); });
}
const sw = (c) => `<span class="sw" style="background:${c}"></span>`;
const tipRows = (title, rows) => `<b>${title}</b>` + rows.map(([c, l, v]) => `<div>${c ? sw(c) : ""}${l}: <b>${v}</b></div>`).join("");

function planChart(s) {
  const h = s.plan.horizon, n = h.grid.length;
  const areas = [
    { label: "PV used", color: css("--pv"), vals: h.pv_used },
    { label: "Battery out", color: css("--batt"), vals: h.dis },
    { label: "Grid", color: css("--grid"), vals: h.grid },
    { label: "Diesel", color: css("--dg"), vals: h.dg },
  ];
  const tot = h.grid.map((_, i) => areas.reduce((a, b) => a + Math.max(0, b.vals[i]), 0));
  const ymax = Math.ceil(Math.max(10, ...tot, ...h.load, ...h.dem_p50) * 1.1 / 10) * 10;
  chart($("#chart-plan"), {
    n, ymax, unit: " kW", xlabel: (i) => clockOf(s.plan.step + i), areas,
    lines: [{ label: "Planned load", color: css("--load"), vals: h.load, width: 1.6 }],
    tip: (i) => tipRows(`${clockOf(s.plan.step + i)} plan`, [
      ...areas.map((a) => [a.color, a.label, fmt(a.vals[i]) + " kW"]),
      [css("--load"), "planned load", fmt(h.load[i]) + " kW"],
      ["", "forecast demand P50", fmt(h.dem_p50[i]) + " kW"],
      ["", "EV / heater / pump", `${fmt(h.p_ev[i])} / ${fmt(h.p_heater[i])} / ${fmt(h.p_pump[i])} kW`],
      ["", "battery charging", fmt(h.ch[i]) + " kW"]]),
  });
  $("#plan-legend").innerHTML = areas.map((a) => `<span><i style="background:${a.color}"></i>${a.label}</span>`).join("") +
    `<span><i style="background:${css("--load")}"></i>planned load (incl. flexible loads it schedules)</span>`;
  const cap = s.battery.cap_kwh;
  chart($("#chart-soc"), {
    n, ymax: cap, unit: " kWh", xlabel: (i) => clockOf(s.plan.step + i + 1),
    lines: [{ label: "SoC", color: css("--soc"), vals: h.soc, width: 2 },
            { label: "min", color: css("--bad"), vals: h.soc.map(() => 0.1 * cap), width: 1 }],
    tip: (i) => tipRows(`${clockOf(s.plan.step + i + 1)}`, [[css("--soc"), "battery", `${fmt(h.soc[i])} kWh (${fmt(100 * h.soc[i] / cap, 0)}%)`]]),
  });
  $("#plan-meta").textContent = `solved at ${clockOf(s.plan.step)} · ${s.plan.n_branches ? s.plan.n_branches + " outage branches · " : ""}forecaster: ${s.plan.forecaster}`;
}

function histChart(hh) {
  if (!hh || !hh.k || hh.k.length < 2) return;
  const n = hh.k.length;
  const L = [["load", "Load served", "--load"], ["pv", "PV used", "--pv"], ["grid", "Grid", "--grid"], ["dg", "Diesel", "--dg"]];
  const ymax = Math.ceil(Math.max(10, ...hh.load, ...hh.pv, ...hh.grid) * 1.1 / 10) * 10;
  chart($("#chart-hist"), {
    n, ymax, unit: " kW", xlabel: (i) => clockOf(hh.k[i]),
    lines: L.map(([k, l, c]) => ({ label: l, color: css(c), vals: hh[k], width: k === "load" ? 2 : 1.5 })),
    tip: (i) => tipRows(clockOf(hh.k[i]), [...L.map(([k, l, c]) => [css(c), l, fmt(hh[k][i]) + " kW"]),
      ["", "battery", `${fmt(hh.soc[i])} kWh, ${hh.batt[i] >= 0 ? "out " + fmt(hh.batt[i]) : "in " + fmt(-hh.batt[i])} kW`]]),
  });
}

// ---------------------------------------------------------------- proof
async function loadProof() {
  try {
    if (!BENCH || BENCH._fold !== FOLD) { const r = await fetch("/api/bench?fold=" + FOLD); BENCH = r.ok ? await r.json() : null; if (BENCH) BENCH._fold = FOLD; }
    if (!META || META._fold !== FOLD) { META = await (await fetch("/api/meta?fold=" + FOLD)).json(); META._fold = FOLD; }
  } catch (e) { BENCH = null; }
  if (!BENCH || !BENCH.report) { $("#captured").textContent = "—"; $("#proof-table").innerHTML = "<tr><td>No benchmark yet: run <code>python -m gridq.bench.run_days</code></td></tr>"; return; }
  const R = BENCH.report, C = BENCH.controllers;
  const capB1 = BENCH.captured_pct && BENCH.captured_pct.report_total_cost_rs;
  const bb = BENCH.best_baseline && BENCH.best_baseline.report;
  const sumT = (c) => R[c].total_cost_rs.sum;
  const n = R.GridQ.total_cost_rs.n;
  if (bb) {
    $("#captured").textContent = `${fmt(bb.captured_pct, 0)}%`;
    $("#captured-sub").innerHTML = `of the achievable saving captured vs the <b>better</b> rule-based baseline (${bb.name}) on ${n} held-out report scenario-days: ` +
      `GridQ ${rs(sumT("GridQ"))} vs ${bb.name} ${rs(sumT(bb.name))} vs Oracle bound ${rs(sumT("Oracle"))}, saving ${rs(bb.saving_rs)} (${fmt(100 * bb.saving_rs / sumT(bb.name), 1)}%). ` +
      `Spec definition (vs B1 smart ToD rules): <b>${fmt(capB1, 0)}%</b>. Total cost of service = bill + diesel + demand charge + battery wear + unserved energy at VoLL.`;
  } else { $("#captured").textContent = capB1 != null ? `${fmt(capB1, 0)}%` : "—"; }
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
  tuneTable();
  const w = BENCH.worst_gridq;
  const fc = META && META.forecast;
  $("#forecast-card").innerHTML = fc ? forecastHtml(fc) : "<span class='muted'>No trained forecaster (seasonal-naive in use).</span>";
  const d = META || {};
  const fr = d.folds && d.folds[FOLD];
  $("#provenance").innerHTML = `Demand: ${d.source || "?"}${fr ? `, held-out ${fr.test_range[0]} → ${fr.test_range[1]} (forecaster trained on ${fr.train_range[0]} → ${fr.train_range[1]} only)` : ""} · Solar: NASA POWER (${d.solar_site || "site"}) · ` +
    `${R.GridQ.total_cost_rs.n} report scenario-days (held-out days 11–${BENCH.meta.n_days} × ${BENCH.meta.seeds} outage seeds; days 1–10 used for tuning only) · method after Panda et al. 2024 · ` +
    `worst GridQ day: ${w ? `${w.date} seed ${w.seed}, ${rs(w.total_cost_rs)} (${w.outage_steps / 4} h outage)` : "—"} · git ${BENCH.meta.git} · params ${BENCH.meta.params_hash} · ${BENCH.meta.date}` +
    (BENCH.meta.fallback && BENCH.meta.fallback.demand ? `<div class="flag">⚠ benchmark ran on SYNTHETIC demand</div>` : "");
}

async function tuneTable() {
  let t;
  try { const r = await fetch("/api/tune"); if (!r.ok) return; t = await r.json(); } catch (e) { return; }
  let h = `<tr><th>GridQ variant</th><th>total ₹ (${t.n} days)</th><th>captured</th><th>unserved kWh</th><th>wear ₹</th><th>LP ms</th></tr>`;
  h += `<tr><td>B1 smart rules (reference)</td><td class="num">${rs(t.b1)}</td><td class="num">0%</td><td></td><td></td><td></td></tr>`;
  t.rows.forEach((r) => { h += `<tr${r.name === "fan tree" ? ' class="gq"' : ""}><td>${r.name}${r.name === "fan tree" ? " ✔ chosen" : ""}</td><td class="num">${rs(r.total_cost_rs)}</td>` +
    `<td class="num">${fmt(r.captured_pct, 1)}%</td><td class="num">${fmt(r.unserved_kwh, 0)}</td><td class="num">${rs(r.wear_rs)}</td><td class="num">${fmt(r.solve_ms, 0)}</td></tr>`; });
  h += `<tr><td>Oracle (perfect foresight)</td><td class="num">${rs(t.oracle)}</td><td class="num">100%</td><td></td><td></td><td></td></tr>`;
  $("#tune-table").innerHTML = h;
}

function forecastHtml(fc) {
  const f = fc.held_out || fc;
  return `<div>P50 error (MAE): <b>${fmt(f.p50_mae_kw, 2)} kW</b> vs "same as yesterday" ${fmt(f.naive_mae_kw, 2)} kW (${fmt(100 * (1 - f.p50_mae_kw / f.naive_mae_kw), 0)}% better)</div>` +
    `<div>P90 coverage: <b>${fmt(f.p90_coverage_pct, 1)}%</b> of actuals ≤ P90 (target ≈ 90%) · pinball ${fmt(f.p90_pinball, 3)}</div>` +
    (f.pv_mae_kw != null ? `<div>Solar: MAE ${fmt(f.pv_mae_kw, 2)} kW vs persistence ${fmt(f.pv_persist_mae_kw, 2)} kW</div>` : "") +
    `<div class="muted">${fc.model || ""}</div>`;
}

function daysChart() {
  const C = BENCH.controllers, r0 = BENCH.report_days ? BENCH.report_days[0] : 11;
  const days = {};
  C.forEach((c) => { days[c] = BENCH.per_day[c].filter((d) => d.day >= r0).sort((a, b) => a.seed - b.seed || a.day - b.day); });
  const ref = days.B1 || days[C[0]];
  const order = ref.map((_, i) => i).sort((a, b) => ref[a].total_cost_rs - ref[b].total_cost_rs);
  const all = C.flatMap((c) => days[c].map((d) => d.total_cost_rs));
  const ymin = Math.floor(Math.min(...all) * 0.95 / 1000) * 1000, ymax = Math.ceil(Math.max(...all) * 1.02 / 1000) * 1000;
  const col = { B0: css("--b0"), B1: css("--b1"), GridQ: css("--gq"), Oracle: css("--or") };
  chart($("#chart-days"), {
    n: order.length, ymin, ymax, unit: "", yfmt: (v) => "₹" + (v / 1000).toFixed(0) + "k", xlabel: (i) => (i === 0 ? "cheapest" : ""),
    lines: C.map((c) => ({ label: c, color: col[c], vals: order.map((j) => days[c][j].total_cost_rs), width: c === "GridQ" ? 2.5 : 1.5, dash: c === "Oracle" ? "5 3" : "" })),
    tip: (i) => { const j = order[i], d = ref[j]; return tipRows(`${d.date} · outage seed ${d.seed} · ${fmt(d.outage_steps / 4, 2)} h outage`,
      C.map((c) => [col[c], c, rs(days[c][j].total_cost_rs)])); },
  });
  $("#days-legend").innerHTML = C.map((c) => `<span><i style="background:${col[c]}"></i>${c}${c === "Oracle" ? " (perfect-foresight bound, dashed)" : ""}</span>`).join("") +
    `<span>x: ${order.length} report scenario-days, sorted by B1 cost</span>`;
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
