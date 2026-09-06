#!/usr/bin/env bash
# Lint all viewer JS files with node --check. Catches syntax errors
# before they break the live studio in the browser.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)/scripts/okf_loom/viewer/static"
FAILED=0
for f in "$DIR"/*.js "$DIR"/../../../../plugins/meridian/*.js; do
  if ! node --check "$f" 2>/dev/null; then
    echo "FAIL: $f"
    node --check "$f" 2>&1 | head -5
    FAILED=1
  fi
done
if [ "$FAILED" -eq 0 ]; then
  echo "All JS files pass syntax check."
else
  exit 1
fi
