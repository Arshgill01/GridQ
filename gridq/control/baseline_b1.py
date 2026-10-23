"""B1: smart ToD rules, what a careful facility manager would program. Spec §7.2.

One addition over the spec (DECISIONS.md D5): B1 also discharges to keep grid
import under the 40 kW limit, because any competent manager would program that
and a weaker baseline would flatter GridQ. It also refills the battery from the
grid during the cheap solar-tariff hours (09-17), within the import limit.
"""
from __future__ import annotations

from gridq.common.config import DT, INFLEX
from gridq.control.base import (Controller, active_remaining, battery_limits, dg_limit,
                                earliest_session, in_hours, make_control, run_full)

RESERVE_FRAC = 0.40


class B1(Controller):
    name = "B1"
    shed_policy = "baseline"

    def decide(self, obs, ctx=None):
        p, h, L = self.p, obs.hour, self.p["loads"]
        cap_kw = p["grid"]["import_limit_kw"]
        pf = {
            "pump": run_full(obs, p, "pump") if in_hours(h, L["pump"]["b1_start"], L["pump"]["window"][1]) else 0.0,
            "heater": run_full(obs, p, "heater") if in_hours(h, L["heater"]["b1_start"], L["heater"]["window"][1]) else 0.0,
            "ev": 0.0,
        }
        base = sum(obs.demand_now[l] for l in INFLEX) + pf["pump"] + pf["heater"]
        surplus = obs.pv_now - base
        ev_max = run_full(obs, p, "ev")
        if ev_max > 0:
            s = earliest_session(obs, "ev")
            hours_left = max(DT, (s["deadline"] - obs.k) * DT)
            if active_remaining(obs, "ev") / hours_left >= 0.9 * L["ev"]["p_kw"]:
                pf["ev"] = ev_max                                   # deadline guard
            elif in_hours(h, 23, 6):
                pf["ev"] = ev_max
            elif surplus > 0:
                pf["ev"] = min(ev_max, surplus)
        net = base + pf["ev"] - obs.pv_now
        ch_max, dis_any = battery_limits(obs, p)
        _, dis_res = battery_limits(obs, p, floor_frac=RESERVE_FRAC)
        ch = dis = dg = 0.0
        if net < 0:
            ch = min(-net, ch_max)
        elif not obs.grid_up:
            if obs.soc_frac < 0.25:
                dg = min(dg_limit(obs, p), net)
            dis = min(dis_any, net - dg)
        else:
            if in_hours(h, p["grid"]["tariff_rs_per_kwh"]["solar"]["from"], p["grid"]["tariff_rs_per_kwh"]["solar"]["to"]):
                ch = min(ch_max, max(0.0, cap_kw - net))           # refill at the cheap solar-hours tariff
            if in_hours(h, 18, 23):
                dis = min(dis_res, net)
            if net - dis > cap_kw:                                   # keep import under the limit
                dis = min(dis_any, net - cap_kw)
        return make_control(pf, ch, dis, dg, policy=self.shed_policy)
