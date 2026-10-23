"""SAFE-mode rule controller: what the plant does while the MCU is in SAFE. Spec §7.3."""
from __future__ import annotations

from gridq.common.config import BIT, INFLEX
from gridq.control.base import Controller, battery_limits, dg_limit, make_control, run_full


class Safe(Controller):
    name = "SAFE"
    shed_policy = "priority"

    def __init__(self, p, safe_mask: int | None = None):
        super().__init__(p)
        from gridq.common.config import mask_from_loads
        self.safe_mask = safe_mask if safe_mask is not None else mask_from_loads(p["safety"]["safe_mask_loads"])

    def decide(self, obs, ctx=None):
        p = self.p
        on = lambda n: bool(self.safe_mask >> BIT[n] & 1)
        served = {l: (1.0 if on(l) else 0.0) for l in INFLEX}
        pf = {f: (run_full(obs, p, f) if on(f) else 0.0) for f in ("pump", "ev", "heater")}
        net = sum(obs.demand_now[l] * served[l] for l in INFLEX) + sum(pf.values()) - obs.pv_now
        ch_max, dis_max = battery_limits(obs, p)
        ch = dis = dg = 0.0
        if net < 0:
            ch = min(-net, ch_max)
        elif not obs.grid_up:
            if obs.soc_frac < 0.20:
                dg = min(dg_limit(obs, p), net)
            dis = min(dis_max, net - dg)
        return make_control(pf, ch, dis, dg, served=served, policy=self.shed_policy)
