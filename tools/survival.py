"""Survival resources — consumable inventory, light sources, encumbrance, ration tracking.

Consumables are stored in state.json under:
    state.consumables.<character_key>          = {item_name: qty, ...}     # carried, weighs on the PC
    state.vehicle_consumables.<mount_slug>     = {item_name: qty, ...}     # stowed in a cart/wagon/saddle bag, no carry weight

Both bags share the same item-name vocabulary. add_inventory/consume
accept either a PC name OR a mount slug for the first argument; PC
resolution wins on ambiguity. Vehicle pools are intentionally invisible
to compute_encumbrance — items stowed there don't load the rider.

Light sources are a stack in state.light_sources:
    [{type: 'torch'|'lantern'|...,
      minutes_remaining: int,
      holder: char_label,
      lit: bool}, ...]

Lit-time defaults follow the PHB: torch 60 min, lantern 240 min, candle 60 min.
The actual minute-by-minute decrement happens in tools/log.py advance_time
(added alongside time-of-day support).
"""
import sqlite3
from pathlib import Path
import _campaign as _c
from tools import item_weights as _iw

BASE_DIR = Path(__file__).parent.parent
_2E_DB = BASE_DIR / "global" / "2e.db"

# PHB Table 47 STR bands and the encumbrance calculator are in
# tools/item_weights.py so the dashboard and the MCP tool report identically.
_str_thresholds = _iw.str_band_for


def _item_weight(name: str, conn: sqlite3.Connection | None = None) -> float:
    """Resolve an inventory string to its weight in pounds. Honors
    INVENTORY_ALIASES, PHB_OVERRIDES, and the 'x N' qty suffix.

    See tools/item_weights.py for resolution order. Pass a sqlite3 connection
    to amortize open cost across many lookups."""
    return _iw.item_weight(name, conn=conn)


_LIGHT_DEFAULTS = {
    "torch":    60,
    "lantern":  240,
    "candle":   60,
    "continual_light": -1,   # magical, no decay
}


def _resolve_char(cfg: dict, character: str) -> tuple[str | None, dict | None, str]:
    """Resolve a character key (PCs first, then NPC slugs).
    Returns (key, char_dict, display_label) or (None, None, '')."""
    key, char = _c.char_key_for(cfg, character)
    if key is not None:
        return key, char, char.get("label", key)
    npcs = cfg.get("npcs", {})
    low = character.lower()
    for k, c in npcs.items():
        if c.get("label", "").lower() == low or k.lower() == low:
            return k, c, c.get("label", k)
    for k, c in npcs.items():
        if c.get("label", "").lower().startswith(low) or k.lower().startswith(low):
            return k, c, c.get("label", k)
    return None, None, ""


def _consumables(state: dict, key: str) -> dict:
    return state.setdefault("consumables", {}).setdefault(key, {})


def _vehicle_pool(state: dict, slug: str) -> dict:
    return state.setdefault("vehicle_consumables", {}).setdefault(slug, {})


def _resolve_target(cfg: dict, name: str) -> tuple[str, str | None, str]:
    """Resolve the consumable-bag target. Tries PC/NPC first, falls back
    to a mount slug (vehicle pool). Returns ``(kind, key, label)`` where
    kind is "char" or "vehicle", or ("none", None, "") on miss."""
    key, _char, label = _resolve_char(cfg, name)
    if key is not None:
        return ("char", key, label)
    low = name.lower().strip()
    mounts = cfg.get("mounts") or {}
    if low in mounts:
        return ("vehicle", low, mounts[low].get("name", low))
    # Allow display-name match for ergonomics (e.g. "Trader's Cart").
    for slug, m in mounts.items():
        if (m.get("name") or "").lower() == low:
            return ("vehicle", slug, m.get("name", slug))
    return ("none", None, "")


def register(mcp):

    @mcp.tool()
    def add_inventory(character: str, item: str, qty: int = 1) -> dict:
        """Add a quantity of a consumable to a character's tracked inventory,
        or to a vehicle's stowed pool when the first argument is a mount slug
        (e.g. ``trader-cart``). Use for torches, rations, arrows, oil flasks,
        holy water, potions, etc. Stowed items in vehicle pools do NOT count
        toward the rider's encumbrance.

        For one-of-a-kind items (a magic sword, a key) keep them in the
        character markdown sheet — this tool is for things that get used up."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        kind, key, label = _resolve_target(cfg, character)
        if kind == "none":
            return {"error": f"Target '{character}' is neither a character nor a mount slug."}

        item_key = item.lower().strip()
        bag = _vehicle_pool(state, key) if kind == "vehicle" else _consumables(state, key)
        bag[item_key] = bag.get(item_key, 0) + int(qty)
        _c.save_state(cfg, state)

        result = {"item": item_key, "qty_added": qty, "now_holds": bag[item_key]}
        if kind == "vehicle":
            result["vehicle"] = label
            result["stowed_in"] = key
        else:
            result["character"] = label
        return result

    @mcp.tool()
    def consume(character: str, item: str, qty: int = 1) -> dict:
        """Decrement a character's consumable, or a vehicle's stowed pool when
        the first argument is a mount slug. Errors if they don't have enough.
        Use proactively whenever an in-fiction action depletes supplies:
        firing arrows, lighting a torch, eating a ration, drinking a potion.

        Returns a 'low_supply' warning when stock falls to ≤ 2 (rationing matters)."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        kind, key, label = _resolve_target(cfg, character)
        if kind == "none":
            return {"error": f"Target '{character}' is neither a character nor a mount slug."}

        item_key = item.lower().strip()
        bag = _vehicle_pool(state, key) if kind == "vehicle" else _consumables(state, key)
        have = bag.get(item_key, 0)
        if have < qty:
            return {"error": f"{label} has only {have} {item_key} (needed {qty})."}

        bag[item_key] = have - qty
        if bag[item_key] == 0:
            del bag[item_key]
        _c.save_state(cfg, state)

        result = {
            "item":       item_key,
            "qty_used":   qty,
            "remaining":  bag.get(item_key, 0),
        }
        if kind == "vehicle":
            result["vehicle"] = label
        else:
            result["character"] = label
        remaining = bag.get(item_key, 0)
        if remaining == 0:
            result["warning"] = f"{label} is out of {item_key}."
        elif remaining <= 2:
            result["warning"] = f"{label} has only {remaining} {item_key} left."
        return result

    @mcp.tool()
    def inventory(character: str = "") -> dict:
        """List tracked consumables.
        character: empty = all party + named NPCs; named = just that character."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        bag_all = state.get("consumables", {})

        if character:
            key, _char, label = _resolve_char(cfg, character)
            if key is None:
                return {"error": f"Character '{character}' not found."}
            return {"character": label, "items": dict(bag_all.get(key, {}))}

        # Party + NPC view
        out = {}
        for key, items in bag_all.items():
            if not items:
                continue
            char = cfg.get("characters", {}).get(key) or cfg.get("npcs", {}).get(key, {})
            label = char.get("label", key)
            out[label] = dict(items)
        return {"by_character": out}

    @mcp.tool()
    def light_torch(character: str, source_type: str = "torch") -> dict:
        """Light a fresh torch / lantern / candle. Consumes one from inventory.
        For lanterns, also consumes one oil flask (lanterns burn oil, not the lantern itself).
        Pushes a new lit source onto state.light_sources with default duration.
        Use extinguish_light to put it out early; advance_time decrements remaining minutes."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, _char, label = _resolve_char(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        stype = source_type.lower().strip().replace(" ", "_")
        if stype not in _LIGHT_DEFAULTS:
            return {"error": f"Unknown light source '{source_type}'. Use: torch, lantern, candle, continual_light."}

        bag = _consumables(state, key)

        # Lanterns burn oil; the lantern itself is durable
        if stype == "lantern":
            if bag.get("oil", 0) < 1:
                return {"error": f"{label} has no oil for the lantern."}
            bag["oil"] -= 1
            if bag["oil"] == 0:
                del bag["oil"]
        elif stype != "continual_light":
            if bag.get(stype, 0) < 1:
                return {"error": f"{label} has no {stype} to light."}
            bag[stype] -= 1
            if bag[stype] == 0:
                del bag[stype]

        sources = state.setdefault("light_sources", [])
        entry = {
            "type":              stype,
            "holder":            label,
            "minutes_remaining": _LIGHT_DEFAULTS[stype],
            "lit":               True,
        }
        sources.append(entry)
        _c.save_state(cfg, state)

        return {
            "lit":          stype,
            "holder":       label,
            "duration_min": _LIGHT_DEFAULTS[stype],
            "active_count": sum(1 for s in sources if s.get("lit")),
        }

    @mcp.tool()
    def extinguish_light(holder: str = "", source_type: str = "") -> dict:
        """Extinguish a lit light source. Filters by holder and/or type;
        empty filters = extinguish the most recently lit one."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        sources = state.get("light_sources", [])

        candidates = [
            (i, s) for i, s in enumerate(sources)
            if s.get("lit")
            and (not holder      or s.get("holder", "").lower().startswith(holder.lower()))
            and (not source_type or s.get("type", "").lower() == source_type.lower())
        ]
        if not candidates:
            return {"error": "No matching lit light source."}

        i, s = candidates[-1]   # most recently lit
        s["lit"] = False
        _c.save_state(cfg, state)
        return {"extinguished": s["type"], "holder": s.get("holder", ""), "remaining_min": s["minutes_remaining"]}

    @mcp.tool()
    def encumbrance(character: str) -> dict:
        """Calculate carry weight from a character's static inventory + tracked
        consumables, vs their STR encumbrance bands.

        Returns total weight, applicable band (light/moderate/heavy/severe),
        movement-rate penalty, and a per-item breakdown with weight source.

        Resolution honors PHB Chapter 6 weights, item-name aliases (e.g.
        'longsword' → 'Long sword'), and 'x N' quantity suffixes. Items with
        no PHB or DB match return 0 lb and source='unknown'; override via
        item_update if needed.

        Use after major loot pickups, before long marches, and when the player
        argues 'I don't have to drop the chest'."""
        cfg   = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char, label = _resolve_char(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        enc = _iw.compute_encumbrance(char, state, key)
        return {
            "character":        label,
            "strength":         enc["strength"],
            "total_weight":     enc["weight"],
            "thresholds":       enc["thresholds"],
            "band":             enc["band"],
            "movement_penalty": enc["penalty"],
            "items":            enc["items"],
        }

    @mcp.tool()
    def light_state() -> dict:
        """Return all lit and recently-extinguished light sources.
        Useful at any point in a dungeon to check 'is the party still lit?'.
        Sources with minutes_remaining ≤ 5 are flagged as 'guttering'."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        sources = state.get("light_sources", [])

        lit  = []
        used = []
        for s in sources:
            entry = dict(s)
            if s.get("lit"):
                if 0 <= s.get("minutes_remaining", 0) <= 5:
                    entry["status"] = "guttering"
                else:
                    entry["status"] = "burning"
                lit.append(entry)
            else:
                used.append(entry)
        return {
            "any_lit":   bool(lit),
            "lit":       lit,
            "burnt_out": [s for s in used if s.get("minutes_remaining", 1) <= 0],
        }
