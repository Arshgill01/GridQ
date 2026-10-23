#!/usr/bin/env bash
# Print docs/onepager/onepager.html to a one-page A4 PDF (docs/GridQ-Project-Description.pdf) with headless Chrome.
set -euo pipefail
cd "$(dirname "$0")/.."
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$CHROME" --headless=new --disable-gpu --no-pdf-header-footer --allow-file-access-from-files --virtual-time-budget=4000 \
  --print-to-pdf="$PWD/docs/GridQ-Project-Description.pdf" "file://$PWD/docs/onepager/onepager.html" 2>/dev/null
pages=$(grep -a -c "/Type /Page$\|/Type /Page[^s]" docs/GridQ-Project-Description.pdf || true)
echo "docs/GridQ-Project-Description.pdf · pages: $pages"
