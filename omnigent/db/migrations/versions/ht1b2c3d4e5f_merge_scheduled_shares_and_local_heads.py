"""Merge the upstream scheduled-shares schema and the deployed local schema.

Revision ID: ht1b2c3d4e5f
Revises: hs1b2c3d4e5f, a8be64286ffc

Both parent revisions remain unchanged. Upgrade and downgrade only reconcile
the branches, so a database already at either head converges on one lineage.
"""

revision: str = "ht1b2c3d4e5f"
down_revision: tuple[str, ...] = ("hs1b2c3d4e5f", "a8be64286ffc")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Separate the branches again without touching either side."""
