"""Monster lookup, spell lookup, class lookup, and treasure generation tools."""
import io
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
import _campaign as _c
from tools import homebrew_classes as _hb

BASE_DIR = Path(__file__).parent.parent

_MONSTERS_DB_CANDIDATES = [
    BASE_DIR / "global" / "monsters.db",
    BASE_DIR.parent / "ttrpg" / "global" / "monsters.db",
]

_2E_DB_CANDIDATES = [
    BASE_DIR / "global" / "2e.db",
]


def _find_db(candidates) -> Path | None:
    for p in candidates:
        if p.exists():
            return p
    return None


def _has(row, col) -> bool:
    """True if a sqlite3.Row carries `col` (tolerates pre-migration DBs)."""
    return col in row.keys()


def _generate_treasure_text(types_list: list[str]) -> str:
    """Run treasure.generate_treasure for each type, capture printed output as a
    single string. Replaces the previous subprocess+regex approach."""
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    import treasure  # local import; first call pays parse cost, subsequent are free
    expanded = treasure.expand_types(types_list)
    buf = io.StringIO()
    with redirect_stdout(buf):
        for t in expanded:
            treasure.generate_treasure(t, verbose=True)
    return buf.getvalue()


def register(mcp):

    @mcp.tool()
    def monster_lookup(name: str) -> dict:
        """Look up a monster by name in the AD&D 2e monster database.
        Returns stats including HD, THAC0, AC, damage, treasure type, morale, and XP value.
        Supports partial/case-insensitive name matching.

        An exact (case-insensitive) name match always wins and returns that single
        monster — so monster_lookup('skeleton') returns the HD 1 standard skeleton,
        not the 'skeleton, warrior' variant. When there is no exact match but several
        partial matches, returns {"matches": [...], "count": N} so the caller can
        disambiguate variants (e.g. the many 'skeleton, *' entries)."""
        db_path = _find_db(_MONSTERS_DB_CANDIDATES)
        if db_path is None:
            return {"error": "monsters.db not found."}

        def _row_to_monster(row) -> dict:
            return {
                "name":              row["name"],
                "climate_terrain":   row["climate_terrain"],
                "frequency":         row["frequency"],
                "no_appearing":      row["no_appearing"],
                "ac":                row["armor_class"],
                "move":              row["move"],
                "hit_dice":          row["hit_dice"],
                "thac0":             row["thac0"],
                "pct_in_lair":       row["pct_in_lair"],
                "treasure_type":     row["treasure_type"],
                "no_of_attacks":     row["no_of_attacks"],
                "damage":            row["damage_attack"],
                "special_attacks":   row["special_attacks"],
                "special_defenses":  row["special_defenses"],
                "magic_resistance":  row["magic_resistance"],
                "intelligence":      row["intelligence"],
                "alignment":         row["alignment"],
                "size":              row["size"],
                "psionic_ability":   row["psionic_ability"],
                "attack_defense_modes": row["attack_defense_modes"],
                "morale":            row["morale"],
                "xp_value":          row["xp_value"],
                "description":       row["description"],
                "categories":        json.loads(row["categories"]) if _has(row, "categories") and row["categories"] else [],
                "source":            (row["source"] if _has(row, "source") else None),
            }

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Exact (case-insensitive) match wins outright.
            cur = conn.execute(
                "SELECT * FROM monsters WHERE name = ? COLLATE NOCASE LIMIT 1",
                (name,),
            )
            row = cur.fetchone()
            if row is not None:
                return _row_to_monster(row)

            # Otherwise gather every partial match.
            cur = conn.execute(
                "SELECT * FROM monsters WHERE name LIKE ? COLLATE NOCASE ORDER BY name",
                (f"%{name}%",),
            )
            rows = cur.fetchall()
            if not rows:
                return {"error": f"No monster matching '{name}' found."}
            if len(rows) == 1:
                return _row_to_monster(rows[0])
            return {
                "matches": [_row_to_monster(r) for r in rows],
                "count":   len(rows),
            }
        finally:
            conn.close()

    @mcp.tool()
    def spell_lookup(name: str, caster: str = "") -> dict:
        """Look up a spell by name in the AD&D 2e spell database.
        Returns level, school, caster type, casting time (and its initiative value),
        range, area of effect, duration, saving throw, components, and description.
        caster: optional filter — 'wizard', 'priest', or blank for either.
        Supports partial/case-insensitive name matching.
        casting_time_init is the integer used for initiative (1–10; 10 = full round or longer)."""
        db_path = _find_db(_2E_DB_CANDIDATES)
        if db_path is None:
            return {"error": "2e.db not found. Run tools/build_2e_db.py first."}

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            if caster:
                cur = conn.execute(
                    "SELECT * FROM spells WHERE name LIKE ? COLLATE NOCASE AND caster LIKE ? COLLATE NOCASE LIMIT 1",
                    (f"%{name}%", f"%{caster}%"),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM spells WHERE name LIKE ? COLLATE NOCASE LIMIT 1",
                    (f"%{name}%",),
                )
            row = cur.fetchone()
            if row is None:
                return {"error": f"No spell matching '{name}'" + (f" for caster '{caster}'" if caster else "") + " found."}

            return {
                "name":             row["name"],
                "level":            row["level"],
                "caster":           row["caster"],
                "school":           row["school"],
                "casting_time":     row["casting_time"],
                "casting_time_init": row["casting_time_init"],
                "range":            row["range"],
                "aoe":              row["aoe"],
                "duration":         row["duration"],
                "save":             row["save"],
                "damage":           row["damage"] or None,
                "components": {
                    "verbal":   bool(row["verbal"]),
                    "somatic":  bool(row["somatic"]),
                    "material": bool(row["material"]),
                    "details":  row["materials"],
                },
                "reversible":       bool(row["reversible"]),
                "source":           row["source"],
                "page":             row["page"],
                "description":      row["description"],
            }
        finally:
            conn.close()

    @mcp.tool()
    def class_lookup(class_name: str, level: int = 0) -> dict:
        """Look up AD&D 2e class progression data. Searches PHB classes
        (global/2e.db) first, then homebrew classes (global/homebrew/classes/*.json).

        If level > 0: returns that level's stats (THAC0, saves, spell slots, XP required,
        XP to next level). Also returns special abilities for the class.
        If level == 0: returns class overview (hit die, prime requisite, XP table summary,
        and all special abilities).
        class_name: Fighter, Mage, Cleric, Thief, Ranger, Paladin, Druid, Bard, Barbarian, ..."""
        # Try PHB DB first.
        db_path = _find_db(_2E_DB_CANDIDATES)
        cls_row = None
        level_rows: list = []
        source = "phb"
        homebrew_extras: dict = {}

        if db_path is not None:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                cls_row = conn.execute(
                    "SELECT * FROM classes WHERE name LIKE ? COLLATE NOCASE LIMIT 1",
                    (f"%{class_name}%",),
                ).fetchone()
                if cls_row is not None:
                    level_rows = list(conn.execute(
                        "SELECT * FROM class_levels WHERE class_id=? ORDER BY level",
                        (cls_row["id"],),
                    ).fetchall())
            finally:
                conn.close()

        # Fall back to homebrew if PHB miss.
        if cls_row is None:
            hb = _hb.get_homebrew(class_name)
            if hb is None:
                return {"error": f"No class matching '{class_name}' found in PHB or homebrew."}
            cls_row = hb
            level_rows = hb.get("level_rows", [])
            source = "homebrew"
            homebrew_extras = {
                k: hb[k] for k in (
                    "ability_requirements", "allowed_races", "allowed_alignments",
                    "allowed_armor", "weapon_specialization", "casts_spells",
                    "base_movement_rate", "weapon_proficiency_slots",
                    "nonweapon_proficiency_slots", "progression_tables",
                    "source", "source_url",
                ) if hb.get(k) is not None
            }

        # Decode common fields (works for both sqlite Row and homebrew dict)
        def _g(row, key):
            try:
                return row[key]
            except (KeyError, IndexError):
                return None

        special_abilities = json.loads(_g(cls_row, "special_abilities") or "[]")
        prime_req = _g(cls_row, "prime_requisite")
        try:
            prime_req = json.loads(prime_req) if prime_req else []
        except (json.JSONDecodeError, TypeError):
            pass

        if level > 0:
            lvl_row = next((r for r in level_rows if int(_g(r, "level") or 0) == level), None)
            if lvl_row is None:
                return {"error": f"No data for {_g(cls_row, 'name')} level {level}."}
            next_row = next((r for r in level_rows if int(_g(r, "level") or 0) == level + 1), None)

            spell_slots = json.loads(_g(lvl_row, "spell_slots")) if _g(lvl_row, "spell_slots") else None
            turn_undead = json.loads(_g(lvl_row, "turn_undead")) if _g(lvl_row, "turn_undead") else None

            result = {
                "class":         _g(cls_row, "name"),
                "level":         level,
                "xp_required":   _g(lvl_row, "xp_required"),
                "xp_next_level": _g(next_row, "xp_required") if next_row else "max level",
                "hit_dice":      _g(lvl_row, "hit_dice"),
                "attacks":       _g(lvl_row, "attacks"),
                "thac0":         _g(lvl_row, "thac0"),
                "saving_throws": {
                    "paralysis_poison_death": _g(lvl_row, "save_paralysis"),
                    "rod_staff_wand":         _g(lvl_row, "save_rsw"),
                    "petrify_polymorph":      _g(lvl_row, "save_petrify"),
                    "breath_weapon":          _g(lvl_row, "save_breath"),
                    "spell":                  _g(lvl_row, "save_spell"),
                },
                "spell_slots":       spell_slots,
                "turn_undead":       turn_undead,
                "special_abilities": special_abilities,
                "source":            source,
            }
            if homebrew_extras:
                result["homebrew"] = homebrew_extras
            return result

        # Overview
        xp_table = [
            {"level": _g(r, "level"), "xp": _g(r, "xp_required"),
             "hd": _g(r, "hit_dice"), "thac0": _g(r, "thac0")}
            for r in level_rows
        ]
        result = {
            "class":             _g(cls_row, "name"),
            "hit_die":           _g(cls_row, "hit_die"),
            "prime_requisite":   prime_req,
            "special_abilities": special_abilities,
            "xp_table":          xp_table,
            "source":            source,
        }
        if homebrew_extras:
            result["homebrew"] = homebrew_extras
        return result

    @mcp.tool()
    def generate_treasure(treasure_types: str) -> dict:
        """Generate treasure for one or more AD&D 2e treasure types.
        treasure_types is a space-separated string such as 'A', 'A B', or 'Qx5 S'.
        Uses DMG Tables 84-110: coins, gems, art objects, and magic items."""
        types_list = treasure_types.split()
        if not types_list:
            return {"error": "No treasure types provided."}

        try:
            text = _generate_treasure_text(types_list)
        except Exception as e:
            return {"error": f"treasure generation failed: {e}"}

        return {
            "types":  treasure_types,
            "result": text,
            "error":  None,
        }

    @mcp.tool()
    def award_treasure(treasure_types: str, log_note: str = "") -> dict:
        """Generate treasure AND auto-route the result into the campaign state.
        Coins (cp/sp/ep/gp) are added to state.coin directly. Gems, art objects,
        and magic items are staged in state.loot_pile for the DM to distribute
        to PC inventories via add_inventory.

        treasure_types: space-separated AD&D treasure types ('A B' or 'Qx5 S')
        log_note:       free-form note appended to events.json

        Returns the parsed parcel + the new coin totals + the staged loot list."""
        types_list = treasure_types.split()
        if not types_list:
            return {"error": "No treasure types provided."}

        try:
            text = _generate_treasure_text(types_list)
        except Exception as e:
            return {"error": f"treasure generation failed: {e}"}

        # ── Parse ────────────────────────────────────────────────────────
        import re as _re
        coin_added = {"cp": 0, "sp": 0, "ep": 0, "gp": 0, "pp": 0}
        gems       = []
        art        = []
        magic      = []

        # Lines of form: '  Gold (gp): 1,736 (25% ✓)' — only parse when value is numeric
        for m in _re.finditer(
            r'\b(Platinum|Copper|Silver|Electrum|Gold)\s*\((pp|cp|sp|ep|gp)\):\s+([\d,]+)\b',
            text,
        ):
            denom = m.group(2)
            val = int(m.group(3).replace(",", ""))
            coin_added[denom] = coin_added.get(denom, 0) + val

        # Gems: numbered list under "Gems (...)"
        in_gems = False
        in_art  = False
        in_magic = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("Gems"):
                in_gems, in_art, in_magic = True, False, False
                continue
            if stripped.startswith("Art Objects"):
                in_gems, in_art, in_magic = False, True, False
                continue
            if stripped.startswith("Magic Items"):
                in_gems, in_art, in_magic = False, False, True
                continue
            if stripped.startswith("==="):
                in_gems = in_art = in_magic = False
                continue

            entry_m = _re.match(r'^\d+\.\s+(.*?)(\s+—\s+([\d,]+)\s*gp)?(\s*\[(.*?)\])?\s*(\(\+?\d+%\))?\s*$', stripped)
            if entry_m and (in_gems or in_art or in_magic):
                desc = entry_m.group(1).strip()
                gp_val = int(entry_m.group(3).replace(",", "")) if entry_m.group(3) else 0
                tag = (entry_m.group(5) or "").strip()
                rec = {"description": desc, "value_gp": gp_val}
                if tag:
                    rec["category"] = tag
                if in_gems:    gems.append(rec)
                elif in_art:   art.append(rec)
                elif in_magic: magic.append(rec)

        # ── Apply to state ───────────────────────────────────────────────
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        coin = state.setdefault("coin", {"pp": 0, "gp": 0, "ep": 0, "sp": 0, "cp": 0})
        for denom, val in coin_added.items():
            coin[denom] = coin.get(denom, 0) + val

        loot_pile = state.setdefault("loot_pile", [])
        for g in gems:    loot_pile.append({"kind": "gem",   **g})
        for a in art:     loot_pile.append({"kind": "art",   **a})
        for mi in magic:  loot_pile.append({"kind": "magic", **mi})

        _c.save_state(cfg, state)
        _c.append_event(cfg, {
            "type":  "treasure_awarded",
            "notes": log_note or f"Types: {treasure_types}",
            "coin":  dict(coin_added),
            "gems":  len(gems),
            "art":   len(art),
            "magic": len(magic),
        })

        return {
            "types":          treasure_types,
            "coin_added":     coin_added,
            "coin_total":     dict(coin),
            "gems":           gems,
            "art":            art,
            "magic_items":    magic,
            "loot_pile_size": len(loot_pile),
            "raw_output":     text,
        }

    @mcp.tool()
    def loot_pile() -> dict:
        """Return the staged loot pile (gems, art, magic items not yet
        distributed). Use to remind the party of pending decisions."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        return {"loot_pile": list(state.get("loot_pile", []))}

    @mcp.tool()
    def claim_loot(index: int, character: str) -> dict:
        """Remove an item from the loot pile by index and assign it to a PC's
        markdown inventory (appends a line to the character file).
        For consumable magic items (potions, scrolls), prefer add_inventory."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        pile = state.setdefault("loot_pile", [])
        if not (0 <= index < len(pile)):
            return {"error": f"Index {index} out of range (pile has {len(pile)} items)."}

        item = pile.pop(index)
        _c.save_state(cfg, state)

        # Append to character markdown
        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found.", "item": item}
        char.setdefault("inventory", []).append(
            f"{item.get('description', '?')} ({item.get('kind', '?')}, {item.get('value_gp', 0)} gp)"
        )
        _c.save_campaign(cfg)
        _c.write_character_sheet(cfg, key, char)
        return {"claimed_by": char.get("label", key), "item": item}

    @mcp.tool()
    def item_lookup(name: str, item_type: str = "", max_rarity: int = 100) -> list:
        """Look up items in the AD&D 2e item database.
        Returns up to 20 matching items with stats, cost, weight, and description.
        item_type: optional filter — armor, weapon_melee, weapon_ranged, weapon_ammo,
          misc_equipment, provisions, clothing, scroll, potion, ring, rod, staff, wand,
          magic_item_armor_special, magic_item_weapon_special, misc_magic_* categories.
        max_rarity: 0=common only, 100=all items including artifacts.
        rarity scale: 0=common, 10=uncommon, 30=rare, 50=very rare, 65=legendary, 100=artifact.
        Supports partial/case-insensitive name matching."""
        db_path = _find_db(_2E_DB_CANDIDATES)
        if db_path is None:
            return [{"error": "2e.db not found."}]

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            where, params = ["name LIKE ? COLLATE NOCASE", "rarity <= ?"], [f"%{name}%", max_rarity]
            if item_type:
                where.append("item_type LIKE ? COLLATE NOCASE")
                params.append(f"%{item_type}%")
            rows = conn.execute(
                f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY rarity, name LIMIT 20",
                params,
            ).fetchall()
            if not rows:
                return [{"error": f"No items matching '{name}'" + (f" of type '{item_type}'" if item_type else "") + " found."}]

            results = []
            for r in rows:
                item = {
                    "id":           r["id"],
                    "name":         r["name"],
                    "item_type":    r["item_type"],
                    "cost":         r["cost"],
                    "weight":       r["weight"],
                    "rarity":       r["rarity"],
                    "description":  r["description"],
                    "source":       r["source"],
                }
                if r["ac"] is not None:
                    item["ac"] = r["ac"]
                if r["speed"] is not None:
                    item.update({"size": r["size"], "weapon_type": r["weapon_type"],
                                 "speed": r["speed"], "damage_sm": r["damage_sm"],
                                 "damage_l": r["damage_l"]})
                    if r["rof"]:
                        item.update({"rof": r["rof"], "range_s": r["range_s"],
                                     "range_m": r["range_m"], "range_l": r["range_l"]})
                if r["xp_value"] is not None:
                    item["xp_value"] = r["xp_value"]
                results.append(item)
            return results
        finally:
            conn.close()

    @mcp.tool()
    def item_update(item_id: int, rarity: int = None, cost: str = None, description: str = None) -> dict:
        """Update an item's rarity, cost, or description in the database.
        rarity: 0=common, 10=uncommon, 30=rare, 50=very rare, 65=legendary, 100=artifact.
        Note: rebuilding 2e.db with build_2e_db.py resets rarity to defaults.
        At least one field must be provided."""
        db_path = _find_db(_2E_DB_CANDIDATES)
        if db_path is None:
            return {"error": "2e.db not found."}
        if rarity is None and cost is None and description is None:
            return {"error": "No fields to update provided."}

        conn = sqlite3.connect(str(db_path))
        try:
            sets, params = [], []
            if rarity is not None:
                sets.append("rarity = ?")
                params.append(max(0, min(100, rarity)))
            if cost is not None:
                sets.append("cost = ?")
                params.append(cost)
            if description is not None:
                sets.append("description = ?")
                params.append(description)
            params.append(item_id)
            with conn:
                cur = conn.execute(
                    f"UPDATE items SET {', '.join(sets)} WHERE id = ?", params
                )
            if cur.rowcount == 0:
                return {"error": f"No item with id {item_id} found."}
            return {"updated": item_id, "fields": {k.split(" =")[0]: v for k, v in zip(sets, params[:-1])}}
        finally:
            conn.close()

    _ABILITY_NAMES = {"strength", "dexterity", "constitution",
                      "intelligence", "wisdom", "charisma"}

    def _ability_canonical(s: str) -> str | None:
        s = (s or "").strip().lower()
        if s in _ABILITY_NAMES:
            return s
        # Accept common short forms: str, dex, con, int, wis, cha
        short = {"str": "strength", "dex": "dexterity", "con": "constitution",
                 "int": "intelligence", "wis": "wisdom", "cha": "charisma"}
        return short.get(s)

    @mcp.tool()
    def ability_lookup(ability: str, score: str = "") -> dict:
        """Look up AD&D 2e derived attributes for an ability score.

        ability: 'strength'|'dexterity'|'constitution'|'intelligence'|'wisdom'|
                 'charisma' (or 3-letter shortcut: str, dex, con, int, wis, cha).
        score:   '15', '18/76-90', '4-5', etc. Optional. If omitted, returns
                 the column definitions plus every row for that ability.

        Returns derived values such as hit probability, bend bars/lift gates %,
        system shock %, bonus spells, henchmen cap, etc., along with brief
        column descriptions."""
        canon = _ability_canonical(ability)
        if canon is None:
            return {"error": f"Unknown ability '{ability}'. Use one of: "
                              f"{', '.join(sorted(_ABILITY_NAMES))}."}
        db_path = _find_db(_2E_DB_CANDIDATES)
        if db_path is None:
            return {"error": "2e.db not found."}

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            note = conn.execute(
                "SELECT headline, xp_bonus, extra FROM ability_notes "
                "WHERE ability=?", (canon,),
            ).fetchone()
            if note is None:
                return {"error": f"No ability data for '{canon}'. Rebuild with "
                                  f"tools/build_phb_ref.py."}
            cols = list(conn.execute(
                "SELECT name, short_name, note FROM ability_columns "
                "WHERE ability=? ORDER BY sort_order", (canon,),
            ).fetchall())
            score_rows = list(conn.execute(
                "SELECT score, data FROM ability_scores "
                "WHERE ability=? ORDER BY sort_order", (canon,),
            ).fetchall())
        finally:
            conn.close()

        columns = [{
            "name":       r["name"],
            "short_name": r["short_name"],
            "note":       r["note"],
        } for r in cols]

        base = {
            "ability":  canon,
            "headline": note["headline"],
            "xp_bonus": note["xp_bonus"],
            "extra":    note["extra"],
            "columns":  columns,
        }

        if not score:
            base["rows"] = [
                {"score": r["score"], **json.loads(r["data"])}
                for r in score_rows
            ]
            return base

        # Match the requested score against the stored row labels. Accept
        # exact matches, dash-range membership (e.g. '12' → '12-13'), and
        # 18/xx percentile bands.
        score = score.strip()
        match = None
        for r in score_rows:
            label = r["score"]
            if label == score:
                match = r
                break
        if match is None and score.isdigit():
            n = int(score)
            for r in score_rows:
                label = r["score"]
                if "-" in label and "/" not in label:
                    try:
                        lo, hi = label.split("-")
                        if int(lo) <= n <= int(hi):
                            match = r
                            break
                    except ValueError:
                        continue
        if match is None and score.startswith("18/") and score[3:].isdigit():
            pct = int(score[3:])
            if pct == 0 or pct == 100:
                match = next((r for r in score_rows if r["score"] == "18/00"), None)
            else:
                for r in score_rows:
                    lbl = r["score"]
                    if not lbl.startswith("18/") or lbl == "18/00":
                        continue
                    rest = lbl[3:]
                    if "-" in rest:
                        lo, hi = rest.split("-")
                        if int(lo) <= pct <= int(hi):
                            match = r
                            break

        if match is None:
            return {**base, "error": f"No row for score '{score}'.",
                    "available": [r["score"] for r in score_rows]}

        return {**base,
                "score": match["score"],
                "values": json.loads(match["data"])}

    _PROF_GROUPS = {"general", "priest", "rogue", "warrior", "wizard"}

    @mcp.tool()
    def proficiency_lookup(name: str = "", group: str = "",
                           ability: str = "") -> dict:
        """Look up AD&D 2e nonweapon proficiencies (PHB Table 37).

        name:    proficiency name (partial, case-insensitive). Returns matching
                 entries across all groups.
        group:   'general'|'priest'|'rogue'|'warrior'|'wizard'. Filter results.
        ability: 'Strength'|'Dexterity'|… Filter by relevant ability.

        At least one of name/group/ability must be supplied. With no filters
        the result would be the entire table; use the dashboard /proficiencies
        page for that.

        Each entry: name, group, slots required, relevant ability, check
        modifier. A proficiency check is d20 ≤ (ability score + check
        modifier)."""
        if not (name or group or ability):
            return {"error": "Provide at least one of name, group, or ability."}
        db_path = _find_db(_2E_DB_CANDIDATES)
        if db_path is None:
            return {"error": "2e.db not found."}

        group_norm = group.strip().lower() if group else ""
        if group_norm and group_norm not in _PROF_GROUPS:
            return {"error": f"Unknown group '{group}'. Use one of: "
                              f"{', '.join(sorted(_PROF_GROUPS))}."}

        where, params = [], []
        if name:
            where.append("name LIKE ? COLLATE NOCASE")
            params.append(f"%{name}%")
        if group_norm:
            where.append("group_name = ?")
            params.append(group_norm)
        if ability:
            where.append("ability LIKE ? COLLATE NOCASE")
            params.append(ability)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = list(conn.execute(
                f"SELECT name, group_name, slots, ability, check_modifier "
                f"FROM proficiencies WHERE {' AND '.join(where)} "
                f"ORDER BY name COLLATE NOCASE, group_name",
                params,
            ).fetchall())
        finally:
            conn.close()

        if not rows:
            return {"error": "No matching proficiencies.",
                    "filters": {"name": name or None, "group": group or None,
                                "ability": ability or None}}

        return {
            "count": len(rows),
            "results": [{
                "name":           r["name"],
                "group":          r["group_name"],
                "slots":          r["slots"],
                "ability":        r["ability"],
                "check_modifier": r["check_modifier"],
            } for r in rows],
        }

    _TURNING_LEVEL_COLUMNS = [
        "1", "2", "3", "4", "5", "6", "7", "8", "9",
        "10-11", "12-13", "14+",
    ]

    def _turning_level_column_index(eff_level: int) -> int | None:
        if eff_level < 1:
            return None
        if 1 <= eff_level <= 9:
            return eff_level - 1
        if 10 <= eff_level <= 11:
            return 9
        if 12 <= eff_level <= 13:
            return 10
        if eff_level >= 14:
            return 11
        return None

    def _turning_decode(cell: str) -> dict:
        if cell == "—":
            return {
                "needs_roll":        False,
                "destroys":          False,
                "extra_turned_2d4":  False,
                "affects_2d6":       False,
                "meaning":           "A priest of this level cannot turn this type.",
            }
        if cell == "T":
            return {
                "needs_roll":        False,
                "destroys":          False,
                "extra_turned_2d4":  False,
                "affects_2d6":       True,
                "meaning":           "Automatically turned — no d20 roll required. "
                                     "Affects 2d6 creatures.",
            }
        if cell == "D":
            return {
                "needs_roll":        False,
                "destroys":          True,
                "extra_turned_2d4":  False,
                "affects_2d6":       True,
                "meaning":           "Automatically destroyed (dispel). Affects 2d6 creatures.",
            }
        if cell == "D*":
            return {
                "needs_roll":        False,
                "destroys":          True,
                "extra_turned_2d4":  True,
                "affects_2d6":       True,
                "meaning":           "Automatically destroyed (dispel). Affects 2d6 creatures, "
                                     "plus an additional 2d4 of that type are turned.",
            }
        # Numeric target
        return {
            "needs_roll":        True,
            "target":            int(cell),
            "destroys":          False,
            "extra_turned_2d4":  False,
            "affects_2d6":       True,
            "meaning":           f"Roll 1d20; on a result of {cell} or higher, "
                                 f"2d6 of these undead are turned and flee.",
        }

    @mcp.tool()
    def turning_undead(level: int, undead: str = "", hd: int = 0,
                       role: str = "priest") -> dict:
        """Look up DMG Table 47 — turning undead.

        level: caster's actual class level (priest or paladin).
        undead: undead type name (e.g. 'ghoul', 'vampire', 'spectre', 'lich').
                Case-insensitive; matches aliases stored on each row.
        hd:     hit dice of the undead — used if undead is not supplied.
                Recognised buckets: 1, 2, 3-4, 5, 6, 7, 8, 9, 10, 11+.
        role:   'priest' (default) or 'paladin'. Paladins read the column
                two lower than their actual level (a 5th-level paladin uses
                column 3; 1st- and 2nd-level paladins cannot turn).

        Druids cannot turn undead — not modelled here.

        Returns the d20 target and a decoded meaning of the cell value
        (number = roll-to-turn; T = auto-turn; D = destroy; D* = destroy
        plus 2d4 extra; — = cannot turn at this level)."""
        role_norm = (role or "priest").strip().lower()
        if role_norm not in ("priest", "paladin"):
            return {"error": f"Unknown role '{role}'. Use 'priest' or 'paladin'."}
        if not undead and hd <= 0:
            return {"error": "Provide either undead (name) or hd (hit dice)."}

        eff_level = level - 2 if role_norm == "paladin" else level
        col_idx = _turning_level_column_index(eff_level)
        if col_idx is None:
            return {
                "error": f"{role_norm.capitalize()} of level {level} cannot "
                         f"turn undead (effective turning level {eff_level}).",
                "role": role_norm,
                "caster_level": level,
                "effective_level": eff_level,
            }

        db_path = _find_db(_2E_DB_CANDIDATES)
        if db_path is None:
            return {"error": "2e.db not found."}

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = None
            if undead:
                needle = undead.strip().lower()
                candidates = list(conn.execute(
                    "SELECT undead_type, min_hd, max_hd, results, aliases "
                    "FROM turning_undead ORDER BY sort_order"
                ).fetchall())
                # Alias match first (cleanest).
                for c in candidates:
                    if needle in json.loads(c["aliases"]):
                        row = c
                        break
                # Fallback: substring match on undead_type.
                if row is None:
                    for c in candidates:
                        if needle in c["undead_type"].lower():
                            row = c
                            break
                if row is None:
                    return {
                        "error": f"No turning entry matching '{undead}'.",
                        "available": [c["undead_type"] for c in candidates],
                    }
            else:
                row = conn.execute(
                    "SELECT undead_type, min_hd, max_hd, results, aliases "
                    "FROM turning_undead "
                    "WHERE min_hd IS NOT NULL AND min_hd <= ? AND max_hd >= ? "
                    "ORDER BY sort_order LIMIT 1",
                    (hd, hd),
                ).fetchone()
                if row is None:
                    return {
                        "error": f"No HD-based turning entry for {hd} HD. "
                                  f"(Zombies and Ghasts have no HD bucket — "
                                  f"look them up by name.)",
                    }
        finally:
            conn.close()

        cells = json.loads(row["results"])
        cell = cells[col_idx]
        decoded = _turning_decode(cell)
        return {
            "undead":          row["undead_type"],
            "role":            role_norm,
            "caster_level":    level,
            "effective_level": eff_level,
            "level_column":    _TURNING_LEVEL_COLUMNS[col_idx],
            "result":          cell,
            **decoded,
        }

    @mcp.tool()
    def proficiency_groups(class_name: str = "") -> dict:
        """Return PHB Table 38 — which proficiency groups each class draws from.

        class_name: optional. If supplied, returns just that class's groups.
        Otherwise returns the whole crossover table. A class can spend
        nonweapon proficiency slots on any proficiency in its listed groups.
        Every class can additionally pick from General."""
        db_path = _find_db(_2E_DB_CANDIDATES)
        if db_path is None:
            return {"error": "2e.db not found."}
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            if class_name:
                row = conn.execute(
                    "SELECT class_name, groups FROM proficiency_class_crossover "
                    "WHERE class_name LIKE ? COLLATE NOCASE LIMIT 1",
                    (f"%{class_name}%",),
                ).fetchone()
                if row is None:
                    return {"error": f"No class matching '{class_name}'."}
                return {"class": row["class_name"],
                        "groups": json.loads(row["groups"])}
            rows = list(conn.execute(
                "SELECT class_name, groups FROM proficiency_class_crossover "
                "ORDER BY class_name"
            ).fetchall())
            return {"crossover": [
                {"class": r["class_name"], "groups": json.loads(r["groups"])}
                for r in rows
            ]}
        finally:
            conn.close()
