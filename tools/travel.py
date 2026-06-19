"""Overland travel — chains weather, encounter checks, world-clock ticks, and ration use.

The DM provides the route (from / to / terrain / days) and this tool runs each
day in order, returning a chronological digest. Encounters are flagged but not
auto-resolved: the DM narrates the result of each `encounter_triggered: True` day.
"""
import random
import _campaign as _c
from tools import weather as _weather
from tools import factions as _factions


# Pace modifier per weather descriptor (substring match)
_WEATHER_PACE = [
    ("blizzard",       0.0),
    ("thunderstorm",   0.0),
    ("heavy rain",     0.75),
    ("heavy snow",     0.5),
    ("light snow",     0.9),
    ("rain",           0.9),
    ("heat wave",      0.75),
    ("windstorm",      0.5),
    ("freezing fog",   0.7),
    ("fog",            0.85),
]


def _weather_pace(weather: str) -> float:
    w = (weather or "").lower()
    for needle, mult in _WEATHER_PACE:
        if needle in w:
            return mult
    return 1.0


def _check_encounter(cfg: dict, terrain: str) -> dict:
    """Mirror of dice.check_encounter without the registration overhead — terrain key only."""
    freq = cfg.get("encounter_frequency", {})
    defaults = {
        "dungeon":    {"die": 6, "threshold": 1, "interval_minutes": 10},
        "wilderness": {"die": 6, "threshold": 1, "interval_minutes": 240},
        "road":       {"die": 6, "threshold": 1, "interval_minutes": 60},
        "urban":      {"die": 20, "threshold": 1, "interval_minutes": 120},
    }
    config = freq.get(terrain.lower(), defaults.get(terrain.lower(), defaults["wilderness"]))
    die = config.get("die", 6)
    thresh = config.get("threshold", 1)
    roll = random.randint(1, die)
    return {"roll": roll, "die": die, "threshold": thresh, "triggered": roll <= thresh}


def _consume_rations(cfg: dict, state: dict) -> tuple[list, list]:
    """Each PC eats one ration. Vehicle stowed pools are drained first
    (any cart/wagon with rations feeds the party before personal bags are
    touched), then each PC's own consumables. Returns (fed, hungry)."""
    consumables = state.setdefault("consumables", {})
    pools = state.setdefault("vehicle_consumables", {})
    fed, hungry = [], []
    for key, char in cfg.get("characters", {}).items():
        ate = False
        # First try any vehicle pool with rations.
        for slug, pool in pools.items():
            if pool.get("rations", 0) > 0:
                pool["rations"] -= 1
                if pool["rations"] == 0:
                    del pool["rations"]
                ate = True
                break
        if not ate:
            bag = consumables.setdefault(key, {})
            if bag.get("rations", 0) > 0:
                bag["rations"] -= 1
                if bag["rations"] == 0:
                    del bag["rations"]
                ate = True
        (fed if ate else hungry).append(char.get("label", key))
    return fed, hungry


def register(mcp):

    @mcp.tool()
    def travel(
        destination:    str,
        terrain:        str = "wilderness",
        days:           int = 1,
        distance_miles: int = 0,
        base_pace_mpd:  int = 24,
        forced_march:   bool = False,
    ) -> dict:
        """Run overland travel one day at a time, chaining weather, encounters,
        ration consumption, and world-clock ticks. Returns a per-day digest.

        destination:    free-form target (used in the digest only)
        terrain:        wilderness | road | urban (encounter table key)
        days:           explicit travel duration in days; overrides distance_miles
        distance_miles: distance to cover; combined with base_pace_mpd to compute days
        base_pace_mpd:  base miles/day (24 = human walking, 12 = halfling/dwarf, 30 = mounted)
        forced_march:   1.5× pace; each PC makes a CON check (DM rolls separately)

        Per day the tool:
         1. Rolls weather for the terrain (state.current_weather updated)
         2. Applies weather pace modifier; reports days extended by bad weather
         3. Rolls one encounter check; reports trigger so DM can resolve
         4. Consumes 1 ration per PC; flags any going hungry
         5. Advances the calendar by 1 day
         6. Ticks all world clocks by 1 day

        At the end, returns a `clocks_completed` and `clocks_urgent` digest.
        """
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        if days <= 0 and distance_miles > 0:
            pace = base_pace_mpd * (1.5 if forced_march else 1.0)
            days = max(1, (distance_miles + int(pace) - 1) // max(1, int(pace)))

        if days <= 0:
            return {"error": "Specify days or distance_miles."}

        legs = []
        completed_clocks: list = []
        urgent_clocks:    list = []

        for leg_n in range(1, int(days) + 1):
            season = state.get("current_season", "spring")
            table = _weather._terrain_table(season, terrain)
            if table is None:
                return {
                    "error": f"Unknown terrain '{terrain}'. Call list_terrains() for supported keys.",
                    "supported": _weather._supported_terrains(season),
                }
            w_label, w_effect = _weather._weighted_pick(table)
            descriptor = f"{w_label} ({terrain}, {season})"
            state["current_weather"] = descriptor

            pace_mult = _weather_pace(w_label)
            if forced_march:
                pace_mult *= 1.5

            enc = _check_encounter(cfg, terrain)
            fed, hungry = _consume_rations(cfg, state)

            cur_day = state.get("current_day", 1) + 1
            state["current_day"] = cur_day

            # Tick clocks by 1 day in line with calendar
            for c in state.get("faction_clocks", []):
                before = c.get("days_remaining", 0)
                c["days_remaining"] = before - 1
                if before > 0 and c["days_remaining"] <= 0:
                    completed_clocks.append(dict(c))
                elif 0 < c["days_remaining"] <= 7 and c not in urgent_clocks:
                    urgent_clocks.append(dict(c))

            _c.save_state(cfg, state)

            leg = {
                "day":               leg_n,
                "calendar_day":      cur_day,
                "weather":           w_label,
                "weather_effect":    w_effect,
                "pace_multiplier":   round(pace_mult, 2),
                "encounter_roll":    enc["roll"],
                "encounter_triggered": enc["triggered"],
                "ration_fed":        fed,
                "ration_hungry":     hungry,
            }
            legs.append(leg)

        return {
            "destination":     destination,
            "terrain":         terrain,
            "total_days":      days,
            "legs":            legs,
            "clocks_completed": completed_clocks,
            "clocks_urgent":    urgent_clocks,
            "reminder":         (
                "For each leg with encounter_triggered=True, call determine_encounter(terrain) "
                "and resolve. For each completed clock, narrate the consequence. Hungry PCs "
                "lose 1 HP per day until fed (PHB)."
            ),
        }
