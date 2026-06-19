"""Pre-turn check — populates decision-time state before the DM narrates.

Returns a structured checklist (an enforcement reminder of the Hard
Procedural Constraints in CLAUDE.md) plus the current scene state:
which named NPCs are recently in scene, which have not had a
``reaction_roll`` yet this session, which active effects are running
on whom, whether a combat is in flight.

Designed to be **silent-read**: the DM uses the returned state as
decision context, never echoes the checklist into player-facing prose.

Fire targets (from CLAUDE.md):
  - Multi-NPC scenes where consensus or disagreement is about to happen
  - Player proposes a multi-step plan
  - First contact with a named NPC
  - Any moment that would otherwise narrate an uncertain outcome

Do NOT fire on:
  - Pure descriptive prose ("the morning mist lifts off the river")
  - Mid-combat turns (combat pipeline handles its own ordering)
  - Atmospheric or transitional beats with no uncertainty to resolve
"""
import json
from pathlib import Path

import _campaign as _c


_CHECKLIST = [
    "DIE-FIRST. Have you rolled the resolution mechanic *before* writing the outcome? If the outcome is already in prose, stop — call the tool first.",
    "NPC CONSENSUS. If 2+ named NPCs are about to agree, roll reaction() per NPC with interest-cost modifiers. Three+ NPCs with conflicting interests must produce disagreement unless their interests genuinely align.",
    "CLEVERNESS != OUTCOME. A clever player plan earns a roll, not a guaranteed success. The DM's reward for cleverness is allowing the attempt with a realistic chance.",
    "SPELL MECHANICS. Every spell named in a player plan needs spell_lookup() before the plan is ratified. Don't trust the spell to do what the player thinks it does without checking.",
    "FOURTH WALL. OOC choice prompts (the '(do X, or Y, or Z?)' menu at the end of a turn) must contain only information the *character* has. No hidden monsters, no module canon, no unknown threats named.",
    "FREE INTEL. Is the player about to know something they haven't earned via dice, in-fiction discovery, or NPC disclosure? Familiars report sensed data, not DM-omniscient data.",
    "OPPOSITION ACTS. Intelligent enemies (INT 10+) detect threats and respond proportionally within the plan window. They are not passive while the player plans.",
    "RESOURCES. Count spell slots, torches, rations. Scarcity is a mechanical constraint, not flavour.",
]


def _recent_npc_state(cfg: dict, current_session: int) -> dict:
    """Walk the session's events to find: NPCs recently in scene, and which
    of them have had a reaction_roll logged this session."""
    events = _c.load_events(cfg)
    in_session = [e for e in events if e.get("session") == current_session]

    # Last ~30 events for scene scoping
    recent_window = in_session[-30:]
    recent_slugs: list[str] = []
    for e in recent_window:
        slug = e.get("slug")
        if not slug:
            continue
        if e.get("type") in ("npc_met", "npc_interaction") and slug not in recent_slugs:
            recent_slugs.append(slug)

    # Reactions rolled across the whole session — once rolled, an NPC's
    # disposition is on the record; don't keep flagging.
    reacted = {e.get("slug") for e in in_session if e.get("type") == "reaction_roll"}

    unreacted = [s for s in recent_slugs if s not in reacted]

    # Enrich with stored disposition + label so the DM can see at a
    # glance which way each NPC leans before the next decision.
    npcs_cfg = cfg.get("npcs", {})
    def _label(slug: str) -> dict:
        d = npcs_cfg.get(slug, {})
        return {
            "slug":        slug,
            "label":       d.get("label", slug),
            "disposition": int(d.get("disposition", 0)),
            "faction":     d.get("faction", ""),
        }

    return {
        "npcs_recently_in_scene": [_label(s) for s in recent_slugs],
        "npcs_without_reaction_this_session": [_label(s) for s in unreacted],
    }


def _combat_state(cfg: dict) -> dict:
    """Return active-combat snapshot — round number, current actor, and any
    durational effects on combatants. Empty when no combat is in flight."""
    path = cfg["_data_dir"] / "combat_state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"in_combat": False}

    if not data.get("active"):
        return {"in_combat": False}

    effects = []
    for c in data.get("combatants", []) or []:
        for fx in c.get("effects", []) or []:
            effects.append({
                "target":       c.get("name"),
                "name":         fx.get("name"),
                "rounds_left":  fx.get("duration_rounds"),
            })

    return {
        "in_combat": True,
        "round":     data.get("round"),
        "current":   data.get("current"),
        "active_effects": effects,
    }


def register(mcp):

    @mcp.tool()
    def pre_turn_check(situation: str = "") -> dict:
        """Pre-narration audit. Call before any DM response that resolves
        uncertainty: NPC reactions, player plan outcomes, skill checks,
        saves, multi-NPC consensus moments, first-contact NPCs.

        Returns a structured checklist plus the current scene state.

        DO NOT echo the checklist into player-facing prose. The tool
        exists to populate your decision-time context, not to give the
        player a meta-monologue. Read it silently. If a turn would only
        produce a checklist acknowledgment with no real narration,
        produce no text at all and just make the tool calls the check
        revealed are needed.

        See CLAUDE.md "Pre-Turn Check" and "Hard Procedural Constraints"
        for the full rule set this enforces.

        Args:
          situation: short free-text label of the decision point
                     ("council vote on flood plan", "Pippa scouts the
                     wall", "first contact with the bishop"). Stored on
                     the response so the next audit can correlate.
        """
        cfg     = _c.load_campaign()
        state   = _c.load_state(cfg)
        current = int(state.get("current_session", 1))

        npc_state = _recent_npc_state(cfg, current)
        combat    = _combat_state(cfg)

        return {
            "situation":           situation,
            "current_session":     current,
            "before_you_narrate":  _CHECKLIST,
            "session_state": {
                **npc_state,
                "combat": combat,
            },
        }
