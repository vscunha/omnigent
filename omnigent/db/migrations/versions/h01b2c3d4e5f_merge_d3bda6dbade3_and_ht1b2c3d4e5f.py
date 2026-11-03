"""Merge upstream and local heads

Revision ID: h01b2c3d4e5f
Revises: ht1b2c3d4e5f, d3bda6dbade3

Both parent revisions remain unchanged. Upgrade and downgrade only reconcile
the branches, so a database already at either head converges on one lineage.
"""

revision: str = "h01b2c3d4e5f"
down_revision: tuple[str, ...] = ("ht1b2c3d4e5f", "d3bda6dbade3")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Separate the branches again without touching either side."""
