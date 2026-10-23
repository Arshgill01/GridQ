#!/usr/bin/env bash
# Assemble the Hugging Face Space (Docker) and upload it.
#   tools/deploy_hf.sh <hf-user>/<space>        # e.g. arshgill01/GridQ
# Needs a one-time `huggingface-cli login` (write token). Local test: docker build -t gridq-hf build/hf-space
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=build/hf-space
rm -rf "$OUT" && mkdir -p "$OUT/data"
cp deploy/hf/Dockerfile deploy/hf/README.md requirements.txt "$OUT/"
rsync -a --exclude __pycache__ gridq config web "$OUT/"
cp data/demand_15min.csv data/demand_meta.json data/solar_hourly.csv "$OUT/data/"
rsync -a data/models "$OUT/data/"
mkdir -p "$OUT/data/bench" && cp data/bench/*.json "$OUT/data/bench/"
du -sh "$OUT"
if [ "${1:-}" ]; then
  huggingface-cli upload "$1" "$OUT" . --repo-type space --commit-message "Deploy GridQ $(git rev-parse --short HEAD)"
fi
