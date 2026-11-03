"""Drop the scheduled supervision target binding.

Revision ID: hr1b2c3d4e5f
Revises: hq1b2c3d4e5f

Automations now use the native peer-message path, which needs no per-task
authorization target. Downgrade restores the nullable column so the previous
release can read the schema again.
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "hr1b2c3d4e5f"
down_revision: str | None = "hq1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Remove the unused supervision target binding."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("supervised_session_id")


def downgrade() -> None:
    """Restore the nullable supervision target binding."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(sa.Column("supervised_session_id", Uuid16(), nullable=True))
