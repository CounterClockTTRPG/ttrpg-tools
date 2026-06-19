"""Hireling and henchman tracking — loyalty checks driven by CHA + interactions.

A hired NPC gets these fields on its stat block in campaign.json[npcs]:
    is_hireling:     True
    employer:        PC key (or 'party')
    terms:           free-form string (e.g. '5gp/week, share of loot')
    loyalty_base:    derived from employer's CHA (PHB table)
    current_loyalty: starts == loyalty_base, drifts based on adjust_loyalty calls

A loyalty check rolls 2d10. If the result EXCEEDS current_loyalty (after
modifiers), the hireling acts on it — deserting, refusing the order, or
betraying. The DM narrates the consequence based on personality.

Trigger checks at:
  - First request to do something dangerous beyond the contract
  - When pay is missed or treasure split is unfair
  - After a battle where a comrade fell
  - Monthly (long downtime)
"""
import random
import _campaign as _c


# Quick-ref CHA → loyalty base (a *score*, not a modifier — high = more loyal).
_CHA_LOYALTY_BASE = {
    3:  -8, 4:  -6, 5:  -6, 6:  -4, 7:  -4,
    8:  -2, 9:  -2, 10:  0, 11:  0, 12:  0, 13: 0,
    14: +1, 15: +1, 16: +4, 17: +6, 18: +8, 19: +10,
}


def _resolve_npc(cfg: dict, slug: str) -> tuple[str | None, dict | None]:
    s = slug.lower().strip()
    npcs = cfg.get("npcs", {})
    if s in npcs:
        return s, npcs[s]
    for k, v in npcs.items():
        if k.lower().startswith(s) or v.get("label", "").lower().startswith(s):
            return k, v
    return None, None


def _cha_for(cfg: dict, key_or_label: str) -> int:
    """Look up Charisma for a PC (or NPC) by key/label. Defaults to 10."""
    label_l = key_or_label.lower().strip()
    for k, c in cfg.get("characters", {}).items():
        if k.lower() == label_l or c.get("label", "").lower() == label_l:
            return int((c.get("ability_scores") or {}).get("cha", 10))
    for k, c in cfg.get("npcs", {}).items():
        if k.lower() == label_l or c.get("label", "").lower() == label_l:
            return int((c.get("ability_scores") or {}).get("cha", 10))
    return 10


def register(mcp):

    @mcp.tool()
    def hire(slug: str, employer: str, terms: str = "") -> dict:
        """Mark an NPC as a hireling and set initial loyalty from the employer's CHA.
        slug:     NPC slug (must already exist via introduce_npc / quick_npc)
        employer: PC key or 'party' for shared loyalty (defaults to default_character's CHA)
        terms:    free-form contract description"""
        cfg = _c.load_campaign()
        key, npc = _resolve_npc(cfg, slug)
        if key is None:
            return {"error": f"NPC '{slug}' not found. Use introduce_npc or quick_npc first."}

        emp_label = employer.strip() or "party"
        if emp_label.lower() == "party":
            emp_label = cfg.get("default_character", emp_label)
        cha = _cha_for(cfg, emp_label)
        base = _CHA_LOYALTY_BASE.get(cha, 0)

        npc["is_hireling"]     = True
        npc["employer"]        = emp_label
        npc["terms"]           = terms
        npc["loyalty_base"]    = base
        npc["current_loyalty"] = base
        _c.save_campaign(cfg)

        _c.append_event(cfg, {
            "type":  "hireling_hired",
            "slug":  key,
            "name":  npc.get("label", key),
            "notes": f"Hired by {emp_label} on terms: {terms or '(none specified)'}; CHA {cha} → loyalty {base}",
        })

        return {
            "slug":           key,
            "label":          npc.get("label", key),
            "employer":       emp_label,
            "employer_cha":   cha,
            "loyalty_base":   base,
            "current_loyalty": base,
        }

    @mcp.tool()
    def loyalty_check(slug: str, modifier: int = 0) -> dict:
        """Roll 2d10 loyalty check. If roll EXCEEDS current_loyalty + modifier,
        the hireling breaks — DM narrates desertion, refusal, or betrayal.

        modifier: situational shift (+ for favourable, - for harsh).
                  Suggested: +2 fair share, +2 saved their life, -2 abandoned in danger,
                  -3 leader incapacitated, -1 hostile environment, -2 owed wages."""
        cfg = _c.load_campaign()
        key, npc = _resolve_npc(cfg, slug)
        if key is None:
            return {"error": f"Hireling '{slug}' not found."}
        if not npc.get("is_hireling"):
            return {"error": f"{npc.get('label', key)} is not a hireling. Call hire() first."}

        loyalty = int(npc.get("current_loyalty", npc.get("loyalty_base", 0)))
        threshold = loyalty + int(modifier)

        # Normalise into the 2-20 range and treat threshold like a morale rating.
        # Default loyalty 0 means "neutral, average chance" — anchor base 11.
        anchored = 11 + threshold
        roll = random.randint(1, 10) + random.randint(1, 10)
        breaks = roll > anchored

        outcome = "holds" if not breaks else "breaks"
        return {
            "hireling":       npc.get("label", key),
            "loyalty":        loyalty,
            "modifier":       modifier,
            "anchor":         anchored,
            "roll":           roll,
            "breaks":         breaks,
            "outcome":        outcome,
            "description":    (
                f"Holds firm — roll {roll} ≤ {anchored}." if not breaks
                else f"Loyalty breaks — roll {roll} > {anchored}. The hireling deserts, refuses the order, or worse."
            ),
        }

    @mcp.tool()
    def adjust_loyalty(slug: str, delta: int, reason: str = "") -> dict:
        """Bump current_loyalty by delta after a meaningful interaction.
        Suggested adjustments:
          +1 routine fair treatment / on-time pay
          +2 fair share of loot / saved their life
          +3 healed when wounded / public praise
          -1 wages late / harsh order
          -2 sent into mortal danger / share withheld
          -3 abandoned in combat / leader killed in front of them"""
        cfg = _c.load_campaign()
        key, npc = _resolve_npc(cfg, slug)
        if key is None:
            return {"error": f"Hireling '{slug}' not found."}
        if not npc.get("is_hireling"):
            return {"error": f"{npc.get('label', key)} is not a hireling."}

        before = int(npc.get("current_loyalty", 0))
        after  = before + int(delta)
        npc["current_loyalty"] = after
        _c.save_campaign(cfg)

        _c.append_event(cfg, {
            "type":  "loyalty_change",
            "slug":  key,
            "before": before,
            "after":  after,
            "notes":  reason or f"loyalty {before:+d} → {after:+d}",
        })

        return {"hireling": npc.get("label", key), "before": before, "after": after}
