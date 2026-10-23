#!/usr/bin/env bash
# Assemble the GridQ App Lab app and push it to the UNO Q over adb.
#   tools/deploy_unoq.sh            # push to ~/ArduinoApps/gridq on the board
# Then on the board:  arduino-app-cli app start ~/ArduinoApps/gridq
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=build/gridq-app
rm -rf "$OUT" && mkdir -p "$OUT/python" "$OUT/sketch"
cp deploy/app.yaml "$OUT/"
cp deploy/main.py "$OUT/python/main.py"
grep -v -E '^(pandas|PyYAML|fastapi|uvicorn|msgpack)==' requirements.txt > "$OUT/python/requirements.txt"   # already in the base image
rsync -a --exclude __pycache__ gridq config web "$OUT/python/"
mkdir -p "$OUT/python/data"
for f in demand_15min.csv demand_meta.json solar_hourly.csv; do [ -f "data/$f" ] && cp "data/$f" "$OUT/python/data/"; done
rsync -a data/models data/bench "$OUT/python/data/" 2>/dev/null || true
[ -d build/wheels ] && rsync -a build/wheels "$OUT/python/"
cp sketch/sketch.ino sketch/sketch.yaml "$OUT/sketch/"
du -sh "$OUT"
adb shell 'mkdir -p /home/arduino/ArduinoApps'
adb push "$OUT/." /home/arduino/ArduinoApps/gridq/ >/dev/null
adb shell 'ls /home/arduino/ArduinoApps/gridq /home/arduino/ArduinoApps/gridq/python'
echo "pushed. start with:  adb shell arduino-app-cli app start /home/arduino/ArduinoApps/gridq"
