#!/usr/bin/env bash
# Build, run, update, back up, or restore GuideBot through Docker Compose.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"
dc() { docker compose "$@"; }
usage() { echo 'Usage: ./manage.sh {init|build|up|stop|status|logs|update CITY [FLAGS]|backup|restore ARCHIVE}'; }
[[ $# -gt 0 ]] || { usage; exit 2; }
COMMAND=$1; shift

case "$COMMAND" in
  logs) exec docker compose logs --tail 100 -f bot ;;
  status) exec docker compose ps ;;
  init|build|up|stop|update|backup|restore) ;;
  *) usage; exit 2 ;;
esac
exec 8>"$ROOT/.manage.lock"
flock -n 8 || { echo 'Another management operation is running.' >&2; exit 1; }

load_paths() {
  mapfile -t SETTINGS < <(dc config --format json | python3 -c '
import json, sys
s = json.load(sys.stdin)["services"]["bot"]
for target in ("/app/data", "/app/logs", "/app/cache"):
    print(next(v["source"] for v in s["volumes"] if v["target"] == target))
print(s["user"])
')
  [[ ${#SETTINGS[@]} -eq 4 ]] || { echo 'Cannot resolve Compose paths.' >&2; exit 1; }
  DATA=${SETTINGS[0]}; LOGS=${SETTINGS[1]}; CACHE=${SETTINGS[2]}; OWNER=${SETTINGS[3]}
  BACKUPS=${BACKUP_DIR:-"$ROOT/backups"}
}

with_bot_stopped() (
  local running=0 id ids oneoff
  ids=$(dc ps --status running -q bot) || return 1
  for id in $ids; do
    oneoff=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.oneoff"}}' "$id") || return 1
    if [[ ${oneoff,,} == true ]]; then
      echo 'A one-off bot command is running; wait for it to finish.' >&2
      return 1
    fi
    running=1
  done
  finish() {
    local rc=$?
    trap - EXIT
    if [[ $running -eq 1 ]]; then
      dc up -d --no-deps --no-recreate --wait --wait-timeout 180 bot || rc=1
    fi
    exit "$rc"
  }
  if [[ $running -eq 1 ]]; then dc stop -t 60 bot; fi
  trap finish EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  "$@"
)

lock_data() {
  [[ ! -L "$DATA/.update.lock" ]] || { echo 'The data lock must not be a symlink.' >&2; return 1; }
  exec 9>>"$DATA/.update.lock"
  flock -n 9 || { echo 'A data writer is running.' >&2; return 1; }
  chown --no-dereference "$OWNER" "$DATA/.update.lock"
}

backup_data() {
  lock_data
  mkdir -p "$BACKUPS"
  chmod 700 "$BACKUPS"
  local name="data-$(date -u +%Y%m%dT%H%M%SZ).tar"
  [[ ! -e "$BACKUPS/$name" ]] || { echo 'Backup already exists.' >&2; return 1; }
  umask 077
  tar -C "$DATA" --exclude='./.update.lock' -cf "$BACKUPS/$name.tmp" .
  mv "$BACKUPS/$name.tmp" "$BACKUPS/$name"
  (cd "$BACKUPS" && sha256sum "$name" > "$name.sha256")
  echo "$BACKUPS/$name"
}

restore_data() {
  local archive=$1
  [[ -f "$archive" && -f "$archive.sha256" ]] || { echo 'Archive and .sha256 file are required.' >&2; return 1; }
  archive=$(realpath "$archive")
  (cd "$(dirname "$archive")" && sha256sum -c "$(basename "$archive").sha256")
  lock_data
  local stage old
  stage=$(mktemp -d "$(dirname "$DATA")/.guidebot-restore.XXXXXX")
  if ! python3 - "$archive" "$stage" <<'PY'
import sqlite3, sys, tarfile
from pathlib import Path, PurePosixPath
archive, output = sys.argv[1:]
with tarfile.open(archive) as tar:
    for member in tar:
        path = PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
            raise ValueError(f'Unsafe archive entry: {member.name}')
    print('Extracting backup...', flush=True)
    tar.extractall(output, filter='data')
for path in Path(output).rglob('*'):
    if path.suffix in ('.db', '.sqlite3'):
        print(f'Checking SQLite: {path.relative_to(output)}', flush=True)
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError(f'Database check failed: {path.name}')
PY
  then
    rm -rf -- "$stage"
    return 1
  fi
  chown -R "$OWNER" "$stage"
  old="${DATA}.before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
  [[ ! -e "$old" ]] || { echo 'Restore destination already exists.' >&2; return 1; }
  mv "$DATA" "$old"
  if ! mv "$stage" "$DATA"; then mv "$old" "$DATA"; return 1; fi
  echo "Restored data; previous data retained at $old"
}

case "$COMMAND" in
  init)
    load_paths
    for path in "$DATA" "$LOGS" "$CACHE"; do
      mkdir -p "$path"
      chown "$OWNER" "$path"
      chmod 750 "$path"
    done
    ;;
  build) dc build bot collector ;;
  up) dc up -d --wait --wait-timeout 180 bot ;;
  stop) dc stop -t 60 bot ;;
  update)
    [[ $# -gt 0 ]] || { usage; exit 2; }
    with_bot_stopped dc run --rm --no-deps -T --interactive=false collector "$@"
    ;;
  backup) load_paths; with_bot_stopped backup_data ;;
  restore)
    [[ $# -eq 1 ]] || { usage; exit 2; }
    load_paths; with_bot_stopped restore_data "$1"
    ;;
esac
