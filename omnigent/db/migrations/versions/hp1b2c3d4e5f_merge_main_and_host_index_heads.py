"""Merge the rebased main and local PostgreSQL heads.

Revision ID: hp1b2c3d4e5f
Revises: hn1b2c3d4e5f, nf12ab34cd56
Create Date: 2026-10-21 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "hp1b2c3d4e5f"
down_revision: tuple[str, str] = ("hn1b2c3d4e5f", "nf12ab34cd56")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the existing schema branches without changing data."""


def downgrade() -> None:
    """Restore the two parent heads when rolling back the merge revision."""
