"""Homebrew classes loader.

Reads JSON files from `global/homebrew/classes/*.json` and exposes them
shaped to match the rows produced by `2e.db` so they can flow through
class_lookup, the dashboard /classes pages, and any other consumer
without a separate code path.

Each homebrew JSON defines one class. Required top-level fields:
    name              str
    hit_die           str   (e.g. "d10", "d12")
    levels            list  (per-level rows, see below)

Recommended fields:
    description       str
    prime_requisite   list[str]
    special_abilities list[str]
    source            str   (citation, e.g. "Complete Barbarian's Handbook")
    source_url        str

Optional kit metadata (preserved for the detail-page renderer; ignored
by class_lookup):
    ability_requirements   dict   {"str": 12, ...}
    allowed_races          list
    allowed_alignments     list
    allowed_armor          list
    weapon_specialization  bool
    casts_spells           bool
    base_movement_rate     int
    weapon_proficiency_slots    {"initial": int, "advance_every_levels": int}
    nonweapon_proficiency_slots {...}
    progression_tables     dict   (kit-specific tables, e.g. climbing %)

Each level row (in `levels`):
    Required: level, xp_required, hit_dice, attacks, thac0,
              save_paralysis, save_rsw, save_petrify, save_breath, save_spell
    Optional: spell_slots (dict|null), turn_undead (dict|null)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

_HOMEBREW_DIR = Path(__file__).resolve().parent.parent / "global" / "homebrew" / "classes"

_REQUIRED_LEVEL_KEYS = {
    "level", "xp_required", "hit_dice", "thac0",
    "save_paralysis", "save_rsw", "save_petrify",
    "save_breath", "save_spell",
}

# Cache: invalidated by mtime of the directory.
_cache: dict[str, dict] | None = None
_cache_mtime: float = 0.0


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _dir_mtime() -> float:
    """Largest mtime across the homebrew/classes/ tree (dir + each .json)."""
    if not _HOMEBREW_DIR.exists():
        return 0.0
    m = _HOMEBREW_DIR.stat().st_mtime
    for p in _HOMEBREW_DIR.glob("*.json"):
        m = max(m, p.stat().st_mtime)
    return m


def _normalize(raw: dict, src_path: Path) -> dict:
    """Validate and shape a homebrew class dict to mirror a 2e.db classes row
    plus a `level_rows` list mirroring class_levels rows."""
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ValueError(f"{src_path.name}: missing/invalid 'name'")
    raw_levels = raw.get("levels")
    if not isinstance(raw_levels, list) or not raw_levels:
        raise ValueError(f"{src_path.name}: 'levels' must be a non-empty list")

    level_rows: list[dict] = []
    for i, lvl in enumerate(raw_levels):
        missing = _REQUIRED_LEVEL_KEYS - lvl.keys()
        if missing:
            raise ValueError(
                f"{src_path.name}: level row {i} missing keys: {sorted(missing)}"
            )
        # JSON-encode spell_slots / turn_undead so the row is byte-compatible
        # with what class_levels would yield via sqlite (which stores them as
        # JSON strings in TEXT columns).
        spell_slots = lvl.get("spell_slots")
        if spell_slots is not None and not isinstance(spell_slots, str):
            spell_slots = json.dumps(spell_slots)
        turn_undead = lvl.get("turn_undead")
        if turn_undead is not None and not isinstance(turn_undead, str):
            turn_undead = json.dumps(turn_undead)
        level_rows.append({
            "level":          int(lvl["level"]),
            "xp_required":    int(lvl["xp_required"]),
            "hit_dice":       lvl.get("hit_dice"),
            "attacks":        lvl.get("attacks", "1"),
            "thac0":          int(lvl["thac0"]),
            "save_paralysis": int(lvl["save_paralysis"]),
            "save_rsw":       int(lvl["save_rsw"]),
            "save_petrify":   int(lvl["save_petrify"]),
            "save_breath":    int(lvl["save_breath"]),
            "save_spell":     int(lvl["save_spell"]),
            "spell_slots":    spell_slots,
            "turn_undead":    turn_undead,
        })
    level_rows.sort(key=lambda r: r["level"])

    pr = raw.get("prime_requisite")
    pr_json = json.dumps(pr) if isinstance(pr, list) else (pr if isinstance(pr, str) else "[]")
    abil = raw.get("special_abilities")
    abil_json = json.dumps(abil) if isinstance(abil, list) else (abil if isinstance(abil, str) else "[]")

    return {
        "name":              name,
        "slug":              _slugify(name),
        "description":       raw.get("description", ""),
        "hit_die":           raw.get("hit_die", ""),
        "prime_requisite":   pr_json,
        "special_abilities": abil_json,
        "source":            raw.get("source", "homebrew"),
        "source_url":        raw.get("source_url", ""),
        # Optional kit metadata, passed through verbatim
        "ability_requirements":         raw.get("ability_requirements"),
        "allowed_races":                raw.get("allowed_races"),
        "allowed_alignments":           raw.get("allowed_alignments"),
        "allowed_armor":                raw.get("allowed_armor"),
        "weapon_specialization":        raw.get("weapon_specialization"),
        "casts_spells":                 raw.get("casts_spells"),
        "base_movement_rate":           raw.get("base_movement_rate"),
        "weapon_proficiency_slots":     raw.get("weapon_proficiency_slots"),
        "nonweapon_proficiency_slots":  raw.get("nonweapon_proficiency_slots"),
        "progression_tables":           raw.get("progression_tables"),
        "level_rows":                   level_rows,
        "_homebrew":                    True,
        "_source_file":                 str(src_path),
    }


def _load_all() -> dict[str, dict]:
    """Return {slug: class_dict}, refreshing from disk when files change."""
    global _cache, _cache_mtime
    m = _dir_mtime()
    if _cache is not None and m == _cache_mtime:
        return _cache

    out: dict[str, dict] = {}
    if _HOMEBREW_DIR.exists():
        for p in sorted(_HOMEBREW_DIR.glob("*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                norm = _normalize(raw, p)
            except (json.JSONDecodeError, ValueError, OSError) as e:
                # Surface bad files with a placeholder entry so they're visible
                # but don't crash the dashboard / MCP server.
                slug = _slugify(p.stem)
                out[slug] = {
                    "name":  f"{p.stem} (load error)",
                    "slug":  slug,
                    "_error": str(e),
                    "_homebrew": True,
                    "_source_file": str(p),
                    "level_rows": [],
                }
                continue
            out[norm["slug"]] = norm

    _cache = out
    _cache_mtime = m
    return out


def list_homebrew() -> list[dict]:
    """All homebrew classes (load-error entries included), sorted by name."""
    return sorted(_load_all().values(), key=lambda c: c.get("name", "").lower())


def get_homebrew(slug_or_name: str) -> dict | None:
    """Return one homebrew class by slug, name, or case-insensitive name match."""
    if not slug_or_name:
        return None
    classes = _load_all()
    key = slug_or_name.strip()
    slug = _slugify(key)
    if slug in classes:
        return classes[slug]
    low = key.lower()
    for c in classes.values():
        if c.get("name", "").lower() == low:
            return c
    return None


def homebrew_xp_table(slug_or_name: str) -> list[int]:
    """[xp_required at L1, L2, …] for a homebrew class, or [] if unknown."""
    c = get_homebrew(slug_or_name)
    if not c:
        return []
    return [int(r["xp_required"]) for r in c.get("level_rows", [])]
