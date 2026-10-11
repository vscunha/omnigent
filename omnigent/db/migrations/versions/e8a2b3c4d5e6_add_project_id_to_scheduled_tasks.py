"""Add optional project membership to scheduled tasks.

Revision ID: e8a2b3c4d5e6
Revises: d7b0e5f3a2c9
Create Date: 2026-09-06 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "e8a2b3c4d5e6"
down_revision: str | None = "d7b0e5f3a2c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a nullable project binding; existing tasks remain unfiled."""
    op.add_column(
        "scheduled_tasks",
        sa.Column("project_id", Uuid16(), nullable=True),
    )


def downgrade() -> None:
    """Remove scheduled-task project bindings."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("project_id")
