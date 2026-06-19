"""Lore and history tools: search, timelines, quests, and session primers."""
import re
import json
from pathlib import Path
import _campaign as _c


def _read_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _excerpt(text: str, pos: int, radius: int = 100) -> str:
    """Return up to 2*radius chars centred on pos, with ellipsis markers."""
    start = max(0, pos - radius)
    end   = min(len(text), pos + radius)
    snippet = text[start:end].replace("\n", " ")
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + snippet + suffix


def register(mcp):

    @mcp.tool()
    def search_lore(query: str) -> dict:
        """Case-insensitive search across chronicle, character files, location files, and events.
        Returns up to 20 results with source and excerpt for each hit."""
        cfg   = _c.load_campaign()
        q     = query.lower()
        results: list[dict] = []

        def _search_text(text: str, source: str):
            for m in re.finditer(re.escape(q), text, re.IGNORECASE):
                if len(results) >= 20:
                    return
                line_no = text[:m.start()].count("\n") + 1
                results.append({
                    "source":  f"{source}:{line_no}",
                    "excerpt": _excerpt(text, m.start()),
                })

        # 1. Chronicle / session log
        log_file = cfg["_data_dir"] / cfg.get("session_log_file", "adventure_log.md")
        if log_file.exists():
            _search_text(_read_safe(log_file), log_file.name)

        # 2. Character files
        chars_dir = cfg["_data_dir"] / "characters"
        if chars_dir.exists():
            for md in sorted(chars_dir.rglob("*.md")):
                if len(results) >= 20:
                    break
                _search_text(_read_safe(md), f"characters/{md.name}")

        # 3. Location files (recursive)
        locs_dir = cfg["_data_dir"] / "locations"
        if locs_dir.exists():
            for md in sorted(locs_dir.rglob("*.md")):
                if len(results) >= 20:
                    break
                rel = md.relative_to(cfg["_data_dir"])
                _search_text(_read_safe(md), str(rel))

        # 4. Events JSON (search "notes", "result", "name" fields)
        for idx, event in enumerate(_c.load_events(cfg)):
            if len(results) >= 20:
                break
            for field in ("notes", "result", "name"):
                val = event.get(field, "")
                if val and q in str(val).lower():
                    results.append({
                        "source":  f"events.json[{idx}]",
                        "excerpt": str(val)[:200],
                    })
                    break  # one hit per event is enough

        return {"query": query, "results": results}

    @mcp.tool()
    def npc_history(slug: str) -> dict:
        """Return all events associated with an NPC slug, sorted by day then session."""
        cfg    = _c.load_campaign()
        events = [e for e in _c.load_events(cfg) if e.get("slug") == slug]
        events.sort(key=lambda e: (e.get("day", 0), e.get("session", 0)))
        return {"slug": slug, "events": events}

    @mcp.tool()
    def location_history(slug: str, area: str = "") -> dict:
        """Return all events for a location slug (and optional area), sorted by day.
        slug: location slug
        area: parent area slug, or empty for top-level"""
        cfg    = _c.load_campaign()
        events = [
            e for e in _c.load_events(cfg)
            if e.get("slug") == slug and e.get("area", "") == area
        ]
        events.sort(key=lambda e: (e.get("day", 0), e.get("session", 0)))
        return {"slug": slug, "area": area, "events": events}

    @mcp.tool()
    def active_quests() -> dict:
        """Return open quests. Prefers the structured campaign.json[quests]
        schema (use add_quest / update_quest); falls back to legacy
        quest_update events for older campaigns."""
        cfg = _c.load_campaign()
        structured = cfg.get("quests", {})

        quests = []
        if structured:
            for slug, q in structured.items():
                if q.get("status", "active") != "active":
                    continue
                latest_note = ""
                log = q.get("notes_log", [])
                if log:
                    latest_note = log[-1].get("text", "")
                quests.append({
                    "slug":   slug,
                    "title":  q.get("title", slug),
                    "scope":  q.get("scope", ""),
                    "stakes": q.get("stakes", ""),
                    "giver":  q.get("giver", ""),
                    "notes":  latest_note,
                    "day":    q.get("started_day", 0),
                })
            quests.sort(key=lambda q: q["day"])
            return {"quests": quests, "source": "structured"}

        # Legacy fallback
        events = _c.load_events(cfg)
        quest_events: dict[str, list[dict]] = {}
        for e in events:
            if e.get("type") == "quest_update":
                slug = e.get("slug", "")
                quest_events.setdefault(slug, []).append(e)
        for slug, evts in quest_events.items():
            completed = any(e.get("status") == "complete" for e in evts)
            if not completed:
                latest = max(evts, key=lambda e: (e.get("day", 0), e.get("session", 0)))
                quests.append({
                    "slug":  slug,
                    "notes": latest.get("notes", ""),
                    "day":   latest.get("day", 0),
                })
        quests.sort(key=lambda q: q["day"])
        return {"quests": quests, "source": "events"}

    @mcp.tool()
    def party_timeline(session_from: int = 1, session_to: int = 0) -> dict:
        """Return all events within a session range, sorted by day and session.
        session_from: first session to include (default 1)
        session_to: last session to include, 0 means no upper bound"""
        cfg    = _c.load_campaign()
        events = _c.load_events(cfg)

        filtered = []
        for e in events:
            s = e.get("session", 1)
            if s < session_from:
                continue
            if session_to and s > session_to:
                continue
            filtered.append(e)

        filtered.sort(key=lambda e: (e.get("day", 0), e.get("session", 0)))
        return {"events": filtered}

    @mcp.tool()
    def session_primer() -> dict:
        """Generate a session-start brief: current day/session, party status,
        last 3 events, active quests, and the most recent session heading from the chronicle."""
        cfg   = _c.load_campaign()
        state = _c.load_state(cfg)

        # --- Party status ---
        party = {}
        chars_cfg = cfg.get("characters", {})
        chars_state = state.get("characters", {})
        for key, char in chars_cfg.items():
            cs = chars_state.get(key, {})
            entry: dict = {
                "label":   char.get("label", key),
                "hp":      cs.get("hp", char.get("hp_max")),
                "hp_max":  char.get("hp_max"),
            }
            slots = cs.get("spell_slots")
            if slots:
                entry["spell_slots"] = slots
            party[key] = entry

        # --- Last 3 events ---
        events = _c.load_events(cfg)
        last_events = events[-3:] if len(events) >= 3 else events[:]

        # --- Active quests (inline logic, same as active_quests tool) ---
        quest_events: dict[str, list[dict]] = {}
        for e in events:
            if e.get("type") == "quest_update":
                slug = e.get("slug", "")
                quest_events.setdefault(slug, []).append(e)

        open_quests = []
        for slug, evts in quest_events.items():
            completed = any(e.get("status") == "complete" for e in evts)
            if not completed:
                latest = max(evts, key=lambda e: (e.get("day", 0), e.get("session", 0)))
                open_quests.append({
                    "slug":  slug,
                    "notes": latest.get("notes", ""),
                    "day":   latest.get("day", 0),
                })
        open_quests.sort(key=lambda q: q["day"])

        # --- Last session heading from chronicle ---
        log_file = cfg["_data_dir"] / cfg.get("session_log_file", "adventure_log.md")
        last_session_title = None
        if log_file.exists():
            text = _read_safe(log_file)
            # Match any ### heading (session entries typically use ### Session N — Title)
            headings = re.findall(r"^#{1,3}\s+(.+)$", text, re.MULTILINE)
            if headings:
                last_session_title = headings[-1].strip()

        # --- Active world clocks (urgent first) ---
        clocks = state.get("faction_clocks", [])
        active_clocks = sorted(
            [c for c in clocks if c.get("days_remaining", 0) > 0],
            key=lambda c: c.get("days_remaining", 999),
        )[:5]

        # --- Time of day ---
        h = state.get("current_hour", 6)
        m = state.get("current_minute", 0)
        time_str = f"{h:02d}:{m:02d}"

        # --- Audit findings from the prior session ---
        # Surfaces procedural lapses (missing reactions, freehand combat,
        # scope creep, etc.) at session start so the DM can address them
        # before adding new content. See tools/audit.py for the full
        # check list and CLAUDE.md "Audit" for usage guidance.
        current_session = state.get("current_session", 1)
        prior_session = max(1, current_session - 1)
        try:
            from tools.audit import _run_audit
            prior_audit = _run_audit(prior_session)
        except Exception as exc:  # pragma: no cover - audit must never break primer
            prior_audit = {"error": str(exc), "findings": []}

        # --- Narrative detail level (how much raw mechanical detail to expose
        # in player-facing prose; set from the dashboard, honoured every turn) ---
        try:
            from tools.dm_session import detail_setting
            narrative_detail = detail_setting()
        except Exception:
            narrative_detail = {"level": 2, "label": "Standard"}

        # --- Per-campaign instructions (binding canon/procedural constraint,
        # e.g. a module lock; injected into every turn's system prompt) ---
        try:
            from tools.dm_session import instructions_setting
            campaign_instructions = instructions_setting()
        except Exception:
            campaign_instructions = {"text": "", "enabled": True}

        return {
            "current_day":         state.get("current_day", 1),
            "current_time":        time_str,
            "current_session":     current_session,
            "current_weather":     state.get("current_weather", ""),
            "party":               party,
            "last_events":         last_events,
            "active_quests":       open_quests,
            "active_clocks":       active_clocks,
            "last_session_title":  last_session_title,
            "prior_session_audit": prior_audit,
            "narrative_detail":    narrative_detail,
            "campaign_instructions": campaign_instructions,
        }
