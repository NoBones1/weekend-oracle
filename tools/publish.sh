#!/bin/bash
# Copy the Oracle's public files from this folder (the source of truth, inside
# HyperTrader10) to the public GitHub repo that runs the schedule and hosts the
# page. Private notes (HANDOFF.md, the Mac launcher) and data never leave.
#   bash tools/publish.sh "what changed"
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
REPO="NoBones1/weekend-oracle"
PUB="$HERE/.publish"
MSG="${1:-Update from HyperTrader10/weekend-oracle}"

if [ ! -d "$PUB/.git" ]; then
  gh repo clone "$REPO" "$PUB" -- -q
fi
cd "$PUB"
git checkout -q main 2>/dev/null || git checkout -q -b main
git pull -q --rebase origin main 2>/dev/null || true   # the Monday run commits status/last-run.json

FILES=(oracle.py page.html markets.json markets_extra.json README.md .gitignore
       tools/cache_guard.py tools/health.py tools/export_markets.mjs tools/publish.sh
       tests/test_oracle.py tests/test_tools.py .github/workflows/oracle.yml)
for f in "${FILES[@]}"; do
  mkdir -p "$(dirname "$f")"
  cp "$HERE/$f" "$f"
done
git add -A
if git diff --cached --quiet; then echo "Nothing new to publish."; exit 0; fi
git commit -qm "$MSG"
git push -q origin main
echo "Published to https://github.com/$REPO"
