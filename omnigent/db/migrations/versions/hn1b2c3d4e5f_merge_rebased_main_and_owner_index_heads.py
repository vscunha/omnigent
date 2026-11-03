"""Merge the rebased main and owner-agent index revisions.

Revision ID: hn1b2c3d4e5f
Revises: hm1b2c3d4e5f, mm1a2b3c4d5e
Create Date: 2026-10-10 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "hn1b2c3d4e5f"
down_revision: tuple[str, str] = ("hm1b2c3d4e5f", "mm1a2b3c4d5e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the existing schema branches without changing data."""


def downgrade() -> None:
    """Restore the two parent heads when rolling back the merge revision."""
