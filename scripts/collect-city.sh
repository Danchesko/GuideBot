#!/usr/bin/env bash
# Update or set up a city's data: restaurants → reviews → trust → embeddings.
# Run through ./manage.sh update so the bot is stopped before shared data changes.
# Usage: scripts/collect-city.sh <almaty|bishkek> [--test] [--step STEP | --from STEP]
# Requires Python dependencies, flock, and Chrome/Xvfb for restaurant collection.

set -euo pipefail

cd "$(dirname "$0")/.."

# ---- args ----
CITY=""
TEST=0
STEP=""
FROM=""
TEST_PAGES=2
TEST_RESTAURANTS=10

usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \?//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test)       TEST=1; shift ;;
    --step)       STEP="${2:-}"; shift 2 ;;
    --from)       FROM="${2:-}"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    -*)           echo "unknown flag: $1" >&2; exit 2 ;;
    *)            CITY="$1"; shift ;;
  esac
done

[[ -z "$CITY" ]] && { usage; exit 2; }
case "$CITY" in
  almaty|bishkek) ;;
  *) echo "unknown city: $CITY" >&2; exit 2 ;;
esac
if [[ -n "$STEP" && -n "$FROM" ]]; then
  echo "--step and --from cannot be combined" >&2
  exit 2
fi
if [[ -n "$STEP" ]]; then
  case "$STEP" in
    restaurants|reviews|trust|embeddings) ;;
    *) echo "unknown step: $STEP" >&2; exit 2 ;;
  esac
fi

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

# Use one lock for all cities because the bot serves both.
command -v flock >/dev/null || { echo "flock is required" >&2; exit 1; }
mkdir -p data
exec 9>data/.update.lock
flock -n 9 || { echo "another data update is running" >&2; exit 1; }

# ---- helpers ----
db_stats() {
  # informational only — never let stats failures kill the pipeline
  [[ ! -f "$DB" ]] && return 0
  python - "$DB" <<'PY' || true
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
        xvfb-run -a python -m bishkek_food_finder.scraper.restaurants \
          --city "$CITY" --search-term "$term" "${rextra[@]}"
      done ;;
    reviews)
      local rvextra=("${extra[@]}"); [[ $TEST -eq 1 ]] && rvextra+=(--limit "$TEST_RESTAURANTS")
      python -m bishkek_food_finder.scraper.reviews --city "$CITY" "${rvextra[@]}" ;;
    trust)
      python -m bishkek_food_finder.indexer.trust --city "$CITY" "${extra[@]}" ;;
    embeddings)
      python -m bishkek_food_finder.indexer.embeddings --city "$CITY" "${extra[@]}" --chroma "$CHROMA" ;;
    *) echo "unknown step: $step" >&2; exit 2 ;;
  esac

  echo
  echo ">> stats after $step:"
  db_stats
}

# ---- failure report ----
on_exit() {
  local rc=$?
  if [[ $rc -ne 0 ]] && [[ -n "${CURRENT_STEP:-}" ]]; then
    local test_flag=""; [[ $TEST -eq 1 ]] && test_flag=" --test"
    echo
    echo "FAILED at step: $CURRENT_STEP"
    echo "Resume with: ./manage.sh update $CITY --from $CURRENT_STEP$test_flag"
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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
