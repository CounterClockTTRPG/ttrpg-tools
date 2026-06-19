"""Combat tracker — initiative, attack resolution, HP management, spell declaration."""
import json
import random
import re
import sqlite3
from pathlib import Path
import _campaign as _c

_session: dict | None = None

BASE_DIR = Path(__file__).parent.parent
_2E_DB = BASE_DIR / "global" / "2e.db"


def get_session() -> dict | None:
    """Return the active combat session (used by party tools for heal sync)."""
    return _session


def _persist_session() -> None:
    """Write combat_state.json after a state mutation so the dashboard and
    status line can show live HP/conditions during combat. Deferred import
    avoids a circular dep with tools.combat_map."""
    if _session is None:
        return
    try:
        from tools.combat_map import save_combat_state
        cfg = _c.load_campaign()
        save_combat_state(cfg, _session)
    except Exception:
        pass  # never break a tool response on a dashboard write


def _sum_modifier(combatant: dict, key: str) -> int:
    """Total a numeric effect modifier across all active effects on a combatant."""
    total = 0
    for eff in combatant.get("effects", []):
        v = eff.get(key)
        if isinstance(v, int):
            total += v
    return total


def _check_morale_trigger(side: str) -> dict | None:
    """Return a one-shot morale-due notice when `side` first crosses 50% casualties.
    Tracks fired sides on the session so each threshold fires only once."""
    if _session is None or side == "party":
        return None
    members = [c for c in _session["combatants"] if c.get("side") == side]
    if not members:
        return None
    down = sum(1 for c in members if c.get("hp", 0) <= 0)
    if down * 2 < len(members):
        return None
    fired = _session.setdefault("morale_triggered_sides", [])
    if side in fired:
        return None
    fired.append(side)
    return {
        "side":     side,
        "down":     down,
        "total":    len(members),
        "message":  f"Morale check due: {down}/{len(members)} of side '{side}' down. "
                    f"Call morale_check(rating) using the group's morale rating "
                    f"(monster_lookup → 'morale').",
    }


def _roll_dice(notation: str) -> int:
    m = re.match(r'^(\d*)d(\d+)(([+-]\d+)*)$', notation.strip().lower())
    if not m:
        return 1
    count_str, sides_str, mod_str, _ = m.groups()
    count    = int(count_str) if count_str else 1
    sides    = int(sides_str)
    modifier = sum(int(p) for p in re.findall(r'[+-]\d+', mod_str or ''))
    total    = sum(random.randint(1, sides) for _ in range(count)) + modifier
    return max(0, total)


def _find_combatant(name: str) -> dict | None:
    if _session is None:
        return None
    low = name.lower()
    for c in _session["combatants"]:
        if c["name"].lower() == low:
            return c
    for c in _session["combatants"]:
        if c["name"].lower().startswith(low):
            return c
    return None


_spell_ct_cache: dict[str, tuple | None] = {}


def _spell_casting_time(spell_name: str) -> tuple | None:
    """Return (casting_time, casting_time_init) from 2e.db, or None if not found.
    Cached: spell DB is read-only at runtime."""
    key = spell_name.lower().strip()
    if key in _spell_ct_cache:
        return _spell_ct_cache[key]
    if not _2E_DB.exists():
        _spell_ct_cache[key] = None
        return None
    try:
        conn = sqlite3.connect(str(_2E_DB))
        row = conn.execute(
            "SELECT casting_time, casting_time_init FROM spells "
            "WHERE name = ? COLLATE NOCASE "
            "ORDER BY CASE WHEN caster='wizard' THEN 0 ELSE 1 END LIMIT 1",
            (spell_name.strip(),),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT casting_time, casting_time_init FROM spells "
                "WHERE name LIKE ? COLLATE NOCASE "
                "ORDER BY CASE WHEN caster='wizard' THEN 0 ELSE 1 END, length(name) LIMIT 1",
                (f"%{spell_name}%",),
            ).fetchone()
        conn.close()
        result = (row[0], row[1]) if row else None
    except Exception:
        result = None
    _spell_ct_cache[key] = result
    return result


def register(mcp):

    @mcp.tool()
    def start_combat() -> dict:
        """Start a new combat encounter. Loads all party members from the active campaign
        and resets the combat session. Call this before adding enemies or rolling initiative."""
        global _session
        cfg   = _c.load_campaign()
        state = _c.load_state(cfg)

        _session = {
            "round":       1,
            "current_idx": 0,
            "combatants":  [],
            "grid":        None,
        }

        party_loaded = []
        for key, char in cfg.get("characters", {}).items():
            char_state = state.get("characters", {}).get(key, {})
            hp_current = char_state.get("hp", char["hp_max"])
            conditions = list(char_state.get("conditions", []))
            _session["combatants"].append({
                "name":         char["label"],
                "hp":           hp_current,
                "hp_max":       char["hp_max"],
                "thac0":        char.get("thac0", 20),
                "ac":           char.get("ac", 10),
                "dmg":          char.get("weapon", "1d6"),
                "bonus_hit":    char.get("bonus_hit", 0),
                "bonus_dmg":    char.get("bonus_dmg", 0),
                "weapon_speed": char.get("weapon_speed", 5),
                "side":         "party",
                "init":         None,
                "conditions":   conditions,
                "effects":      [],
                "_key":         key,
                "x":            None,
                "y":            None,
                "movement":     char.get("movement_rate", 12) // 2,
            })
            party_loaded.append(char["label"])

        _persist_session()
        return {
            "message":      f"Combat started. {len(party_loaded)} party member(s) loaded.",
            "party_loaded": party_loaded,
        }

    @mcp.tool()
    def add_combatant(
        name:         str,
        hp:           int,
        ac:           int,
        thac0:        int,
        dmg:          str = "1d6",
        side:         str = "enemy",
        weapon_speed: int = 5,
        movement:     int = 6,
    ) -> dict:
        """Add a combatant (usually an enemy) to the active combat session.
        Call start_combat first. dmg is a dice notation string e.g. '2d6'."""
        if _session is None:
            return {"error": "No active combat session. Call start_combat first."}

        _session["combatants"].append({
            "name":         name,
            "hp":           hp,
            "hp_max":       hp,
            "thac0":        thac0,
            "ac":           ac,
            "dmg":          dmg,
            "bonus_hit":    0,
            "bonus_dmg":    0,
            "weapon_speed": weapon_speed,
            "side":         side,
            "init":         None,
            "conditions":   [],
            "effects":      [],
            "x":            None,
            "y":            None,
            "movement":     movement,
        })

        _persist_session()
        return {"name": name, "hp": hp, "ac": ac, "thac0": thac0, "side": side}

    @mcp.tool()
    def roll_initiative() -> dict:
        """Roll initiative for all combatants. Lower total (d10 + weapon_speed) acts first.
        Spellcasters who have declared a spell via declare_spell() will use casting_time
        instead of weapon_speed. Stores results and returns sorted order."""
        if _session is None:
            return {"error": "No active combat session. Call start_combat first."}

        order = []
        for c in _session["combatants"]:
            d10   = random.randint(1, 10)
            total = d10 + c["weapon_speed"]
            c["init"] = total
            order.append({
                "name":   c["name"],
                "roll":   d10,
                "wspeed": c["weapon_speed"],
                "total":  total,
                "side":   c["side"],
            })

        order.sort(key=lambda x: x["total"])
        name_to_total = {e["name"]: e["total"] for e in order}
        _session["combatants"].sort(key=lambda c: name_to_total.get(c["name"], 999))
        _session["current_idx"] = 0

        _persist_session()
        return {"round": _session["round"], "order": order}

    @mcp.tool()
    def declare_spell(character: str, spell_name: str) -> dict:
        """Declare a spell before initiative is rolled.
        Looks up the spell's casting time in the 2e DB and sets the caster's
        weapon_speed to that value so roll_initiative() uses it correctly.
        Also adds the 'casting' condition so any damage before their turn disrupts the spell.
        Must be called before roll_initiative() each round the spell is cast."""
        if _session is None:
            return {"error": "No active combat session. Call start_combat first."}

        caster = _find_combatant(character)
        if caster is None:
            return {"error": f"Combatant '{character}' not found in combat."}

        result = _spell_casting_time(spell_name)
        if result is None:
            ct_raw, ct_init = "unknown", 5
            db_found = False
        else:
            ct_raw, ct_init = result
            db_found = True

        # Set weapon_speed to casting time for initiative
        caster["weapon_speed"] = ct_init

        # Mark as casting so damage before their turn disrupts
        if "casting" not in caster["conditions"]:
            caster["conditions"].append("casting")

        _persist_session()
        return {
            "caster":           caster["name"],
            "spell":            spell_name,
            "casting_time":     ct_raw if db_found else "not found in DB — defaulting to 5",
            "casting_time_init": ct_init,
            "effect":           f"Initiative will be d10+{ct_init}. Spell disrupted if hit before turn.",
        }

    @mcp.tool()
    def attack(attacker: str, target: str, condition: str = "") -> dict:
        """Resolve one attack roll. Natural 20 always hits; natural 1 always misses.
        condition options: 'rear' (+2 hit), 'charging' (+2 hit, AC penalty),
        'set_vs_charge' (double damage on hit), 'fleeing' (auto-hit).
        HP floor: 0 = unconscious (stable), -1 to -9 = bleeding (1 HP/round), -10 = dead."""
        if _session is None:
            return {"error": "No active combat session. Call start_combat first."}

        atk = _find_combatant(attacker)
        if atk is None:
            return {"error": f"Attacker '{attacker}' not found in combat."}
        tgt = _find_combatant(target)
        if tgt is None:
            return {"error": f"Target '{target}' not found in combat."}

        notes: list[str] = []
        cond = condition.lower().strip()

        if cond == "fleeing":
            roll = 10
            hit  = True
            notes.append("Target is fleeing — automatic hit.")
        else:
            roll      = random.randint(1, 20)
            hit_bonus = atk["bonus_hit"]

            # Active-effect modifiers on attacker (to_hit) and target (ac)
            atk_eff_hit = _sum_modifier(atk, "to_hit")
            tgt_eff_ac  = _sum_modifier(tgt, "ac")
            if atk_eff_hit:
                hit_bonus += atk_eff_hit
                notes.append(f"{atk_eff_hit:+d} to hit (active effects on {atk['name']}).")
            if tgt_eff_ac:
                notes.append(f"{tgt_eff_ac:+d} AC modifier on {tgt['name']} (active effects).")

            if cond == "rear":
                hit_bonus += 2
                notes.append("+2 to hit (rear attack).")
            elif cond == "charging":
                hit_bonus += 2
                if "charging_ac_penalty" not in atk["conditions"]:
                    atk["conditions"].append("charging_ac_penalty")
                notes.append("+2 to hit (charging). Attacker has AC penalty this round.")

            if roll == 20:
                hit = True
                notes.append("Natural 20 — critical hit!")
            elif roll == 1:
                hit = False
                notes.append("Natural 1 — automatic miss!")
            else:
                effective_roll = roll + hit_bonus
                target_ac = tgt["ac"] + tgt_eff_ac
                if cond == "rear":
                    target_ac += 2
                hit = effective_roll >= (atk["thac0"] - target_ac)

        # Spell disruption on target
        spell_disrupted = False
        if "casting" in tgt["conditions"]:
            tgt["conditions"].remove("casting")
            spell_disrupted = True
            notes.append(f"{tgt['name']} was casting — spell disrupted!")

        damage   = None
        hp_after = None
        downed   = False
        dead     = False

        if hit:
            atk_eff_dmg = _sum_modifier(atk, "dmg")
            raw_dmg = _roll_dice(atk["dmg"]) + atk["bonus_dmg"] + atk_eff_dmg
            if atk_eff_dmg:
                notes.append(f"{atk_eff_dmg:+d} damage (active effects).")
            if cond == "set_vs_charge":
                raw_dmg *= 2
                notes.append("Set vs. charge — double damage!")
            damage    = max(0, raw_dmg)
            tgt["hp"] = max(-10, tgt["hp"] - damage)
            hp_after  = tgt["hp"]
            dead      = tgt["hp"] <= -10
            downed    = tgt["hp"] <= 0
            if dead:
                notes.append(f"{tgt['name']} is dead!")
            elif downed:
                notes.append(f"{tgt['name']} is unconscious!")
                if tgt["hp"] < 0:
                    notes.append(f"Bleeding: loses 1 HP/round until stabilised or dead at -10.")

        result = {
            "attacker":        atk["name"],
            "target":          tgt["name"],
            "roll":            roll,
            "hit":             hit,
            "damage":          damage,
            "hp_after":        hp_after,
            "downed":          downed,
            "dead":            dead,
            "spell_disrupted": spell_disrupted,
            "notes":           notes,
        }
        if downed:
            morale = _check_morale_trigger(tgt["side"])
            if morale:
                result["morale_due"] = morale
        _persist_session()
        return result

    @mcp.tool()
    def apply_combat_damage(name: str, amount: int) -> dict:
        """Apply direct damage to a named combatant (e.g. from spells or traps).
        If the target was casting, their spell is disrupted.
        HP floor: 0 = unconscious, -10 = dead."""
        if _session is None:
            return {"error": "No active combat session. Call start_combat first."}

        c = _find_combatant(name)
        if c is None:
            return {"error": f"Combatant '{name}' not found."}

        spell_disrupted = False
        if "casting" in c["conditions"]:
            c["conditions"].remove("casting")
            spell_disrupted = True

        hp_before = c["hp"]
        c["hp"]   = max(-10, c["hp"] - amount)
        downed    = c["hp"] <= 0
        dead      = c["hp"] <= -10

        result = {
            "name":            c["name"],
            "hp_before":       hp_before,
            "hp_after":        c["hp"],
            "downed":          downed,
            "dead":            dead,
            "spell_disrupted": spell_disrupted,
        }
        if downed:
            morale = _check_morale_trigger(c["side"])
            if morale:
                result["morale_due"] = morale
        _persist_session()
        return result

    @mcp.tool()
    def apply_combat_heal(name: str, amount: int) -> dict:
        """Apply healing directly to a combatant in the active session.
        Caps at hp_max. Removes 'unconscious' status if HP rises above 0.
        Use this during combat instead of apply_heal (which writes to the character file
        but does not update the in-session HP tracker)."""
        if _session is None:
            return {"error": "No active combat session. Call start_combat first."}

        c = _find_combatant(name)
        if c is None:
            return {"error": f"Combatant '{name}' not found."}

        hp_before = c["hp"]
        hp_max    = c.get("hp_max", hp_before)
        c["hp"]   = min(hp_before + amount, hp_max)
        healed    = c["hp"] - hp_before

        notes = []
        if hp_before <= 0 and c["hp"] > 0:
            notes.append(f"{c['name']} regains consciousness.")

        _persist_session()
        return {
            "name":     c["name"],
            "healed":   healed,
            "hp_before": hp_before,
            "hp_after":  c["hp"],
            "notes":    notes,
        }

    @mcp.tool()
    def add_condition(name: str, condition: str) -> dict:
        """Add a condition string to a combatant (e.g. 'stunned', 'prone', 'casting').
        No-op if condition is already present."""
        if _session is None:
            return {"error": "No active combat session. Call start_combat first."}

        c = _find_combatant(name)
        if c is None:
            return {"error": f"Combatant '{name}' not found."}

        if condition not in c["conditions"]:
            c["conditions"].append(condition)

        _persist_session()
        return {"name": c["name"], "conditions": list(c["conditions"])}

    @mcp.tool()
    def remove_condition(name: str, condition: str) -> dict:
        """Remove a condition from a combatant. No-op if condition is not present."""
        if _session is None:
            return {"error": "No active combat session. Call start_combat first."}

        c = _find_combatant(name)
        if c is None:
            return {"error": f"Combatant '{name}' not found."}

        if condition in c["conditions"]:
            c["conditions"].remove(condition)

        _persist_session()
        return {"name": c["name"], "conditions": list(c["conditions"])}

    @mcp.tool()
    def apply_effect(
        target:    str,
        name:      str,
        duration:  int,
        to_hit:    int = 0,
        ac:        int = 0,
        dmg:       int = 0,
        save:      int = 0,
        source:    str = "",
    ) -> dict:
        """Attach a timed effect (buff or debuff) to a combatant.
        Auto-decrements at the start of each new round and expires when duration hits 0.
        Numeric modifiers are auto-applied:
          to_hit: delta to attacker's d20 attack roll (e.g. bless +1)
          ac:     delta to AC when targeted (negative = harder to hit; e.g. shield -1)
          dmg:    delta to damage rolls
          save:   not auto-applied — saving_throw() does not consult effects;
                  surfaced for the DM's reference when calling it manually
        duration: rounds remaining; 1 = expires at start of next round.
        Examples:
          apply_effect("Maren", "bless", 6, to_hit=1)
          apply_effect("Goblin #2", "stinking cloud", 3, to_hit=-2)
          apply_effect("Torven", "haste", 9, to_hit=0)  # mark, narrate doubled actions
        """
        if _session is None:
            return {"error": "No active combat session. Call start_combat first."}

        c = _find_combatant(target)
        if c is None:
            return {"error": f"Combatant '{target}' not found."}

        eff = {"name": name, "duration": int(duration)}
        if to_hit: eff["to_hit"] = int(to_hit)
        if ac:     eff["ac"]     = int(ac)
        if dmg:    eff["dmg"]    = int(dmg)
        if save:   eff["save"]   = int(save)
        if source: eff["source"] = source

        c.setdefault("effects", []).append(eff)
        _persist_session()
        return {"target": c["name"], "effect": eff, "active_count": len(c["effects"])}

    @mcp.tool()
    def remove_effect(target: str, name: str) -> dict:
        """Remove a named effect from a combatant before its natural expiry
        (e.g. dispel magic, save succeeded on next round, target moved out of AOE).
        Removes only the first matching effect; call again if duplicates exist."""
        if _session is None:
            return {"error": "No active combat session. Call start_combat first."}

        c = _find_combatant(target)
        if c is None:
            return {"error": f"Combatant '{target}' not found."}

        effects = c.get("effects", [])
        for i, eff in enumerate(effects):
            if eff.get("name", "").lower() == name.lower():
                removed = effects.pop(i)
                _persist_session()
                return {"target": c["name"], "removed": removed, "remaining": len(effects)}
        return {"error": f"No active effect named '{name}' on {c['name']}."}

    @mcp.tool()
    def next_turn() -> dict:
        """Advance to the next combatant in initiative order, skipping those who cannot act
        (HP <= 0). Wraps to round start and increments the round counter.
        At each new round: party members with HP between -1 and -9 lose 1 HP (bleeding).
        Dead combatants (HP <= -10) are always skipped."""
        global _session
        if _session is None:
            return {"error": "No active combat session. Call start_combat first."}

        combatants = _session["combatants"]
        n = len(combatants)
        if n == 0:
            return {"error": "No combatants in session."}

        start_idx = _session["current_idx"]
        idx       = start_idx
        bleeding_report = []

        for _ in range(n):
            idx = (idx + 1) % n

            if idx == 0:
                # New round — increment counter and apply bleeding
                _session["round"] += 1
                for c in combatants:
                    if c["side"] == "party" and -10 < c["hp"] < 0:
                        c["hp"] = max(-10, c["hp"] - 1)
                        status = "dead" if c["hp"] <= -10 else f"hp now {c['hp']}"
                        bleeding_report.append(f"{c['name']} bleeds 1 HP ({status})")
                # Decrement active effects; remove expired
                for c in combatants:
                    surviving = []
                    for eff in c.get("effects", []):
                        d = eff.get("duration", 0)
                        if d <= 1:
                            bleeding_report.append(f"{c['name']}: '{eff.get('name','effect')}' expires.")
                        else:
                            eff["duration"] = d - 1
                            surviving.append(eff)
                    c["effects"] = surviving

            c = combatants[idx]
            if c["hp"] > 0:
                _session["current_idx"] = idx
                result = {
                    "round":   _session["round"],
                    "current": c["name"],
                    "init":    c["init"],
                }
                if bleeding_report:
                    result["bleeding"] = bleeding_report
                _persist_session()
                return result

        return {"error": "All combatants are down."}

    @mcp.tool()
    def combat_status() -> dict:
        """Return full current combat status: round, turn, and all combatants with
        HP, AC, initiative, side, conditions, and alive/dead status."""
        if _session is None:
            return {"error": "No active combat session. Call start_combat first."}

        combatants_out = []
        for i, c in enumerate(_session["combatants"]):
            status = "active"
            if c["hp"] <= -10:
                status = "dead"
            elif c["hp"] <= 0:
                status = "unconscious"
            combatants_out.append({
                "name":       c["name"],
                "side":       c["side"],
                "hp":         c["hp"],
                "hp_max":     c["hp_max"],
                "ac":         c["ac"],
                "init":       c["init"],
                "conditions": list(c["conditions"]),
                "effects":    list(c.get("effects", [])),
                "status":     status,
                "current":    i == _session["current_idx"],
            })

        combatants_out.sort(key=lambda x: (x["init"] is None, x["init"] or 999))
        current = _session["combatants"][_session["current_idx"]]
        return {
            "round":      _session["round"],
            "current":    current["name"],
            "combatants": combatants_out,
        }

    @mcp.tool()
    def end_combat() -> dict:
        """End combat: save all party members' current HP back to state.json,
        record a combat event, and clear the in-memory session."""
        global _session
        if _session is None:
            return {"error": "No active combat session."}

        cfg   = _c.load_campaign()
        state = _c.load_state(cfg)

        rounds   = _session["round"]
        party_hp = {}
        saved    = {}

        for c in _session["combatants"]:
            if c["side"] != "party":
                continue
            key = c.get("_key")
            if key and key in state.get("characters", {}):
                state["characters"][key]["hp"] = c["hp"]
                party_hp[c["name"]] = c["hp"]
                saved[c["name"]]    = c["hp"]

        _c.save_state(cfg, state)
        _c.append_event(cfg, {
            "type":     "combat",
            "outcome":  "ended",
            "rounds":   rounds,
            "party_hp": party_hp,
        })

        # Remove dashboard map state file
        cs_file = cfg["_dir"] / "combat_state.json"
        if cs_file.exists():
            cs_file.unlink()

        _session = None
        return {"saved": saved, "rounds": rounds}
