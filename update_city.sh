#!/usr/bin/env bash
# Update or set up a city's data: restaurants → reviews → trust → embeddings.
# Replaces the old setup_city.py orchestrator.
#
# Usage:
#   ./update_city.sh <city>                  # full update (all 4 steps)
#   ./update_city.sh <city> --test           # small data, auto-cleanup (new-city sanity check)
#   ./update_city.sh <city> --step reviews   # one step only
#   ./update_city.sh <city> --from trust     # this step to end (resume from failure)
#   ./update_city.sh <city> --no-service     # don't stop/start guidebot systemd service
#
# Steps:
#   1. restaurants  Selenium scrape under xvfb (search terms: еда, кофейня)
#   2. reviews      Async fetch from 2GIS reviews API (incremental)
#   3. trust        Recompute trust scores
#   4. embeddings   BERT embeddings + FTS5 (REQUIRES bot stopped)
#
# Needs sudo for `systemctl stop|start guidebot`. Add a NOPASSWD rule once:
#   /etc/sudoers.d/guidebot:
#     ubuntu ALL=NOPASSWD: /bin/systemctl stop guidebot, /bin/systemctl start guidebot

set -euo pipefail

cd "$(dirname "$0")"

# ---- args ----
CITY=""
TEST=0
STEP=""
FROM=""
NO_SERVICE=0
TEST_PAGES=2
TEST_RESTAURANTS=10

usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \?//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test)       TEST=1; shift ;;
    --step)       STEP="${2:-}"; shift 2 ;;
    --from)       FROM="${2:-}"; shift 2 ;;
    --no-service) NO_SERVICE=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    -*)           echo "unknown flag: $1" >&2; exit 2 ;;
    *)            CITY="$1"; shift ;;
  esac
done

[[ -z "$CITY" ]] && { usage; exit 2; }

# ---- which steps to run ----
ALL_STEPS=(restaurants reviews trust embeddings)

if [[ -n "$STEP" ]]; then
  STEPS=("$STEP")
elif [[ -n "$FROM" ]]; then
  STEPS=()
  found=0
  for s in "${ALL_STEPS[@]}"; do
    [[ "$s" == "$FROM" ]] && found=1
    [[ $found -eq 1 ]] && STEPS+=("$s")
  done
  [[ ${#STEPS[@]} -eq 0 ]] && { echo "unknown step: $FROM" >&2; exit 2; }
else
  STEPS=("${ALL_STEPS[@]}")
fi

SUFFIX=""; [[ $TEST -eq 1 ]] && SUFFIX="_test"
DB="data/${CITY}${SUFFIX}.db"
CHROMA="data/chroma_${CITY}${SUFFIX}"

# ---- bot service ----
# Always (re)start guidebot at exit if --no-service wasn't passed, even if it
# was already stopped when the script began. "Only restart what I stopped"
# left the bot dead when someone pre-stopped it manually.
if [[ $NO_SERVICE -eq 0 ]] && systemctl is-active --quiet guidebot 2>/dev/null; then
  echo ">> stopping guidebot"
  sudo systemctl stop guidebot
fi

# ---- helpers ----
db_stats() {
  # informational only — never let stats failures kill the pipeline
  [[ ! -f "$DB" ]] && return 0
  uv run --quiet python - "$DB" <<'PY' || true
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
def count(sql):
    try: return db.execute(sql).fetchone()[0]
    except sqlite3.OperationalError: return None
rows = [
    ("restaurants",     count("SELECT COUNT(*) FROM restaurants")),
    ("reviews",         count("SELECT COUNT(*) FROM reviews")),
    ("trusted (>=0.3)", count("SELECT COUNT(*) FROM review_trust WHERE base_trust*burst*recency >= 0.3")),
]
width = max(len(name) for name, _ in rows)
for name, n in rows:
    print(f"  {name:<{width}}  {'-' if n is None else f'{n:,}'}")
PY
  return 0
}

run_step() {
  local step="$1"
  echo
  echo "================================================="
  echo "  STEP: $step"
  echo "================================================="
  local extra=(--db "$DB")  # storage decoupled from city; $DB already carries _test suffix in test mode

  case "$step" in
    restaurants)
      local rextra=("${extra[@]}"); [[ $TEST -eq 1 ]] && rextra+=(--pages "$TEST_PAGES")
      for term in еда кофейня; do
        echo ">> term: $term"
        xvfb-run -a uv run python -m bishkek_food_finder.scraper.restaurants \
          --city "$CITY" --search-term "$term" "${rextra[@]}"
      done ;;
    reviews)
      local rvextra=("${extra[@]}"); [[ $TEST -eq 1 ]] && rvextra+=(--limit "$TEST_RESTAURANTS")
      uv run python -m bishkek_food_finder.scraper.reviews --city "$CITY" "${rvextra[@]}" ;;
    trust)
      uv run python -m bishkek_food_finder.indexer.trust --city "$CITY" "${extra[@]}" ;;
    embeddings)
      uv run python -m bishkek_food_finder.indexer.embeddings --city "$CITY" "${extra[@]}" --chroma "$CHROMA" ;;
    *) echo "unknown step: $step" >&2; exit 2 ;;
  esac

  echo
  echo ">> stats after $step:"
  db_stats
}

# ---- exit handler: restart bot, print resume hint on failure ----
on_exit() {
  local rc=$?
  if [[ $rc -ne 0 ]] && [[ -n "${CURRENT_STEP:-}" ]]; then
    local test_flag=""; [[ $TEST -eq 1 ]] && test_flag=" --test"
    echo
    echo "FAILED at step: $CURRENT_STEP"
    echo "Resume with: ./update_city.sh $CITY --from $CURRENT_STEP$test_flag"
  fi
  if [[ $NO_SERVICE -eq 0 ]]; then
    echo ">> starting guidebot"
    sudo systemctl start guidebot || echo "WARN: failed to start guidebot — check 'systemctl status guidebot'"
  fi
}
trap on_exit EXIT

# ---- run ----
START=$(date +%s)
for step in "${STEPS[@]}"; do
  CURRENT_STEP="$step"
  run_step "$step"
done
CURRENT_STEP=""

ELAPSED=$(($(date +%s) - START))
echo
echo "================================================="
echo "  COMPLETE in ${ELAPSED}s ($DB)"
echo "================================================="
db_stats

if [[ $TEST -eq 1 ]]; then
  echo
  echo ">> cleaning up test data"
  rm -f "$DB"
  rm -rf "$CHROMA"
fi
