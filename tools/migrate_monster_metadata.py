#!/usr/bin/env python3
"""Move embedded Category:/Source: metadata out of monsters.description.

Historically, wiki-imported monster rows appended a metadata block to the end
of the ``description`` field::

    ...prose...

    Category:Creatures
    Category:Planescape Appendix I Creatures

    Source: Monstrous Compendium Planescape Appendix I (2602) (adnd2e.fandom.com)

This migration adds two structured columns — ``categories`` (a JSON array of
category names) and ``source`` (the source string) — peels the trailing block
off each ``description``, and stores the parts in their own columns.

Idempotent: re-running only touches rows that still carry an embedded block.
A timestamped ``.bak`` of the DB is written before any change unless
``--no-backup`` is given.

Usage:
    python3 tools/migrate_monster_metadata.py [--dry-run] [--db PATH] [--no-backup]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from import_wiki_monsters import split_metadata  # noqa: E402  (sibling module)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO_ROOT / "global" / "monsters.db"


def _columns(conn) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(monsters)")}


def add_columns(conn) -> list[str]:
    """Add categories/source columns if missing; return the ones added."""
    have = _columns(conn)
    added = []
    for col in ("categories", "source"):
        if col not in have:
            conn.execute(f"ALTER TABLE monsters ADD COLUMN {col} TEXT")
            added.append(col)
    return added


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    ap.add_argument("--db", default=str(_DEFAULT_DB))
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"error: monsters db not found: {db_path}", file=sys.stderr)
        return 2

    if not args.dry_run and not args.no_backup:
        bak = db_path.with_suffix(
            db_path.suffix + f".bak.{datetime.now():%Y%m%d_%H%M%S}")
        shutil.copy2(db_path, bak)
        print(f"backup: {bak}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if not args.dry_run:
            added = add_columns(conn)
            if added:
                print(f"added columns: {', '.join(added)}")
        elif not {"categories", "source"} <= _columns(conn):
            print("(dry-run) would add columns: categories, source")

        rows = conn.execute(
            "SELECT id, description FROM monsters WHERE description IS NOT NULL"
        ).fetchall()

        changed = 0
        for row in rows:
            body, categories, source = split_metadata(row["description"])
            if body == row["description"] and not categories and not source:
                continue  # nothing embedded — already clean
            changed += 1
            if args.dry_run:
                if changed <= 5:
                    print(f"  id={row['id']}: "
                          f"{len(categories)} categories, "
                          f"source={source[:60]!r}")
                continue
            conn.execute(
                "UPDATE monsters SET description = ?, categories = ?, source = ? "
                "WHERE id = ?",
                (body or None,
                 json.dumps(categories) if categories else None,
                 source or None,
                 row["id"]),
            )

        if not args.dry_run:
            conn.commit()
        verb = "would update" if args.dry_run else "updated"
        print(f"{verb} {changed} of {len(rows)} rows")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
