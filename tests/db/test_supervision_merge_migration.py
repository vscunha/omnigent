"""Upgrade and downgrade migration branches without losing scheduled projects."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config


@pytest.mark.parametrize("original_head", ["hp1b2c3d4e5f", "b2e4f6a8c013", "d0a1f879a01d"])
def test_both_lineages_upgrade_preserving_scheduled_project(
    tmp_path: Path, original_head: str
) -> None:
    """A task's existing project binding survives the cross-branch upgrade."""
    uri = f"sqlite:///{tmp_path / 'lineage.db'}"
    config = _build_alembic_config(uri)
    command.upgrade(config, original_head)
    engine = sa.create_engine(uri)
    task_id = bytes.fromhex("11" * 16)
    project_id = bytes.fromhex("22" * 16)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(workspace_id,id,name,prompt,rrule,agent_id,timezone,created_at,project_id) "
                    "VALUES (0,:id,'test','test','FREQ=HOURLY',:agent,'UTC',1,:project)"
                ),
                {"id": task_id, "agent": bytes.fromhex("33" * 16), "project": project_id},
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT project_id FROM scheduled_tasks WHERE id=:id"), {"id": task_id}
                ).scalar_one()
                == project_id
            )
            assert connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("hr1b2c3d4e5f")
        columns = {column["name"] for column in sa.inspect(engine).get_columns("scheduled_tasks")}
        assert {
            "project_id",
            "agent_session_control",
            "next_run_at",
            "delegation_ref",
        } <= columns
        # The retired supervision binding is gone again at head.
        assert "supervised_session_id" not in columns
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("original_head", "rollback_head"),
    [("hr1b2c3d4e5f", "hq1b2c3d4e5f"), ("d0a1f879a01d", "d0a1f879a01d")],
)
def test_upgrade_and_downgrade_preserve_continued_session(
    tmp_path: Path, original_head: str, rollback_head: str
) -> None:
    """Existing project and continued-session bindings survive the supported round trip."""
    uri = f"sqlite:///{tmp_path / 'continued-session.db'}"
    config = _build_alembic_config(uri)
    command.upgrade(config, original_head)
    engine = sa.create_engine(uri)
    task_id = bytes.fromhex("77" * 16)
    project_id = bytes.fromhex("88" * 16)
    session_id = bytes.fromhex("99" * 16)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(workspace_id,id,name,prompt,rrule,agent_id,timezone,created_at,"
                    "project_id,session_mode,session_id) "
                    "VALUES (0,:id,'test','test','FREQ=HOURLY',:agent,'UTC',1,"
                    ":project,2,:session)"
                ),
                {
                    "id": task_id,
                    "agent": bytes.fromhex("aa" * 16),
                    "project": project_id,
                    "session": session_id,
                },
            )
        for operation, target in (
            (command.upgrade, "head"),
            (command.downgrade, rollback_head),
            (command.upgrade, "head"),
        ):
            operation(config, target)
            with engine.connect() as connection:
                row = connection.execute(
                    sa.text(
                        "SELECT project_id, session_mode, session_id "
                        "FROM scheduled_tasks WHERE id=:id"
                    ),
                    {"id": task_id},
                ).one()
                assert tuple(row) == (project_id, 2, session_id)
    finally:
        engine.dispose()


@pytest.mark.parametrize("remaining_head", ["hp1b2c3d4e5f", "d0a1f879a01d"])
def test_downgrade_one_branch_preserves_shared_project_column(
    tmp_path: Path, remaining_head: str
) -> None:
    """Rewinding either lineage must not drop the other lineage's project data."""
    uri = f"sqlite:///{tmp_path / 'downgrade.db'}"
    config = _build_alembic_config(uri)
    command.upgrade(config, "head")
    engine = sa.create_engine(uri)
    task_id = bytes.fromhex("44" * 16)
    project_id = bytes.fromhex("55" * 16)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(workspace_id,id,name,prompt,rrule,agent_id,timezone,created_at,project_id) "
                    "VALUES (0,:id,'test','test','FREQ=HOURLY',:agent,'UTC',1,:project)"
                ),
                {"id": task_id, "agent": bytes.fromhex("66" * 16), "project": project_id},
            )
        command.downgrade(config, remaining_head)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT project_id FROM scheduled_tasks WHERE id=:id"), {"id": task_id}
                ).scalar_one()
                == project_id
            )
    finally:
        engine.dispose()
