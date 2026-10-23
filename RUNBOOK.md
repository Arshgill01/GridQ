# GridQ runbook

## Laptop (development, mock MCU)

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt pytest httpx requests
GRIDQ_MOCK_BRIDGE=1 .venv/bin/python -m gridq.io.app          # http://localhost:8000
.venv/bin/python -m pytest -q                                  # all tests
```

In mock mode a "Pull cable (laptop mock)" button appears on *Break it*. It drives the MockBridge,
which implements the same state machine as the sketch (3 s watchdog, 5 s reclose).

## Data and models (once, with internet)

```bash
.venv/bin/python tools/prepare_iblend.py       # data/iblend/raw/*.zip -> data/demand_15min.csv + demand_meta.json
.venv/bin/python tools/fetch_nasa_power.py     # -> data/solar_hourly.csv (same dates, IIIT-Delhi)
.venv/bin/python -m gridq.forecast.train       # -> data/models/*.joblib + metrics.json
.venv/bin/python -m gridq.bench.run_days       # -> data/bench/results.json (B0/B1/GridQ/Oracle, 150 scenario-days)
```

## UNO Q

Facts verified on our board: Debian 13, aarch64, 4 cores, 3.6 GB RAM, `arduino-app-cli 0.7.0`,
zephyr core 0.52.0, App Lab container `python-apps-base:0.5.0` (Python 3.13.9, numpy/pandas/
fastapi/uvicorn/msgpack preinstalled), router socket `/var/run/arduino-router.sock`.

### 1. Compile the sketch on the board (no flash), to check it builds

```bash
adb shell 'mkdir -p /tmp/gq/sketch' && adb push sketch/sketch.ino sketch/sketch.yaml /tmp/gq/sketch/
adb shell 'cd /tmp/gq/sketch && TMPDIR=/tmp arduino-cli compile --profile default .'
```
Expected: `Sketch uses ~23 KB … Global variables use ~5 KB`.

### 2. Deploy and start the app (this flashes the sketch and stops any other running app)

```bash
tools/deploy_unoq.sh
adb shell arduino-app-cli app start /home/arduino/ArduinoApps/gridq
adb shell arduino-app-cli app logs /home/arduino/ArduinoApps/gridq --follow
```
Phone on the same 2.4 GHz hotspot: `http://<board-ip>:8000` (`adb shell ip -4 addr show wlan0`).

### 3. Hardware acceptance (spec §16 items 1–3)

Stop the GridQ app first (the test owns the Bridge), then run the test inside the app container
or with any Python that has msgpack:
```bash
adb shell arduino-app-cli app stop /home/arduino/ArduinoApps/gridq
adb shell 'cd /home/arduino/ArduinoApps/gridq/python && python3 tools/hw_test.py'
```
What you should see is printed before each phase: double chirp and LED3 green on the first heartbeat,
SAFE (long tone, LED3 red) about 3 s after heartbeats stop *changing*, the relay click with LED4 blue on
grid-open, and reclose after 5 s. Pulling the D3 jumper must click the relay within 100 ms
with or without Linux.

## ESP32 clinic node

1. Arduino IDE: install the ESP32 core, then the libraries **ArduinoJson 7**, **LiquidCrystal I2C**
   (Frank de Brabander) and **ESP32Servo**.
2. Copy `esp32/clinic_node/secrets.h.example` to `secrets.h` and set the hotspot SSID/password and the UNO Q IP.
3. Flash `esp32/clinic_node/clinic_node.ino`. Serial monitor at 115200 prints the I2C scan and the detected sensor.
4. Expected: LCD line 1 `GRID OK   16:15`, line 2 cycles `SECURE …`, the top decision, and `BATT ..% PV ..kW`.
   The servo tracks battery SoC. A hand under the HC-SR04 (< 12 cm, 3 readings) fires one `ev_fleet` event.
   Stop the app on the UNO Q and the LCD shows `LINK LOST / CLINIC LOCAL ON` within 6 s.
5. Blue LED (GPIO2): solid = linked · slow blink = on Wi-Fi, waiting for GridQ · fast blink = no Wi-Fi.
   At power-up the LCD shows `CONNECTING WIFI`, then `FINDING GRIDQ...` until the UNO Q answers (about a
   minute when both boards power up together: Linux boot plus app start). It relinks about 1 s after GridQ answers.
