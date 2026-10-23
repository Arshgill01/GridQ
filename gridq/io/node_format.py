"""LCD lines for the ESP32 clinic node (spec §10.4). 16 ASCII chars max; 'Rs' not the rupee glyph."""
from __future__ import annotations

W = 16


def fit(s: str) -> str:
    s = s.encode("ascii", "replace").decode()
    return s[:W]


def clock(step_abs: int) -> str:
    sod = step_abs % 96
    return f"{sod // 4:02d}:{(sod % 4) * 15:02d}"


def line1(mode: str, grid_up: bool, step_abs: int) -> str:
    head = "SAFE MODE" if mode == "S" else ("GRID OK" if grid_up else "GRID DOWN")
    return fit(f"{head:<10}{clock(step_abs)}")


def secure(hours: float) -> str:
    if hours >= 48:
        return fit("SECURE >48h")
    h = int(hours)
    m = int(round((hours - h) * 60))
    if m == 60:
        h, m = h + 1, 0
    return fit(f"SECURE {h}h{m:02d}m")


def batt_pv(soc_frac: float, pv_kw: float) -> str:
    return fit(f"BATT {100 * soc_frac:.0f}% PV {pv_kw:.0f}kW")


def lines(mode: str, grid_up: bool, step_abs: int, survival_h: float, top_expl: str | None,
          soc_frac: float, pv_kw: float) -> tuple[str, list[str]]:
    l2 = [secure(survival_h)]
    l2.append(fit(top_expl) if top_expl else fit("PLAN NOMINAL"))
    l2.append(batt_pv(soc_frac, pv_kw))
    return line1(mode, grid_up, step_abs), l2
