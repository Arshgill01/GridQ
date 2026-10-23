"""gridq-io: always-running process (spec §3, §10, §13).

FastAPI web + SSE, the plant twin and sim clock, the event log and replay, the
Bridge client (sole owner) and the supervisor of the killable gridq-brain.

    GRIDQ_MOCK_BRIDGE=1 python -m gridq.io.app        # laptop
    python -m gridq.io.app                              # on the UNO Q (router socket present)
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from gridq.common.config import (BIT, DT, FLEX, INFLEX, ROOT, SPD, load_hardware, load_params,
                                 mask_from_loads, tariff_for_hour)
from gridq.common.schema import state_payload
from gridq.control import explain as X
from gridq.control.baseline_b1 import B1
from gridq.control.safe import Safe
from gridq.forecast.solar import clearness
from gridq.io import node_format as NF
from gridq.io.bridge import McuStatus, make_bridge, outputs_csv
from gridq.plant.events import EVENT_TYPES, Event
from gridq.plant.profiles import clinic_nominal, load_dataset
from gridq.plant.twin import Control, Timeline, Twin

log = logging.getLogger("gridq.io")
P = load_params()
HW = load_hardware()
T = P["mpc"]["horizon_steps"]
SAFE_MASK = mask_from_loads(P["safety"]["safe_mask_loads"])
PORT = int(os.environ.get("GRIDQ_PORT", HW["web"]["port"]))
LIVE_DAYS = 90          # held-out days cycled; ~2.4 h of wall time at 1x before the live run restarts itself


# ------------------------------------------------------------------ brain supervisor
class BrainSupervisor:
    def __init__(self, io_url: str):
        self.io_url = io_url
        self.proc: subprocess.Popen | None = None
        self.respawn_at: float | None = None
        self.killed_at: float | None = None
        self.restarts = 0
        self.enabled = os.environ.get("GRIDQ_NO_BRAIN", "0") != "1"
        self.lock = threading.Lock()

    def spawn(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen([sys.executable, "-m", "gridq.brain.main", "--io", self.io_url],
                                     cwd=str(ROOT), env=env)
        self.respawn_at = None
        log.info("brain spawned pid=%s", self.proc.pid)

    def kill(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                os.kill(self.proc.pid, signal.SIGKILL)
                self.killed_at = time.time()
                log.warning("brain SIGKILLed (demo)")

    def watch(self):
        if not self.enabled:
            return
        self.spawn()
        while True:
            time.sleep(0.25)
            with self.lock:
                if self.proc and self.proc.poll() is not None:
                    if self.respawn_at is None:
                        self.respawn_at = time.time() + P["safety"]["brain_restart_delay_s"]
                        log.warning("brain exited (%s); respawn in %ss", self.proc.returncode,
                                    P["safety"]["brain_restart_delay_s"])
                    elif time.time() >= self.respawn_at:
                        self.restarts += 1
                        self.spawn()

    def status(self, last_hb_age: float) -> dict:
        alive = bool(self.proc and self.proc.poll() is None)
        st = {"alive": alive and last_hb_age < 1.5, "process": alive, "restarts": self.restarts, "enabled": self.enabled}
        if self.respawn_at:
            st["respawn_in_s"] = max(0.0, self.respawn_at - time.time())
        return st

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ------------------------------------------------------------------ live simulation
class Live:
    def __init__(self):
        self.p = P
        self.bridge = make_bridge()
        self.ds = load_dataset(P)
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.events_q: list[dict] = []
        self.speed = 1.0
        self.paused = False
        self.plan: dict | None = None
        self.explain: dict | None = None
        self.last_hb_val: int | None = None
        self.last_hb_t = 0.0
        self.hb_count = 0
        self.sky = {"enabled": False, "lux_ref": None, "ref_samples": [], "last_mult": 1.0}
        self.heat = {"enabled": False, "t_ref": None, "last_mult": 1.0}
        self.sensor_last: dict = {}
        self.seq = 0
        self.status = McuStatus()
        self.mcu_ok = False
        self.subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self.hist: dict[str, list] = {}
        self.reset()

    # ---------------- setup / reset
    def _timeline(self):
        test = self.ds.test_days()
        name = os.environ.get("GRIDQ_LIVE_DATE")
        if name and any(d.date == name for d in test):
            i0 = [d.date for d in test].index(name)
        else:   # the held-out day with the highest evening demand: the cap binds, the demo has teeth
            ev = [sum(d.demand[f][72:92].sum() for f in d.demand) for d in test]
            i0 = int(np.argmax(ev))
        days = [test[(i0 + i) % len(test)] for i in range(LIVE_DAYS + 1)]
        first = self.ds.days.index(test[i0])
        pre = self.ds.days[max(0, first - 7):first]
        return Timeline.from_days(P, days, n_sim=LIVE_DAYS, pre_days=pre), test[i0].date

    def reset(self):
        with self.lock:
            tl, date = self._timeline()
            tw = Twin(P, tl)
            warm = B1(P)                     # history before the demo starts (16:00) is run by B1 rules
            for _ in range(P["time"]["demo_start_hour"] * 4):
                tw.step(warm.decide(tw.observe(), tw))
            self.twin = tw
            self.snapshot = copy.deepcopy(tw)
            self.live_date = date
            self.session = {"start_step": tw.k, "date": date, "steps": []}
            ts = time.strftime("%Y%m%d-%H%M%S")
            self.session_path = ROOT / "data" / f"session_{ts}.jsonl"
            self.events_q.clear()
            self.plan = None
            self.explain = None
            self.grid_up = True
            self.software_outage_cmd = 0
            self.last_cable = None
            self.last_result = None
            self.hist = {k: [] for k in ("k", "pv", "load", "grid", "soc", "dg", "batt", "price")}
            self.brain_state = state_payload(tw, tw.observe(grid_up=True), T)
            self.cond.notify_all()
            self._write_log({"type": "start", "step": tw.k, "date": date})

    def _write_log(self, rec: dict):
        try:
            with open(self.session_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    # ---------------- control selection
    def _control(self, obs) -> tuple[Control, str]:
        st = self.status
        if not self.mcu_ok or st.mode == "S":
            return Safe(P, safe_mask=st.applied_mask if self.mcu_ok else SAFE_MASK).decide(obs), "SAFE"
        pl = self.plan
        if pl is not None:
            off = obs.k - pl["step"]
            n = len(pl["ctrl"]["ch"])
            if 0 <= off < min(n, 9):
                c = pl["ctrl"]
                return Control(served_frac={l: c["served_frac"][l][off] for l in INFLEX},
                               p_flex={f: c["p_flex"][f][off] for f in FLEX},
                               p_charge=c["ch"][off], p_discharge=c["dis"][off], p_dg=c["dg"][off],
                               shed_policy="priority"), f"PLAN+{off}"
        return Safe(P).decide(obs), "SAFE(no plan)"

    def _plan_mask(self, k: int) -> int:
        pl = self.plan
        if pl is None:
            return 127
        off = k - pl["step"]
        n = len(pl["ctrl"]["ch"])
        if not 0 <= off < n:
            return 127
        c = pl["ctrl"]
        m = 0
        for l in INFLEX:
            if c["served_frac"][l][off] > 0.05:
                m |= 1 << BIT[l]
        for f in FLEX:
            # a flexible load with no open session keeps its bit (nothing to gate); paused ones drop it
            open_ = any(s.load == f and s.start <= k < s.deadline and s.remaining > 1e-6 for s in self.twin.sessions)
            if not open_ or c["p_flex"][f][off] > 0.05 * P["loads"][f]["p_kw"]:
                m |= 1 << BIT[f]
        return m

    # ---------------- one sim step (spec §13)
    def tick(self):
        with self.lock:
            tw = self.twin
            if tw.k >= tw.tl.n_steps - 1:      # end of the live timeline: start a fresh session (logged)
                log.warning("live timeline exhausted at step %d: auto-reset", tw.k)
                self.reset()
                tw = self.twin
            # 1. MCU status
            try:
                self.status = self.bridge.get_status()
                self.mcu_ok = True
            except Exception as e:  # MCU unreachable: plant falls to SAFE rules, flagged
                log.warning("get_status failed: %s", e)
                self.mcu_ok = False
            st = self.status
            # 2. events: cable changes from status, then the queue
            if self.mcu_ok and self.last_cable is not None and st.cable_present != self.last_cable:
                self._log_event(Event.make(tw.k, "grid_outage", {"on": not st.cable_present}, "cable"), apply=False)
            if self.mcu_ok:
                self.last_cable = st.cable_present
            q, self.events_q = self.events_q, []
            for e in q:
                self._log_event(Event.make(tw.k, e["type"], e.get("params"), e.get("source", "api")))
            # grid availability
            cable = st.cable_present if self.mcu_ok else True
            relay = st.relay_energised if self.mcu_ok else False
            self.grid_up = cable and not relay and not tw.software_outage
            obs = tw.observe(grid_up=self.grid_up)
            # 3. control
            ctrl, source = self._control(obs)
            mode = 1 if source.startswith("SAFE") else 0
            applied = st.applied_mask if self.mcu_ok else SAFE_MASK
            # 4. twin step
            res = tw.step(ctrl, applied_mask=applied, grid_up=self.grid_up, mode=mode)
            self.last_result = res
            self.last_source = source
            self.session["steps"].append({"k": res["k"], "grid_up": self.grid_up, "mask": applied, "src": source,
                                          "ctrl": _ctrl_dict(ctrl)})
            self._write_log({"type": "step", "k": res["k"], "grid_up": self.grid_up, "mask": applied,
                             "src": source, "ctrl": _ctrl_dict(ctrl)})
            # 5. outputs to the MCU
            cols = self._cols(res)
            shed_any = any(res["served"][l] < 0.99 * res["demand"][l] for l in INFLEX)
            led = "C" if (shed_any or not self.grid_up) else "N"
            safe_mask = SAFE_MASK
            mask = self._plan_mask(tw.k)
            try:
                self.bridge.set_outputs(outputs_csv(mask, safe_mask, 1 if tw.software_outage else 0, cols, led))
            except Exception as e:
                log.warning("set_outputs failed: %s", e)
            self.led_state = led
            # history for charts
            h = self.hist
            load = sum(res["served"].values()) + sum(res["p_flex"].values())
            for key, val in (("k", res["k"]), ("pv", res["pv"] - res["curt"]), ("load", load), ("grid", res["grid"]),
                             ("soc", res["soc"]), ("dg", res["dg"]), ("batt", res["dis"] - res["ch"]),
                             ("price", self.plan["shadow_price"] if self.plan else None)):
                h[key].append(val)
                if len(h[key]) > 2 * SPD:
                    h[key].pop(0)
            # brain input for the next step
            self.brain_state = state_payload(tw, tw.observe(grid_up=self.grid_up), T)
            self.seq += 1
            self.cond.notify_all()
            snap = self.public_state()
        self._broadcast(snap)

    def _log_event(self, ev: Event, apply: bool = True):
        if apply:
            self.twin.apply(ev)
        else:
            self.twin.event_log.append(ev.to_dict())
        self.session.setdefault("events", []).append({**ev.to_dict(), "applied": apply})
        self._write_log({"type": "event", **ev.to_dict(), "applied": apply})

    def _cols(self, r) -> str:
        p = P
        L = p["loads"]
        def d(x):
            return str(int(min(8, max(0, round(8 * x)))))
        fr = lambda l: (r["served"][l] / r["demand"][l]) if r["demand"][l] > 1e-9 else 0.0
        c = [d(fr("clinic")), d(r["p_flex"]["pump"] / L["pump"]["p_kw"]), d(fr("hostel_a")), d(fr("hostel_b")),
             d(fr("academic")), d(r["p_flex"]["ev"] / L["ev"]["p_kw"]), d(r["p_flex"]["heater"] / L["heater"]["p_kw"]),
             "0", d(r["pv"] / p["solar"]["kwp"]), d(r["soc"] / self.twin.cap),
             d(r["grid"] / p["grid"]["import_limit_kw"] if r["grid_up"] else 0.0),
             d(r["dg"] / p["diesel"]["p_max_kw"]), "0"]
        return "".join(c)

    # ---------------- derived values
    def survival(self, use_dg: bool = True) -> float:
        """Grid down from now, clinic only at P90, forecast PV, battery to its floor (then diesel)."""
        tw = self.twin
        k = tw.k
        n = 2 * SPD
        sod = (k + np.arange(n)) % SPD
        clinic_p90 = clinic_nominal(P)[sod] * 1.1
        ke = clearness(tw, k)
        pv = tw.clear(k, k + n) * ke if k + n <= len(tw.tl.pv_clear) else np.zeros(n)
        return X.survival_hours(P, tw.soc, tw.soc_min, tw.pdis0 * tw.pow_f, tw.tank, clinic_p90, pv, use_dg=use_dg)

    def feeder_chips(self, r) -> list[dict]:
        chips = []
        for name in ["clinic", "pump", "hostel_a", "hostel_b", "academic", "ev", "heater"]:
            if name in INFLEX:
                dem, srv = r["demand"][name], r["served"][name]
                frac = srv / dem if dem > 1e-9 else 1.0
                state = "ON" if frac >= 0.99 else ("SHED" if frac < 0.05 else "PARTIAL")
                chips.append({"name": name, "state": state, "pct": round(100 * frac), "kw": round(srv, 2),
                              "demand_kw": round(dem, 2)})
            else:
                pk = P["loads"][name]["p_kw"]
                kw = r["p_flex"][name]
                k = r["k"]
                open_s = [s for s in self.twin.sessions if s.load == name and s.start <= k + 1 < s.deadline + 1
                          and s.remaining > 1e-6]
                rem = sum(s.remaining for s in open_s)
                if kw >= 0.95 * pk:
                    state = "ON"
                elif kw > 0.05 * pk:
                    state = "PARTIAL"
                elif rem > 1e-6:
                    state = "DEFERRED"
                else:
                    state = "IDLE"
                dl = min((s.deadline for s in open_s), default=None)
                chips.append({"name": name, "state": state, "pct": round(100 * kw / pk), "kw": round(kw, 2),
                              "remaining_kwh": round(rem, 2), "deadline": NF.clock(dl) if dl is not None else None})
        return chips

    def public_state(self) -> dict:
        tw = self.twin
        r = self.last_result
        k = tw.k
        hour = (k % SPD) * DT
        rate, band = tariff_for_hour(P, hour)
        surv = self.survival()
        surv_batt = self.survival(use_dg=False)
        expl = self.explain["items"] if self.explain and self.plan and self.explain["step"] >= self.plan["step"] - 1 else []
        mode = "SAFE" if (not self.mcu_ok or self.status.mode == "S") else (
            "CONSTRAINED" if getattr(self, "led_state", "N") == "C" else "NORMAL")
        hb_age = time.time() - self.last_hb_t if self.last_hb_t else 1e9
        plan = self.plan
        return {
            "seq": self.seq, "step": k, "clock": NF.clock(k), "day": k // SPD, "date": self.live_date,
            "tariff": rate, "band": band, "mode": mode, "speed": self.speed, "paused": self.paused,
            "grid": {"up": self.grid_up, "cable_present": self.status.cable_present, "relay_islanded": self.status.relay_energised,
                     "software_outage": tw.software_outage},
            "mcu": {"ok": self.mcu_ok, "mode": self.status.mode, "applied_mask": self.status.applied_mask,
                    "uptime_ms": self.status.uptime_ms, "last_hb": self.status.last_hb, "bridge": self.bridge.kind},
            "brain": {**SUP.status(hb_age), "hb": self.last_hb_val, "hb_age_s": round(min(hb_age, 999), 2)},
            "solve_ms": plan["solve_ms"] if plan else None,
            "control_source": getattr(self, "last_source", None),
            "battery": {"soc_kwh": tw.soc, "soc_frac": tw.soc / tw.cap, "cap_kwh": tw.cap, "fault": tw.cap_f < 1,
                        "kw": (r["dis"] - r["ch"]) if r else 0.0},
            "flows": {k2: (round(r[k2], 2) if r else 0.0) for k2 in ("pv", "grid", "dg", "curt", "ch", "dis")},
            "tank_l": tw.tank,
            "feeders": self.feeder_chips(r) if r else [],
            "survival_h": surv, "survival_batt_h": surv_batt,
            "shadow_price": plan["shadow_price"] if plan else None,
            "explanations": expl,
            "plan": {"step": plan["step"], "horizon": plan["horizon"], "binding": plan["binding"],
                     "forecaster": plan.get("forecaster"), "forecast_fallback": plan.get("forecast_fallback"),
                     "n_branches": plan.get("n_branches", 0)} if plan else None,
            "hist": self.hist,
            "events": tw.event_log[-12:],
            "live": {"sky": self.sky["enabled"], "heat": self.heat["enabled"], "sky_mult": tw.sky_mult,
                     "heat_mult": tw.heat_mult, "sensor": self.sensor_last},
            "data": {"fallback": self.ds.fallback, "source": self.ds.meta.get("source")},
        }

    def node(self) -> dict:
        with self.lock:
            tw = self.twin
            mode = "S" if (not self.mcu_ok or self.status.mode == "S") else "N"
            top = None
            if self.explain:
                for it in self.explain["items"]:
                    if it.get("kind") in ("defer", "shed") and it.get("lcd"):
                        top = it["lcd"]
                        break
                if top is None and self.explain["items"]:
                    top = self.explain["items"][0].get("lcd")
            pv = self.last_result["pv"] if self.last_result else 0.0
            l1, l2 = NF.lines(mode, self.grid_up, tw.k, self.survival(use_dg=False), top, tw.soc / tw.cap, pv)
            l2.insert(1, NF.fit(f"+DIESEL {'>48h' if (s := self.survival()) >= 48 else NF.secure(s)[7:]}"))
            clinic_on = bool(self.last_result and self.last_result["served"]["clinic"] >= 0.99 * self.last_result["demand"]["clinic"])
            return {"l1": l1, "l2": l2, "soc": round(tw.soc / tw.cap, 3), "mode": mode, "clinic_on": clinic_on,
                    "seq": self.seq}

    # ---------------- SSE
    def _broadcast(self, snap: dict):
        data = json.dumps(snap, default=_json_default)
        for loop, q in list(self.subscribers):
            try:
                loop.call_soon_threadsafe(_put_latest, q, data)
            except RuntimeError:
                pass

    # ---------------- clock thread
    def run_clock(self):
        nxt = time.monotonic()
        while True:
            if not self.paused:
                try:
                    self.tick()
                except Exception:
                    log.exception("tick failed")
            nxt += 1.0 / max(self.speed, 0.1)
            delay = nxt - time.monotonic()
            if delay < -2:
                nxt = time.monotonic()
            time.sleep(max(0.0, delay))

    # ---------------- sensors
    def sensor(self, kind: str, value: float):
        self.sensor_last = {"kind": kind, "value": value, "t": time.time()}
        if kind == "lux" and self.sky["enabled"]:
            s = self.sky
            if s["lux_ref"] is None:
                s["ref_samples"].append(value)
                if len(s["ref_samples"]) >= 10:
                    s["lux_ref"] = max(max(s["ref_samples"]), 1.0)
                return
            m = min(1.0, max(0.0, value / s["lux_ref"]))
            if abs(m - s["last_mult"]) > 0.05:
                s["last_mult"] = m
                self.events_q.append({"type": "live_sky", "params": {"multiplier": m}, "source": "esp32"})
        elif kind == "temp_c" and self.heat["enabled"]:
            h = self.heat
            if h["t_ref"] is None:
                h["t_ref"] = value
            m = min(1.4, max(0.8, 1 + 0.04 * (value - h["t_ref"])))
            if abs(m - h["last_mult"]) > 0.02:
                h["last_mult"] = m
                self.events_q.append({"type": "heat", "params": {"multiplier": m}, "source": "esp32"})


def _put_latest(q: asyncio.Queue, data: str):
    while q.qsize() > 2:      # slow client: drop stale frames, keep the latest
        q.get_nowait()
    q.put_nowait(data)


def _json_default(o):
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))


def _ctrl_dict(c: Control) -> dict:
    return {"served_frac": c.served_frac, "p_flex": c.p_flex, "ch": c.p_charge, "dis": c.p_discharge,
            "dg": c.p_dg, "policy": c.shed_policy}


# ------------------------------------------------------------------ FastAPI
app = FastAPI(title="GridQ")
LIVE: Live | None = None
SUP = BrainSupervisor(f"http://127.0.0.1:{PORT}")


@app.on_event("startup")
def _startup():
    global LIVE
    logging.basicConfig(level=logging.INFO, format="%(asctime)s io %(levelname)s %(message)s")
    LIVE = Live()
    threading.Thread(target=LIVE.run_clock, daemon=True).start()
    threading.Thread(target=SUP.watch, daemon=True).start()


@app.on_event("shutdown")
def _shutdown():
    SUP.stop()


@app.exception_handler(json.JSONDecodeError)
async def _bad_json(req, exc):
    return Response(json.dumps({"error": f"bad JSON: {exc}"}), status_code=400, media_type="application/json")


def J(obj, code=200):
    return Response(json.dumps(obj, default=_json_default), status_code=code, media_type="application/json")


# ---------------- internal (brain)
@app.get("/internal/state")
def internal_state(after_step: int = -1):
    L = LIVE
    deadline = time.time() + 2.0
    with L.cond:
        while L.brain_state["step"] <= after_step and time.time() < deadline:
            L.cond.wait(timeout=max(0.0, deadline - time.time()))
        if L.brain_state["step"] <= after_step:
            return Response(status_code=204)
        st = L.brain_state
    return J(st)


@app.post("/internal/plan")
async def internal_plan(req: Request):
    pl = await req.json()
    with LIVE.lock:
        if LIVE.plan is None or pl["step"] >= LIVE.plan["step"]:
            LIVE.plan = pl
    return {"ok": True}


@app.post("/internal/heartbeat")
async def internal_hb(req: Request):
    hb = int((await req.json())["hb"])
    L = LIVE
    L.last_hb_val = hb
    L.last_hb_t = time.time()
    try:
        await asyncio.to_thread(L.bridge.hb, str(hb))   # forwarded as-is: io never invents a heartbeat
    except Exception as e:
        log.warning("hb forward failed: %s", e)
    return {"ok": True}


@app.post("/internal/explain")
async def internal_explain(req: Request):
    ex = await req.json()
    with LIVE.lock:
        if LIVE.explain is None or ex["step"] >= LIVE.explain["step"]:
            LIVE.explain = ex
    return {"ok": True}


# ---------------- public
@app.get("/api/state")
def api_state():
    with LIVE.lock:
        return J(LIVE.public_state())


@app.get("/api/stream")
async def api_stream(request: Request):
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    sub = (loop, q)
    LIVE.subscribers.append(sub)
    with LIVE.lock:
        first = json.dumps(LIVE.public_state(), default=_json_default)

    async def gen():
        try:
            yield f"event: state\ndata: {first}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: state\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                if await request.is_disconnected():
                    break
        finally:
            if sub in LIVE.subscribers:
                LIVE.subscribers.remove(sub)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/events")
async def api_events(req: Request):
    body = await req.json()
    t = body.get("type")
    if t not in EVENT_TYPES:
        return J({"error": f"unknown type {t}"}, 400)
    if t == "grid_outage" and "on" not in (body.get("params") or {}):   # dashboard toggle
        body["params"] = {"on": not LIVE.twin.software_outage}
    with LIVE.lock:
        LIVE.events_q.append({"type": t, "params": body.get("params"), "source": body.get("source", "api")})
    if body.get("source") == "esp32" and t == "ev_fleet":
        log.info("EV fleet arrival from ESP32 ultrasonic")
    return {"ok": True, "queued": t}


@app.post("/api/sim/{cmd}")
async def api_sim(cmd: str, req: Request):
    L = LIVE
    if cmd == "play":
        L.paused = False
    elif cmd == "pause":
        L.paused = True
    elif cmd == "reset":
        await asyncio.to_thread(L.reset)
    elif cmd == "speed":
        x = float((await req.json()).get("x", 1))
        L.speed = x if x in (0.5, 1, 2, 4, 8) else 1.0
    elif cmd == "step":
        await asyncio.to_thread(L.tick)
    else:
        return J({"error": cmd}, 404)
    return {"ok": True, "paused": L.paused, "speed": L.speed}


@app.post("/api/demo/kill-brain")
def api_kill():
    SUP.kill()
    return {"ok": True, "respawn_after_s": P["safety"]["brain_restart_delay_s"]}


@app.post("/api/live/{which}")
async def api_live(which: str, req: Request):
    en = bool((await req.json()).get("enabled"))
    L = LIVE
    with L.lock:
        if which == "sky":
            L.sky.update(enabled=en, lux_ref=None, ref_samples=[], last_mult=1.0)
            if not en:
                L.events_q.append({"type": "live_sky", "params": {"multiplier": 1.0}, "source": "api"})
        elif which == "heat":
            L.heat.update(enabled=en, t_ref=None, last_mult=1.0)
            if not en:
                L.events_q.append({"type": "heat", "params": {"multiplier": 1.0}, "source": "api"})
        else:
            return J({"error": which}, 404)
    return {"ok": True}


@app.get("/api/node/clinic")
def api_node():
    return J(LIVE.node())


@app.post("/api/sensor")
async def api_sensor(req: Request):
    b = await req.json()
    with LIVE.lock:
        LIVE.sensor(b.get("kind"), float(b.get("value", 0)))
    return {"ok": True}


@app.post("/api/sensor/calibrate")
def api_calibrate():
    with LIVE.lock:
        LIVE.sky.update(lux_ref=None, ref_samples=[])
        LIVE.heat.update(t_ref=None)
    return {"ok": True}


@app.get("/api/bench")
def api_bench(fold: str = "winter"):
    f = ROOT / "data" / "bench" / ("results.json" if fold == "winter" else f"results_{fold}.json")
    if not f.exists():
        return J({"error": "no results.json: run python -m gridq.bench.run_days"}, 404)
    return FileResponse(f, media_type="application/json")


@app.get("/api/tune")
def api_tune():
    f = ROOT / "data" / "bench" / "tune.json"
    if not f.exists():
        return J({"error": "no tune.json: run python tools/tune.py"}, 404)
    t = json.loads(f.read_text())
    b1, orc = t["B1"]["total_cost_rs"]["sum"], t["Oracle"]["total_cost_rs"]["sum"]
    rows = [{"name": n, "total_cost_rs": v["summary"]["total_cost_rs"]["sum"], "captured_pct": v["captured_pct"],
             "unserved_kwh": v["summary"]["unserved_kwh_total"]["sum"], "wear_rs": v["summary"]["battery_wear_rs"]["sum"],
             "solve_ms": v["extra"]["solve_ms_median"]} for n, v in t["variants"].items()]
    return J({"b1": b1, "oracle": orc, "rows": rows, "n": t["B1"]["total_cost_rs"]["n"]})


@app.get("/api/meta")
def api_meta(fold: str = "winter"):
    f = ROOT / P["data"]["meta_json"]
    m = json.loads(f.read_text()) if f.exists() else {}
    fm = ROOT / "data" / "models" / ("" if fold == "winter" else fold) / "metrics.json"
    if fm.exists():
        m["forecast"] = json.loads(fm.read_text())
    m["fallback"] = LIVE.ds.fallback
    return J(m)


@app.post("/api/replay")
async def api_replay():
    from gridq.bench.replay import replay_session
    with LIVE.lock:
        snap = copy.deepcopy(LIVE.snapshot)
        sess = copy.deepcopy(LIVE.session)
    return J(await asyncio.to_thread(replay_session, P, snap, sess))


@app.post("/api/dev/cable")
async def api_dev_cable(req: Request):
    """Laptop only (MockBridge): simulate pulling / re-inserting the grid cable."""
    b = LIVE.bridge
    if b.kind != "mock":
        return J({"error": "real hardware: pull the actual cable"}, 400)
    body = await req.json()
    b.cable_present = bool(body.get("present", not b.cable_present))
    return {"ok": True, "cable_present": b.cable_present}


WEB = ROOT / "web"


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=str(WEB)), name="web")


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
