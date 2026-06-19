"""Hidden DM-only state — facts, motives, and notes the LLM may recall but must not narrate.

Stored in campaigns/<slug>/secrets.json as a list of records:
    {id, day, session, timestamp, text, tags, related_to}

Use cases:
- Hidden NPC motives ("the innkeeper reports to the cult")
- Foreshadowed-but-not-yet-revealed truths
- Failed-perception rolls the player shouldn't know about
- Rumors flagged with their actual truth value
"""
import json
from datetime import datetime
from pathlib import Path
import _campaign as _c


def _secrets_path(cfg: dict) -> Path:
    return cfg["_dir"] / "secrets.json"


def _load(cfg: dict) -> list:
    p = _secrets_path(cfg)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return []


def _save(cfg: dict, records: list):
    _c.atomic_write_text(
        _secrets_path(cfg),
        json.dumps(records, indent=2, ensure_ascii=False),
    )


def register(mcp):

    @mcp.tool()
    def dm_note(text: str, tags: list = None, related_to: str = "") -> dict:
        """Record a hidden DM-only note. NEVER narrate the contents to the player.
        Use for hidden NPC motives, foreshadowed truths, secrets the player has not learned,
        or rolls the player must not see (failed perception, true rumour values, etc.).

        text:        the secret fact, in plain language
        tags:        optional list of free-form tags ('motive', 'rumour', 'foreshadow', 'plot')
        related_to:  optional slug of an NPC, location, or quest the secret concerns

        Returns the record id for later reference."""
        cfg   = _c.load_campaign()
        state = _c.load_state(cfg)
        recs  = _load(cfg)

        next_id = (max((r.get("id", 0) for r in recs), default=0)) + 1
        rec = {
            "id":         next_id,
            "day":        state.get("current_day", 1),
            "session":    state.get("current_session", 1),
            "timestamp":  datetime.now().isoformat(timespec="seconds"),
            "text":       text,
            "tags":       list(tags or []),
            "related_to": related_to,
        }
        recs.append(rec)
        _save(cfg, recs)
        return {"id": next_id, "stored": True}

    @mcp.tool()
    def dm_secrets(tag: str = "", related_to: str = "", contains: str = "") -> dict:
        """Recall hidden DM notes. Filters are AND-combined; all empty = return all.

        tag:        match any record whose tags include this string (case-insensitive)
        related_to: match records whose related_to equals this slug
        contains:   substring match against the note text (case-insensitive)

        Reminder: these are private. Surface them in your reasoning, never to the player."""
        cfg = _c.load_campaign()
        recs = _load(cfg)

        out = []
        tag_l = tag.lower().strip()
        rel_l = related_to.lower().strip()
        sub_l = contains.lower().strip()
        for r in recs:
            if tag_l and not any(tag_l in t.lower() for t in r.get("tags", [])):
                continue
            if rel_l and r.get("related_to", "").lower() != rel_l:
                continue
            if sub_l and sub_l not in r.get("text", "").lower():
                continue
            out.append(r)
        return {"count": len(out), "secrets": out}

    @mcp.tool()
    def dm_secret_update(secret_id: int, text: str = "", tags: list = None,
                         related_to: str = "", revealed: bool = False) -> dict:
        """Edit or reveal a hidden note.
        Pass revealed=True to mark a secret as no longer hidden (it then becomes searchable
        via search_lore as a normal event). Other fields, when given, overwrite the existing
        value; pass empty to leave alone."""
        cfg = _c.load_campaign()
        recs = _load(cfg)

        for r in recs:
            if r.get("id") == secret_id:
                if text:
                    r["text"] = text
                if tags is not None:
                    r["tags"] = list(tags)
                if related_to:
                    r["related_to"] = related_to
                if revealed:
                    r["revealed"] = True
                    r["revealed_day"] = _c.load_state(cfg).get("current_day", 1)
                    # Also append a public event so search_lore can surface it
                    _c.append_event(cfg, {
                        "type":  "secret_revealed",
                        "slug":  r.get("related_to", ""),
                        "notes": r.get("text", ""),
                    })
                _save(cfg, recs)
                return {"id": secret_id, "updated": True, "revealed": r.get("revealed", False)}

        return {"error": f"No secret with id {secret_id}."}
