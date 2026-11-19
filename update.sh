#!/usr/bin/env bash

# Apply the current checkout to the live local Omnigent services.
# This script never fetches, merges, resets, or switches Git branches.
# When called by a session runner, it hands itself to user systemd so the
# updater survives the host restart that it performs later.

set -Eeuo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly repo_root
readonly server_unit="omnigent-server.service"
readonly host_unit="omnigent-host.service"
readonly api_base_url="http://127.0.0.1:6767"
readonly health_url="http://127.0.0.1:6767/health"
readonly data_root="${OMNIGENT_DATA_DIR:-${HOME}/.omnigent}"
readonly live_db="${data_root}/chat.db"
readonly backup_dir="$(dirname -- "$live_db")/backups"
readonly update_state_path="${data_root}/update-state.json"
readonly backup_keep="${OMNIGENT_UPDATE_BACKUP_KEEP:-2}"
readonly health_timeout="${OMNIGENT_UPDATE_HEALTH_TIMEOUT:-300}"

# Type checks and the web build run close to this host's memory ceiling.
export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=3072}"

recovery_snapshot_path=""
backup_path=""
handoff_queued=0
update_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
update_started_epoch=$(date +%s)
update_finished_at=""
update_result="failed"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"

fail() {
  printf 'update.sh: %s\n' "$*" >&2
  exit 1
}

handoff_in_session_update() {
  if [[ "${OMNIGENT:-}" == "1" || -n "${OMNIGENT_RUNNER_ID:-}" ]]; then
    command -v systemd-run >/dev/null 2>&1 || fail \
      "systemd-run is required to update from inside an Omnigent session"
    # Keep the finished unit so a failure can still be inspected, but clear a
    # previous terminal instance so this run can reuse the name.
    systemctl --user reset-failed omnigent-update.service >/dev/null 2>&1 || true
    systemd-run \
      --user \
      --unit=omnigent-update.service \
      --no-block \
      --working-directory="$repo_root" \
      --setenv=OMNIGENT= \
      --setenv=OMNIGENT_RUNNER_ID= \
      --setenv=HOME="${HOME:-}" \
      --setenv=PATH="${PATH:-/usr/bin:/bin}" \
      --setenv=XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
      --setenv=DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
      "$repo_root/update.sh" || fail \
      "could not queue the update in the user systemd manager"
    handoff_queued=1
    printf 'Update queued in omnigent-update.service.\n'
    printf 'Monitor it with: systemctl --user status omnigent-update.service\n'
    printf 'Read its log with: journalctl --user -u omnigent-update.service --since "%s"\n' \
      "$update_started_at"
    exit 0
  fi
}

cleanup_recovery_snapshot() {
  if [[ -n "$recovery_snapshot_path" && -e "$recovery_snapshot_path" ]]; then
    rm -f -- "$recovery_snapshot_path"
  fi
}

write_update_state() {
  local status="$1"
  update_finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  UPDATE_STATE_PATH="$update_state_path" \
    UPDATE_STATUS="$status" \
    UPDATE_REPO="$repo_root" \
    UPDATE_BRANCH="${branch:-}" \
    UPDATE_COMMIT="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)" \
    UPDATE_CODE_REVISION="${code_revision:-}" \
    UPDATE_DB_REVISION="$(read_db_state 2>/dev/null || echo unknown)" \
    UPDATE_STARTED_AT="$update_started_at" \
    UPDATE_FINISHED_AT="$update_finished_at" \
    UPDATE_DURATION_SECONDS="$(( $(date +%s) - update_started_epoch ))" \
    UPDATE_BACKUP="${backup_path:-}" \
    "$repo_root/.venv/bin/python" - <<'PY'
import json
import os
from pathlib import Path

state_path = Path(os.environ["UPDATE_STATE_PATH"])
state = {
    "status": os.environ["UPDATE_STATUS"],
    "repo": os.environ["UPDATE_REPO"],
    "branch": os.environ["UPDATE_BRANCH"],
    "commit": os.environ["UPDATE_COMMIT"],
    "code_revision": os.environ["UPDATE_CODE_REVISION"],
    "database_revision": os.environ["UPDATE_DB_REVISION"],
    "started_at": os.environ["UPDATE_STARTED_AT"],
    "finished_at": os.environ["UPDATE_FINISHED_AT"],
    "duration_seconds": int(os.environ["UPDATE_DURATION_SECONDS"]),
    "backup": os.environ["UPDATE_BACKUP"] or None,
}
state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
temporary = state_path.with_suffix(".json.tmp")
temporary.write_text(json.dumps(state, indent=2) + "\n")
temporary.chmod(0o600)
temporary.replace(state_path)
PY
}

finish() {
  local status=$?
  cleanup_recovery_snapshot
  if (( handoff_queued )); then
    exit "$status"
  fi
  if [[ "$update_result" != "failed" ]]; then
    status=0
  fi
  write_update_state "$update_result" 2>/dev/null || \
    printf 'update.sh: could not write %s\n' "$update_state_path" >&2
  exit "$status"
}

trap finish EXIT

handoff_in_session_update

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

read_db_state() {
  "$repo_root/.venv/bin/python" - "$live_db" <<'PY'
import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print("missing")
    raise SystemExit(0)

with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    revision = (
        connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        if "alembic_version" in tables
        else "none"
    )

print(revision)
PY
}

backup_live_db() {
  local timestamp
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  backup_path="${backup_dir}/chat-before-update-${timestamp}.db"
  printf 'Pending migration detected; creating SQLite backup: %s\n' "$backup_path"
  .venv/bin/python - "$live_db" "$backup_path" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

source_path = Path(sys.argv[1])
backup_path = Path(sys.argv[2])
backup_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
try:
    with sqlite3.connect(f"file:{source_path}?mode=ro", uri=True) as source:
        # Pin a WAL snapshot so concurrent writes cannot restart the backup indefinitely.
        source.execute("BEGIN")
        source.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        with sqlite3.connect(backup_path) as destination:
            source.backup(destination, pages=8192, sleep=0.1)
        source.rollback()
except BaseException:
    backup_path.unlink(missing_ok=True)
    raise
PY
}

wait_for_server() {
  local attempt
  for attempt in $(seq 1 "$health_timeout"); do
    if curl -fsS "$health_url" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

require_disk_space() {
  local needs_backup="$1" needed_bytes available_bytes database_bytes
  database_bytes=0
  if [[ "$needs_backup" == "yes" && -e "$live_db" ]]; then
    database_bytes=$(stat -c %s "$live_db")
  fi
  # A pending migration copies the database; the dependency sync and the web
  # build need room of their own. Compare in KiB from df -Pk.
  needed_bytes=$(( database_bytes + 2 * 1024 * 1024 * 1024 ))
  local target="$data_root"
  while [[ ! -e "$target" && "$target" != "/" ]]; do
    target=$(dirname -- "$target")
  done
  available_bytes=$(( $(df -Pk "$target" | awk 'NR == 2 {print $4}') * 1024 ))
  if (( available_bytes >= needed_bytes )); then
    printf 'Free space: %d MiB (need %d MiB).\n' \
      $(( available_bytes / 1024 / 1024 )) $(( needed_bytes / 1024 / 1024 ))
    return 0
  fi
  printf 'Insufficient free space on %s: %d MiB available, %d MiB required.\n' \
    "$data_root" $(( available_bytes / 1024 / 1024 )) $(( needed_bytes / 1024 / 1024 )) >&2
  if [[ -d "$backup_dir" ]]; then
    printf 'Existing backups (keep the newest; OMNIGENT_UPDATE_BACKUP_KEEP=%s):\n' \
      "$backup_keep" >&2
    du -h -- "$backup_dir"/chat-before-update-*.db 2>/dev/null >&2 || true
  fi
  fail "free up space before updating"
}

prune_old_backups() {
  local keep="$backup_keep" removed=0 path
  [[ "$keep" =~ ^[0-9]+$ ]] || fail "OMNIGENT_UPDATE_BACKUP_KEEP must be a number"
  (( keep >= 1 )) || fail "OMNIGENT_UPDATE_BACKUP_KEEP must keep at least one backup"
  shopt -s nullglob
  # Timestamped names sort newest last; never drop the copy this run created.
  local -a backups
  mapfile -t backups < <(printf '%s\n' "$backup_dir"/chat-before-update-*.db | sort -r)
  shopt -u nullglob
  for path in "${backups[@]:keep}"; do
    [[ "$path" == "${backup_path:-}" ]] && continue
    # A copy that was opened read-only leaves -wal/-shm siblings behind.
    rm -f -- "$path" "${path}-wal" "${path}-shm" &&
      printf 'Pruned superseded backup: %s\n' "$path" && removed=$((removed + 1))
  done
  (( removed > 0 )) || printf 'Backup retention: keeping %d newest copy or copies.\n' "$keep"
}

wait_for_host_connection() {
  local attempt host_pid
  for attempt in $(seq 1 "$health_timeout"); do
    host_pid=$(systemctl --user show "$host_unit" --property=MainPID --value)
    if [[ "$host_pid" =~ ^[1-9][0-9]*$ ]] && \
      journalctl --user -u "$host_unit" --since "-90 seconds" --no-pager | \
        grep -F "omnigent[$host_pid]:" | grep -F "Connected as" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

capture_live_sessions() {
  recovery_snapshot_path=$(mktemp "${XDG_RUNTIME_DIR}/omnigent-update-recovery.XXXXXX")
  chmod 600 "$recovery_snapshot_path"
  .venv/bin/python -m omnigent.update_recovery snapshot \
    --db "$live_db" \
    --output "$recovery_snapshot_path"
}

recover_live_sessions() {
  [[ -n "$recovery_snapshot_path" ]] || return 0
  .venv/bin/python -m omnigent.update_recovery recover \
    --snapshot "$recovery_snapshot_path" \
    --base-url "$api_base_url" || fail "one or more live sessions could not be recovered"
}

require_command git
require_command uv
require_command rg
require_command corepack
require_command curl
require_command systemctl
require_command journalctl
require_command mktemp

cd "$repo_root"

branch=$(git branch --show-current)
[[ -n "$branch" ]] || fail "the checkout is in detached HEAD state"
printf 'Updating Omnigent from %s (%s)\n' "$repo_root" "$branch"

for unit in "$server_unit" "$host_unit"; do
  unit_root=$(systemctl --user show "$unit" --property=WorkingDirectory --value)
  [[ "$unit_root" == "$repo_root" ]] || fail \
    "$unit uses WorkingDirectory=$unit_root instead of $repo_root"
done

printf 'Current services: '
systemctl --user is-active "$server_unit" "$host_unit" || true

printf 'Synchronizing locked backend dependencies...\n'
sync_extras=(--extra tracing)
credential_env="$HOME/.config/omnigent/github-app.env"
if [[ -r "$credential_env" ]] && rg -q '^OMNIGENT_CREDENTIAL_CIPHER=azure_key_vault$' "$credential_env"; then
  sync_extras+=(--extra azure-key-vault)
fi
uv sync --frozen --group dev "${sync_extras[@]}" --inexact
uv pip check --python .venv/bin/python
printf 'Checking native harness imports...\n'
.venv/bin/python -c \
  'from omnigent.runtime.tool_result_replay import sanitize_replayed_image_blocks; from omnigent.harnesses.codex_native import app_server'
# Resolve the target revision and the database state before the slow frontend
# build, so a stray migration head or a full disk fails in seconds.
migration_output=$(.venv/bin/python -m alembic -c omnigent/db/alembic.ini heads)
mapfile -t migration_heads < <(printf '%s\n' "$migration_output" | awk 'NF {print $1}')
[[ ${#migration_heads[@]} -eq 1 ]] || fail \
  "expected one Alembic head, found ${#migration_heads[@]}; run scripts/merge_migration_heads.py"
code_revision=${migration_heads[0]}

db_state=$(read_db_state)
db_revision="$db_state"
printf 'Database revision: %s (code head %s)\n' "$db_revision" "$code_revision"
if [[ "$db_revision" == "missing" || "$db_revision" == "$code_revision" ]]; then
  require_disk_space no
else
  require_disk_space yes
fi

printf 'Synchronizing locked frontend dependencies...\n'
corepack pnpm install --frozen-lockfile
printf 'Building embedded web UI...\n'
corepack pnpm --filter web run build
[[ -s omnigent/server/static/web-ui/index.html ]] || fail "web UI build produced no index.html"

if [[ "$db_revision" != "missing" && "$db_revision" != "$code_revision" ]]; then
  backup_live_db
fi

printf 'Capturing live sessions before service restart...\n'
capture_live_sessions

printf 'Restarting %s...\n' "$server_unit"
systemctl --user daemon-reload
systemctl --user restart "$server_unit"
if ! wait_for_server; then
  journalctl --user -u "$server_unit" -n 80 --no-pager >&2
  fail "server did not become healthy within ${health_timeout} seconds"
fi

db_state=$(read_db_state)
db_revision="$db_state"
if [[ "$db_revision" != "$code_revision" ]]; then
  journalctl --user -u "$server_unit" -n 80 --no-pager >&2
  fail "database revision is $db_revision after the migration; expected $code_revision"
fi

printf 'Restarting %s...\n' "$host_unit"
systemctl --user restart "$host_unit"
systemctl --user is-active --quiet "$server_unit" || fail "$server_unit is not active"
systemctl --user is-active --quiet "$host_unit" || fail "$host_unit is not active"
if ! wait_for_host_connection; then
  journalctl --user -u "$host_unit" -n 80 --no-pager >&2
  fail "host did not register within ${health_timeout} seconds"
fi
curl -fsS "$health_url" >/dev/null || fail "server health check failed after host restart"
recover_live_sessions

db_state=$(read_db_state)
db_revision="$db_state"
[[ "$db_revision" == "$code_revision" ]] || \
  fail "database revision is $db_revision; expected $code_revision"

prune_old_backups
update_result="success"
printf 'Services updated successfully.\n'
printf 'Wrote %s\n' "$update_state_path"
systemctl --user is-active "$server_unit" "$host_unit"
journalctl --user -u "$server_unit" -u "$host_unit" -n 30 --no-pager
