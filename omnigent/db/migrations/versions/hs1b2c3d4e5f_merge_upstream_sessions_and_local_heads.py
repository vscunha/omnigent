"""Merge the upstream session-metadata schema and the deployed local schema.

Revision ID: hs1b2c3d4e5f
Revises: hr1b2c3d4e5f, a7d4c2e9f610

Both parent revisions remain unchanged. Upgrade and downgrade only reconcile
the branches, so a database already at either head converges on one lineage.
"""

revision: str = "hs1b2c3d4e5f"
down_revision: tuple[str, ...] = ("hr1b2c3d4e5f", "a7d4c2e9f610")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Separate the branches again without touching either side."""
