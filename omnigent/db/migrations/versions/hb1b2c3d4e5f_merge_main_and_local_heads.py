"""Merge the main and local migration branches.

Revision ID: hb1b2c3d4e5f
Revises: gg1b2c3d4e5f, h1b2c3d4e5f
Create Date: 2026-09-12 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "hb1b2c3d4e5f"
down_revision: tuple[str, str] = ("gg1b2c3d4e5f", "h1b2c3d4e5f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the already-applied schema branches without changing data."""


def downgrade() -> None:
    """Restore the two parent heads when rolling back the merge revision."""
