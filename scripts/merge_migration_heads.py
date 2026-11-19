#!/usr/bin/env python
"""Join divergent Alembic heads with a generated merge revision."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnigent.db.migration_heads import heads, merge_heads, script_directory
from omnigent.db.utils import _build_alembic_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-uri",
        default="sqlite:///:memory:",
        help="Only the revision graph is read; the default keeps the live database untouched.",
    )
    parser.add_argument("--check", action="store_true", help="Report the heads without writing")
    args = parser.parse_args(argv)

    config = _build_alembic_config(args.database_uri)
    current = heads(script_directory(config))
    if len(current) == 1:
        print(f"Single Alembic head: {current[0]}")
        return 0
    if args.check:
        print(f"Divergent Alembic heads ({len(current)}): {', '.join(current)}")
        return 1
    path = merge_heads(config)
    if path is None:  # pragma: no cover - the head count above decides this
        print(f"Single Alembic head: {current[0]}")
        return 0
    print(f"Wrote merge revision {path.name}")
    print(f"Joined: {', '.join(current)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
