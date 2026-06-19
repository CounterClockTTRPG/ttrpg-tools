"""Party and character management tools: HP, XP, spells, rest, coin."""
import re
import _campaign as _c


def _normalize_memorized(spells: list) -> list:
    """Ensure memorized_spells is a list of {name, level, cast} dicts.
    Accepts flat strings (legacy) or existing dicts."""
    result = []
    for s in spells:
        if isinstance(s, dict):
            result.append({
                "name":  s.get("name", ""),
                "level": int(s.get("level", 0)),
                "cast":  bool(s.get("cast", False)),
            })
        else:
            result.append({"name": str(s), "level": 0, "cast": False})
    return result


def _validate_memorized(slot_cfg: dict, normalized: list) -> str | None:
    """Check a normalized spell list against per-level slot capacity.
    Returns an error string, or None if the list fits."""
    total_capacity = sum(slot_cfg.values()) if slot_cfg else 0
    if len(normalized) > total_capacity:
        return f"Too many spells: {len(normalized)} given, {total_capacity} total slots."

    level_counts: dict[str, int] = {}
    for s in normalized:
        if s["level"]:
            k = str(s["level"])
            level_counts[k] = level_counts.get(k, 0) + 1
    for lvl_key, count in level_counts.items():
        cap = slot_cfg.get(lvl_key, 0)
        if count > cap:
            return f"Too many level-{lvl_key} spells: {count} given, {cap} slots available."
    return None


# Rough XP thresholds per class keyword → list of (threshold, level)
_XP_THRESHOLDS = {
    "fighter":  [(2000, 2), (4000, 3), (8000, 4)],
    "paladin":  [(2750, 2), (5500, 3), (12000, 4)],
    "ranger":   [(2250, 2), (4500, 3), (10000, 4)],
    "thief":    [(1250, 2), (2500, 3), (5000, 4)],
    "assassin": [(1500, 2), (3000, 3), (6000, 4)],
    "mage":     [(2500, 2), (5000, 3), (10000, 4)],
    "wizard":   [(2500, 2), (5000, 3), (10000, 4)],
    "illusionist": [(2250, 2), (4500, 3), (9000, 4)],
    "cleric":   [(1500, 2), (3000, 3), (6000, 4)],
    "druid":    [(2000, 2), (4000, 3), (7500, 4)],
    "bard":     [(1333, 2), (2666, 3), (5332, 4)],
}


def _level_up_hint(char: dict, xp_before: int, xp_after: int) -> str:
    cls_str = char.get("cls", "").lower()
    for kw, thresholds in _XP_THRESHOLDS.items():
        if kw in cls_str:
            for threshold, level in thresholds:
                if xp_before < threshold <= xp_after:
                    return f"Level up! {char.get('label', '?')} has reached {threshold} XP — eligible for level {level} ({kw.capitalize()})."
    return ""


def register(mcp):

    @mcp.tool()
    def party_status() -> dict:
        """Return HP, conditions, spell slots, coin, and current day for all characters."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        result = {
            "campaign": cfg.get("name", ""),
            "current_day": state.get("current_day", 1),
            "current_session": state.get("current_session", 0),
            "coin": state.get("coin", {"pp": 0, "gp": 0, "ep": 0, "sp": 0, "cp": 0}),
            "characters": {},
        }

        for key, char in cfg.get("characters", {}).items():
            cstate = state.get("characters", {}).get(key, {})
            hp_max = char.get("hp_max", 0)
            hp_cur = cstate.get("hp", hp_max)

            # Build spell slot summary
            slot_cfg = char.get("spell_slots") or {}
            slot_state = cstate.get("spell_slots") or {}
            slots_summary = {}
            for lvl, max_count in slot_cfg.items():
                used = max_count - slot_state.get(lvl, max_count)
                slots_summary[f"L{lvl}"] = f"{slot_state.get(lvl, max_count)}/{max_count} remaining"

            result["characters"][key] = {
                "label": char.get("label", key),
                "class": char.get("cls", ""),
                "hp": f"{hp_cur}/{hp_max}",
                "conditions": cstate.get("conditions", []),
                "xp": cstate.get("xp", 0),
                "spell_slots": slots_summary,
            }

        return result

    @mcp.tool()
    def character_status(character: str) -> dict:
        """Full status for one character: HP, XP, conditions, spell slots, memorized spells, saves, skills."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        cstate = state.get("characters", {}).get(key, {})
        hp_max = char.get("hp_max", 0)
        hp_cur = cstate.get("hp", hp_max)

        slot_cfg = char.get("spell_slots") or {}
        slot_state = cstate.get("spell_slots") or {}
        slots_detail = {}
        for lvl, max_count in slot_cfg.items():
            remaining = slot_state.get(lvl, max_count)
            slots_detail[f"level_{lvl}"] = {"remaining": remaining, "max": max_count}

        save_names = ["Paralysis/Poison/Death", "Rod/Staff/Wand", "Polymorph/Petrify", "Breath Weapon", "Spell/Magic"]
        saves_norm = _c.normalize_saves(char.get("saves"))
        by_type = {s["type"]: s["value"] for s in saves_norm}
        defaults = [16, 18, 17, 20, 19]
        saves_named = {save_names[i]: by_type.get(_c.SAVE_TYPES[i], defaults[i]) for i in range(5)}

        return {
            "key": key,
            "label": char.get("label", key),
            "class": char.get("cls", ""),
            "race": char.get("race", ""),
            "hp": {"current": hp_cur, "max": hp_max},
            "ac": char.get("ac", 10),
            "thac0": char.get("thac0", 20),
            "weapon": char.get("weapon", "1d6"),
            "weapon_speed": char.get("weapon_speed", 5),
            "bonus_hit": char.get("bonus_hit", 0),
            "bonus_dmg": char.get("bonus_dmg", 0),
            "xp": cstate.get("xp", 0),
            "conditions": cstate.get("conditions", []),
            "spell_slots": slots_detail,
            "memorized_spells": cstate.get("memorized_spells", []),
            "default_spells": char.get("default_spells", []),
            "saves": saves_named,
            "skills": char.get("skills", {}),
        }

    @mcp.tool()
    def apply_damage(character: str, amount: int) -> dict:
        """Reduce a character's HP outside combat.
        0 = unconscious (stable). -1 to -9 = bleeding (1 HP/round). -10 = dead."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        chars_state = state.setdefault("characters", {})
        cstate = chars_state.setdefault(key, {})
        hp_max    = char.get("hp_max", 0)
        hp_before = cstate.get("hp", hp_max)
        hp_after  = max(hp_before - amount, -10)
        cstate["hp"] = hp_after

        downed = hp_after <= 0
        dead   = hp_after <= -10
        if downed and "downed" not in cstate.get("conditions", []):
            cstate.setdefault("conditions", []).append("downed")

        _c.save_state(cfg, state)

        return {
            "character": char.get("label", key),
            "damage":    amount,
            "hp_before": hp_before,
            "hp_after":  hp_after,
            "downed":    downed,
            "dead":      dead,
        }

    @mcp.tool()
    def apply_heal(character: str, amount: int) -> dict:
        """Increase a character's HP, capped at hp_max.
        Also syncs with the active combat session if one is running."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        chars_state = state.setdefault("characters", {})
        cstate  = chars_state.setdefault(key, {})
        hp_max  = char.get("hp_max", 0)
        hp_before = cstate.get("hp", hp_max)
        hp_after  = min(hp_before + amount, hp_max)
        cstate["hp"] = hp_after

        if hp_after > 0 and "downed" in cstate.get("conditions", []):
            cstate["conditions"].remove("downed")

        _c.save_state(cfg, state)

        # Sync with active combat session — and persist combat_state.json so
        # the dashboard sidebar and statusline (which read live HP from that
        # file) reflect the heal immediately. Without the persist call the
        # in-memory tracker would be correct but the on-disk file stale.
        combat_synced = False
        try:
            from tools import combat as _combat
            sess = _combat.get_session()
            if sess:
                for c in sess["combatants"]:
                    if c.get("_key") == key:
                        c["hp"] = min(c["hp"] + amount, hp_max)
                        combat_synced = True
                        break
                if combat_synced:
                    _combat._persist_session()
        except Exception:
            pass

        return {
            "character":     char.get("label", key),
            "healed":        hp_after - hp_before,
            "hp_before":     hp_before,
            "hp_after":      hp_after,
            "combat_synced": combat_synced,
        }

    @mcp.tool()
    def memorize_spells(character: str, spells: list) -> dict:
        """Declare the spell list a caster has prepared after rest.
        Each entry is a spell name string or {name, level} dict.
        All spells start uncast. Validates against per-level slot counts.

        Examples:
          ["sleep"]
          [{"name": "sleep", "level": 1}, {"name": "magic missile", "level": 1}]
        """
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        slot_cfg = char.get("spell_slots") or {}
        normalized = _normalize_memorized(spells)

        err = _validate_memorized(slot_cfg, normalized)
        if err:
            return {"error": err}

        cstate = state.setdefault("characters", {}).setdefault(key, {})
        cstate["memorized_spells"] = normalized
        _c.save_state(cfg, state)

        by_level = {}
        for s in normalized:
            k = str(s["level"]) if s["level"] else "?"
            by_level.setdefault(k, []).append(s["name"])

        return {
            "character": char.get("label", key),
            "memorized": [s["name"] for s in normalized],
            "by_level": by_level,
        }

    @mcp.tool()
    def set_default_spells(character: str, spells: list) -> dict:
        """Set a caster's default spell loadout, stored on the character in campaign.json.

        After this is set, rest() automatically re-memorizes this list each day, so
        you only call memorize_spells() to deviate from the standard kit. Same entry
        format and slot-validation as memorize_spells. Pass an empty list to clear the
        default (rest then reverts to leaving the prior loadout in place).

        Examples:
          ["sleep", "magic missile"]
          [{"name": "bless", "level": 1}, {"name": "cure light wounds", "level": 1}]
          []   # clear the default
        """
        cfg = _c.load_campaign()

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        slot_cfg = char.get("spell_slots") or {}
        normalized = _normalize_memorized(spells)

        if normalized:
            err = _validate_memorized(slot_cfg, normalized)
            if err:
                return {"error": err}

        if normalized:
            # store the lean {name, level} form; cast flags are runtime-only
            char["default_spells"] = [{"name": s["name"], "level": s["level"]} for s in normalized]
        else:
            char.pop("default_spells", None)
        _c.save_campaign(cfg)

        return {
            "character": char.get("label", key),
            "default_spells": [s["name"] for s in normalized],
            "cleared": not normalized,
            "note": "rest() will now apply this loadout automatically." if normalized
                    else "Default loadout cleared.",
        }

    @mcp.tool()
    def cast_spell(character: str, spell_name: str) -> dict:
        """Cast a memorized spell by name.
        Marks the first uncast instance as spent and decrements its slot.
        Returns an error if the spell is not memorized or all instances are already cast."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        cstate = state.setdefault("characters", {}).setdefault(key, {})
        memorized = _normalize_memorized(cstate.get("memorized_spells", []))
        needle = spell_name.lower().strip()

        # Find first uncast instance
        target = next(
            (i for i, s in enumerate(memorized) if s["name"].lower() == needle and not s["cast"]),
            None,
        )
        if target is None:
            already_cast = any(s["name"].lower() == needle and s["cast"] for s in memorized)
            if already_cast:
                return {"error": f"'{spell_name}' has already been cast. All memorized instances are spent."}
            return {"error": f"'{spell_name}' is not in {char.get('label', key)}'s memorized spells."}

        spell = memorized[target]
        spell_level = spell["level"]

        # Decrement slot
        slot_cfg = char.get("spell_slots") or {}
        slot_state = cstate.setdefault("spell_slots", dict(slot_cfg))
        if spell_level:
            lvl_key = str(spell_level)
            remaining = slot_state.get(lvl_key, 0)
            if remaining <= 0:
                return {"error": f"No level {spell_level} slots remaining — cannot cast '{spell_name}'."}
            slot_state[lvl_key] = remaining - 1

        memorized[target]["cast"] = True
        cstate["memorized_spells"] = memorized
        _c.save_state(cfg, state)

        available = [s["name"] for s in memorized if not s["cast"]]
        return {
            "character": char.get("label", key),
            "cast": spell["name"],
            "level": spell_level or "unknown",
            "slots_remaining": {k: v for k, v in slot_state.items()},
            "available_spells": available,
        }

    @mcp.tool()
    def spell_status(character: str) -> dict:
        """Return the full spell state: available (uncast) and spent spells, with slot counts."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        cstate = state.get("characters", {}).get(key, {})
        memorized = _normalize_memorized(cstate.get("memorized_spells", []))

        slot_cfg  = char.get("spell_slots") or {}
        slot_state = cstate.get("spell_slots") or {}

        slots = {
            f"level_{lvl}": {"remaining": slot_state.get(str(lvl), max_c), "max": max_c}
            for lvl, max_c in slot_cfg.items()
        }

        return {
            "character": char.get("label", key),
            "available": [s["name"] for s in memorized if not s["cast"]],
            "spent":     [s["name"] for s in memorized if s["cast"]],
            "slots":     slots,
        }

    @mcp.tool()
    def use_spell_slot(character: str, level: int = 1) -> dict:
        """Legacy: decrement a slot by level without specifying a spell name.
        Prefer cast_spell() which tracks the exact spell used."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        cstate = state.setdefault("characters", {}).setdefault(key, {})
        slot_cfg  = char.get("spell_slots") or {}
        lvl_key   = str(level)
        max_slots = slot_cfg.get(lvl_key, 0)
        if max_slots == 0:
            return {"error": f"{char.get('label', key)} has no level {level} spell slots."}

        slot_state = cstate.setdefault("spell_slots", dict(slot_cfg))
        current = slot_state.get(lvl_key, max_slots)
        if current <= 0:
            return {"error": f"No level {level} slots remaining for {char.get('label', key)}."}

        slot_state[lvl_key] = current - 1

        # Mark first uncast spell of matching level (or any level if unknown)
        memorized = _normalize_memorized(cstate.get("memorized_spells", []))
        removed = None
        for s in memorized:
            if not s["cast"] and (s["level"] == level or s["level"] == 0):
                s["cast"] = True
                removed = s["name"]
                break

        cstate["memorized_spells"] = memorized
        _c.save_state(cfg, state)

        return {
            "character": char.get("label", key),
            "level": level,
            "slots_remaining": slot_state,
            "spell_marked_cast": removed,
            "available_spells": [s["name"] for s in memorized if not s["cast"]],
        }

    @mcp.tool()
    def rest(character: str = "") -> dict:
        """Long rest. Restores HP to max and spell slots to full.
        If character is empty string, rests the entire party.

        Casters with a default_spells loadout (see set_default_spells) are
        auto-memorized to that list. Casters without a default keep their
        previous loadout, with cast flags reset. Call memorize_spells to deviate."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        chars_state = state.setdefault("characters", {})
        rested = []
        auto_memorized = []

        target_keys = list(cfg.get("characters", {}).keys()) if not character else None
        if character:
            key, char = _c.char_key_for(cfg, character)
            if key is None:
                return {"error": f"Character '{character}' not found."}
            target_keys = [key]

        for key in target_keys:
            char = cfg["characters"].get(key, {})
            cstate = chars_state.setdefault(key, {})
            cstate["hp"] = char.get("hp_max", cstate.get("hp", 0))
            # Restore spell slots from cfg
            slot_cfg = char.get("spell_slots") or {}
            if slot_cfg:
                cstate["spell_slots"] = dict(slot_cfg)
            # Apply default loadout if set; otherwise keep prior list, reset cast flags
            default_spells = char.get("default_spells")
            if default_spells:
                memorized = _normalize_memorized(default_spells)
                auto_memorized.append(char.get("label", key))
            else:
                memorized = _normalize_memorized(cstate.get("memorized_spells", []))
            for s in memorized:
                s["cast"] = False
            cstate["memorized_spells"] = memorized
            # Remove downed condition
            conditions = cstate.get("conditions", [])
            cstate["conditions"] = [c for c in conditions if c != "downed"]
            rested.append(char.get("label", key))

        _c.save_state(cfg, state)

        if auto_memorized:
            reminder = ("Spell slots restored. Default loadouts re-memorized for: "
                        + ", ".join(auto_memorized)
                        + ". Call memorize_spells only to deviate from a default.")
        else:
            reminder = "Spell slots restored. Call memorize_spells for each spellcaster to set their memorized spell list."

        return {
            "rested": rested,
            "auto_memorized": auto_memorized,
            "reminder": reminder,
        }

    @mcp.tool()
    def award_xp(character: str, amount: int) -> dict:
        """Add XP to a character. Includes a level-up hint if a threshold is crossed."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        chars_state = state.setdefault("characters", {})
        cstate = chars_state.setdefault(key, {})
        xp_before = cstate.get("xp", 0)
        xp_after = xp_before + amount
        cstate["xp"] = xp_after
        _c.save_state(cfg, state)

        hint = _level_up_hint(char, xp_before, xp_after)

        return {
            "character": char.get("label", key),
            "xp_gained": amount,
            "xp_total": xp_after,
            "level_up_hint": hint,
        }

    @mcp.tool()
    def update_coin(delta: str) -> dict:
        """Parse a coin delta like '+450pp', '+50gp', '-10sp', '+3ep', '+100cp' and update the party coin."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        m = re.match(r'^([+-]?\d+)(pp|gp|ep|sp|cp)$', delta.strip().lower())
        if not m:
            return {"error": f"Cannot parse coin delta '{delta}'. Expected format: +450pp, +50gp, -10sp, +3ep, +100cp."}

        amount = int(m.group(1))
        denom = m.group(2)

        coin = state.setdefault("coin", {"pp": 0, "gp": 0, "ep": 0, "sp": 0, "cp": 0})
        coin[denom] = coin.get(denom, 0) + amount
        _c.save_state(cfg, state)

        return {
            "delta": delta,
            "coin": dict(coin),
        }
