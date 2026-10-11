"""Add durable user-facing Inbox notifications.

Revision ID: c6a9d4e2f1b8
Revises: e5d9bc8ac650
Create Date: 2026-08-27 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "c6a9d4e2f1b8"
down_revision: str | None = "gb1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the owner-scoped Inbox notification table."""
    op.create_table(
        "inbox_notifications",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("source_session_id", Uuid16(), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("acknowledged_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "severity IN ('info', 'attention', 'critical')",
            name="ck_inbox_notifications_severity",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_inbox_notifications_user_outstanding",
        "inbox_notifications",
        ["workspace_id", "user_id", "acknowledged_at", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop durable Inbox notifications."""
    op.drop_index(
        "ix_inbox_notifications_user_outstanding",
        table_name="inbox_notifications",
    )
    op.drop_table("inbox_notifications")
