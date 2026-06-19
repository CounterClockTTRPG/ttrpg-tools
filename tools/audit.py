"""Self-policing audit — scan events.json and the latest DM prose for
procedural lapses.

Catches:
  - Procedural lapses (NPCs without reactions, freehand combat, scope creep,
    sparse encounter checks) from the events.jsonl event stream.
  - Fourth-wall and railroading lapses (module room IDs leaked in
    narrative, hidden features named in OOC prompts, directive language,
    DM-omniscience leaks) from the latest DM response text.

Run mid-session, at session end, or — automatically — at the start of
each session via ``session_primer``, and again on every turn end via
``tools.dm_session._audit_after_turn``.
"""
import re

import _campaign as _c


# ---------- Prose-pattern detectors ------------------------------------
# These run on the latest DM response text to catch fourth-wall and
# railroading violations CLAUDE.md mandates against. Each detector is a
# regex tuned to its specific failure mode; the prose audit reports the
# matched span so the DM can see what exactly tripped it.

# Module room IDs: single uppercase letter (T/D/K/S/P/M/C) + digits.
# UK4 uses T1-T21, D1-D11, K1-K12, P1-P4, S1-S2, M, C1-C7. These should
# NEVER appear in player-facing narrative — they are DM-side labels.
_MODULE_ROOM_RE = re.compile(r"\b([TDKSPMC])([0-9]{1,3})\b")

# "Hidden X" — naming a hidden feature in an OOC choice prompt tells the
# player the feature is there, defeating the discovery.
_HIDDEN_FEATURE_RE = re.compile(
    r"\b(hidden|secret|concealed|disguised)\s+"
    r"(door|doorway|passage|passageway|trap|chamber|trigger|switch|lever|"
    r"compartment|stair|stairs|panel|button|plate|alcove|niche|tunnel|exit|"
    r"mechanism|catch|latch)\b",
    re.IGNORECASE,
)

# Directive language: "obvious tool", "would be wise to", etc. These
# steer player decisions instead of leaving them open.
_DIRECTIVE_RE = re.compile(
    r"\b(?:obvious|clearly|naturally|of course|would be wise|wisest to|"
    r"best (?:choice|option|approach|tool|move)|right (?:choice|tool|move)|"
    r"easiest|simplest|safest|smartest|sensible)\b\s+"
    r"(?:tool|choice|approach|way|option|move)?",
    re.IGNORECASE,
)

# Negative-knowledge leak: stating absence of threat implies DM-omniscience
# the party hasn't earned via search/perception/etc.
_NEGATIVE_KNOWLEDGE_RE = re.compile(
    r"\b(?:no (?:other|further|more|additional|remaining) "
    r"(?:trap|danger|threat|enemy|inhabitant|surprise|hostile|creature|monster)s?|"
    r"(?:the )?(?:chamber|room|area|hall|passage|corridor) is "
    r"(?:safe|empty|clear|secure|unoccupied)|"
    r"nothing else (?:here|in (?:the|this) (?:chamber|room|area|hall)))",
    re.IGNORECASE,
)

# Last parenthesized block in the message — heuristic for the OOC
# choice prompt (the "(do X, or Y, or Z?)" at the end of a DM reply).
_OOC_PROMPT_RE = re.compile(r"\(([^()]{30,800})\)\s*$", re.DOTALL)


def _extract_ooc_prompt(text: str) -> str | None:
    """Return the trailing OOC choice prompt if the message ends with one,
    else None. Heuristic: a parenthesized block of 30+ chars at the very
    end that contains a question mark or the word 'or'."""
    if not text:
        return None
    m = _OOC_PROMPT_RE.search(text.rstrip())
    if not m:
        return None
    candidate = m.group(1).strip()
    low = candidate.lower()
    if "?" in candidate or " or " in low:
        return candidate
    return None


def _scan_dm_prose(text: str) -> list[dict]:
    """Scan the latest DM response for fourth-wall and railroading lapses.
    Returns a list of finding dicts in the same shape as the event-based
    checks."""
    if not text:
        return []

    findings: list[dict] = []

    # Module room IDs leaked in narrative — always a lapse, anywhere
    # in the text. Some false positives possible (e.g. AC values like
    # "AC 5"), so the regex is restricted to single uppercase prefixes
    # that match published-module conventions, and we filter out a few
    # common acronyms.
    seen_room_ids: set[str] = set()
    for m in _MODULE_ROOM_RE.finditer(text):
        prefix, num = m.group(1), m.group(2)
        ident = f"{prefix}{num}"
        # Skip common false positives: PCs/STR/DEX bands etc don't
        # match the pattern, but a few abbreviations could. Restrict
        # to numbers >= 1.
        if int(num) < 1 or int(num) > 999:
            continue
        if ident in seen_room_ids:
            continue
        seen_room_ids.add(ident)
        findings.append({
            "severity": "lapse",
            "kind":     "module_room_id_in_prose",
            "slug":     ident,
            "fix":      f"'{ident}' appears in player-facing narrative. Module room IDs are DM-only labels; the party doesn't know rooms have those names. Rewrite the prose using in-fiction descriptors (\"the antechamber\", \"the green-statue chamber\", etc.).",
        })

    # The remaining checks focus on the trailing OOC choice prompt
    # (Hard Constraint #5, the worked anti-pattern in CLAUDE.md). If
    # there's no recognisable OOC prompt, skip them — the heuristic
    # would produce too many false positives on narrative prose.
    ooc = _extract_ooc_prompt(text)
    if ooc:
        for m in _HIDDEN_FEATURE_RE.finditer(ooc):
            findings.append({
                "severity": "lapse",
                "kind":     "hidden_feature_named_in_ooc",
                "slug":     m.group(0),
                "fix":      f"OOC choice prompt names '{m.group(0)}' — that's DM-only knowledge being handed to the player. Rewrite the choice without naming what's hidden (CLAUDE.md Hard Constraint #5).",
            })
        for m in _DIRECTIVE_RE.finditer(ooc):
            phrase = m.group(0).strip()
            findings.append({
                "severity": "warning",
                "kind":     "directive_in_ooc_prompt",
                "slug":     phrase,
                "fix":      f"OOC prompt uses directive language ('{phrase}') — steers the player toward the DM's preferred answer. Let the choice be open (CLAUDE.md Hard Constraint #5).",
            })
        for m in _NEGATIVE_KNOWLEDGE_RE.finditer(ooc):
            phrase = m.group(0).strip()
            findings.append({
                "severity": "warning",
                "kind":     "negative_knowledge_in_ooc",
                "slug":     phrase[:60],
                "fix":      f"OOC prompt implies DM-knowledge ('{phrase}') — the party doesn't know what's absent until they search. Let absence be discovered, not announced.",
            })

    return findings


def _avg_party_level(cfg: dict) -> float:
    chars = cfg.get("characters", {})
    if not chars:
        return 1.0
    return sum(int(c.get("level", 1)) for c in chars.values()) / len(chars)


def _run_audit(target_session: int, dm_text: str | None = None) -> dict:
    """Pure function (no MCP wrapping). Used internally by session_primer
    to surface prior-session findings without going through the tool layer.

    Args:
        target_session: session id to audit (0/None falls back to current)
        dm_text:        latest DM response text. When supplied, prose-pattern
                        checks fire too (fourth-wall, directive language,
                        module room IDs in narrative). The post-turn hook in
                        ``tools.dm_session`` passes this in via
                        ``_dm.last_dm_text()`` so the dashboard panel
                        surfaces leaks as they happen.
    """
    cfg    = _c.load_campaign()
    state  = _c.load_state(cfg)
    events = _c.load_events(cfg)

    in_session = [e for e in events if e.get("session") == target_session]

    # PC roster — these are party members, not NPCs to be reacted to. The
    # update_character tool logs `npc_interaction` for anyone whose file
    # changes, PCs included, so without this filter the audit flags PCs as
    # needing reaction rolls (which is wrong: the party doesn't roll
    # reactions at itself).
    pc_slugs = set(cfg.get("characters", {}).keys())

    # Establishment signals — count any of these as "the relationship is
    # already shaped, no need to keep flagging missing-reaction." All three
    # are checked across ALL sessions (not just the current one), because a
    # reaction rolled in session 2 should not still flag in session 4.
    reacted_ever  = {e.get("slug") for e in events if e.get("type") == "reaction_roll"}
    dispositioned = {e.get("slug") for e in events if e.get("type") == "disposition_change"}
    has_stored_disposition = {
        slug for slug, npc in (cfg.get("npcs") or {}).items()
        if int((npc or {}).get("disposition", 0)) != 0
    }
    established = reacted_ever | dispositioned | has_stored_disposition

    # Position-of-introduction lookup for each NPC met this session. Used
    # to bound the "missing reaction" check to recently-introduced NPCs:
    # rolling reaction for an NPC introduced 100+ events ago is
    # anachronistic — the relationship is already shaped by play. Surface
    # those as a quieter info note instead, recommending set_disposition.
    RECENT_WINDOW = 30  # events
    total_events_this_session = len(in_session)
    npc_intro_index: dict[str, int] = {}
    for i, e in enumerate(in_session):
        if e.get("type") == "npc_met":
            slug = e.get("slug")
            if slug and slug not in npc_intro_index:
                npc_intro_index[slug] = i

    findings: list[dict] = []

    # 1. NPCs met without any reaction-or-disposition signal anywhere.
    # Severity depends on recency of introduction.
    for slug, intro_idx in npc_intro_index.items():
        if not slug or slug in pc_slugs or slug in established:
            continue
        events_since_intro = total_events_this_session - intro_idx - 1
        if events_since_intro <= RECENT_WINDOW:
            # Fresh introduction — reaction roll is still actionable.
            findings.append({
                "severity": "lapse",
                "kind":     "missing_reaction",
                "slug":     slug,
                "fix":      f"Call reaction(npc='{slug}') for first encounter, or set_disposition if previously known. NPCs interacted with without reaction rolls slip into unearned consensus (CLAUDE.md Hard Constraint #4).",
            })
        else:
            # Old introduction — relationship is already shaped by play.
            # Quieter info note: anchor with set_disposition rather than
            # an anachronistic reaction roll.
            findings.append({
                "severity": "info",
                "kind":     "old_npc_no_disposition",
                "slug":     slug,
                "fix":      f"NPC '{slug}' was introduced {events_since_intro} events ago without a reaction roll or stored disposition. Reaction now is anachronistic; anchor the existing relationship with set_disposition(slug, value, reason) so future reactions apply correctly.",
            })

    # 2. Combat ends without surprise_check earlier in the session
    combat_indices = [i for i, e in enumerate(in_session) if e.get("type") == "combat"]
    surprise_indices = [i for i, e in enumerate(in_session) if e.get("type") == "surprise_check"]
    for ci in combat_indices:
        preceding_surprise = [si for si in surprise_indices if si < ci]
        if not preceding_surprise:
            findings.append({
                "severity": "warning",
                "kind":     "missing_surprise_check",
                "fix":      "At the next encounter, call surprise_check before initiative. Surprise is easy to forget.",
            })
            break  # one warning per session is enough

    # 3. Combat events with no morale_check anywhere in session
    morale_indices = [i for i, e in enumerate(in_session) if e.get("type") == "morale_check"]
    if combat_indices and not morale_indices:
        findings.append({
            "severity": "warning",
            "kind":     "missing_morale_check",
            "fix":      "If any enemy group lost 50% in combat this session, call morale_check(rating). Morale breaks are part of fairness.",
        })

    # 4. Quests with scope mismatch
    avg = _avg_party_level(cfg)
    for slug, q in cfg.get("quests", {}).items():
        if q.get("status") != "active":
            continue
        scope = q.get("scope", "local")
        if scope == "regional" and avg < 4:
            findings.append({
                "severity": "lapse",
                "kind":     "scope_too_large",
                "slug":     slug,
                "fix":      f"Quest '{slug}' is regional but party avg level is {avg:.1f}. CLAUDE.md says regional is L4+. Demote scope or pause.",
            })
        elif scope == "continental" and avg < 8:
            findings.append({
                "severity": "lapse",
                "kind":     "scope_too_large",
                "slug":     slug,
                "fix":      f"Quest '{slug}' is continental but party avg level is {avg:.1f}. CLAUDE.md says continental is L8+. Demote scope or pause.",
            })

    # 5. NPC met but no disposition recorded after meaningful interactions.
    # Excludes PCs (party slugs share the event type via update_character)
    # and NPCs already flagged by check #1's integrated_npc branch to avoid
    # double-reporting.
    already_flagged_old = {
        f.get("slug") for f in findings
        if f.get("kind") == "old_npc_no_disposition"
    }
    slug_counts: dict[str, int] = {}
    for e in in_session:
        s = e.get("slug")
        if not s or s in pc_slugs:
            continue
        if e.get("type") in ("npc_interaction", "reaction_roll", "npc_met"):
            slug_counts[s] = slug_counts.get(s, 0) + 1
    for slug, count in slug_counts.items():
        if count >= 3 and slug not in established and slug not in already_flagged_old:
            findings.append({
                "severity": "info",
                "kind":     "frequent_npc_no_disposition",
                "slug":     slug,
                "fix":      f"NPC '{slug}' appeared {count} times this session — consider set_disposition to anchor future reactions.",
            })

    # 6. Hireling appearing in events but no loyalty_check across the session
    hire_events = [e for e in in_session if e.get("type") == "hireling_hired"]
    loyalty_events = [e for e in in_session if e.get("type") == "loyalty_change"]
    if hire_events and not loyalty_events and len(in_session) > 20:
        findings.append({
            "severity": "info",
            "kind":     "hireling_no_loyalty_review",
            "fix":      "Long session with active hirelings but no loyalty adjustments. Did anything happen they would react to?",
        })

    # 7. Active world clocks ignored too long (>14 days remaining still in same session)
    # This is informational only.
    for c in state.get("faction_clocks", []):
        if 0 < c.get("days_remaining", 999) <= 7:
            findings.append({
                "severity": "info",
                "kind":     "urgent_clock",
                "fix":      f"Clock '{c.get('label','?')}' has {c.get('days_remaining')} days left. Consider letting NPCs reference urgency.",
            })

    # 8. Combat narrated but the in-session pipeline never ran a roll_initiative
    # alongside the combat event. Catches "freehand combat" — narrative says
    # there was a fight, no procedural events accompanied it. CLAUDE.md Hard
    # Constraint #8: a combat with zero pipeline events is not a combat; it
    # is prose.
    if combat_indices:
        init_events = [e for e in in_session if e.get("type") in ("roll_initiative", "attack", "apply_combat_damage")]
        if not init_events:
            findings.append({
                "severity": "lapse",
                "kind":     "combat_without_pipeline",
                "fix":      "Combat event(s) logged this session with zero accompanying roll_initiative / attack / apply_combat_damage events. The combat was narrated freehand. CLAUDE.md Hard Constraint #8: next combat must use start_combat -> monster_lookup -> add_combatant -> surprise_check -> roll_initiative -> attack -> apply_combat_damage -> end_combat.",
            })

    # 9. encounter_check cadence — if the session has any travel/dungeon
    # references but very few encounter checks for its length, flag.
    # Heuristic: if events count is high (>60) but encounter_check density
    # is below 1-in-10, flag.
    encounter_checks = [e for e in in_session if e.get("type") == "encounter_check"]
    if len(in_session) > 60 and len(encounter_checks) < len(in_session) // 15:
        findings.append({
            "severity": "info",
            "kind":     "sparse_encounter_checks",
            "fix":      f"Only {len(encounter_checks)} encounter_check calls across {len(in_session)} session events. Cadence: every 4h overland, every 10min dungeon, every 2h urban at night. Sparse checks let the world go quiet.",
        })

    # 10. Plain reaction rolls (no NPC slug) — CLAUDE.md NPCs section says
    # reaction() should be called with the slug so the event logs and a
    # subsequent audit can verify. If many in-session reaction_roll events
    # exist with no slug, that's a flag.
    sluggless_reactions = [e for e in in_session
                           if e.get("type") == "reaction_roll" and not e.get("slug")]
    if len(sluggless_reactions) >= 3:
        findings.append({
            "severity": "info",
            "kind":     "reactions_without_slug",
            "fix":      f"{len(sluggless_reactions)} reaction rolls this session had no NPC slug attached. Pass npc=slug so disposition + faction reputation are applied and the audit can verify follow-up.",
        })

    # Prose audit — fourth-wall / railroading / directive-language checks.
    # Only runs when dm_text is supplied (post-turn hook passes the latest
    # response; the standalone audit_session tool can pass it too).
    if dm_text:
        findings.extend(_scan_dm_prose(dm_text))

    return {
        "session":  target_session,
        "checked":  len(in_session),
        "findings": findings,
        "summary":  f"{sum(1 for f in findings if f['severity']=='lapse')} lapses, "
                    f"{sum(1 for f in findings if f['severity']=='warning')} warnings, "
                    f"{sum(1 for f in findings if f['severity']=='info')} info notes.",
    }


def register(mcp):

    @mcp.tool()
    def audit_session(session: int = 0) -> dict:
        """Audit a session for fairness lapses. session=0 means current session.

        Runs automatically inside session_primer() at session start — the
        DM sees prior-session findings before narrating anything new.

        Checks (event-based):
          1. NPCs introduced without a follow-up reaction roll
          2. Combat events without a preceding surprise_check
          3. Combat events without a morale_check (when enemies fell)
          4. Active quests with scope mismatched to party level
          5. NPCs interacted with 3+ times this session without a recorded disposition
          6. Active hirelings with no loyalty_check over a long session
          7. World clocks with <=7 days remaining
          8. Combat events with zero accompanying combat-pipeline events
             (freehand combat — CLAUDE.md Hard Constraint #8)
          9. Sparse encounter_check cadence relative to total session activity
         10. Reaction rolls called without an NPC slug (audit-blind reactions)

        Checks (prose-based, only when the post-turn hook supplies the
        latest DM response — runs automatically after every turn):
         11. Module room IDs (T9, T20, D11, K12, ...) leaking into
             player-facing narrative (CLAUDE.md Hard Constraint #5)
         12. Hidden features (door / trigger / passage / trap / ...) named
             in OOC choice prompts
         13. Directive language ('obvious tool', 'would be wise', ...) in
             OOC choice prompts that steers player decisions
         14. DM-omniscience leaks ('no other traps', 'the chamber is safe')
             stating absence the party hasn't earned

        Returns a list of findings with severity (info / warning / lapse).
        """
        cfg   = _c.load_campaign()
        state = _c.load_state(cfg)
        target_session = session or state.get("current_session", 1)

        # Best-effort: pull the latest DM text so the prose checks fire
        # when audit_session is called manually mid-session too.
        dm_text = None
        try:
            from tools.dm_session import last_dm_text
            dm_text = last_dm_text()
        except Exception:
            pass

        return _run_audit(target_session, dm_text=dm_text)
