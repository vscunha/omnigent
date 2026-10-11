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

from omnigent.update_recovery import (
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
                id BLOB NOT NULL,
                host_id BLOB,
                runner_id TEXT,
                runner_last_seen INTEGER
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO omnigent_conversation_metadata
                (id, host_id, runner_id, runner_last_seen)
            VALUES (?, ?, ?, ?)
            """,
            [
                (bytes.fromhex("aa" * 16), bytes.fromhex(_LOCAL_HOST_ID), "runner-a", 950),
                (bytes.fromhex("bb" * 16), bytes.fromhex(_LOCAL_HOST_ID), "runner-b", 909),
                (bytes.fromhex("cc" * 16), bytes.fromhex(_LOCAL_HOST_ID), None, 950),
                (bytes.fromhex("dd" * 16), bytes.fromhex(_OTHER_HOST_ID), "runner-d", 950),
            ],
        )


def test_snapshot_selects_only_fresh_runners_on_this_host(tmp_path: Path) -> None:
    database_path = tmp_path / "chat.db"
    _create_metadata_db(database_path)

    assert snapshot_host_sessions(database_path, _LOCAL_HOST_ID, now=1_000) == ["aa" * 16]


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
