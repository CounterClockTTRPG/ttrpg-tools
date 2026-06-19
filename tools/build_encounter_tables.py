#!/usr/bin/env python3
"""Generate global, tiered, terrain-keyed encounter tables from monsters.db.

Output: ``global/encounter_tables.json`` — campaign-independent reference data
consumed by ``determine_encounter`` (see tools/dice.py). Per-campaign overrides
in campaign.json still take precedence when present.

Design (agreed):
  * Four difficulty BANDS keyed to party level (low/mid/high/epic).
  * Each terrain has one entry-table per band. ``determine_encounter`` derives a
    base band from party level, then rolls 2d6 for drift (2 -> step down,
    12 -> step up), and rolls on the resulting band's table.
  * A monster lands in a band by its Hit Dice, in one or more terrains by its
    climate/terrain string. Broad "any/any land/temperate/..." monsters are
    spread across land terrains so every terrain has variety.
  * Within a band, each monster's die-range width is weighted by frequency
    (Common rarer-to-roll? no — Common = widest), so common monsters come up
    more often. Number-appearing is parsed and capped per band so a high-HD
    result doesn't arrive in swarm numbers.

Usage:
    python3 tools/build_encounter_tables.py [--db PATH] [--out PATH]
                                            [--max-per-table N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO_ROOT / "global" / "monsters.db"
_DEFAULT_OUT = _REPO_ROOT / "global" / "encounter_tables.json"

# Difficulty bands. ``party_levels`` is inclusive; ``hd`` is the Hit-Dice span a
# monster needs to land in this band. Kept aligned so a level-N party meets
# roughly HD-N foes at its base band.
BANDS = [
    {"name": "low",  "party_levels": [1, 4],   "hd": [0, 3]},
    {"name": "mid",  "party_levels": [5, 9],   "hd": [4, 7]},
    {"name": "high", "party_levels": [10, 14], "hd": [8, 12]},
    {"name": "epic", "party_levels": [15, 99], "hd": [13, 999]},
]

# Number-appearing cap per band index. Swarms are fine for challenge; the cap
# just trims absurd tribal counts (10-100) and keeps tough/high-tier foes in
# smaller groups.
_COUNT_CAP = {0: 20, 1: 12, 2: 6, 3: 3}

# Specific TERRAIN tokens — a monster carrying one is placed in that terrain.
_TERRAIN_KEYWORDS = {
    "dungeon":  ["subterranean", "underdark", "underground", "cavern", "cave"],
    "forest":   ["forest", "wood"],
    "mountain": ["mountain", "alpine"],
    "hills":    ["hill"],
    "swamp":    ["swamp", "marsh", "fen", "bog", "moor"],
    "desert":   ["desert", "waste", "sandy", "dune", "barren"],
    "arctic":   ["arctic", "tundra", "glacier", "polar", "ice cap", "icy"],
    "jungle":   ["jungle", "rainforest"],
    "plains":   ["plain", "grassland", "steppe", "savanna", "prairie", "scrub", "veldt"],
    "ocean":    ["ocean", "sea", "aquatic", "water", "coast", "marine", "river", "lake"],
    "urban":    ["urban", "city", "civiliz", "ruin", "town", "village"],
}
_LAND_TERRAINS = ["forest", "mountain", "hills", "swamp", "desert",
                  "arctic", "jungle", "plains"]

# CLIMATE-only tokens map to a SUBSET of land terrains (so 'temperate' monsters
# don't leak into deserts/arctic, etc.). Used only when no specific terrain token
# is present.
_CLIMATE_TERRAINS = {
    "subarctic":   ["arctic"],
    "arctic":      ["arctic"],
    "cold":        ["arctic"],
    "temperate":   ["forest", "hills", "plains", "mountain"],
    "subtropical": ["jungle", "swamp", "plains", "desert"],
    "tropical":    ["jungle", "swamp", "desert"],
    "warm":        ["desert", "jungle", "plains"],
}
# Truly-anywhere tokens — spread across every land terrain.
_ANY_TOKENS = ["any", "wilderness", "remote", "non-arctic", "non-mountainous"]

# "Very exotic" markers — creatures native to other planes or non-core
# campaign settings (Spelljammer, Ravenloft, Dark Sun) don't belong in
# ordinary terrestrial wandering-encounter tables, even when they carry a
# generic "any"/"any space" climate that would otherwise spread them onto
# land. Matched against the climate_terrain field + the name (so setting tags
# like "(athas)" are caught). Pure-planar creatures usually fall out anyway
# (their climate matches no terrain), but this also catches the "any"-tagged
# fiends and the alt-setting natives. Note: scanning the *description* was
# rejected — prose like "on this plane"/"in the space of" false-positives
# core monsters (basilisk, b'rohg). Frequency "unique"/"mythical" is exotic
# by definition (a wandering tarrasque is absurd).
_EXOTIC_MARKERS = [
    # planes / planar
    "plane", "planar", "abyss", "baator", "gehenna", "acheron", "carceri",
    "pandemonium", "hades", "tarterus", "tartarus", "elysium", "bytopia",
    "arcadia", "mechanus", "arborea", "ysgard", "beastlands", "limbo",
    "outlands", "astral", "ethereal", "demiplane", "para-elemental",
    "quasi-elemental", "positive energy", "negative energy", "lower planes",
    "upper planes", "outer plane", "inner plane", "mephit",
    # Spelljammer
    "wildspace", "space", "phlogiston", "crystal sphere", "spelljamm",
    "rock of bral",
    # Ravenloft / domains of dread
    "ravenloft", "shadow rift", "nightmare lands", "domains of dread",
    # Dark Sun
    "athas",
]
_EXOTIC_FREQ = ("unique", "mythical")


def is_exotic(name: str | None, frequency: str | None,
              climate: str | None) -> bool:
    """True for other-planar / alt-setting / unique creatures that should be
    kept out of ordinary terrestrial encounter tables."""
    f = (frequency or "").lower()
    if any(tok in f for tok in _EXOTIC_FREQ):
        return True
    blob = f"{(climate or '').lower()} {(name or '').lower()}"
    return any(m in blob for m in _EXOTIC_MARKERS)


# Structured-category filter (preferred over the string heuristics above, which
# miss "Any"-climate natives of other settings — e.g. mephits tagged
# climate "Any" but sourced only from Planescape, or the tarek, climate
# "Any plains" but Dark Sun). A creature is exotic-by-category when it carries a
# source category from an off-Prime / off-world setting (_DENY_CATEGORIES) and
# NONE from a core Prime-material line (_ALLOW_CATEGORIES) — so a monster that
# also appears in the Monstrous Manual / Greyhawk / Forgotten Realms is kept,
# but one that ONLY exists in Dark Sun / Planescape / Dragonlance / etc. is cut.
# Matched as case-insensitive substrings against each category name.
_DENY_CATEGORIES = [
    # planar / Planescape
    "planescape", "planes of law", "planes of chaos", "planes of conflict",
    "outer planes", "in the cage", "sigil", "ethereal plane", "astral plane",
    "hellbound", "blood war", "guide to hell", "dead gods", "modron",
    "baatezu", "tanar'ri",
    # Spelljammer
    "spelljammer", "wildspace", "realmspace", "greyspace", "krynnspace",
    "crystal sphere", "rock of bral", "lost ships", "practical planetology",
    "under the dark fist", "dawn of the overmind", "legend of spelljammer",
    "skull & crossbows",
    # Dark Sun (Athas)
    "dark sun", "athas", "silt sea", "dragon kings", "ivory triangle",
    "dust and fire", "mind lords", "thri-kreen", "dune trader",
    # Ravenloft (Domains of Dread)
    "ravenloft", "van richten", "nightmare lands", "shadow rift", "darklords",
    "islands of terror", "ship of horror", "feast of goblyns", "castles forlorn",
    "bleak house", "requiem", "grim harvest", "servants of darkness",
    "from the shadows", "hour of the knife", "adam's wrath", "vecna reborn",
    "touch of death", "transylvania", "masters of eternal night",
    "dark of the moon", "thoughts of darkness", "death ascendant",
    "a darkness gathering",
    # Al-Qadim (Zakhara)
    "al-qadim", "land of fate", "city of delights", "corsairs", "golden voyages",
    "ruined kingdoms", "caravans", "asticlian",
    # Kara-Tur / Oriental
    "kara-tur", "test of the samurai",
    # Maztica
    "maztica", "fires of zatal", "city of gold", "endless armies",
    # Nehwon / Lankhmar
    "nehwon", "lankhmar",
    # Dragonlance (Krynn)
    "dragonlance", "krynn", "taladas", "flint's axe", "dragon dawn",
    "dragon's rest",
    # Mystara / Savage Coast
    "mystara", "savage coast",
    # Birthright
    "rjurik", "khourane",
    # historical / standalone settings
    "celts", "glory of rome", "age of heroes", "council of wyrms",
    "chronomancer", "legends & lore", "storm riders", "black courser",
    "netherbird", "baba yaga", "otherlands",
]
_ALLOW_CATEGORIES = [
    "monstrous manual", "fiend folio", "compendium volume", "annual volume",
    "greyhawk", "forgotten realms", "cult of the dragon", "undermountain",
    "menzoberranzan", "drow of the underdark", "zhentil keep",
    "city of splendors", "ravens bluff", "raven's bluff", "myth drannor",
    "daggerdale", "giantcraft", "evermeet", "wild elves", "shining south",
    "old empires", "scarlet brotherhood", "from the ashes", "against the giants",
    "tomb of horrors", "rary", "vecna lives", "code of the harpers",
    "secrets of the magister", "pages from the mages", "complete book of elves",
    "sea devils", "horde",
]


def is_exotic_by_category(categories) -> bool:
    """True when a monster's source categories place it in an off-Prime or
    off-world setting with no core Prime-material source to anchor it.

    `categories` may be a JSON-encoded list (as stored in monsters.db), a list
    of strings, or None. Returns False when categories are absent (the row then
    falls back to the climate/frequency heuristics in `is_exotic`)."""
    if not categories:
        return False
    if isinstance(categories, str):
        try:
            categories = json.loads(categories)
        except (ValueError, TypeError):
            return False
    low = [str(c).lower() for c in categories]
    deny = any(any(m in c for m in _DENY_CATEGORIES) for c in low)
    if not deny:
        return False
    allow = any(any(m in c for m in _ALLOW_CATEGORIES) for c in low)
    return not allow

# Frequency -> die-range weight (Common comes up most often).
_FREQ_WEIGHT = [("very rare", 1), ("rare", 2), ("uncommon", 4), ("common", 8)]
_DEFAULT_WEIGHT = 3

# All terrains we emit (land + dungeon/ocean/urban + generic wilderness/road).
ALL_TERRAINS = list(_TERRAIN_KEYWORDS.keys()) + ["wilderness", "road"]


def _first_int(s: str | None, default: int = 1) -> int:
    if not s:
        return default
    m = re.search(r"\d+", s)
    return int(m.group(0)) if m else default


def parse_hd(s: str | None) -> int | None:
    """Hit Dice as an int, or None when the field is prose ('See below',
    'As per dragon', 'Varies', None). Fractional HD (½, ¼, '1/2') -> 0 so weak
    creatures (kobolds, molds, sprites) still band as low."""
    if not s:
        return None
    t = s.strip()
    if t[:1] in ("½", "¼", "⅓", "⅛", "⅔", "¾") or re.match(r"^\s*1\s*/\s*[2-9]", t):
        return 0
    m = re.search(r"\d+", t)
    return int(m.group(0)) if m else None


def parse_xp(s: str | None) -> int | None:
    """First integer in an XP field ('9,000 (7,000 …)' -> 9000), or None for
    prose ('Varies', 'As per individual dragon')."""
    if not s:
        return None
    m = re.search(r"\d[\d,]*", s)
    return int(m.group(0).replace(",", "")) if m else None


def hd_to_band(hd: int) -> int:
    for i, b in enumerate(BANDS):
        lo, hi = b["hd"]
        if lo <= hd <= hi:
            return i
    return len(BANDS) - 1


# XP thresholds used to band monsters whose HD is given as prose. Keeps
# prose-HD heavyweights (mind flayer ~9k, demilich ~10k) out of the low tier.
def xp_to_band(xp: int) -> int:
    if xp <= 175:
        return 0   # low
    if xp <= 1400:
        return 1   # mid
    if xp <= 6000:
        return 2   # high
    return 3       # epic


def freq_weight(freq: str | None) -> int:
    f = (freq or "").lower()
    for key, w in _FREQ_WEIGHT:          # very rare before rare; uncommon before common
        if key in f:
            return w
    return _DEFAULT_WEIGHT


# How strongly a monster belongs in a terrain, by how it matched. A terrain
# native gets a gentle nudge over a ubiquitous "any"-climate creature, but the
# multiplier is deliberately small (max ×2) so it can NEVER flip a rarity tier:
# rarity stays the dominant lever (a Common always out-rolls an Uncommon, etc.)
# and terrain fit only orders peers within a tier.
_SPECIFICITY_MULT = {3: 2, 2: 2, 1: 1}  # specific terrain / climate / any


def terrains_for(climate: str | None) -> dict[str, int]:
    """Map a monster's climate_terrain string to {terrain: specificity}.

    specificity 3 = a specific terrain token (forest/desert/…); 2 = a climate
    token (temperate/tropical/…) mapped to a subset of land terrains; 1 =
    'any'/blank spread across all land terrains. Keeps terrains distinct
    instead of every land tile sharing one giant pool.
    """
    c = (climate or "").lower()
    out: dict[str, int] = {}

    def add(terrain: str, spec: int) -> None:
        out[terrain] = max(out.get(terrain, 0), spec)

    # Handle negated terrains ("any non-arctic land", "non-mountainous"):
    # record what's excluded, then scrub the "non-X" phrase so its terrain
    # keyword ('arctic'/'mountain') can't false-match as a POSITIVE terrain
    # below — otherwise a goblin ("any non-arctic land") would be dumped into
    # the arctic table, the one place it never appears.
    negated: set[str] = set()
    for t, kws in _TERRAIN_KEYWORDS.items():
        if any(re.search(r"non[-\s]?" + re.escape(k), c) for k in kws):
            negated.add(t)
    c_pos = re.sub(r"non[-\s]?[a-z]+", " ", c)   # strip negation phrases

    specific = [t for t, kws in _TERRAIN_KEYWORDS.items() if any(k in c_pos for k in kws)]
    if specific:
        for t in specific:
            add(t, 3)
    else:
        for clim, terrs in _CLIMATE_TERRAINS.items():
            if clim in c_pos:
                for t in terrs:
                    add(t, 2)
        if not c_pos.strip() or any(tok in c_pos for tok in _ANY_TOKENS):
            for t in _LAND_TERRAINS:
                add(t, 1)

    # Drop any terrain the climate explicitly negated.
    for t in negated:
        out.pop(t, None)

    # Land dwellers also feed the generic wilderness + road tables, at the
    # monster's best land specificity.
    land_spec = max((s for t, s in out.items() if t in _LAND_TERRAINS), default=0)
    if land_spec:
        add("wilderness", land_spec)
        add("road", land_spec)
    return out


def number_appearing(no_app: str | None, band: int) -> str:
    """Cleaned, band-capped number-appearing prefix, e.g. '2-12' or '1'."""
    cap = _COUNT_CAP[band]
    nums = [int(n) for n in re.findall(r"\d+", no_app or "")]
    if not nums:
        return "1"
    lo = min(nums)
    hi = min(max(nums), cap)
    lo = min(lo, hi)
    return f"{lo}-{hi}" if hi > lo else f"{lo}"


def category_of(name: str, intelligence: str | None, descr: str | None) -> str:
    n = (name or "").lower()
    blob = f"{n} {(descr or '').lower()[:200]}"
    if "dragon" in n:
        return "dragon"
    if any(k in blob for k in ("undead", "skeleton", "zombie", "ghoul", "wraith",
                               "lich", "vampire", "ghost", "spectre", "specter",
                               "wight", "mummy")):
        return "undead"
    if "giant" in n:
        return "giant"
    if any(k in n for k in ("golem", "elemental")):
        return "construct"
    if any(k in n for k in ("demon", "devil", "fiend", "tanar", "baatez")):
        return "fiend"
    if (intelligence or "").strip().lower().startswith(("animal", "non-", "0", "1 ")):
        return "animal"
    return "monster"


# --------------------------------------------------------------------------- #
# Curated overlays — hand-authored entries for terrains the bestiary can't
# supply (cities are full of *people*, which monsters.db doesn't tag). Each
# entry is (weight, result, category). Layered on top of the auto-generated
# entries during build, so regeneration never loses them. Bands listed in
# _CURATED_REPLACE_BANDS use the curated entries ONLY (no auto bleed) — e.g.
# low-tier urban, so a 1st-level party never meets 2d20 level-draining shadows.
_CURATED: dict[str, dict[int, list[tuple[int, str, str]]]] = {
    "urban": {
        0: [  # low (party L1-4): petty crime, watch, vermin, city texture
            (8, "2d4 Cutpurses working the crowd", "humanoid"),
            (7, "City watch patrol (1d6 guards)", "npc"),
            (6, "1d3 Thugs demanding a back-alley toll", "humanoid"),
            (6, "Drunken brawl spilling out of a tavern", "event"),
            (6, "2d6 Giant rats boiling from a sewer grate", "animal"),
            (5, "Beggar with a rumor — or a pickpocket's lure", "npc"),
            (5, "Street festival / funeral / market crush (no combat)", "event"),
            (5, "A pursued thief barrels into the party", "event"),
            (4, "1d4 Stirges roosting under a bridge", "monster"),
            (4, "1d4 Stray dogs scavenging refuse", "animal"),
            (4, "Con artist running a shell game or fake fortune", "npc"),
            (3, "Fire breaks out in the quarter", "event"),
            (2, "A lone wererat in human guise, watching", "lycanthrope"),
        ],
        1: [  # mid (L5-9): guilds, corruption, shapeshifters
            (8, "2d4 Thieves' guild enforcers spring an ambush", "humanoid"),
            (6, "A corrupt watch sergeant shakes the party down", "npc"),
            (5, "1d3 Wererats up from the undercity", "lycanthrope"),
            (5, "Smugglers or cultists meeting in a warehouse", "event"),
            (4, "A doppelganger wearing a contact's face", "monster"),
            (4, "Bravos pick a fight over honor", "humanoid"),
            (4, "Plague swarm: 3d6 giant rats and a handler", "animal"),
            (3, "An assassin shadows the party through the streets", "npc"),
            (3, "Riot or collapse throws the quarter into chaos", "event"),
        ],
        2: [  # high (L10-14): intrigue, urban undead, infiltration
            (7, "An assassins' guild hit-team strikes", "humanoid"),
            (6, "A vampire prowls the night streets", "undead"),
            (5, "A doppelganger ring has replaced local officials", "monster"),
            (5, "A wererat clan erupts from the undercity", "lycanthrope"),
            (4, "2-12 Shadows haunt the abandoned quarter", "undead"),
            (4, "A noble's elite house guard (2d4 veterans)", "humanoid"),
            (3, "A mind flayer lairs in the deep sewers", "aberration"),
        ],
        3: [  # epic (L15+)
            (5, "A vampire lord and thralls move against the city", "undead"),
            (4, "A lich's agents search the archives", "undead"),
            (3, "A rakshasa noble manipulates the court", "fiend"),
        ],
    },
}
# (terrain -> set of band indices) where curated entries fully replace auto ones.
_CURATED_REPLACE_BANDS: dict[str, set[int]] = {"urban": {0, 1}}


def build(db_path: Path, max_per_table: int) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        has_categories = any(
            r[1] == "categories" for r in conn.execute("PRAGMA table_info(monsters)")
        )
        cat_col = "categories" if has_categories else "NULL AS categories"
        rows = conn.execute(
            f"SELECT name, frequency, no_appearing, hit_dice, climate_terrain, "
            f"intelligence, description, xp_value, {cat_col} FROM monsters "
            f"WHERE name IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    # buckets[terrain][band] = list of candidate dicts
    buckets: dict[str, list[list[dict]]] = {
        t: [[] for _ in BANDS] for t in ALL_TERRAINS
    }

    skipped_no_band = 0
    skipped_exotic = 0
    for r in rows:
        # Keep very-exotic creatures (other planes, Spelljammer, Ravenloft,
        # Dark Sun, uniques) out of ordinary terrestrial encounter tables.
        # Structured source categories catch the "Any"-climate alt-setting
        # natives the string heuristics miss (mephits, tarek, draconians, …).
        if (is_exotic(r["name"], r["frequency"], r["climate_terrain"])
                or is_exotic_by_category(r["categories"])):
            skipped_exotic += 1
            continue
        # Band from HD; if HD is prose, fall back to XP; if neither is usable
        # the row is a generic stub (bird/fish/golem,general/…) — skip it.
        hd = parse_hd(r["hit_dice"])
        if hd is not None:
            band, hd_disp = hd_to_band(hd), hd
        else:
            xp = parse_xp(r["xp_value"])
            if xp is None:
                skipped_no_band += 1
                continue
            band, hd_disp = xp_to_band(xp), 0
        terrains = terrains_for(r["climate_terrain"])
        if not terrains:
            continue
        base_weight = freq_weight(r["frequency"])
        name = (r["name"] or "").strip()
        result = f'{number_appearing(r["no_appearing"], band)} {name.title()}'
        category = category_of(name, r["intelligence"], r["description"])
        for t, spec in terrains.items():
            buckets[t][band].append({
                "result": result,
                "monster": name,
                "hd": hd_disp,
                # frequency × how characteristic this monster is of the terrain
                "weight": base_weight * _SPECIFICITY_MULT[spec],
                "freq_w": base_weight,   # rarity alone — drives table inclusion
                "spec": spec,            # terrain fit — secondary ordering
                "category": category,
            })

    # Convert each band's candidates into a die-range table.
    terrains_out: dict[str, dict] = {}
    for terrain, bands in buckets.items():
        tiers = []
        for band_idx, cands in enumerate(bands):
            # Curated overlay entries for this terrain/band, if any.
            curated = [
                {"result": res, "monster": "", "hd": 0, "weight": w,
                 "category": cat}
                for (w, res, cat) in _CURATED.get(terrain, {}).get(band_idx, [])
            ]
            if band_idx in _CURATED_REPLACE_BANDS.get(terrain, set()):
                cands = []  # curated-only band — no auto bleed
            # Curated first (so they survive the cap and lead the table), then
            # auto entries. Selection is FREQUENCY-FIRST: every Common that
            # maps to the terrain is kept, then Uncommon, then rarer fill the
            # remaining slots — so common staples (orcs, goblins) can't be
            # evicted by a flood of terrain-specific rarities. Terrain fit and
            # combined weight only break ties within a frequency tier.
            cands = curated + sorted(
                cands, key=lambda c: (-c["freq_w"], -c["spec"], -c["weight"], c["monster"])
            )
            cands = cands[:max_per_table]
            entries, cursor = [], 1
            for c in cands:
                lo = cursor
                hi = cursor + c["weight"] - 1
                entries.append({
                    "range": [lo, hi],
                    "result": c["result"],
                    "category": c["category"],
                    "monster": c["monster"],
                    "hd": c["hd"],
                })
                cursor = hi + 1
            tiers.append(entries)
        terrains_out[terrain] = {"tiers": tiers}

    return {
        "meta": {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": str(db_path.relative_to(_REPO_ROOT)),
            "monsters_scanned": len(rows),
            "skipped_stubs": skipped_no_band,
            "skipped_exotic": skipped_exotic,
            "note": "Tiered terrain encounter tables. Consumed by "
                    "determine_encounter; campaign.json overrides take precedence.",
        },
        "bands": [{"name": b["name"], "party_levels": b["party_levels"]} for b in BANDS],
        "terrains": terrains_out,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(_DEFAULT_DB))
    ap.add_argument("--out", default=str(_DEFAULT_OUT))
    # 120 fits every Common + Uncommon for the current (~2.3k-creature) DB, so
    # frequency-first selection trims only the rarest tail — common staples
    # (orcs, goblins) are never evicted. Was 40 for the old ~470-creature DB.
    ap.add_argument("--max-per-table", type=int, default=120)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"error: monsters db not found: {db}", file=sys.stderr)
        return 2

    data = build(db, args.max_per_table)

    # Coverage report.
    print(f"Scanned {data['meta']['monsters_scanned']} monsters "
          f"({data['meta']['skipped_exotic']} very-exotic excluded, "
          f"{data['meta']['skipped_stubs']} stubs without HD or XP skipped).")
    print(f"{'terrain':12s} " + " ".join(f"{b['name']:>5s}" for b in BANDS))
    for terrain, td in data["terrains"].items():
        counts = " ".join(f"{len(t):>5d}" for t in td["tiers"])
        print(f"{terrain:12s} {counts}")

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return 0

    out = Path(args.out)
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"\nWrote {out} ({out.stat().st_size // 1024} KB).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
