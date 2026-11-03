"""Helpers for migrations that share a column across schema branches."""

from alembic import context, op
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text


def migration_column_exists(table_name: str, column_name: str) -> bool:
    """Inspect a column online; offline migrations cannot reconcile branches safely."""
    if context.is_offline_mode():
        raise RuntimeError("Migration requires a live connection to reconcile shared columns")
    return any(
        column["name"] == column_name for column in inspect(op.get_bind()).get_columns(table_name)
    )


def migration_branch_is_applied(revision: str) -> bool:
    """Check whether any current database head includes a sibling revision."""
    if context.is_offline_mode():
        raise RuntimeError("Migration requires a live connection to reconcile shared columns")
    script = ScriptDirectory.from_config(context.config)
    heads = op.get_bind().execute(text("SELECT version_num FROM alembic_version")).scalars()
    return any(
        item.revision == revision
        for head in heads
        for item in script.iterate_revisions(head, "base")
    )
