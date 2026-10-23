"""B0: inverter default (self-consumption, battery as backup only). Spec §7.1.

Addition (DECISIONS.md D5): like any backup-mode inverter, B0 recharges the battery
from the grid when it is below full (within the import limit); otherwise one
outage would leave its battery empty for the rest of the run.
"""
from __future__ import annotations

from gridq.common.config import INFLEX
from gridq.control.base import (Controller, battery_limits, dg_limit, in_hours, make_control,
                                run_full)


class B0(Controller):
    name = "B0"
    shed_policy = "baseline"

    def decide(self, obs, ctx=None):
        p, h, L = self.p, obs.hour, self.p["loads"]
        pf = {
            "pump": run_full(obs, p, "pump") if in_hours(h, L["pump"]["b0_start"], L["pump"]["window"][1]) else 0.0,
            "heater": run_full(obs, p, "heater") if in_hours(h, L["heater"]["b0_start"], L["heater"]["window"][1]) else 0.0,
            "ev": run_full(obs, p, "ev"),
        }
        net = sum(obs.demand_now[l] for l in INFLEX) + sum(pf.values()) - obs.pv_now
        ch_max, dis_max = battery_limits(obs, p)
        ch = dis = dg = 0.0
        if net < 0:
            ch = min(-net, ch_max)
        elif obs.grid_up:
            # backup-mode inverter keeps the battery topped up from the grid, within import headroom
            ch = min(ch_max, max(0.0, p["grid"]["import_limit_kw"] - net))
        else:
            if obs.soc_frac < 0.20:
                dg = min(dg_limit(obs, p), net)
            dis = min(dis_max, net - dg)
        return make_control(pf, ch, dis, dg, policy=self.shed_policy)
