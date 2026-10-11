"""Preserve live host-bound sessions across a local service update."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from omnigent.host.identity import load_host_identity_if_present
from omnigent.stores.conversation_store import RUNNER_LIVENESS_TTL_S

_RECOVERY_REQUEST_TIMEOUT_S = 120.0
_RECOVERY_MAX_WORKERS = 4


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


def snapshot_host_sessions(
    database_path: Path,
    host_id: str,
    *,
    now: int | None = None,
    liveness_ttl_s: int = RUNNER_LIVENESS_TTL_S,
) -> list[str]:
    """List this host's sessions whose runner was recently seen alive."""
    if not database_path.exists():
        return []

    host_id_bytes = _uuid_hex_bytes(host_id, field="host id")
    reference_time = now if now is not None else int(time.time())
    stale_before = reference_time - liveness_ttl_s
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=30) as connection:
        rows = connection.execute(
            """
            SELECT lower(hex(id))
            FROM omnigent_conversation_metadata
            WHERE host_id = ?
              AND runner_id IS NOT NULL
              AND runner_id <> ''
              AND runner_last_seen >= ?
            ORDER BY id
            """,
            (host_id_bytes, stale_before),
        )
        return [row[0] for row in rows]


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
        session_ids = snapshot_host_sessions(database_path, host_id)
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
