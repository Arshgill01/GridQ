from gridq.io import node_format as NF


def test_lcd_lines_fit():
    for mode, up in (("N", True), ("N", False), ("S", True)):
        for h in (0, 0.5, 11.34, 47.99, 48, 300):
            l1, l2 = NF.lines(mode, up, 1234, h, "Water heater paused +Rs1234567", 0.623, 18.4)
            assert len(l1) <= 16 and all(len(x) <= 16 for x in l2)
            assert all(ord(c) < 128 for c in l1 + "".join(l2))
    assert NF.line1("N", True, 64) == "GRID OK   16:00"
    assert NF.line1("N", False, 64) == "GRID DOWN 16:00"
    assert NF.line1("S", True, 65) == "SAFE MODE 16:15"
    assert NF.secure(11 + 20 / 60) == "SECURE 11h20m"
    assert NF.secure(50) == "SECURE >48h"
    assert NF.batt_pv(0.62, 18.2) == "BATT 62% PV 18kW"
