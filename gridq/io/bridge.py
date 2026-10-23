"""Bridge to the MCU safety supervisor (spec §10.1).

RealBridge speaks the arduino-router's msgpack-RPC directly over its unix socket
(`/var/run/arduino-router.sock`), so the same code runs inside an App Lab
container or natively on the board. MockBridge emulates the sketch's state
machine (watchdog, local grid trip, reclose delay) for laptop development
(`GRIDQ_MOCK_BRIDGE=1`).

io is the sole owner of the Bridge. It must never invent heartbeat values: `hb()`
is only called with a value that arrived from the brain process.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("gridq.bridge")
ROUTER_SOCK = os.environ.get("APP_SOCKET", "unix:///var/run/arduino-router.sock").replace("unix://", "")


@dataclass
class McuStatus:
    mode: str = "S"             # N | S
    applied_mask: int = 3
    cable_present: bool = True
    relay_energised: bool = False
    uptime_ms: int = 0
    last_hb: str = "0"
    ok: bool = True             # False when the MCU could not be reached

    @staticmethod
    def parse(csv: str) -> "McuStatus":
        f = csv.strip().split(",")
        return McuStatus(mode=f[0], applied_mask=int(f[1]), cable_present=f[2] == "1",
                         relay_energised=f[3] == "1", uptime_ms=int(f[4]), last_hb=f[5])


def outputs_csv(mask: int, safe_mask: int, grid_open_cmd: int, cols: str, led_state: str) -> str:
    assert len(cols) == 13 and all(c in "012345678" for c in cols), cols
    return f"{mask},{safe_mask},{grid_open_cmd},{cols},{led_state}"


class RealBridge:
    kind = "real"

    def __init__(self, path: str = ROUTER_SOCK, timeout: float = 1.0):
        import msgpack
        self._mp = msgpack
        self.path = path
        self.timeout = timeout
        self._sock = None
        self._msgid = 0
        self._lock = threading.Lock()
        self._unpacker = msgpack.Unpacker(raw=False)

    def _connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.path)
        self._sock = s
        self._unpacker = self._mp.Unpacker(raw=False)

    def call(self, method: str, *params):
        with self._lock:
            for attempt in range(2):
                try:
                    if self._sock is None:
                        self._connect()
                    self._msgid = (self._msgid + 1) % 2**31
                    mid = self._msgid
                    self._sock.sendall(self._mp.packb([0, mid, method, list(params)]))
                    deadline = time.monotonic() + self.timeout
                    while True:
                        for msg in self._unpacker:
                            if isinstance(msg, list) and len(msg) == 4 and msg[0] == 1 and msg[1] == mid:
                                if msg[2]:
                                    raise RuntimeError(f"bridge {method}: {msg[2]}")
                                return msg[3]
                        if time.monotonic() > deadline:
                            raise TimeoutError(method)
                        data = self._sock.recv(4096)
                        if not data:
                            raise ConnectionError("router closed")
                        self._unpacker.feed(data)
                except (OSError, ConnectionError, TimeoutError) as e:
                    log.warning("bridge %s failed (%s), reconnecting", method, e)
                    try:
                        self._sock and self._sock.close()
                    finally:
                        self._sock = None
                    if attempt == 1:
                        raise

    def hb(self, value: str) -> str:
        return self.call("hb", str(value))

    def set_outputs(self, csv: str) -> str:
        return self.call("set_outputs", csv)

    def get_status(self) -> McuStatus:
        return McuStatus.parse(self.call("get_status"))


class MockBridge:
    """Python model of sketch/sketch.ino (same timings)."""
    kind = "mock"

    def __init__(self, hb_timeout_ms=3000, reclose_ms=5000):
        self.t0 = time.monotonic()
        self.hb_timeout = hb_timeout_ms / 1000
        self.reclose = reclose_ms / 1000
        self.mode = "S"
        self.last_hb = ""
        self.last_hb_t = -1e9
        self.plan_mask, self.safe_mask, self.grid_open = 127, 3, 0
        self.cols, self.led = "0" * 13, "N"
        self.cable_present = True           # laptop: toggled via /api/dev/cable
        self.relay = False
        self.ok_since = None
        self.lock = threading.Lock()

    def _tick(self):
        now = time.monotonic()
        if self.mode == "N" and now - self.last_hb_t > self.hb_timeout:
            self.mode = "S"
            log.warning("MOCK MCU: heartbeat lost -> SAFE")
        want_open = (not self.cable_present) or self.grid_open == 1
        if want_open:
            self.ok_since = None
            self.relay = True
        elif self.relay:
            self.ok_since = self.ok_since or now
            if now - self.ok_since >= self.reclose:
                self.relay, self.ok_since = False, None

    def hb(self, value: str) -> str:
        with self.lock:
            if value and value != self.last_hb:
                self.last_hb, self.last_hb_t = value, time.monotonic()
                if self.mode == "S":
                    self.mode = "N"
            self._tick()
        return "ok"

    def set_outputs(self, csv: str) -> str:
        f = csv.split(",")
        with self.lock:
            self.plan_mask, self.safe_mask, self.grid_open = int(f[0]), int(f[1]), int(f[2])
            self.cols, self.led = f[3], f[4]
            self._tick()
        return "ok"

    def get_status(self) -> McuStatus:
        with self.lock:
            self._tick()
            return McuStatus(mode=self.mode, applied_mask=self.plan_mask if self.mode == "N" else self.safe_mask,
                             cable_present=self.cable_present, relay_energised=self.relay,
                             uptime_ms=int((time.monotonic() - self.t0) * 1000), last_hb=self.last_hb or "0")


def make_bridge():
    if os.environ.get("GRIDQ_MOCK_BRIDGE", "0") == "1" or not os.path.exists(ROUTER_SOCK):
        if os.environ.get("GRIDQ_MOCK_BRIDGE") != "1":
            log.warning("router socket %s not found: using MockBridge", ROUTER_SOCK)
        return MockBridge()
    return RealBridge()
