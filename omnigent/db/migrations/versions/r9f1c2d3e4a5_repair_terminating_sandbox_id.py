"""Repair hosts schemas that were stamped past the managed-sandbox migration."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r9f1c2d3e4a5"
down_revision: str | None = "e8a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Restore the column when migration history says it already exists."""
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("hosts")}
    if "terminating_sandbox_id" in columns:
        return

    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("terminating_sandbox_id", sa.String(256), nullable=True))


def downgrade() -> None:
    """Keep the column owned by the original managed-sandbox migration."""
    # The parent revision canonically includes this column. A repair downgrade
    # must not turn a valid schema back into the historical inconsistency.
