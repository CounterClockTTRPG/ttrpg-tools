"""Campaign management tools: create, list, switch, inspect, add characters."""
import json
import re
from pathlib import Path
from datetime import datetime, date
import _campaign as _c


_CAMPAIGN_SCHEMA = {
    "name": "",
    "system": "AD&D 2nd Edition",
    "world": "",
    "tone": "high fantasy",
    "data_dir": ".",
    "default_character": "",
    "session_log_file": "adventure_log.md",
    "chronicle_file": "adventure_log.md",
    "initial_coin": {"pp": 0, "gp": 0, "ep": 0, "sp": 0, "cp": 0},
    "characters": {},
    "encounter_tables": {
        "dungeon_l1": [],
        "road": [],
        "wilderness": [],
        "urban": [],
    },
    "encounter_frequency": {
        "dungeon":    {"die": 6,  "threshold": 1, "interval_minutes": 10},
        "wilderness": {"die": 6,  "threshold": 1, "interval_minutes": 240},
        "road":       {"die": 6,  "threshold": 1, "interval_minutes": 60},
        "urban":      {"die": 20, "threshold": 1, "interval_minutes": 120},
    },
}


def _campaigns_root() -> Path:
    """Find the campaigns/ directory relative to _campaign.py's location."""
    import _campaign as _c_mod
    mod_path = Path(_c_mod.__file__).resolve().parent
    return mod_path / "campaigns"


def register(mcp):

    @mcp.tool()
    def create_campaign(
        name: str,
        world: str,
        tone: str,
        system: str = "AD&D 2nd Edition",
        initial_gp: int = 0,
    ) -> dict:
        """Create a new campaign directory and scaffold campaign.json and adventure_log.md."""
        slug = name.lower().replace(" ", "-")
        slug = re.sub(r'[^a-z0-9-]', '', slug)

        root = _campaigns_root()
        camp_dir = root / slug

        # Create directories
        for subdir in ("", "characters", "locations", "images"):
            (camp_dir / subdir).mkdir(parents=True, exist_ok=True)

        # Build campaign.json
        campaign_data = dict(_CAMPAIGN_SCHEMA)
        campaign_data["name"] = name
        campaign_data["system"] = system
        campaign_data["world"] = world
        campaign_data["tone"] = tone
        campaign_data["initial_coin"] = {"pp": 0, "gp": initial_gp, "ep": 0, "sp": 0, "cp": 0}

        # Deep-copy nested dicts so schema isn't mutated
        campaign_data["encounter_tables"] = {k: list(v) for k, v in _CAMPAIGN_SCHEMA["encounter_tables"].items()}
        campaign_data["encounter_frequency"] = {k: dict(v) for k, v in _CAMPAIGN_SCHEMA["encounter_frequency"].items()}
        campaign_data["characters"] = {}

        camp_json = camp_dir / "campaign.json"
        _c.atomic_write_text(camp_json, json.dumps(campaign_data, indent=2))

        # Create empty adventure_log.md
        log_path = camp_dir / "adventure_log.md"
        if not log_path.exists():
            log_path.write_text(f"# {name} — Adventure Log\n\n", encoding="utf-8")

        # Set as active
        _c.set_active(slug)

        return {
            "slug": slug,
            "path": str(camp_dir),
        }

    @mcp.tool()
    def list_campaigns() -> dict:
        """List all campaigns found in the campaigns/ directory."""
        root = _campaigns_root()

        active_slug = ""
        active_file = root.parent / ".active"
        if active_file.exists():
            active_slug = active_file.read_text(encoding="utf-8").strip()

        campaigns = []
        if root.exists():
            for camp_json in sorted(root.glob("*/campaign.json")):
                slug = camp_json.parent.name
                try:
                    data = json.loads(camp_json.read_text(encoding="utf-8"))
                    mtime = datetime.fromtimestamp(camp_json.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                    campaigns.append({
                        "slug": slug,
                        "name": data.get("name", slug),
                        "active": slug == active_slug,
                        "status": data.get("status", "active"),
                        "closed_reason": data.get("closed_reason", ""),
                        "closed_at": data.get("closed_at", ""),
                        "last_modified": mtime,
                    })
                except (json.JSONDecodeError, OSError):
                    campaigns.append({
                        "slug": slug,
                        "name": slug,
                        "active": slug == active_slug,
                        "status": "active",
                        "closed_reason": "",
                        "closed_at": "",
                        "last_modified": "",
                    })

        return {"campaigns": campaigns}

    @mcp.tool()
    def close_campaign(
        epitaph: str,
        reason: str = "total_party_kill",
    ) -> dict:
        """Mark the active campaign as closed and record a final epitaph.

        Writes status/closed metadata to campaign.json, appends a closing
        section to the adventure log, and creates EPITAPH.md in the campaign
        directory.

        reason: one of 'total_party_kill', 'victory', 'abandoned', 'hiatus',
                or any free-form string describing the cause.
        epitaph: narrative text summarising the campaign's end — who fell,
                 what was left undone, what the world remembers.
        """
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        camp_dir = cfg["_dir"]

        today = date.today().strftime("%Y-%m-%d")
        sessions = state.get("current_session", 0)
        days_elapsed = state.get("current_day", 1) - 1

        # ── Update campaign.json ─────────────────────────────────────────────
        cfg["status"] = "closed"
        cfg["closed_at"] = today
        cfg["closed_reason"] = reason
        cfg["epitaph"] = epitaph
        _c.save_campaign(cfg)

        # ── Append closing section to adventure log ──────────────────────────
        log_path = camp_dir / cfg.get("session_log_file", "adventure_log.md")
        reason_label = {
            "total_party_kill": "Total Party Kill",
            "victory": "Victory",
            "abandoned": "Abandoned",
            "hiatus": "Hiatus",
        }.get(reason, reason.replace("_", " ").title())

        closing_block = (
            f"\n---\n\n"
            f"## Campaign Closed — {reason_label}\n"
            f"*{today}*  "
            f"Sessions played: {sessions}  "
            f"In-game days: {days_elapsed}\n\n"
            f"{epitaph}\n"
        )
        with log_path.open("a", encoding="utf-8") as f:
            f.write(closing_block)

        # ── Write EPITAPH.md ─────────────────────────────────────────────────
        camp_name = cfg.get("name", camp_dir.name)
        epitaph_path = camp_dir / "EPITAPH.md"
        epitaph_md = (
            f"# {camp_name} — Epitaph\n\n"
            f"**Closed:** {today}  \n"
            f"**Cause:** {reason_label}  \n"
            f"**Sessions:** {sessions}  \n"
            f"**In-game days:** {days_elapsed}\n\n"
            f"---\n\n"
            f"{epitaph}\n"
        )
        epitaph_path.write_text(epitaph_md, encoding="utf-8")

        # ── Log event ────────────────────────────────────────────────────────
        _c.append_event(cfg, {
            "type": "campaign_closed",
            "reason": reason,
            "date": today,
            "epitaph_preview": epitaph[:120] + ("…" if len(epitaph) > 120 else ""),
        })

        return {
            "campaign": camp_name,
            "status": "closed",
            "closed_at": today,
            "closed_reason": reason,
            "epitaph_path": str(epitaph_path),
            "sessions": sessions,
            "in_game_days": days_elapsed,
        }

    @mcp.tool()
    def switch_campaign(name: str) -> dict:
        """Switch the active campaign by slug or name prefix."""
        root = _campaigns_root()
        name_lower = name.lower()

        matched_slug = None
        if root.exists():
            for camp_json in sorted(root.glob("*/campaign.json")):
                slug = camp_json.parent.name
                if slug == name_lower or slug.startswith(name_lower):
                    matched_slug = slug
                    break
                try:
                    data = json.loads(camp_json.read_text(encoding="utf-8"))
                    camp_name = data.get("name", "").lower()
                    if camp_name.startswith(name_lower):
                        matched_slug = slug
                        break
                except (json.JSONDecodeError, OSError):
                    pass

        if matched_slug is None:
            return {"error": f"No campaign found matching '{name}'."}

        _c.set_active(matched_slug)

        # Load to get the display name
        camp_json = root / matched_slug / "campaign.json"
        try:
            data = json.loads(camp_json.read_text(encoding="utf-8"))
            display_name = data.get("name", matched_slug)
        except (json.JSONDecodeError, OSError):
            display_name = matched_slug

        return {
            "active": matched_slug,
            "name": display_name,
        }

    @mcp.tool()
    def campaign_info() -> dict:
        """Return active campaign details: name, world, tone, system, characters, current day, session count."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        characters = []
        for key, char in cfg.get("characters", {}).items():
            cstate = state.get("characters", {}).get(key, {})
            hp_max = char.get("hp_max", 0)
            hp_cur = cstate.get("hp", hp_max)
            characters.append({
                "key": key,
                "label": char.get("label", key),
                "class": char.get("cls", ""),
                "hp": f"{hp_cur}/{hp_max}",
            })

        result = {
            "name": cfg.get("name", ""),
            "world": cfg.get("world", ""),
            "tone": cfg.get("tone", ""),
            "system": cfg.get("system", ""),
            "status": cfg.get("status", "active"),
            "current_day": state.get("current_day", 1),
            "current_session": state.get("current_session", 0),
            "default_character": cfg.get("default_character", ""),
            "characters": characters,
        }
        if cfg.get("status") == "closed":
            result["closed_at"] = cfg.get("closed_at", "")
            result["closed_reason"] = cfg.get("closed_reason", "")
            result["epitaph"] = cfg.get("epitaph", "")
        return result

    @mcp.tool()
    def add_character(
        key: str,
        label: str,
        cls: str,
        hp_max: int,
        thac0: int,
        ac: int,
        weapon: str = "1d6",
        weapon_speed: int = 5,
        bonus_hit: int = 0,
        bonus_dmg: int = 0,
        saves: list = None,
        race: str = "human",
        gender: str = "male",
        level: int = 1,
        ability_scores: dict = None,
        background: str = "",
        attacks: list = None,
        weapon_profs: list = None,
        nwps: list = None,
        natural_abilities: list = None,
        inventory: list = None,
        portrait_prompt: str = "",
        skills: dict = None,
        spell_slots: dict = None,
        memorized_spells: list = None,
    ) -> dict:
        """Add a PC to the campaign (campaign.json + state.json) and auto-generate their sheet.

        ability_scores: {"str": 16, "dex": 12, "con": 14, "int": 10, "wis": 11, "cha": 8}
        attacks: [{name, speed, attacks, thac0, damage_sm, damage_l?, range?}, ...]
        weapon_profs: ["Longsword", "Dagger", ...]
        nwps: ["Riding (WIS)", ...] or [{name, ability, slots?}, ...]
        natural_abilities: ["Infravision 60'", ...]
        inventory: ["Longsword", "Chain mail", "Backpack", ...]
        portrait_prompt: canonical image-generation prompt (stored and reused for future portraits)
        skills: thief/bard skill percentages {skill_name: pct}
        saves: either five ints in PP&D/RSW/PETR/BREATH/SPELL order, or a list of
               {type, value} dicts. When omitted, derived from cls + level via the
               2e DB (and equipment/level-up adjustments should be applied
               afterwards through normal updates).
        """
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        saves_norm = _c.normalize_saves(saves) or _c.base_saves_for(cls, level)
        if not saves_norm:
            # Class unknown to the 2e DB and no explicit saves passed — fall back
            # to a placeholder so the field exists, but flag it in the return.
            saves_norm = _c.normalize_saves([16, 18, 17, 20, 19])
        saves = saves_norm
        spell_slots_cfg = {str(k): int(v) for k, v in spell_slots.items()} if spell_slots else None

        char_data: dict = {
            "label":        label,
            "cls":          cls,
            "race":         race,
            "gender":       gender,
            "level":        level,
            "hp_max":       hp_max,
            "thac0":        thac0,
            "ac":           ac,
            "weapon":       weapon,
            "weapon_speed": weapon_speed,
            "bonus_hit":    bonus_hit,
            "bonus_dmg":    bonus_dmg,
            "saves":        saves,
            "skills":       skills or {},
        }
        if ability_scores:      char_data["ability_scores"]    = ability_scores
        if background:          char_data["background"]        = background
        if attacks:             char_data["attacks"]           = attacks
        if weapon_profs:        char_data["weapon_profs"]      = weapon_profs
        if nwps:                char_data["nwps"]              = nwps
        if natural_abilities:   char_data["natural_abilities"] = natural_abilities
        if inventory:           char_data["inventory"]         = inventory
        if spell_slots_cfg:     char_data["spell_slots"]       = spell_slots_cfg
        if memorized_spells:    char_data["memorized_spells"]  = memorized_spells

        # Build and store portrait prompt (explicit > auto-generated)
        from tools.images import _portrait_prompt_for
        char_data["portrait_prompt"] = portrait_prompt.strip() or _portrait_prompt_for(char_data)

        cfg.setdefault("characters", {})[key] = char_data
        if not cfg.get("default_character"):
            cfg["default_character"] = key
        _c.save_campaign(cfg)

        cstate = state.setdefault("characters", {}).setdefault(key, {})
        cstate.setdefault("hp", hp_max)
        cstate.setdefault("xp", 0)
        cstate.setdefault("conditions", [])
        if spell_slots_cfg:
            cstate.setdefault("spell_slots", dict(spell_slots_cfg))
        cstate.setdefault("memorized_spells", list(memorized_spells) if memorized_spells else [])
        _c.save_state(cfg, state)

        _c.write_character_sheet(cfg, key, char_data)

        # Auto-generate portrait using the stored prompt — best-effort
        try:
            from tools.images import generate_portrait_for
            generate_portrait_for(cfg, key, char_data["portrait_prompt"])
        except Exception:
            pass

        return {"key": key, "label": label}
