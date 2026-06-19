#!/usr/bin/env python3
"""Claude Code status line — live TTRPG campaign state."""
import json
import sys
from pathlib import Path

BASE = Path(__file__).parent
ACTIVE_FILE = BASE / ".active"
CAMPAIGNS_DIR = BASE / "campaigns"

# ANSI colours
_R = "\033[0m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"


def _c(text: str, *codes: str) -> str:
    return "".join(codes) + text + _R


def _hp_color(cur: int, max_hp: int) -> str:
    if max_hp == 0:
        return _c(str(cur), _DIM)
    if cur <= 0:
        return _c(f"0/{max_hp}†", _RED, _BOLD)
    ratio = cur / max_hp
    if ratio <= 0.25:
        return _c(f"{cur}/{max_hp}", _RED)
    if ratio <= 0.60:
        return _c(f"{cur}/{max_hp}", _YELLOW)
    return _c(f"{cur}/{max_hp}", _GREEN)


def main():
    try:
        ctx = json.loads(sys.stdin.read())
    except Exception:
        ctx = {}

    ctx_pct = ctx.get("context_window", {}).get("used_percentage", 0) or 0

    try:
        if not ACTIVE_FILE.exists():
            print("No active campaign")
            return

        slug = ACTIVE_FILE.read_text().strip()
        camp_dir = CAMPAIGNS_DIR / slug

        camp_json = camp_dir / "campaign.json"
        if not camp_json.exists():
            print(f"[{slug}]")
            return

        cfg = json.loads(camp_json.read_text())
        name = cfg.get("name", slug)
        status = cfg.get("status", "active")

        if status == "closed":
            reason = cfg.get("closed_reason", "closed")
            print(_c(f"[{name}] CLOSED ({reason})", _DIM))
            return

        state_path = camp_dir / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}

        day = state.get("current_day", 1)
        session = state.get("current_session", 1)

        # Time of day (HH:MM) + weather (short label)
        hour = state.get("current_hour", 6)
        minute = state.get("current_minute", 0)
        time_str = f"{hour:02d}:{minute:02d}"
        weather = state.get("current_weather", "")
        # Pull the weather label out of the descriptor "label (terrain, season)"
        weather_label = weather.split(" (", 1)[0] if weather else ""
        weather_seg = f" · {_c(weather_label, _DIM)}" if weather_label else ""

        # Live HP override from combat_state.json (active combat keeps HP
        # current there; state.json only updates on end_combat). Combat
        # conditions come through the same way.
        combat_hp: dict = {}
        combat_conds: dict = {}
        cs_path = camp_dir / "combat_state.json"
        if cs_path.exists():
            try:
                cs = json.loads(cs_path.read_text())
                if cs.get("active"):
                    for c in cs.get("combatants", []):
                        if c.get("side") == "party" and c.get("key"):
                            combat_hp[c["key"]] = c.get("hp")
                            combat_conds[c["key"]] = c.get("conditions") or []
            except Exception:
                pass

        # Party HP
        chars = cfg.get("characters", {})
        char_states = state.get("characters", {})
        hp_parts = []
        for key, char in chars.items():
            max_hp = char.get("hp_max", 0)
            cur_hp = combat_hp.get(key, char_states.get(key, {}).get("hp", max_hp))
            label = char.get("label", key).split()[0]
            conds = combat_conds.get(key, char_states.get(key, {}).get("conditions", []))
            cond_str = _c(f"({','.join(c[:3] for c in conds)})", _DIM) if conds else ""
            hp_parts.append(f"{label} {_hp_color(cur_hp, max_hp)}{cond_str}")
        hp_str = " · ".join(hp_parts)

        # Current location
        location_str = ""
        events_path = camp_dir / "events.json"
        if events_path.exists():
            events = json.loads(events_path.read_text())
            for event in reversed(events):
                if event.get("type") == "location_visited":
                    loc_name = event.get("name") or event.get("slug", "")
                    if loc_name:
                        location_str = f" @ {loc_name}"
                    break

        # Alerts
        in_combat = bool(state.get("combat"))
        combat_str = _c(" [COMBAT]", _RED, _BOLD) if in_combat else ""

        if ctx_pct >= 75:
            ctx_str = _c(f" [{ctx_pct:.0f}%ctx]", _RED)
        elif ctx_pct >= 50:
            ctx_str = _c(f" [{ctx_pct:.0f}%ctx]", _YELLOW)
        elif ctx_pct > 0:
            ctx_str = _c(f" [{ctx_pct:.0f}%ctx]", _DIM)
        else:
            ctx_str = ""

        header = _c(f"[{name}]", _BOLD) + f" S{session} Day {day} {time_str}{weather_seg}{location_str}"
        print(f"{header}{combat_str} | {hp_str}{ctx_str}")

    except Exception as e:
        print(f"[ttrpg] err: {e}")


if __name__ == "__main__":
    main()
