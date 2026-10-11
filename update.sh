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
readonly live_db="/home/vscunha/.omnigent/chat.db"

recovery_snapshot_path=""

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
    systemd-run \
      --user \
      --unit=omnigent-update.service \
      --collect \
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
    printf 'Update queued in omnigent-update.service.\n'
    printf 'Monitor it with: systemctl --user status omnigent-update.service\n'
    exit 0
  fi
}

cleanup_recovery_snapshot() {
  if [[ -n "$recovery_snapshot_path" && -e "$recovery_snapshot_path" ]]; then
    rm -f -- "$recovery_snapshot_path"
  fi
}

trap cleanup_recovery_snapshot EXIT

handoff_in_session_update

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

read_db_state() {
  .venv/bin/python - "$live_db" <<'PY'
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
  local timestamp backup_path
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  backup_path="$(dirname -- "$live_db")/backups/chat-before-update-${timestamp}.db"
  printf 'Pending migration detected; creating SQLite backup: %s\n' "$backup_path"
  .venv/bin/python - "$live_db" "$backup_path" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

source_path = Path(sys.argv[1])
backup_path = Path(sys.argv[2])
backup_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
if backup_path.exists():
    raise SystemExit(f"backup already exists: {backup_path}")

with sqlite3.connect(f"file:{source_path}?mode=ro", uri=True) as source:
    with sqlite3.connect(backup_path) as destination:
        source.backup(destination)
os.chmod(backup_path, 0o600)
PY
}

wait_for_server() {
  local attempt
  for attempt in $(seq 1 60); do
    if curl -fsS "$health_url" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_host_connection() {
  local attempt host_pid
  for attempt in $(seq 1 60); do
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

printf 'Capturing live sessions before service restart...\n'
capture_live_sessions

printf 'Synchronizing locked backend dependencies...\n'
uv sync --frozen --group dev --extra tracing --inexact
uv pip check --python .venv/bin/python
printf 'Checking native harness imports...\n'
.venv/bin/python -c \
  'from omnigent.runtime.tool_result_replay import sanitize_replayed_image_blocks; from omnigent.harnesses.codex_native import app_server'
printf 'Synchronizing locked frontend dependencies...\n'
corepack pnpm install --frozen-lockfile
printf 'Building embedded web UI...\n'
corepack pnpm --filter web run build
[[ -s omnigent/server/static/web-ui/index.html ]] || fail "web UI build produced no index.html"

migration_output=$(uv run --no-sync alembic -c omnigent/db/alembic.ini heads)
mapfile -t migration_heads < <(printf '%s\n' "$migration_output" | awk 'NF {print $1}')
[[ ${#migration_heads[@]} -eq 1 ]] || fail "expected one Alembic head, found ${#migration_heads[@]}"
code_revision=${migration_heads[0]}

db_state=$(read_db_state)
db_revision="$db_state"
if [[ "$db_revision" != "missing" && "$db_revision" != "$code_revision" ]]; then
  backup_live_db
fi

printf 'Restarting %s...\n' "$server_unit"
systemctl --user daemon-reload
systemctl --user restart "$server_unit"
if ! wait_for_server; then
  journalctl --user -u "$server_unit" -n 80 --no-pager >&2
  fail "server did not become healthy within 60 seconds"
fi

printf 'Restarting %s...\n' "$host_unit"
systemctl --user restart "$host_unit"
systemctl --user is-active --quiet "$server_unit" || fail "$server_unit is not active"
systemctl --user is-active --quiet "$host_unit" || fail "$host_unit is not active"
if ! wait_for_host_connection; then
  journalctl --user -u "$host_unit" -n 80 --no-pager >&2
  fail "host did not register within 60 seconds"
fi
curl -fsS "$health_url" >/dev/null || fail "server health check failed after host restart"
recover_live_sessions

db_state=$(read_db_state)
db_revision="$db_state"
[[ "$db_revision" == "$code_revision" ]] || \
  fail "database revision is $db_revision; expected $code_revision"

printf 'Services updated successfully.\n'
systemctl --user is-active "$server_unit" "$host_unit"
journalctl --user -u "$server_unit" -u "$host_unit" -n 30 --no-pager
