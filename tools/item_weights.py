"""Shared item-weight resolution. Used by both dashboard.py and tools/survival.py
so encumbrance numbers stay consistent between the in-play MCP tool and the
character-sheet display.

Resolution order:
  1. Strip trailing parenthetical descriptors and trailing 'x N' qty suffix
     ('Hide armour (custom-fitted)' -> 'Hide armour'; 'Torches x6' qty=6).
  2. Normalize spelling ('armour' -> 'armor', drop apostrophes).
  3. INVENTORY_ALIASES — exact then prefix match — redirects campaign phrasing
     to the canonical 2e item name.
  4. Exact case-insensitive match in 2e.db `items` (preferred so 'plate mail'
     resolves to 'Plate Mail' (50 lb), not 'Bronze Plate Mail' (45 lb)).
  5. Substring match in 2e.db, with plural→singular and trailing-armor fallbacks.
  6. PHB_OVERRIDES — items in PHB Chapter 6 that aren't in 2e.db (shields,
     bedroll, tinderbox, spellbook, ...).

resolve_weight() returns {'lb': float, 'source': str} where source is one of:
  'db', 'alias', 'phb', 'negligible', 'unknown'.
"""
import re
import sqlite3
from pathlib import Path

_2E_DB = Path(__file__).resolve().parent.parent / "global" / "2e.db"

_WEIGHT_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)")
_INV_QTY_SUFFIX_RE = re.compile(r"\s*[x×]\s*(\d+)\s*$", re.IGNORECASE)


# Campaign-inventory phrasing → canonical 2e.db item name. Lets us reach
# entries the substring search misses because of word-order or spelling
# differences. All keys lowercased and 'armour' → 'armor' normalized.
INVENTORY_ALIASES: dict[str, str] = {
    # Weapons
    "hand axe": "Hand or throwing axe",
    "throwing axe": "Hand or throwing axe",
    # Lanterns (DB lists them comma-reversed)
    "hooded lantern":   "Lantern, Hooded",
    "bullseye lantern": "Lantern, Bullseye",
    "beacon lantern":   "Lantern, Beacon",
    # Tack
    "pack saddle":   "Saddle, Pack",
    "riding saddle": "Saddle, Riding",
    "saddlebag":  "Saddle bags, Large",
    "saddlebags": "Saddle bags, Large",
    "saddle bag":  "Saddle bags, Large",
    "saddle bags": "Saddle bags, Large",
    # Clothing
    "robes":           "Robe, Plain",
    "common robe":     "Robe, Common",
    "embroidered robe":"Robe, Embroidered",
    # Rations — campaigns spell these many ways
    "rations":          "Rations, iron (1 week)",
    "iron rations":     "Rations, iron (1 week)",
    "trail rations":    "Rations, iron (1 week)",
    "standard rations": "Rations, iron (1 week)",
    "dry rations":      "Dry rations (per week)",
    # Thief tools
    "thieves tools":    "Thieves' picks",
    "thieves' tools":   "Thieves' picks",
    "thief tools":      "Thieves' picks",
    "lock picks":       "Thieves' picks",
    # Oil / lamp oil
    "flask of oil": "Oil, Lamp",
    "oil flask":    "Oil, Lamp",
    "lamp oil":     "Oil, Lamp",
    # Rope
    "hemp rope": "Rope (per 50 ft.), Hemp",
    "silk rope": "Rope (per 50 ft.), Silk",
    # Mirror
    "small mirror":       "Mirror (small, steel)",
    "small steel mirror": "Mirror (small, steel)",
    "steel mirror":       "Mirror (small, steel)",
    # Common spelling collapses
    "longsword":   "Long sword",
    "shortsword":  "Short sword",
    "shortbow":    "Short bow",
    "longbow":     "Long bow",
    # Rope phrasing variants ("Rope 50' hemp", "Rope 50 ft hemp")
    "rope 50":        "Rope (per 50 ft.), Hemp",
    "rope 50 ft":     "Rope (per 50 ft.), Hemp",
    "rope 50 ft hemp": "Rope (per 50 ft.), Hemp",
    "rope 50 ft silk": "Rope (per 50 ft.), Silk",
    "50 ft rope":     "Rope (per 50 ft.), Hemp",
    "50ft rope":      "Rope (per 50 ft.), Hemp",
}


# Items present in PHB Chapter 6 but absent from 2e.db items table.
# Sources: PHB Table 47 (Armor), Misc Equipment list, Tack & Harness.
PHB_OVERRIDES: dict[str, float] = {
    # Shields (PHB Armor table)
    "small shield":  5.0,
    "medium shield": 10.0,
    "body shield":   15.0,
    "large shield":  15.0,   # synonym for body
    "buckler":       3.0,
    "shield":        10.0,   # generic → assume medium
    # Common kit
    "bedroll":       3.0,    # PHB: "blankets are rolled into bedrolls" (winter blanket = 3 lbs)
    "tinderbox":     0.5,    # PHB substitute for flint & steel; small container
    "spellbook":     3.0,    # PHB convention (no explicit weight given)
    "spell book":    3.0,
    "spell component pouch": 3.0,
    "component pouch":       3.0,
    "saddle blanket":        4.0,  # PHB Tack & Harness
    "winter blanket":        3.0,  # PHB Misc Equipment
}


def parse_weight(raw) -> float:
    """Extract a numeric pound value from item weight strings like
    '5 lbs.', '0.5 lbs.', '1 lb.', '5 lbs. (full)', or bare '7'.
    Returns 0 for placeholders ('*', '**', '_ lbs.') or missing values."""
    if not raw:
        return 0.0
    m = _WEIGHT_NUM_RE.match(str(raw))
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except ValueError:
        return 0.0


def split_inventory_qty(raw: str) -> tuple[str, int]:
    """Split a trailing quantity suffix off an inventory string.
    'Hand axe x2' -> ('Hand axe', 2). 'Spear' -> ('Spear', 1)."""
    s = (raw or "").strip()
    m = _INV_QTY_SUFFIX_RE.search(s)
    if not m:
        return s, 1
    qty = max(1, int(m.group(1)))
    return s[: m.start()].rstrip(" ,"), qty


def resolve_weight(name: str, conn: sqlite3.Connection | None = None) -> dict:
    """Resolve a free-form inventory string to a weight in pounds.

    Returns {'lb': float, 'source': str} where source is one of:
      'db'         — direct match in 2e.db items
      'alias'      — matched via INVENTORY_ALIASES then DB
      'phb'        — PHB-specified weight not in 2e.db
      'negligible' — DB row exists but weight is '*' / '**' / null
      'unknown'    — no match anywhere

    If conn is None, opens (and closes) a connection to 2e.db for the call.
    Pass a cached connection to avoid the open cost in hot loops.
    """
    cleaned = (name or "").strip().lstrip("0123456789x ").strip()
    if not cleaned:
        return {"lb": 0.0, "source": "unknown"}

    # Strip trailing parenthetical descriptors:
    # 'Hide armour (custom-fitted, AC 6)' → 'Hide armour'
    base = re.sub(r"\s*\([^)]*\)\s*$", "", cleaned).strip() or cleaned

    norm = (base.lower()
            .replace("armour", "armor")
            .replace("colour", "color")
            .replace("'", "")
            .replace("’", ""))  # right single quotation mark

    # 1) Alias resolution — exact match, then prefix-based.
    canonical = INVENTORY_ALIASES.get(norm)
    if not canonical:
        for k, v in INVENTORY_ALIASES.items():
            if norm == k or norm.startswith(k + " "):
                canonical = v
                break

    candidates: list[tuple[str, str]] = []
    if canonical:
        candidates.append((canonical, "alias"))
    candidates.append((norm, "db"))
    if norm.endswith("ies") and len(norm) > 3:
        candidates.append((norm[:-3] + "y", "db"))
    elif norm.endswith("es") and len(norm) > 2:
        candidates.append((norm[:-2], "db"))
    elif norm.endswith("s") and len(norm) > 1:
        candidates.append((norm[:-1], "db"))
    # Drop trailing 'armor'/'armour'/'mail' so 'studded leather armor' → 'studded leather'.
    stripped = re.sub(r"\s+(armor|armour|mail)\s*$", "", norm).strip()
    if stripped and stripped != norm:
        candidates.append((stripped, "db"))

    own_conn = False
    if conn is None:
        if not _2E_DB.exists():
            # No DB → only PHB overrides can help.
            if norm in PHB_OVERRIDES:
                return {"lb": PHB_OVERRIDES[norm], "source": "phb"}
            return {"lb": 0.0, "source": "unknown"}
        conn = sqlite3.connect(str(_2E_DB))
        own_conn = True

    try:
        # 2) DB lookup — prefer exact name match, fall back to substring.
        for c, source in candidates:
            row = conn.execute(
                "SELECT weight FROM items WHERE LOWER(name) = LOWER(?) LIMIT 1",
                (c,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT weight FROM items WHERE name LIKE ? COLLATE NOCASE LIMIT 1",
                    (f"%{c}%",),
                ).fetchone()
            if not row:
                continue
            w = parse_weight(row[0])
            if w > 0:
                return {"lb": w, "source": source}
            if row[0] is not None:
                return {"lb": 0.0, "source": "negligible"}
    except sqlite3.Error:
        pass
    finally:
        if own_conn:
            conn.close()

    # 3) PHB-specified items not in 2e.db
    if norm in PHB_OVERRIDES:
        return {"lb": PHB_OVERRIDES[norm], "source": "phb"}

    return {"lb": 0.0, "source": "unknown"}


def item_weight(name: str, conn: sqlite3.Connection | None = None) -> float:
    """Backwards-compatible thin wrapper returning just the weight in lb,
    summed for any trailing 'x N' quantity suffix in the inventory string."""
    base, qty = split_inventory_qty(name)
    return resolve_weight(base, conn=conn)["lb"] * qty


# ---------------------------------------------------------------------------
# Encumbrance bands — sourced from PHB Table 47 (Character Encumbrance).
# ---------------------------------------------------------------------------
# Keys are the LOWER STR value of each PHB Table 47 row (so a 'largest key ≤
# STR' lookup selects the right bucket: STR 9 → key 8 (PHB row "8-9")).
#
# Values are (light, moderate, heavy, severe) upper-bound caps in pounds,
# mapped from PHB's columns (Unencumbered, Light, Moderate, Heavy):
#
#   code band        PHB band        movement effect (PHB)       project penalty
#   "light"          Unencumbered    full MR                     0
#   "moderate"       Light           -1/3 MR  (~-4 on MR 12)     -3
#   "heavy"          Moderate        -1/2 MR  (~-6 on MR 12)     -6
#   "severe"         Heavy           -2/3 MR  (~-8 on MR 12)     -9
#   "overloaded"     Severe or >Max  MR drops to 1                -12
#
# STR 19+ rows are not in PHB Table 47 (which stops at 18/00). Those entries
# extrapolate using the +39 lb stride PHB uses across STR 18/01–50 → 18/00,
# anchored to the Weight Allowance (PHB Table 1) at each score.
STR_BANDS: dict[int, tuple[int, int, int, int]] = {
    2:  (1,    2,    3,    4),
    3:  (5,    6,    7,    9),
    4:  (10,   13,   16,   19),
    6:  (20,   29,   38,   46),
    8:  (35,   50,   65,   80),
    10: (40,   58,   76,   96),
    12: (45,   69,   93,   117),
    14: (55,   85,   115,  145),
    16: (70,   100,  130,  160),
    17: (85,   121,  157,  193),
    18: (110,  149,  188,  227),
    19: (485,  524,  563,  602),
    20: (535,  574,  613,  652),
    21: (635,  674,  713,  752),
    22: (785,  824,  863,  902),
    23: (935,  974,  1013, 1052),
    24: (1235, 1274, 1313, 1352),
    25: (1535, 1574, 1613, 1652),
}


def str_band_for(strength: int) -> tuple[int, int, int, int]:
    """Return (light, moderate, heavy, severe) upper bounds for a STR score."""
    keys = sorted(STR_BANDS.keys())
    pick = keys[0]
    for k in keys:
        if k <= strength:
            pick = k
        else:
            break
    return STR_BANDS[pick]


_BAND_PENALTIES = {
    "light":      0,
    "moderate":  -3,
    "heavy":     -6,
    "severe":    -9,
    "overloaded": -12,
}


def compute_encumbrance(char: dict, state: dict, key: str,
                        conn: sqlite3.Connection | None = None) -> dict:
    """Compute encumbrance for one character. Returns:

      {
        'band':     'light'|'moderate'|'heavy'|'severe'|'overloaded',
        'weight':   float (lb, rounded to 1 decimal),
        'penalty':  int (movement penalty, 0/-3/-6/-9/-12),
        'strength': int,
        'thresholds': {'light': int, 'moderate': int, 'heavy': int, 'severe': int},
        'items':    [{'item','qty','unit_lb','weight_lb','source','origin'}, ...],
      }

    Used by both the dashboard sheet view and the MCP encumbrance tool so
    they stay in lockstep. Pass a sqlite connection to amortize lookups.
    """
    ab = char.get("ability_scores") or {}
    strength = int(ab.get("str", 10))

    items: list[dict] = []
    total = 0.0

    own_conn = False
    if conn is None and _2E_DB.exists():
        conn = sqlite3.connect(str(_2E_DB))
        own_conn = True
    try:
        for raw in char.get("inventory", []) or []:
            base, qty = split_inventory_qty(str(raw))
            res = resolve_weight(base, conn=conn)
            sub = res["lb"] * qty
            total += sub
            items.append({"item": base, "qty": qty, "unit_lb": res["lb"],
                          "weight_lb": sub, "source": res["source"],
                          "origin": "inventory"})
        bag = state.get("consumables", {}).get(key, {}) or {}
        for item_name, qty in bag.items():
            qty_i = int(qty)
            res = resolve_weight(item_name, conn=conn)
            sub = res["lb"] * qty_i
            total += sub
            items.append({"item": item_name, "qty": qty_i, "unit_lb": res["lb"],
                          "weight_lb": sub, "source": res["source"],
                          "origin": "consumable"})
    finally:
        if own_conn:
            conn.close()

    light, moderate, heavy, severe = str_band_for(strength)
    if total <= light:
        band = "light"
    elif total <= moderate:
        band = "moderate"
    elif total <= heavy:
        band = "heavy"
    elif total <= severe:
        band = "severe"
    else:
        band = "overloaded"

    return {
        "band":       band,
        "weight":     round(total, 1),
        "penalty":    _BAND_PENALTIES[band],
        "strength":   strength,
        "thresholds": {"light": light, "moderate": moderate,
                       "heavy": heavy, "severe": severe},
        "items":      items,
    }
