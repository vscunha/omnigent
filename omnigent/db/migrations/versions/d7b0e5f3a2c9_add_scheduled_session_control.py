"""Add the session-control opt-in to scheduled tasks.

Revision ID: d7b0e5f3a2c9
Revises: c6a9d4e2f1b8
Create Date: 2026-08-28 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7b0e5f3a2c9"
down_revision: str | None = "c6a9d4e2f1b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist whether scheduled runs receive session-control tools."""
    op.add_column(
        "scheduled_tasks",
        sa.Column(
            "agent_session_control",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    """Remove the scheduled session-control opt-in."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("agent_session_control")
