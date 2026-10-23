"""Hardware acceptance for the MCU sketch (spec §16 items 1-3), over the real Bridge.

Run on the UNO Q (inside the GridQ App Lab container or anywhere msgpack + the router
socket are available):
    python tools/hw_test.py

What you should see/hear is printed before each phase. The cable test (item 1-2)
needs no Linux at all: pull the D3 jumper at any time and the relay must click.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gridq.io.bridge import RealBridge, outputs_csv  # noqa: E402


def status(b):
    s = b.get_status()
    return f"mode={s.mode} mask={s.applied_mask} cable={'IN' if s.cable_present else 'PULLED'} " \
           f"relay={'ISLANDED' if s.relay_energised else 'grid'} up={s.uptime_ms}ms hb={s.last_hb}"


def phase(title):
    print(f"\n=== {title}")


def main():
    b = RealBridge(timeout=2.0)
    phase("0. read status (expect mode=S at first: SAFE until the first heartbeat)")
    print(status(b))
    hb = int(time.time()) % 100000

    phase("1. heartbeats every 500 ms for 8 s -> expect double chirp, LED3 GREEN, mode=N, matrix bars 1..8")
    t0 = time.time()
    while time.time() - t0 < 8:
        hb += 1
        b.hb(str(hb))
        cols = "".join(str((i + int(time.time())) % 9) for i in range(12)) + "0"
        b.set_outputs(outputs_csv(127, 3, 0, cols, "N"))
        print(status(b), end="\r")
        time.sleep(0.5)
    print()

    phase("2. same heartbeat VALUE repeated (stale) -> MCU must still go SAFE after 3 s: long tone, LED3 RED")
    t0 = time.time()
    entered = None
    while time.time() - t0 < 6:
        b.hb(str(hb))                      # not new: must not feed the watchdog
        s = b.get_status()
        if s.mode == "S" and entered is None:
            entered = time.time() - t0
        print(status(b), end="\r")
        time.sleep(0.25)
    print(f"\nSAFE entered after {entered:.2f}s" if entered else "\nFAIL: never entered SAFE")

    phase("3. heartbeats resume -> double chirp, mode=N")
    for _ in range(4):
        hb += 1
        b.hb(str(hb))
        time.sleep(0.5)
    print(status(b))

    phase("4. constrained LED (amber) for 3 s")
    for _ in range(6):
        hb += 1
        b.hb(str(hb))
        b.set_outputs(outputs_csv(31, 3, 0, "8808800800000", "C"))
        time.sleep(0.5)

    phase("5. software grid open (grid_open_cmd=1) -> relay CLICK (islanded), LED4 BLUE; then 0 -> reclose after 5 s")
    for i in range(24):
        hb += 1
        b.hb(str(hb))
        b.set_outputs(outputs_csv(127, 3, 1 if i < 6 else 0, "8888888088880", "N"))
        print(status(b), end="\r")
        time.sleep(0.5)
    print()

    phase("6. cable test: pull the D3 jumper now (relay must click within 100 ms), re-insert (reclose after 5 s). 20 s")
    t0 = time.time()
    while time.time() - t0 < 20:
        hb += 1
        b.hb(str(hb))
        print(status(b), end="\r")
        time.sleep(0.5)
    print("\nstop sending heartbeats now -> SAFE in 3 s; the cable trip still works in SAFE (no Linux involved)")


if __name__ == "__main__":
    main()
