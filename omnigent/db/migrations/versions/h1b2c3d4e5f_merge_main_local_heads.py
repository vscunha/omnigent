"""Merge the main and local migration branches."""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "h1b2c3d4e5f"
down_revision: tuple[str, str] = ("ge1b2c3d4e5f", "r9f1c2d3e4a5")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the already-applied schema branches without changing data."""


def downgrade() -> None:
    """Restore the two parent heads when rolling back the merge revision."""
