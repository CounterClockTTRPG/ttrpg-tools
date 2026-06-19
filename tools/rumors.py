"""Rumor / gossip table — distinguishes 'what NPCs say' from 'what is true'.

Rumors live in campaign.json under 'rumors' as a list:
    [{id, text, truth, location?, faction?, npc?, source_npc?}, ...]

Truth tiers:
    true         — the rumor is correct
    partly_true  — kernel of truth, distorted detail (e.g. wrong number, wrong actor)
    false        — wrong; usually because the source got it wrong, not lied
    fabrication  — deliberately spread by someone with an agenda

The DM seeds rumors when entering a settlement (or on the fly during play).
gossip() returns a random subset for an NPC to relate, surfacing the truth tier
to the DM (NEVER tell the player the tier — narrate as gossip).
"""
import random
import _campaign as _c


_TRUTH_TIERS = ("true", "partly_true", "false", "fabrication")


def register(mcp):

    @mcp.tool()
    def add_rumor(
        text:        str,
        truth:       str = "partly_true",
        location:    str = "",
        faction:     str = "",
        npc:         str = "",
        source_npc:  str = "",
    ) -> dict:
        """Store a rumor for later distribution. The text should be the rumor as
        an NPC would tell it (not the underlying truth).

        text:       the rumor as spoken
        truth:      one of true | partly_true | false | fabrication
        location:   slug — restricts gossip() to NPCs in this area
        faction:    slug — rumor concerns this faction (gossip-by-topic)
        npc:        slug — the rumor concerns this NPC
        source_npc: slug — the NPC most likely to spread it (innkeeper, sage, etc.)

        If the rumor is fabrication, ALSO call dm_note to record who is
        spreading it and why."""
        cfg = _c.load_campaign()
        if truth not in _TRUTH_TIERS:
            return {"error": f"Unknown truth tier '{truth}'. Use: {', '.join(_TRUTH_TIERS)}."}

        rumors = cfg.setdefault("rumors", [])
        next_id = (max((r.get("id", 0) for r in rumors), default=0)) + 1
        rec = {
            "id":         next_id,
            "text":       text,
            "truth":      truth,
            "location":   location.lower().strip(),
            "faction":    faction.lower().strip(),
            "npc":        npc.lower().strip(),
            "source_npc": source_npc.lower().strip(),
        }
        rumors.append(rec)
        _c.save_campaign(cfg)
        return {"id": next_id, "stored": True}

    @mcp.tool()
    def gossip(
        location:   str = "",
        faction:    str = "",
        npc:        str = "",
        source_npc: str = "",
        count:      int = 3,
    ) -> dict:
        """Draw N rumors matching the filters. Empty filters = unrestricted.
        Use when the party visits a tavern, hires a sage, presses a contact.
        Returns the rumors WITH their truth tier — that is for your reasoning,
        not the player's ears. Narrate the gossip as a believable NPC would
        speak it; never reveal the tier.

        Filters AND-combine. count clamps 1–10."""
        cfg = _c.load_campaign()
        rumors = cfg.get("rumors", [])
        if not rumors:
            return {
                "rumors": [],
                "hint":   "No rumors stored. Add some with add_rumor(text, truth, ...) "
                          "before drawing gossip — or invent one and add it now.",
            }

        loc = location.lower().strip()
        fac = faction.lower().strip()
        nps = npc.lower().strip()
        src = source_npc.lower().strip()

        pool = []
        for r in rumors:
            if loc and r.get("location") and r["location"] != loc:
                continue
            if fac and r.get("faction") and r["faction"] != fac:
                continue
            if nps and r.get("npc") and r["npc"] != nps:
                continue
            if src and r.get("source_npc") and r["source_npc"] != src:
                continue
            pool.append(r)

        if not pool:
            # Soften filters: drop NPC then faction then location
            pool = [r for r in rumors if not (loc and r.get("location") and r["location"] != loc)]
            if not pool:
                pool = list(rumors)

        n = max(1, min(int(count), 10, len(pool)))
        chosen = random.sample(pool, n)

        return {
            "count":  len(chosen),
            "rumors": chosen,
            "reminder": "Truth tier is private. Narrate as natural speech; the player "
                        "should not be able to tell true from false without effort.",
        }

    @mcp.tool()
    def list_rumors(truth: str = "", location: str = "") -> dict:
        """List stored rumors (DM reference). Filter by truth tier or location."""
        cfg = _c.load_campaign()
        rumors = cfg.get("rumors", [])
        out = []
        for r in rumors:
            if truth and r.get("truth") != truth:
                continue
            if location and r.get("location") != location.lower().strip():
                continue
            out.append(r)
        return {"count": len(out), "rumors": out}
