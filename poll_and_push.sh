#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"
source .venv/bin/activate

echo "Starting local ECI poller — fetching every 5 minutes. Press Ctrl+C to stop."

while true; do
  echo ""
  echo "[$(date '+%H:%M:%S')] Fetching ECI data..."

  if python3 generate_results.py; then
    # Sync with remote first, keeping our results.json
    git fetch origin main
    git reset --soft origin/main   # move HEAD to remote tip, keep staged/index
    git add results.json

    if git diff --cached --quiet; then
      echo "[$(date '+%H:%M:%S')] No change in data, skipping push."
    else
      TIMESTAMP=$(python3 -c "import json; d=json.load(open('results.json')); print(d.get('last_updated',''))")
      git commit -m "Refresh ECI results data - ${TIMESTAMP}"
      git push origin main
      git push origin main:gh-pages
      echo "[$(date '+%H:%M:%S')] Pushed updated data: ${TIMESTAMP}"
    fi
  else
    echo "[$(date '+%H:%M:%S')] Fetch failed, will retry in 5 minutes."
  fi

  echo "[$(date '+%H:%M:%S')] Sleeping 5 minutes..."
  sleep 300
done
