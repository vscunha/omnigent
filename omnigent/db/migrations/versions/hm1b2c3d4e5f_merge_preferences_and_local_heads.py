"""Merge the preferences/inference-blob and local migration branches.

Revision ID: hm1b2c3d4e5f
Revises: hj1b2c3d4e5f, ll1a2b3c4d5e
Create Date: 2026-09-27 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "hm1b2c3d4e5f"
down_revision: tuple[str, str] = ("hj1b2c3d4e5f", "ll1a2b3c4d5e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the already-applied schema branches without changing data."""


def downgrade() -> None:
    """Restore the two parent heads when rolling back the merge revision."""
