"""Session log, chronicle note, and calendar tools."""
import re
from pathlib import Path
from datetime import date
import _campaign as _c


def register(mcp):

    @mcp.tool()
    def log_note(text: str) -> dict:
        """Append a bullet note to the current session in the chronicle markdown file.
        Finds the last '### Session' heading and appends the note before the next heading or EOF."""
        cfg = _c.load_campaign()
        chronicle_path = Path(cfg["_data_dir"]) / cfg.get("session_log_file", "adventure_log.md")

        chronicle_path.parent.mkdir(parents=True, exist_ok=True)

        if not chronicle_path.exists():
            chronicle_path.write_text("")

        content = chronicle_path.read_text(encoding="utf-8")
        bullet = f"- {text}\n"

        # Find last "### Session" heading by scanning from the end (rfind beats
        # regex on a long, growing chronicle). Must start at column 0.
        marker = "### Session"
        last = content.rfind(marker)
        while last > 0 and content[last - 1] != "\n":
            last = content.rfind(marker, 0, last)

        if last < 0:
            # No session yet — just append
            if content and not content.endswith("\n"):
                content += "\n"
            content += bullet
        else:
            # Find the next heading (#, ##, ###) after this session header
            after = content.find("\n#", last + len(marker))
            if after >= 0:
                # Insert before the next heading, ensuring a separating newline
                before = content[:after + 1]
                tail   = content[after + 1:]
                if not before.endswith("\n"):
                    before += "\n"
                content = before + bullet + tail
            else:
                if not content.endswith("\n"):
                    content += "\n"
                content += bullet

        chronicle_path.write_text(content, encoding="utf-8")
        return {"appended": bullet.rstrip()}

    @mcp.tool()
    def new_session(title: str) -> dict:
        """Append a new session heading to the chronicle and increment the session counter."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        chronicle_path = Path(cfg["_data_dir"]) / cfg.get("session_log_file", "adventure_log.md")
        chronicle_path.parent.mkdir(parents=True, exist_ok=True)

        session_num = state.get("current_session", 0) + 1
        state["current_session"] = session_num
        _c.save_state(cfg, state)

        today = date.today().strftime("%Y-%m-%d")
        heading = f"\n### Session {session_num} — {title}\n*{today}*\n\n"

        if not chronicle_path.exists():
            chronicle_path.write_text(heading.lstrip(), encoding="utf-8")
        else:
            with chronicle_path.open("a", encoding="utf-8") as f:
                f.write(heading)

        _c.append_event(cfg, {"type": "session_start", "session": session_num, "title": title, "date": today})

        return {
            "session": session_num,
            "title": title,
        }

    @mcp.tool()
    def advance_calendar(days: int) -> dict:
        """Advance the in-game calendar by a number of days. Time of day is preserved."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        current = state.get("current_day", 1)
        new_day = current + days
        state["current_day"] = new_day
        _c.save_state(cfg, state)

        return {
            "days_advanced": days,
            "new_day": new_day,
        }

    @mcp.tool()
    def advance_time(minutes: int) -> dict:
        """Advance the in-game clock by N minutes.
        Rolls into the next day at hour 24. Decrements every lit light source's
        minutes_remaining and extinguishes any that reach zero (returned in `extinguished`).

        Use whenever significant time passes:
        - Searching a room: 10 min
        - Picking a lock: 1d10 min
        - Travelling between dungeon rooms: 1 min per 30 ft
        - Resting (short): 30 min
        - Memorising a spell: 60 min (1st-level), 90 min (3rd), 2 hr (5th+)
        - Travel between settlements: hours; for full days use travel() instead

        For long rests use rest() — that handles HP/slots; this tool only handles
        the clock and consumables.
        """
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        cur_h = state.get("current_hour", 6)
        cur_m = state.get("current_minute", 0)
        cur_d = state.get("current_day", 1)

        total = cur_h * 60 + cur_m + int(minutes)
        days_passed = total // (24 * 60)
        rem = total % (24 * 60)
        cur_d += days_passed
        cur_h = rem // 60
        cur_m = rem % 60

        state["current_day"]    = cur_d
        state["current_hour"]   = cur_h
        state["current_minute"] = cur_m

        # Decrement lit light sources
        extinguished = []
        for s in state.get("light_sources", []):
            if not s.get("lit") or s.get("minutes_remaining", -1) < 0:
                continue
            s["minutes_remaining"] -= int(minutes)
            if s["minutes_remaining"] <= 0:
                s["lit"] = False
                s["minutes_remaining"] = 0
                extinguished.append(dict(s))

        _c.save_state(cfg, state)

        # Period of day for narrative cues
        if cur_h < 5:           period = "night"
        elif cur_h < 8:         period = "dawn"
        elif cur_h < 12:        period = "morning"
        elif cur_h < 14:        period = "midday"
        elif cur_h < 18:        period = "afternoon"
        elif cur_h < 21:        period = "evening"
        else:                   period = "night"

        result = {
            "minutes_advanced": minutes,
            "day":              cur_d,
            "time":             f"{cur_h:02d}:{cur_m:02d}",
            "period":           period,
        }
        if extinguished:
            result["extinguished"] = extinguished
        if days_passed:
            result["days_passed"] = days_passed
        return result

    @mcp.tool()
    def set_time(hour: int, minute: int = 0) -> dict:
        """Explicitly set the clock without advancing day. Use for narrative jumps:
        'they sleep through the night and wake at dawn' → set_time(6).
        For 'next morning' across days, use advance_calendar(1) then set_time(6)."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        h = max(0, min(23, int(hour)))
        m = max(0, min(59, int(minute)))
        state["current_hour"] = h
        state["current_minute"] = m
        _c.save_state(cfg, state)
        return {"day": state.get("current_day", 1), "time": f"{h:02d}:{m:02d}"}
