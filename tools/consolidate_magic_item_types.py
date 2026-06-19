#!/usr/bin/env python3
"""Consolidate the fragmented ``misc_magic_<kind>`` item_type values created by
the wiki import into a compact, human-readable category set.

Scope: only rows with ``id >= 900000`` whose ``item_type`` still starts with
``misc_magic`` (the import signature). Original/mundane items are untouched.

Idempotent: after a run the types are human-readable and no longer match
``misc_magic%``, so re-running is a no-op.

Usage:
    python3 tools/consolidate_magic_item_types.py [--dry-run] [--db PATH]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO_ROOT / "global" / "2e.db"

# kind-token -> human-readable category. First matching set wins; anything not
# listed falls through to "Wondrous Item".
_CATEGORY_SETS: list[tuple[str, set[str]]] = [
    ("Magic Weapon", {
        "sword", "axe", "dagger", "mace", "spear", "hammer", "bow", "dart",
        "polearm", "club", "flail", "lance", "whip", "sling", "crossbow",
        "blowgun", "javelin", "trident", "scythe", "sickle", "harpoon",
        "throwing", "weapon", "fist", "claw", "discus", "balista", "ballistae",
        "catapult", "battering", "morningstar", "blade",
    }),
    ("Magic Ammunition", {"arrow", "arrows", "bolt", "shot", "pellet"}),
    ("Magic Armor", {
        "armor", "shield", "helm", "helmet", "gauntlet", "gauntlets", "bracer",
        "bracers", "barding", "plate", "buckler", "greaves", "mail",
    }),
    ("Ring", {"ring"}),
    ("Rod", {"rod"}),
    ("Staff", {"staff"}),
    ("Wand", {"wand"}),
    ("Potion & Oil", {"potion", "oil", "elixir", "philter", "ointment",
                      "draught", "salve"}),
    ("Scroll", {"scroll"}),
    ("Book & Tome", {"book", "tome", "libram", "manual", "spellbook"}),
    ("Musical Instrument", {
        "harp", "lute", "lyre", "horn", "horns", "drum", "flute", "pipes",
        "bagpipes", "bell", "chime", "gong", "whistle", "bugle", "violin",
        "fiddle", "mandolin", "zither", "cittern", "bandore", "biwa",
        "kantele", "qanun", "rababah", "recorder", "tambourine", "cymbal",
        "organ", "instrument", "musical", "stringed",
    }),
]
_DEFAULT_CATEGORY = "Wondrous Item"

# Original (id < 900000) magic item_type slugs from the 2ERPGDB source, mapped
# into the same readable taxonomy. Mundane equipment slugs (weapon_melee,
# armor, clothing, provisions, animals, services, tack_harness, misc_equipment,
# item_food_lodging, weapon_ranged, weapon_ammo) are deliberately left as-is.
_ORIGINAL_MAGIC_MAP = {
    "ring": "Ring",
    "wand": "Wand",
    "rod": "Rod",
    "staff": "Staff",
    "scroll": "Scroll",
    "potion": "Potion & Oil",
    "magic_item_weapon_special": "Magic Weapon",
    "magic_item_armor_special": "Magic Armor",
    "misc_magic_books": "Book & Tome",
    "misc_magic_musical_instruments": "Musical Instrument",
    "misc_magic_jewelry": "Wondrous Item",
    "misc_magic_cloaks_robes": "Wondrous Item",
    "misc_magic_household_tools": "Wondrous Item",
    "misc_magic_weird": "Wondrous Item",
    "misc_magic_bags_bottles_pouch": "Wondrous Item",
    "misc_magic_boots_gloves": "Wondrous Item",
    "misc_magic_candles_etc": "Wondrous Item",
    "misc_magic_girdle_hats_helms": "Wondrous Item",
}


def categorize(item_type: str) -> str:
    kind = item_type[len("misc_magic_"):] if item_type.startswith("misc_magic_") else ""
    for label, members in _CATEGORY_SETS:
        if kind in members:
            return label
    return _DEFAULT_CATEGORY


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=str(_DEFAULT_DB))
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"error: db not found: {db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, item_type FROM items "
            "WHERE id >= 900000 AND item_type LIKE 'misc_magic%'"
        ).fetchall()
        print(f"{len(rows)} imported rows to recategorize.")

        counts: dict[str, int] = {}
        updates: list[tuple[str, int]] = []
        for r in rows:
            cat = categorize(r["item_type"])
            counts[cat] = counts.get(cat, 0) + 1
            updates.append((cat, r["id"]))

        print("\nResulting categories:")
        for cat, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {n:5d}  {cat}")

        if args.dry_run:
            print("\n(dry run — no changes written)")
            return 0

        conn.executemany("UPDATE items SET item_type = ? WHERE id = ?", updates)

        # Second pass: fold original (id < 900000) magic slugs into the same
        # readable taxonomy via the explicit map. Mundane slugs are untouched.
        orig_total = 0
        for slug, label in _ORIGINAL_MAGIC_MAP.items():
            cur = conn.execute(
                "UPDATE items SET item_type = ? WHERE item_type = ?",
                (label, slug),
            )
            if cur.rowcount:
                print(f"  original: {cur.rowcount:5d}  {slug!r} -> {label!r}")
                orig_total += cur.rowcount

        conn.commit()
        print(f"\nUpdated {len(updates)} imported + {orig_total} original rows.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
