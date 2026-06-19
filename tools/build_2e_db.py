#!/usr/bin/env python3
"""Build global/2e.db from 2ERPGDB JSON sources.

Reads:
  - Spell data: fetched from GitHub or from SPELLS_CACHE if present
  - Class data: fetched from GitHub or from CLASSES_CACHE if present

Creates:
  - global/2e.db with tables: spells, classes, class_levels
"""
import json
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "global" / "2e.db"

SPELLS_URL  = "https://raw.githubusercontent.com/brandonm4/2ERPGDB/master/2E/spells.json"
CLASSES_URL = "https://raw.githubusercontent.com/brandonm4/2ERPGDB/master/2E/classes.json"
ITEMS_URL   = "https://raw.githubusercontent.com/brandonm4/2ERPGDB/master/2E/items.json"

# Allow local overrides (set by caller or pre-downloaded)
SPELLS_CACHE  = Path(sys.argv[1]) if len(sys.argv) > 1 else None
CLASSES_CACHE = Path(sys.argv[2]) if len(sys.argv) > 2 else None
ITEMS_CACHE   = Path(sys.argv[3]) if len(sys.argv) > 3 else None


def fetch_json(url: str, cache: Path | None) -> dict | list:
    if cache and cache.exists():
        print(f"  Using cached: {cache}")
        return json.loads(cache.read_text())
    print(f"  Fetching: {url}")
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def parse_casting_time_init(ct) -> int:
    """Convert casting_time string to an integer for initiative (1–10).
    1–9 = segment value; 10 = full round or longer."""
    if not ct:
        return 10
    s = str(ct).strip().lower()
    try:
        return min(int(float(s)), 10)
    except ValueError:
        pass
    if any(x in s for x in ["rd", "turn", "hour", "hr", "min", "segment"]):
        return 10
    m = re.match(r"^(\d+)", s)
    if m:
        return min(int(m.group(1)), 10)
    return 10


def build_spells(conn: sqlite3.Connection, data: dict) -> int:
    conn.execute("DROP TABLE IF EXISTS spells")
    conn.execute("""
        CREATE TABLE spells (
            id               INTEGER PRIMARY KEY,
            name             TEXT NOT NULL COLLATE NOCASE,
            level            INTEGER,
            school           TEXT,
            caster           TEXT,
            verbal           INTEGER,
            somatic          INTEGER,
            material         INTEGER,
            materials        TEXT,
            range            TEXT,
            aoe              TEXT,
            casting_time     TEXT,
            casting_time_init INTEGER,
            duration         TEXT,
            save             TEXT,
            damage           TEXT,
            description      TEXT,
            source           TEXT,
            page             INTEGER,
            reversible       INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_spells_name ON spells(name COLLATE NOCASE)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_spells_caster_level ON spells(caster, level)")

    spells = data.get("spells", data) if isinstance(data, dict) else data
    rows = []
    for s in spells:
        rows.append((
            s.get("id"),
            s.get("name", ""),
            _int_or_none(s.get("level")),
            s.get("school"),
            s.get("caster"),
            int(bool(s.get("verbal"))),
            int(bool(s.get("somatic"))),
            int(bool(s.get("material"))),
            s.get("materials"),
            s.get("range"),
            s.get("aoe"),
            s.get("casting_time"),
            parse_casting_time_init(s.get("casting_time")),
            s.get("duration"),
            s.get("save"),
            s.get("damage"),
            s.get("description"),
            s.get("source"),
            _int_or_none(s.get("page")),
            int(bool(s.get("reversible"))),
        ))
    conn.executemany("""
        INSERT INTO spells VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    return len(rows)


def build_classes(conn: sqlite3.Connection, data: dict) -> int:
    conn.execute("DROP TABLE IF EXISTS class_levels")
    conn.execute("DROP TABLE IF EXISTS classes")
    conn.execute("""
        CREATE TABLE classes (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL COLLATE NOCASE,
            description      TEXT,
            hit_die          TEXT,
            prime_requisite  TEXT,
            special_abilities TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_classes_name ON classes(name COLLATE NOCASE)")
    conn.execute("""
        CREATE TABLE class_levels (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id        INTEGER NOT NULL,
            level           INTEGER NOT NULL,
            xp_required     INTEGER,
            hit_dice        TEXT,
            attacks         TEXT,
            thac0           INTEGER,
            save_paralysis  INTEGER,
            save_rsw        INTEGER,
            save_petrify    INTEGER,
            save_breath     INTEGER,
            save_spell      INTEGER,
            spell_slots     TEXT,
            turn_undead     TEXT,
            FOREIGN KEY (class_id) REFERENCES classes(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_class_levels ON class_levels(class_id, level)")

    classes = data.get("classes", data) if isinstance(data, dict) else data
    total_levels = 0
    for cls in classes:
        conn.execute("""
            INSERT INTO classes (name, description, hit_die, prime_requisite, special_abilities)
            VALUES (?,?,?,?,?)
        """, (
            cls.get("name", ""),
            cls.get("description"),
            cls.get("hit_die"),
            json.dumps(cls.get("prime_requisite")) if isinstance(cls.get("prime_requisite"), list) else cls.get("prime_requisite"),
            json.dumps(cls.get("special_abilities", [])),
        ))
        class_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        for lvl in cls.get("experience_levels", []):
            saves = lvl.get("saving_throws", {})
            conn.execute("""
                INSERT INTO class_levels
                (class_id, level, xp_required, hit_dice, attacks, thac0,
                 save_paralysis, save_rsw, save_petrify, save_breath, save_spell,
                 spell_slots, turn_undead)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                class_id,
                lvl.get("level"),
                _int_or_none(lvl.get("xpRequired")),
                lvl.get("hitDice"),
                lvl.get("attacks"),
                _int_or_none(lvl.get("thac0")),
                _int_or_none(saves.get("paralyzation_poison_death_magic")),
                _int_or_none(saves.get("rod_staff_wand")),
                _int_or_none(saves.get("petrification_polymorph")),
                _int_or_none(saves.get("breath_weapon")),
                _int_or_none(saves.get("spell")),
                json.dumps(lvl.get("spells")) if lvl.get("spells") else None,
                json.dumps(lvl.get("turn_undead")) if lvl.get("turn_undead") else None,
            ))
            total_levels += 1
    return total_levels


_ITEM_RARITY: dict[str, int] = {
    "armor":                    0,
    "weapon_melee":             0,
    "weapon_ranged":            0,
    "weapon_ammo":              0,
    "misc_equipment":           0,
    "provisions":               0,
    "clothing":                 0,
    "item_food_lodging":        0,
    "services":                 0,
    "animals":                  0,
    "tack_harness":             0,
    "scroll":                   10,
    "potion":                   15,
    "wand":                     30,
    "ring":                     45,
    "rod":                      55,
    "staff":                    60,
    "magic_item_armor_special": 50,
    "magic_item_weapon_special":50,
}


def build_items(conn: sqlite3.Connection, data: dict) -> int:
    conn.execute("DROP TABLE IF EXISTS items")
    conn.execute("""
        CREATE TABLE items (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL COLLATE NOCASE,
            item_type   TEXT,
            cost        TEXT,
            weight      TEXT,
            description TEXT,
            source      TEXT,
            rarity      INTEGER DEFAULT 0,
            ac          INTEGER,
            size        TEXT,
            weapon_type TEXT,
            speed       INTEGER,
            damage_sm   TEXT,
            damage_l    TEXT,
            rof         TEXT,
            range_s     TEXT,
            range_m     TEXT,
            range_l     TEXT,
            xp_value    INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_name ON items(name COLLATE NOCASE)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_type ON items(item_type)")

    all_items: list[dict] = []
    for section in ("armor", "weapons", "items", "magic_items"):
        all_items.extend(data.get(section, []))

    rows = []
    for item in all_items:
        itype = item.get("itemType", "")
        default_rarity = _ITEM_RARITY.get(itype, 65 if itype.startswith("misc_magic") else 0)
        rows.append((
            item.get("id"),
            item.get("name", ""),
            itype,
            item.get("cost"),
            str(item.get("weight", "")) or None,
            item.get("description"),
            item.get("source"),
            default_rarity,
            _int_or_none(item.get("ac")),
            item.get("size"),
            item.get("type"),
            _int_or_none(item.get("speed")),
            item.get("damage_sm"),
            item.get("damage_l"),
            item.get("rof"),
            item.get("range_s"),
            item.get("range_m"),
            item.get("range_l"),
            _int_or_none(item.get("xpValue")),
        ))
    conn.executemany(
        "INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def _int_or_none(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def main():
    print(f"Building {DB_PATH}")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    spell_data   = fetch_json(SPELLS_URL,  SPELLS_CACHE)
    classes_data = fetch_json(CLASSES_URL, CLASSES_CACHE)
    items_data   = fetch_json(ITEMS_URL,   ITEMS_CACHE)

    conn = sqlite3.connect(str(DB_PATH))
    try:
        with conn:
            n_spells = build_spells(conn, spell_data)
            print(f"  Inserted {n_spells} spells")
            n_levels = build_classes(conn, classes_data)
            print(f"  Inserted {n_levels} class level rows")
            n_items = build_items(conn, items_data)
            print(f"  Inserted {n_items} items")
        print(f"Done → {DB_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
