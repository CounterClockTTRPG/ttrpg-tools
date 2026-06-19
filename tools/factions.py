"""Faction layer and world clocks — the world keeps moving when PCs aren't watching.

Factions live in campaign.json under 'factions' (canonical identity, goals,
alignment). Their dynamic state — strength, reputation with the party,
running clocks — lives in state.json under 'faction_state' and 'faction_clocks'.

A faction clock is a countdown: 'cult ritual completes in 30 days unless
interrupted'. tick_world(days) advances every clock; expirations are returned
in the digest so the DM can fire the consequence.
"""
from datetime import datetime
import _campaign as _c


def _norm_slug(slug: str) -> str:
    return slug.lower().strip().replace(" ", "_")


def _faction_state(state: dict, slug: str) -> dict:
    """Get-or-create the dynamic state record for a faction. Mutates `state`;
    callers that *write* should follow up with save_state. Read-only callers
    should use _faction_state_view instead."""
    return state.setdefault("faction_state", {}).setdefault(slug, {
        "strength":   100,
        "reputation": 0,
        "discovered": False,
    })


_DEFAULT_FACTION_STATE = {"strength": 100, "reputation": 0, "discovered": False}


def _faction_state_view(state: dict, slug: str) -> dict:
    """Read-only view of faction state — returns defaults without mutating
    `state` or producing a needless save."""
    return state.get("faction_state", {}).get(slug, _DEFAULT_FACTION_STATE)


def register(mcp):

    @mcp.tool()
    def add_faction(
        slug:       str,
        name:       str,
        alignment:  str = "",
        goals:      str = "",
        scope:      str = "local",
        known_to_party: bool = False,
    ) -> dict:
        """Register a faction (organisation, cult, guild, noble house, monster tribe).
        slug:           canonical id (e.g. 'cult_of_iuz', 'thieves_guild_phlan')
        name:           display name
        alignment:      LE, CN, NG, etc.
        goals:          one-line summary of what they're after
        scope:          local | regional | continental — should match stake size
        known_to_party: True if the party has heard of them in the fiction"""
        cfg = _c.load_campaign()
        slug = _norm_slug(slug)
        factions = cfg.setdefault("factions", {})
        if slug in factions:
            return {"error": f"Faction '{slug}' already exists. Use set_faction to modify."}

        factions[slug] = {
            "name":           name,
            "alignment":      alignment,
            "goals":          goals,
            "scope":          scope,
            "known_to_party": bool(known_to_party),
        }
        _c.save_campaign(cfg)
        # Seed dynamic state
        state = _c.load_state(cfg)
        _faction_state(state, slug)
        _c.save_state(cfg, state)
        return {"slug": slug, "added": True}

    @mcp.tool()
    def set_faction(
        slug:       str,
        name:       str = "",
        alignment:  str = "",
        goals:      str = "",
        scope:      str = "",
        known_to_party: bool = None,
        strength:   int = -1,
        reputation: int = -999,
    ) -> dict:
        """Update one or more fields on a faction. Empty / sentinel = leave alone.
        strength: 0-100 (% of original capacity).
        reputation: party's standing (-100 to +100). Modifies reaction rolls
                    when an NPC of this faction is encountered (see #16)."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        slug = _norm_slug(slug)

        f = cfg.get("factions", {}).get(slug)
        if not f:
            return {"error": f"Faction '{slug}' not found."}

        if name:      f["name"]      = name
        if alignment: f["alignment"] = alignment
        if goals:     f["goals"]     = goals
        if scope:     f["scope"]     = scope
        if known_to_party is not None:
            f["known_to_party"] = bool(known_to_party)

        fs = _faction_state(state, slug)
        if strength >= 0:
            fs["strength"] = max(0, min(100, int(strength)))
        if reputation > -999:
            fs["reputation"] = max(-100, min(100, int(reputation)))

        _c.save_campaign(cfg)
        _c.save_state(cfg, state)
        return {"slug": slug, "faction": f, "state": fs}

    @mcp.tool()
    def list_factions(known_only: bool = False) -> dict:
        """List factions and their current state.
        known_only: if True, only those flagged known_to_party (avoids leaking
                    hidden factions into player-visible output)."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        factions = cfg.get("factions", {})
        out = []
        for slug, f in factions.items():
            if known_only and not f.get("known_to_party"):
                continue
            fs = _faction_state_view(state, slug)
            out.append({
                "slug":           slug,
                "name":           f.get("name", slug),
                "alignment":      f.get("alignment", ""),
                "goals":          f.get("goals", ""),
                "scope":          f.get("scope", "local"),
                "known_to_party": f.get("known_to_party", False),
                "strength":       fs.get("strength", 100),
                "reputation":     fs.get("reputation", 0),
            })
        return {"factions": out}

    @mcp.tool()
    def change_reputation(faction: str, delta: int, reason: str = "") -> dict:
        """Adjust the party's standing with a faction by delta (-100..+100 cap).
        Reputation auto-applies to reaction(npc=slug) when the NPC is linked to
        this faction (rep / 25 → modifier; ±100 = ±4 to the 2d10 reaction roll).

        Suggested adjustments:
          +5 routine cooperation / minor service
          +10 gift presented / shared meal / public favour
          +20 significant favour (recover lost asset, rescue member)
          +40 saved the faction's leader / prevented major loss
          -5 minor slight / failed contract
          -10 broke a promise / refused a request
          -20 worked against them visibly
          -40 killed a member / robbed a stronghold
          -80 publicly humiliated or financially ruined them"""
        cfg   = _c.load_campaign()
        state = _c.load_state(cfg)
        slug  = _norm_slug(faction)

        if slug not in cfg.get("factions", {}):
            return {"error": f"Faction '{faction}' not registered. Use add_faction first."}

        fs = _faction_state(state, slug)
        before = int(fs.get("reputation", 0))
        after  = max(-100, min(100, before + int(delta)))
        fs["reputation"] = after
        _c.save_state(cfg, state)

        _c.append_event(cfg, {
            "type":   "reputation_change",
            "slug":   slug,
            "before": before,
            "after":  after,
            "notes":  reason or f"reputation {before:+d} → {after:+d}",
        })

        return {
            "faction":   slug,
            "before":    before,
            "after":     after,
            "modifier_applied_to_reaction": after // 25,
        }

    @mcp.tool()
    def add_faction_clock(
        label:       str,
        days:        int,
        faction:     str = "",
        on_complete: str = "",
    ) -> dict:
        """Add a countdown clock. Use for any 'X happens in N days unless interrupted':
        rituals, sieges, ransom deadlines, plague spread, harvest, NPC travel.

        label:       short human-readable description ('cult finishes ritual')
        days:        in-game days until completion
        faction:     optional slug — links the clock to a faction's ledger
        on_complete: optional one-line note to remind the DM what happens at zero
                     (text only — no automatic firing; tick_world surfaces it)"""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        clocks = state.setdefault("faction_clocks", [])
        next_id = (max((c.get("id", 0) for c in clocks), default=0)) + 1

        clock = {
            "id":              next_id,
            "label":           label,
            "days_remaining":  int(days),
            "faction":         _norm_slug(faction) if faction else "",
            "on_complete":     on_complete,
            "started_day":     state.get("current_day", 1),
        }
        clocks.append(clock)
        _c.save_state(cfg, state)
        return {"id": next_id, "clock": clock}

    @mcp.tool()
    def list_clocks(active_only: bool = True) -> dict:
        """List world clocks. active_only=True hides expired (≤0) clocks."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        clocks = state.get("faction_clocks", [])
        if active_only:
            clocks = [c for c in clocks if c.get("days_remaining", 0) > 0]
        clocks_sorted = sorted(clocks, key=lambda c: c.get("days_remaining", 0))
        return {"clocks": clocks_sorted}

    @mcp.tool()
    def remove_clock(clock_id: int) -> dict:
        """Remove a clock by id (party interrupted the threat, or it became moot)."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        clocks = state.get("faction_clocks", [])
        for i, c in enumerate(clocks):
            if c.get("id") == clock_id:
                removed = clocks.pop(i)
                _c.save_state(cfg, state)
                return {"removed": removed}
        return {"error": f"No clock with id {clock_id}."}

    @mcp.tool()
    def tick_world(days: int = 1) -> dict:
        """Advance every faction clock by N days. Returns a digest of clocks that
        have completed (days_remaining ≤ 0) and clocks now within 7 days of completion.
        Does NOT advance the calendar — call advance_calendar separately so you can
        narrate the time passing while inspecting the clock effects.

        Use at session start, after long rests, after extended travel, or whenever
        the party stops engaging with a region for several in-game days."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        clocks = state.get("faction_clocks", [])

        completed = []
        urgent    = []
        ongoing   = []

        for c in clocks:
            before = c.get("days_remaining", 0)
            after  = before - int(days)
            c["days_remaining"] = after
            if before > 0 and after <= 0:
                completed.append(dict(c))
            elif 0 < after <= 7:
                urgent.append(dict(c))
            elif after > 7:
                ongoing.append(dict(c))

        _c.save_state(cfg, state)

        return {
            "days_advanced": days,
            "completed":     completed,   # action required: narrate consequences
            "urgent":        urgent,      # within a week — pressure rising
            "ongoing":       ongoing,     # background timers
        }
