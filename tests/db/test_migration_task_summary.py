"""Regression coverage for databases created by the newer task-summary migration."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, _get_current_db_revision

_CURRENT_HEAD = "za2b3c4d5e6f"
_PREVIOUS_HEAD = "d5e9f1a2b3c4"


def _make_versioned_db(path: Path, revision: str) -> tuple[str, sa.Engine]:
    uri = f"sqlite:///{path}"
    engine = sa.create_engine(uri)
    with engine.begin() as connection:
        connection.execute(
            sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            sa.text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": revision},
        )
    return uri, engine


def test_current_migrations_accept_database_at_task_summary_head(tmp_path: Path) -> None:
    """A restart must recognize a DB migrated by the newer task-summary release."""
    uri, engine = _make_versioned_db(tmp_path / "newer.db", _CURRENT_HEAD)
    try:
        command.upgrade(_build_alembic_config(uri), "head")
        assert _get_current_db_revision(engine) == _CURRENT_HEAD
    finally:
        engine.dispose()


def test_task_summary_migration_is_reachable_from_previous_head(tmp_path: Path) -> None:
    """Databases at the prior head receive the additive task-summary column."""
    uri, engine = _make_versioned_db(tmp_path / "previous.db", _PREVIOUS_HEAD)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text("CREATE TABLE omnigent_conversation_metadata (id VARCHAR(64) NOT NULL)")
            )
        command.upgrade(_build_alembic_config(uri), "head")
        columns = {
            column["name"]
            for column in sa.inspect(engine).get_columns("omnigent_conversation_metadata")
        }
        assert "task_summary" in columns
        assert _get_current_db_revision(engine) == _CURRENT_HEAD
    finally:
        engine.dispose()
