"""Merge the deployed private schema and upstream dispatch schema.

Revision ID: hq1b2c3d4e5f
Revises: hp1b2c3d4e5f, d0a1f879a01d
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "hq1b2c3d4e5f"
down_revision: tuple[str, str] = ("hp1b2c3d4e5f", "d0a1f879a01d")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join both branches and require an explicit supervision target."""
    op.add_column("scheduled_tasks", sa.Column("supervised_session_id", Uuid16(), nullable=True))


def downgrade() -> None:
    """Separate the branches and remove supervision target bindings."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("supervised_session_id")
