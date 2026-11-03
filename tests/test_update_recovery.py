"""Regression tests for safe live-service updates."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from threading import Lock
from typing import Any

import pytest

from omnigent import update_recovery
from omnigent.update_recovery import (
    RecoveryError,
    _recovery_session_query,
    _runner_sessions_from_processes,
    load_recovery_snapshot,
    recover_sessions,
    snapshot_host_sessions,
    write_recovery_snapshot,
)

_ROOT = Path(__file__).resolve().parents[1]
_LOCAL_HOST_ID = "11" * 16
_OTHER_HOST_ID = "22" * 16


def _create_metadata_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE omnigent_conversation_metadata (
                workspace_id INTEGER NOT NULL,
                id BLOB NOT NULL,
                host_id BLOB,
                runner_id TEXT,
                runner_last_seen INTEGER,
                PRIMARY KEY (workspace_id, id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX ix_conversation_metadata_runner_id "
            "ON omnigent_conversation_metadata (workspace_id, runner_id, id)"
        )
        connection.execute(
            "CREATE TABLE hosts ("
            "workspace_id INTEGER NOT NULL, "
            "host_id BLOB NOT NULL, "
            "deleted_at INTEGER, "
            "PRIMARY KEY (workspace_id, host_id))"
        )
        connection.executemany(
            "INSERT INTO hosts (workspace_id, host_id, deleted_at) VALUES (?, ?, NULL)",
            [(0, bytes.fromhex(_LOCAL_HOST_ID)), (7, bytes.fromhex(_LOCAL_HOST_ID))],
        )
        connection.executemany(
            """
            INSERT INTO omnigent_conversation_metadata
                (workspace_id, id, host_id, runner_id, runner_last_seen)
            VALUES (0, ?, ?, ?, ?)
            """,
            [
                (bytes.fromhex("aa" * 16), bytes.fromhex(_LOCAL_HOST_ID), "runner-a", 950),
                (bytes.fromhex("bb" * 16), bytes.fromhex(_LOCAL_HOST_ID), "runner-a", 909),
                (bytes.fromhex("cc" * 16), bytes.fromhex(_LOCAL_HOST_ID), None, 950),
                (bytes.fromhex("dd" * 16), bytes.fromhex(_OTHER_HOST_ID), "runner-a", 950),
                (bytes.fromhex("ee" * 16), bytes.fromhex(_LOCAL_HOST_ID), "runner-a", 950),
                (bytes.fromhex("ff" * 16), bytes.fromhex(_LOCAL_HOST_ID), "runner-f", 800),
            ],
        )
        connection.execute(
            "INSERT INTO omnigent_conversation_metadata "
            "(workspace_id, id, host_id, runner_id, runner_last_seen) "
            "VALUES (7, ?, ?, 'runner-w', 950)",
            (bytes.fromhex("12" * 16), bytes.fromhex(_LOCAL_HOST_ID)),
        )


def test_snapshot_selects_only_fresh_runners_on_this_host(tmp_path: Path) -> None:
    database_path = tmp_path / "chat.db"
    _create_metadata_db(database_path)

    assert snapshot_host_sessions(
        database_path, _LOCAL_HOST_ID, primary_session_ids=["aa" * 16, "ff" * 16], now=1_000
    ) == ["aa" * 16, "ee" * 16, "ff" * 16]


def test_snapshot_includes_live_runners_in_another_workspace(tmp_path: Path) -> None:
    """A host can own sessions in more than one workspace; none may be dropped."""
    database_path = tmp_path / "chat.db"
    _create_metadata_db(database_path)

    sessions = snapshot_host_sessions(
        database_path, _LOCAL_HOST_ID, primary_session_ids=["aa" * 16, "12" * 16], now=1_000
    )

    assert sessions == ["12" * 16, "aa" * 16, "ee" * 16]


def test_snapshot_refuses_more_workspaces_than_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "chat.db"
    _create_metadata_db(database_path)
    monkeypatch.setattr(update_recovery, "_MAX_RECOVERY_WORKSPACES", 1)

    with pytest.raises(RecoveryError, match="too many workspaces"):
        snapshot_host_sessions(
            database_path, _LOCAL_HOST_ID, primary_session_ids=["aa" * 16], now=1_000
        )


def test_snapshot_uses_indexed_primary_and_runner_lookups(tmp_path: Path) -> None:
    database_path = tmp_path / "chat.db"
    _create_metadata_db(database_path)

    with sqlite3.connect(database_path) as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN " + _recovery_session_query(1),
            (0, bytes.fromhex("aa" * 16), bytes.fromhex(_LOCAL_HOST_ID), 1025),
        ).fetchall()

    details = [row[3] for row in plan]
    assert all("SEARCH " in detail and "SCAN " not in detail for detail in details)
    assert any("sqlite_autoindex_omnigent_conversation_metadata_1" in detail for detail in details)
    assert any("ix_conversation_metadata_runner_id" in detail for detail in details)


def test_snapshot_refuses_missing_primary_session(tmp_path: Path) -> None:
    database_path = tmp_path / "chat.db"
    _create_metadata_db(database_path)

    with pytest.raises(RecoveryError, match="missing its host session metadata"):
        snapshot_host_sessions(database_path, _LOCAL_HOST_ID, primary_session_ids=["99" * 16])


def test_snapshot_refuses_more_than_the_bounded_metadata_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "chat.db"
    _create_metadata_db(database_path)
    monkeypatch.setattr(update_recovery, "_MAX_RECOVERY_SESSIONS", 1)

    with pytest.raises(RecoveryError, match="too many sessions"):
        snapshot_host_sessions(database_path, _LOCAL_HOST_ID, primary_session_ids=["aa" * 16])


def _mock_process(proc_root: Path, pid: int, module: str, log_name: str | None) -> None:
    process = proc_root / str(pid)
    (process / "fd").mkdir(parents=True)
    (process / "cmdline").write_bytes(f"python\0-P\0-m\0{module}\0".encode())
    if log_name is not None:
        (process / "fd" / "1").symlink_to(Path("/tmp") / log_name)


def test_runner_discovery_uses_host_processes_and_open_log_names(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    _mock_process(proc_root, 101, "omnigent.runner._zygote", None)
    log_name = f"runner-{'aa' * 16}-20261031-120000-000001.log"
    _mock_process(proc_root, 102, "omnigent.runner._zygote", log_name)
    _mock_process(proc_root, 103, "omnigent.runner._zygote", log_name)
    _mock_process(
        proc_root, 104, "omnigent.runner._entry", f"runner-{'bb' * 16}-20261031-120000-000002.log"
    )
    _mock_process(
        proc_root, 105, "omnigent.runner._entry", f"runner-{'cc' * 16}-20261031-120000-000003.log"
    )
    _mock_process(
        proc_root, 106, "future.runner", f"runner-{'dd' * 16}-20261031-120000-000004.log"
    )
    relationships = [
        (100, 1),
        (101, 100),
        (102, 101),
        (103, 101),
        (104, 100),
        (105, 1),
        (106, 100),
    ]

    assert _runner_sessions_from_processes(100, relationships, proc_root=proc_root) == (
        "aa" * 16,
        "bb" * 16,
        "dd" * 16,
    )


def test_runner_discovery_refuses_unrecognized_log(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    _mock_process(proc_root, 102, "omnigent.runner._entry", "runner-unrecognized.log")

    with pytest.raises(RecoveryError, match="unrecognized recovery log"):
        _runner_sessions_from_processes(100, [(100, 1), (102, 100)], proc_root=proc_root)


def test_recovery_snapshot_round_trips_with_private_permissions(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "recovery.json"

    write_recovery_snapshot(snapshot_path, _LOCAL_HOST_ID, ["aa" * 16])

    assert load_recovery_snapshot(snapshot_path) == ("aa" * 16,)
    assert snapshot_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(snapshot_path.read_text()) == {
        "host_id": _LOCAL_HOST_ID,
        "sessions": ["aa" * 16],
    }


class _Response:
    status = 204

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def test_recover_sessions_posts_retry_event_for_each_snapshot_session(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "recovery.json"
    session_ids = ("aa" * 16, "bb" * 16)
    write_recovery_snapshot(snapshot_path, _LOCAL_HOST_ID, list(session_ids))
    calls: list[tuple[str, dict[str, Any], str | None]] = []
    calls_lock = Lock()

    def opener(request: Any, *, timeout: float) -> _Response:
        with calls_lock:
            calls.append(
                (
                    request.full_url,
                    json.loads(request.data),
                    request.get_header("Authorization"),
                )
            )
        assert timeout == 120.0
        return _Response()

    assert recover_sessions(
        snapshot_path,
        "http://127.0.0.1:6767",
        token="test-token",
        opener=opener,
    )

    assert sorted(calls) == sorted(
        [
            (
                f"http://127.0.0.1:6767/v1/sessions/{session_id}/events",
                {"type": "retry_session", "data": {}},
                "Bearer test-token",
            )
            for session_id in session_ids
        ]
    )


@pytest.mark.parametrize("marker", [{"OMNIGENT": "1"}, {"OMNIGENT_RUNNER_ID": "runner-a"}])
def test_update_script_hands_off_from_inside_a_session(
    tmp_path: Path,
    marker: dict[str, str],
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_path = tmp_path / "systemd-run-args"
    fake_systemd_run = fake_bin / "systemd-run"
    fake_systemd_run.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$UPDATE_TEST_ARGS"\n',
        encoding="utf-8",
    )
    fake_systemd_run.chmod(0o700)

    environment = os.environ.copy()
    environment.pop("OMNIGENT", None)
    environment.pop("OMNIGENT_RUNNER_ID", None)
    environment.update(marker)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["UPDATE_TEST_ARGS"] = str(args_path)
    environment["XDG_RUNTIME_DIR"] = str(tmp_path)
    environment.pop("DBUS_SESSION_BUS_ADDRESS", None)

    result = subprocess.run(
        ["bash", str(_ROOT / "update.sh")],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    args = args_path.read_text(encoding="utf-8").splitlines()
    assert "--user" in args
    assert "--unit=omnigent-update.service" in args
    assert "--collect" in args
    assert "--no-block" in args
    assert f"--working-directory={_ROOT}" in args
    assert "--setenv=OMNIGENT=" in args
    assert "--setenv=OMNIGENT_RUNNER_ID=" in args
    assert f"--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path={tmp_path}/bus" in args
    assert args[-1] == str(_ROOT / "update.sh")
    assert "Update queued in omnigent-update.service." in result.stdout
