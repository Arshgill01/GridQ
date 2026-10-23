---
title: GridQ
emoji: ⚡
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
short_description: Offline LP + MPC energy controller for a clinic microgrid
---

# GridQ · live demo

The full GridQ controller running in the cloud: the plant twin on real I-BLEND campus demand and NASA POWER
solar, the quantile-GBM forecaster, and the outage-aware LP/MPC brain, re-planning every 15 sim-minutes.

The real system runs offline on an Arduino UNO Q, where a microcontroller enforces safety. Here that MCU
is replaced by a software mock with the same state machine (3 s heartbeat watchdog, local grid trip,
5 s reclose). Everyone who opens this page shares one simulation.

Code, results and hardware: https://github.com/Arshgill01/GridQ
