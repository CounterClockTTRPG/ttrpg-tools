"""Quest records — structured schema with scope/stakes for fairness checks.

Quests live in campaign.json under 'quests':
    {slug: {
        title, giver, scope, stakes, status,
        started_day, completed_day?,
        related_npcs[], related_factions[], related_locations[],
        notes_log: [{day, text}]
    }}

scope values: 'local' | 'regional' | 'continental'
status values: 'active' | 'paused' | 'complete' | 'failed'

CLAUDE.md mandates scope-by-level: L1-3 local only, L4-7 regional acceptable,
L8+ continental plausible. add_quest warns when scope exceeds party level.
"""
from datetime import datetime
import _campaign as _c

_VALID_SCOPE  = ("local", "regional", "continental")
_VALID_STATUS = ("active", "paused", "complete", "failed")


def _avg_party_level(cfg: dict) -> float:
    chars = cfg.get("characters", {})
    if not chars:
        return 1.0
    levels = [int(c.get("level", 1)) for c in chars.values()]
    return sum(levels) / len(levels)


def _scope_warning(scope: str, avg_level: float) -> str:
    if scope == "regional" and avg_level < 4:
        return f"Regional scope on a party averaging level {avg_level:.1f}. CLAUDE.md says regional is L4+."
    if scope == "continental" and avg_level < 8:
        return f"Continental scope on a party averaging level {avg_level:.1f}. CLAUDE.md says continental is L8+. Demote scope or wait for the party to engage."
    return ""


def register(mcp):

    @mcp.tool()
    def add_quest(
        slug:     str,
        title:    str,
        giver:    str = "",
        scope:    str = "local",
        stakes:   str = "",
        related_npcs:      list = None,
        related_factions:  list = None,
        related_locations: list = None,
    ) -> dict:
        """Open a new quest with explicit scope. Scope must match party level
        (CLAUDE.md): local for L1-3, regional for L4+, continental for L8+.

        slug:    canonical id ('rescue_old_man_henrik')
        title:   display name
        giver:   NPC slug who issued the quest (or '' for ambient)
        scope:   local | regional | continental
        stakes:  one-line description of what happens if party fails or ignores
        related_*: lists of slugs the quest concerns

        Returns a 'warning' field when scope mismatches party level."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        if scope not in _VALID_SCOPE:
            return {"error": f"Unknown scope '{scope}'. Use: {', '.join(_VALID_SCOPE)}."}

        slug = slug.lower().strip().replace(" ", "_")
        quests = cfg.setdefault("quests", {})
        if slug in quests:
            return {"error": f"Quest '{slug}' already exists. Use update_quest."}

        avg = _avg_party_level(cfg)
        warning = _scope_warning(scope, avg)

        quests[slug] = {
            "title":   title,
            "giver":   giver.lower().strip(),
            "scope":   scope,
            "stakes":  stakes,
            "status":  "active",
            "started_day":  state.get("current_day", 1),
            "related_npcs":      list(related_npcs or []),
            "related_factions":  list(related_factions or []),
            "related_locations": list(related_locations or []),
            "notes_log": [],
        }
        _c.save_campaign(cfg)

        _c.append_event(cfg, {
            "type":  "quest_added",
            "slug":  slug,
            "name":  title,
            "notes": f"{scope} — {stakes}",
        })

        out = {
            "slug":     slug,
            "title":    title,
            "scope":    scope,
            "started":  state.get("current_day", 1),
        }
        if warning:
            out["warning"] = warning
        return out

    @mcp.tool()
    def update_quest(slug: str, notes: str = "", status: str = "") -> dict:
        """Append a progress note and/or change status of a quest.
        status: empty = no change; otherwise active|paused|complete|failed."""
        cfg   = _c.load_campaign()
        state = _c.load_state(cfg)
        slug = slug.lower().strip()
        quests = cfg.setdefault("quests", {})
        q = quests.get(slug)
        if q is None:
            return {"error": f"Quest '{slug}' not found."}

        if status:
            if status not in _VALID_STATUS:
                return {"error": f"Bad status '{status}'. Use: {', '.join(_VALID_STATUS)}."}
            q["status"] = status
            if status in ("complete", "failed"):
                q["completed_day"] = state.get("current_day", 1)

        if notes:
            q.setdefault("notes_log", []).append({
                "day":  state.get("current_day", 1),
                "text": notes,
            })

        _c.save_campaign(cfg)
        _c.append_event(cfg, {
            "type":  "quest_update",
            "slug":  slug,
            "status": q["status"],
            "notes": notes,
        })
        return {"slug": slug, "status": q["status"], "title": q.get("title", slug)}

    @mcp.tool()
    def complete_quest(slug: str, outcome: str = "") -> dict:
        """Mark a quest complete with an outcome note. Routes through update_quest."""
        return update_quest(slug=slug, status="complete", notes=outcome)

    @mcp.tool()
    def fail_quest(slug: str, reason: str = "") -> dict:
        """Mark a quest failed (irrecoverable) with a reason note."""
        return update_quest(slug=slug, status="failed", notes=reason)

    @mcp.tool()
    def quest_status(slug: str = "") -> dict:
        """Return one quest's full record, or all active quests if slug is empty."""
        cfg = _c.load_campaign()
        quests = cfg.get("quests", {})
        if slug:
            q = quests.get(slug.lower().strip())
            if q is None:
                return {"error": f"Quest '{slug}' not found."}
            return {"slug": slug.lower().strip(), **q}

        avg = _avg_party_level(cfg)
        active = []
        for k, q in quests.items():
            if q.get("status") == "active":
                entry = {"slug": k, **q}
                w = _scope_warning(q.get("scope", "local"), avg)
                if w:
                    entry["scope_warning"] = w
                active.append(entry)
        return {"active": active, "avg_party_level": avg}
