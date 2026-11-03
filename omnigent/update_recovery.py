"""Preserve live host-bound sessions across a local service update."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from omnigent.db.db_models import DEFAULT_WORKSPACE_ID
from omnigent.host.identity import load_host_identity_if_present
from omnigent.stores.conversation_store import RUNNER_LIVENESS_TTL_S

_RECOVERY_REQUEST_TIMEOUT_S = 120.0
_RECOVERY_MAX_WORKERS = 4
_MAX_HOST_PROCESSES = 4096
_MAX_HOST_RUNNERS = 256
_MAX_RECOVERY_SESSIONS = 1024
_MAX_RECOVERY_WORKSPACES = 32
_MAX_SQLITE_STEPS = 100_000
_RUNNER_LOG_NAME = re.compile(r"^runner-([0-9a-fA-F]{32})-\d{8}-\d{6}-\d{6}(?:-\d+)?\.log$")


class RecoveryError(RuntimeError):
    """Raised when a session cannot be reconnected after the host restart."""


def _uuid_hex_bytes(value: str, *, field: str) -> bytes:
    """Return a validated UUID-shaped identifier as 16 bytes."""
    normalized = value.replace("-", "").lower()
    try:
        value_bytes = bytes.fromhex(normalized)
    except ValueError:
        value_bytes = b""
    if len(value_bytes) != 16:
        raise ValueError(f"{field} must be a 32-character hexadecimal UUID")
    return value_bytes


def _normalise_session_ids(session_ids: Iterable[str]) -> tuple[str, ...]:
    """Validate, normalize, and de-duplicate session identifiers."""
    normalized: dict[str, None] = {}
    for session_id in session_ids:
        if not isinstance(session_id, str):
            raise ValueError("session id must be a string")
        normalized[(_uuid_hex_bytes(session_id, field="session id")).hex()] = None
    return tuple(normalized)


def _host_service_pid() -> int:
    """Read the active host daemon's PID without inspecting its environment."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "omnigent-host.service", "-p", "MainPID", "--value"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        pid = int(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RecoveryError("could not determine the host service PID") from exc
    if pid <= 1:
        raise RecoveryError("host service is not running")
    return pid


def _process_relationships() -> list[tuple[int, int]]:
    """Read process parentage without collecting command arguments or environment."""
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        relationships = []
        for line in result.stdout.splitlines():
            if line.strip():
                pid, parent_pid = line.split()
                relationships.append((int(pid), int(parent_pid)))
        return relationships
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RecoveryError("could not inspect host process parentage") from exc


def _runner_sessions_from_processes(
    host_pid: int,
    process_relationships: Sequence[tuple[int, int]],
    *,
    proc_root: Path = Path("/proc"),
) -> tuple[str, ...]:
    """Find live host runner primary sessions from their open log filenames."""
    children: dict[int, list[int]] = defaultdict(list)
    parents: dict[int, int] = {}
    for pid, parent_pid in process_relationships:
        children[parent_pid].append(pid)
        parents[pid] = parent_pid
    if host_pid not in parents:
        raise RecoveryError("host process disappeared during recovery snapshot")

    pending = list(children[host_pid])
    visited: set[int] = set()
    sessions: set[str] = set()
    while pending:
        pid = pending.pop()
        if pid in visited:
            continue
        visited.add(pid)
        if len(visited) > _MAX_HOST_PROCESSES:
            raise RecoveryError("too many host descendants for a bounded recovery snapshot")
        pending.extend(children[pid])
        try:
            log_path = os.readlink(proc_root / str(pid) / "fd" / "1")
        except FileNotFoundError:
            if not (proc_root / str(pid)).exists():
                continue
            log_path = ""
        except OSError as exc:
            raise RecoveryError("could not inspect a host descendant's output") from exc
        log_name = Path(log_path.removesuffix(" (deleted)")).name if log_path else ""
        match = _RUNNER_LOG_NAME.fullmatch(log_name)
        if match is not None:
            sessions.add(match.group(1).lower())
            if len(sessions) > _MAX_HOST_RUNNERS:
                raise RecoveryError("too many live runners for a bounded recovery snapshot")
            continue
        if log_name.startswith("runner-"):
            raise RecoveryError("live runner has an unrecognized recovery log name")
        try:
            with (proc_root / str(pid) / "cmdline").open("rb") as source:
                arguments = source.read(8192).split(b"\0")
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RecoveryError("could not inspect a host descendant") from exc
        modules = {
            arguments[index] for index in range(1, len(arguments)) if arguments[index - 1] == b"-m"
        }
        is_zygote = b"omnigent.runner._zygote" in modules
        if (is_zygote and parents[pid] != host_pid) or b"omnigent.runner._entry" in modules:
            raise RecoveryError("live runner has no readable recovery log")
    return tuple(sorted(sessions))


def _recovery_session_query(primary_count: int) -> str:
    placeholders = ", ".join("?" for _ in range(primary_count))
    return f"""
        SELECT candidate.id, candidate.host_id, candidate.runner_last_seen
        FROM omnigent_conversation_metadata AS primary_session
            INDEXED BY sqlite_autoindex_omnigent_conversation_metadata_1
        CROSS JOIN omnigent_conversation_metadata AS candidate
            INDEXED BY ix_conversation_metadata_runner_id
        WHERE primary_session.workspace_id = ?
          AND primary_session.id IN ({placeholders})
          AND primary_session.host_id = ?
          AND primary_session.runner_id IS NOT NULL
          AND primary_session.runner_id <> ''
          AND candidate.workspace_id = primary_session.workspace_id
          AND candidate.runner_id = primary_session.runner_id
        LIMIT ?
    """


def _host_workspaces(connection: sqlite3.Connection, host_id_bytes: bytes) -> tuple[int, ...]:
    """Return the workspaces owning this host, from the ``hosts`` key table."""
    try:
        rows = connection.execute(
            "SELECT workspace_id FROM hosts "
            "WHERE host_id = ? AND deleted_at IS NULL "
            "ORDER BY workspace_id LIMIT ?",
            (host_id_bytes, _MAX_RECOVERY_WORKSPACES + 1),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise RecoveryError("bounded host workspace lookup failed") from exc
    if len(rows) > _MAX_RECOVERY_WORKSPACES:
        raise RecoveryError("too many workspaces for a bounded recovery snapshot")
    return tuple(int(row[0]) for row in rows)


def snapshot_host_sessions(
    database_path: Path,
    host_id: str,
    *,
    primary_session_ids: Iterable[str],
    now: int | None = None,
    liveness_ttl_s: int = RUNNER_LIVENESS_TTL_S,
) -> list[str]:
    """Find live sessions with bounded primary-key and runner-index lookups."""
    primary_ids = _normalise_session_ids(primary_session_ids)
    if len(primary_ids) > _MAX_HOST_RUNNERS:
        raise RecoveryError("too many live runners for a bounded recovery snapshot")
    if not database_path.exists():
        raise RecoveryError("live database is missing")
    if not primary_ids:
        return []

    host_id_bytes = _uuid_hex_bytes(host_id, field="host id")
    stale_before = (now if now is not None else int(time.time())) - liveness_ttl_s
    session_id_bytes = [bytes.fromhex(session_id) for session_id in primary_ids]
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=5) as connection:
        steps = 0

        def check_budget() -> int:
            nonlocal steps
            steps += 1000
            return int(steps > _MAX_SQLITE_STEPS)

        connection.set_progress_handler(check_budget, 1000)
        try:
            workspace_ids = _host_workspaces(connection, host_id_bytes) or (DEFAULT_WORKSPACE_ID,)
            rows: list[tuple[Any, ...]] = []
            for workspace_id in workspace_ids:
                remaining = _MAX_RECOVERY_SESSIONS + 1 - len(rows)
                if remaining <= 0:
                    break
                rows.extend(
                    connection.execute(
                        _recovery_session_query(len(primary_ids)),
                        [workspace_id, *session_id_bytes, host_id_bytes, remaining],
                    ).fetchall()
                )
        except sqlite3.OperationalError as exc:
            raise RecoveryError("bounded session metadata lookup failed") from exc
    if len(rows) > _MAX_RECOVERY_SESSIONS:
        raise RecoveryError("too many sessions for a bounded recovery snapshot")

    primary_set = set(primary_ids)
    matched_primary: set[str] = set()
    sessions: set[str] = set()
    for session_id_bytes, owner_host_id, last_seen in rows:
        if owner_host_id != host_id_bytes:
            continue
        if not isinstance(session_id_bytes, bytes) or len(session_id_bytes) != 16:
            raise RecoveryError("invalid session metadata in recovery snapshot")
        session_id = session_id_bytes.hex()
        if session_id in primary_set:
            matched_primary.add(session_id)
            sessions.add(session_id)
        elif isinstance(last_seen, int) and last_seen >= stale_before:
            sessions.add(session_id)
    if matched_primary != primary_set:
        raise RecoveryError("a live runner is missing its host session metadata")
    return sorted(sessions)


def write_recovery_snapshot(
    path: Path,
    host_id: str | None,
    session_ids: Iterable[str],
) -> None:
    """Atomically write a private update-recovery snapshot."""
    normalized_host_id = None
    if host_id is not None:
        normalized_host_id = _uuid_hex_bytes(host_id, field="host id").hex()
    payload = {
        "host_id": normalized_host_id,
        "sessions": list(_normalise_session_ids(session_ids)),
    }

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_recovery_snapshot(path: Path) -> tuple[str, ...]:
    """Load and validate session identifiers from an update snapshot."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
        raise ValueError(f"invalid update-recovery snapshot: {path}")
    return _normalise_session_ids(payload["sessions"])


def capture_recovery_snapshot(database_path: Path, output_path: Path) -> None:
    """Capture this machine's live sessions for use after host restart."""
    identity = load_host_identity_if_present()
    if identity is None:
        host_id = None
        session_ids: list[str] = []
    else:
        host_id = identity.host_id
        primary_session_ids = _runner_sessions_from_processes(
            _host_service_pid(), _process_relationships()
        )
        session_ids = snapshot_host_sessions(
            database_path, host_id, primary_session_ids=primary_session_ids
        )
    write_recovery_snapshot(output_path, host_id, session_ids)
    print(f"Captured {len(session_ids)} live session(s) for update recovery.")


def retry_session(
    session_id: str,
    base_url: str,
    *,
    token: str | None,
    opener: Callable[..., Any] | None = None,
) -> None:
    """Ask the server to reconnect or relaunch one session's runner."""
    normalized_session_id = (_uuid_hex_bytes(session_id, field="session id")).hex()
    url = (
        f"{base_url.rstrip('/')}/v1/sessions/"
        f"{urllib.parse.quote(normalized_session_id, safe='')}/events"
    )
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps({"type": "retry_session", "data": {}}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    urlopen = opener or urllib.request.urlopen
    try:
        with urlopen(request, timeout=_RECOVERY_REQUEST_TIMEOUT_S) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if not 200 <= status < 300:
                raise RecoveryError(f"server returned HTTP {status}")
    except urllib.error.HTTPError as exc:
        raise RecoveryError(f"server returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RecoveryError(f"request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RecoveryError("request timed out") from exc


def recover_sessions(
    snapshot_path: Path,
    base_url: str,
    *,
    token: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> bool:
    """Retry all sessions in a snapshot, returning whether all succeeded."""
    session_ids = load_recovery_snapshot(snapshot_path)
    if not session_ids:
        print("No live sessions need update recovery.")
        return True

    if token is None:
        from omnigent import cli_auth

        token = cli_auth.load_token(base_url)

    def recover_one(session_id: str) -> tuple[str, str | None]:
        try:
            retry_session(session_id, base_url, token=token, opener=opener)
        except (OSError, RecoveryError, TimeoutError) as exc:
            return session_id, str(exc)
        return session_id, None

    worker_count = min(_RECOVERY_MAX_WORKERS, len(session_ids))
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(recover_one, session_ids))

    failures = [(session_id, error) for session_id, error in results if error is not None]
    if failures:
        for session_id, error in failures:
            print(f"Session {session_id} recovery failed: {error}", file=sys.stderr)
        return False

    print(f"Recovered {len(session_ids)} live session(s) after host restart.")
    return True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--db", type=Path, required=True)
    snapshot_parser.add_argument("--output", type=Path, required=True)

    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--snapshot", type=Path, required=True)
    recover_parser.add_argument("--base-url", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the snapshot or recovery command."""
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            capture_recovery_snapshot(args.db, args.output)
            return 0
        return 0 if recover_sessions(args.snapshot, args.base_url) else 1
    except (OSError, RecoveryError, ValueError, sqlite3.Error) as exc:
        print(f"update recovery: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
