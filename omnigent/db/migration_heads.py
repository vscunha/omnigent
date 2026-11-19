"""Join several Alembic heads with a generated merge revision.

The live installation keeps upstream revisions byte-identical and adds its own
branch on top of them, so every upstream migration batch lands as a second head
until a merge revision joins the two. That revision carries no schema change:
upgrade and downgrade only reconcile the branches.
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

# Local merge revisions use one letter plus a fixed tail: h1..., hq..., ht....
_MERGE_REVISION_PATTERN = re.compile(r"^h([a-z0-9])1b2c3d4e5f$")


def script_directory(config: Config) -> ScriptDirectory:
    """Return the revision graph for *config*."""
    return ScriptDirectory.from_config(config)


def heads(script: ScriptDirectory) -> list[str]:
    """The current heads, sorted so the result is stable across runs."""
    return sorted(script.get_heads())


def next_merge_revision_id(existing: set[str]) -> str:
    """A merge revision id that follows the local ``h?1b2c3d4e5f`` convention.

    Falls back to a free hexadecimal id once every letter is taken.
    """
    used = {
        match.group(1)
        for revision in existing
        if (match := _MERGE_REVISION_PATTERN.match(revision)) is not None
    }
    for code in "0123456789abcdefghijklmnopqrstuvwxyz":
        if code not in used:
            return f"h{code}1b2c3d4e5f"
    for code in range(0x10, 0x100):
        candidate = f"hm{code:02x}b2c3d4e5f"
        if candidate not in existing:
            return candidate
    raise RuntimeError("no free merge revision id")


def ordered_parents(parents: list[str]) -> tuple[str, ...]:
    """Order parents so the local head comes first.

    The convention keeps the private branch first in every existing merge
    revision, which lets callers treat ``down_revision[0]`` as the local step.
    """
    local = [revision for revision in parents if _MERGE_REVISION_PATTERN.match(revision)]
    others = [revision for revision in parents if revision not in local]
    return (*sorted(local), *sorted(others))


def render_merge_revision(revision: str, parents: tuple[str, ...], summary: str) -> str:
    """Source text for a merge revision that only joins its parents."""
    parent_list = ", ".join(parents)
    return f'''"""{summary}

Revision ID: {revision}
Revises: {parent_list}

Both parent revisions remain unchanged. Upgrade and downgrade only reconcile
the branches, so a database already at either head converges on one lineage.
"""

revision: str = "{revision}"
down_revision: tuple[str, ...] = {parents!r}
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Separate the branches again without touching either side."""
'''


def merge_heads(config: Config, *, summary: str = "Merge upstream and local heads") -> Path | None:
    """Write a merge revision when the graph has several heads.

    :returns: The new revision file, or ``None`` when there was nothing to join.
    """
    script = script_directory(config)
    current = heads(script)
    if len(current) <= 1:
        return None
    versions = Path(script.versions)
    existing = {revision.revision for revision in script.walk_revisions()}
    revision = next_merge_revision_id(existing)
    parents = ordered_parents(current)
    slug = "_merge_" + "_and_".join(heads(script)) + ".py"
    path = versions / f"{revision}{slug}"
    path.write_text(render_merge_revision(revision, parents, summary))
    after = heads(script_directory(config))
    if after != [revision]:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"merge revision {revision} did not join the heads: {after}")
    return path
