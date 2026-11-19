"""The merge-revision helper used when upstream adds migrations."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from omnigent.db.migration_heads import (
    heads,
    merge_heads,
    next_merge_revision_id,
    ordered_parents,
    script_directory,
)


def _write_revision(
    versions: Path, revision: str, down_revision: str | tuple[str, ...] | None
) -> Path:
    path = versions / f"{revision}_step.py"
    path.write_text(
        f'''"""Step {revision}."""

revision = "{revision}"
down_revision = {down_revision!r}
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply."""


def downgrade() -> None:
    """Revert."""
'''
    )
    return path


@pytest.fixture
def revision_graph(tmp_path: Path) -> Config:
    """A throwaway Alembic environment with one linear chain."""
    versions = tmp_path / "versions"
    versions.mkdir()
    (tmp_path / "alembic.ini").write_text("[alembic]\n")
    (tmp_path / "env.py").write_text(
        """from alembic import context
from sqlalchemy import engine_from_config, pool

connectable = engine_from_config(
    context.config.get_section(context.config.config_ini_section, {}),
    prefix="sqlalchemy.",
    poolclass=pool.NullPool,
)
with connectable.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
"""
    )
    _write_revision(versions, "aaa111", None)
    _write_revision(versions, "bbb222", "aaa111")
    _write_revision(versions, "ccc333", "bbb222")
    # Alembic resolves revision files under <script_location>/versions.
    config = Config(str(tmp_path / "alembic.ini"))
    config.set_main_option("script_location", str(tmp_path))
    return config


@pytest.fixture
def versions(revision_graph: Config) -> Path:
    return Path(revision_graph.get_main_option("script_location")) / "versions"


def test_single_head_needs_no_merge(revision_graph: Config) -> None:
    assert heads(script_directory(revision_graph)) == ["ccc333"]
    assert merge_heads(revision_graph) is None


def test_two_heads_are_joined_by_a_generated_revision(
    versions: Path, revision_graph: Config
) -> None:
    _write_revision(versions, "ddd444", "aaa111")
    assert heads(script_directory(revision_graph)) == ["ccc333", "ddd444"]

    path = merge_heads(revision_graph)

    assert path is not None and path.parent == versions
    assert heads(script_directory(revision_graph)) == ["h01b2c3d4e5f"]
    source = path.read_text()
    assert 'revision: str = "h01b2c3d4e5f"' in source
    assert "def downgrade() -> None:" in source


def test_generated_revision_joins_the_branches_in_a_database(
    tmp_path: Path, versions: Path, revision_graph: Config
) -> None:
    _write_revision(versions, "ddd444", "aaa111")
    database = tmp_path / "graph.db"
    revision_graph.set_main_option("sqlalchemy.url", f"sqlite:///{database}")

    command.upgrade(revision_graph, "ccc333")
    command.upgrade(revision_graph, "ddd444")
    path = merge_heads(revision_graph)
    assert path is not None

    command.upgrade(revision_graph, "head")

    import sqlalchemy

    engine = sqlalchemy.create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        revision_rows = connection.exec_driver_sql(
            "select version_num from alembic_version"
        ).fetchall()
    engine.dispose()
    assert [row[0] for row in revision_rows] == ["h01b2c3d4e5f"]


def test_next_id_skips_used_codes() -> None:
    existing = {"h01b2c3d4e5f", "h11b2c3d4e5f", "ha1b2c3d4e5f"}
    assert next_merge_revision_id(existing) == "h21b2c3d4e5f"


def test_local_head_is_listed_first() -> None:
    assert ordered_parents(["a8be64286ffc", "ht1b2c3d4e5f"]) == ("ht1b2c3d4e5f", "a8be64286ffc")
    assert ordered_parents(["b2e4f6a8c013", "d0a1f879a01d"]) == ("b2e4f6a8c013", "d0a1f879a01d")


def test_generated_file_is_importable_module_source(
    versions: Path, revision_graph: Config
) -> None:
    _write_revision(versions, "ddd444", "aaa111")
    path = merge_heads(revision_graph)
    assert path is not None
    spec = importlib.util.spec_from_file_location("generated_merge", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "h01b2c3d4e5f"
    assert set(module.down_revision) == {"ccc333", "ddd444"}
    module.upgrade()
    module.downgrade()
