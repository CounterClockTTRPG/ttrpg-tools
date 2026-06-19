"""Weather and seasons. Deterministic-feeling rolls with seasonal weighting.

Weather lives on state.json:
    state.current_season   — 'spring' | 'summer' | 'autumn' | 'winter'
    state.current_weather  — last rolled weather descriptor

Season can be set explicitly via set_season; otherwise it stays whatever was
last set (default 'spring' on a fresh state).
"""
import random
import _campaign as _c


SEASONS = ("spring", "summer", "autumn", "winter")

# (weight, label, effect_summary)
_TABLES: dict[str, dict[str, list[tuple[int, str, str]]]] = {
    "spring": {
        "wilderness": [
            (5, "clear",       "Light wind. No effect."),
            (4, "overcast",    "Heavy clouds. Visibility 1 mile."),
            (5, "light rain",  "Wet ground. Tracking +1 difficulty. Bows -1 to hit."),
            (3, "heavy rain",  "Soaked. Travel pace -25%. Missile -2 to hit. Fire spells partial fizzle."),
            (1, "thunderstorm", "Travel impossible. Tents needed. Lightning rare hazard. Encounter rolls -1."),
            (2, "morning fog", "Visibility 30 ft until midday. Surprise checks +1 to surprise either side."),
        ],
    },
    "summer": {
        "wilderness": [
            (8, "clear",       "Bright and hot."),
            (3, "overcast",    "Sweltering haze. CON check at noon for armoured travellers."),
            (4, "heat wave",   "Travel pace -25% in heavy armour. Double water consumption."),
            (3, "afternoon storm", "Brief downpour at 14:00–16:00. Missile -2 to hit during. Lightning rare hazard."),
            (1, "drought wind", "Dry, dusty. Visibility 1/2 normal at distance."),
        ],
    },
    "autumn": {
        "wilderness": [
            (5, "clear",       "Crisp. No effect."),
            (4, "overcast",    "Grey and cool."),
            (4, "rain",        "Wet roads. Travel pace -10%. Missile -1."),
            (3, "fog",         "Dense morning fog. Visibility 30 ft until late morning."),
            (2, "early frost", "Cold mornings. Light bedrolls insufficient — CON check or fatigue."),
            (1, "windstorm",   "Trees down across roads. Travel pace -50%. Missile -3."),
        ],
    },
    "winter": {
        "wilderness": [
            (3, "clear cold",  "Bitter cold. CON check at night without proper shelter."),
            (3, "overcast cold","Grey and freezing. Wagon wheels stick on packed snow."),
            (4, "light snow",  "Tracking impossible after 1 hr. Travel pace -10%."),
            (3, "heavy snow",  "Travel pace -50%. Missile -3 to hit. Visibility 30 ft."),
            (2, "blizzard",    "Travel impossible. Shelter required. Frostbite risk per hour exposed."),
            (2, "freezing fog","Visibility 60 ft. Surface ice — DEX check or fall."),
        ],
    },
}

# --- Specialty terrain tables --------------------------------------------
# Each terrain dict is keyed by season and contains (weight, label, effect)
# tuples. Splice block at the bottom of this file pushes them into _TABLES.

# Desert (gameplay-tuned arid): ~60% clear, ~20% hot, ~10% sandstorm,
# ~5% rare rain, plus mirage/cold-night flavour. Cooler in spring/autumn,
# brutal in summer, sharp cold at night in winter.
_DESERT_TABLES: dict[str, list[tuple[int, str, str]]] = {
    "spring": [
        (12, "clear dry",     "Bright sun, cool wind. No effect."),
        (3,  "hot day",       "Air shimmers by midday. Double water consumption."),
        (2,  "sandstorm",     "Visibility 30 ft. Missile -3 to hit. Travel pace -50%. Eye/CON checks while exposed."),
        (1,  "rare rain",     "Brief shower. Dust laid for 1 day. Tracking +1 difficulty."),
        (1,  "cold night",    "Temperatures plunge after dusk. CON check at night without proper bedroll."),
        (1,  "mirage haze",   "Distant landmarks waver. Navigation checks +1 difficulty until midday."),
    ],
    "summer": [
        (10, "clear scorching","Furnace sun. Travel by night recommended. Double water consumption all day."),
        (5,  "heat wave",     "Travel pace -25% in any armour. Triple water consumption. CON check at noon or fatigue."),
        (2,  "sandstorm",     "Visibility 30 ft. Missile -3 to hit. Travel pace -50%. Eye/CON checks while exposed."),
        (1,  "monsoon storm", "Brief flash downpour. Wadis flood — never camp in dry riverbeds. Lightning hazard."),
        (1,  "dust devil",    "Spinning dust column crosses path. Cosmetic unless contacted (1d4 dmg, save vs paralysis or blinded 1 round)."),
        (1,  "mirage haze",   "Distant landmarks waver. Navigation checks +1 difficulty until midday."),
    ],
    "autumn": [
        (12, "clear",         "Mild and dry. No effect."),
        (3,  "hot day",       "Warm but bearable. Standard water consumption."),
        (2,  "sandstorm",     "Visibility 30 ft. Missile -3 to hit. Travel pace -50%. Eye/CON checks while exposed."),
        (1,  "rare rain",     "Brief shower wets the dunes. Tracking +1 difficulty for 1 day."),
        (1,  "cold night",    "Sharp cold after dusk. CON check at night without proper bedroll."),
        (1,  "mirage haze",   "Distant landmarks waver until midday."),
    ],
    "winter": [
        (10, "clear cold",    "Sharp blue sky, biting wind. CON check at night without proper shelter."),
        (3,  "cold day",      "Sun without warmth. Hands stiffen — DEX-based skill checks +1 difficulty."),
        (2,  "sandstorm",     "Visibility 30 ft. Missile -3 to hit. Travel pace -50%. Eye/CON checks while exposed."),
        (2,  "freezing night","Severe cold. Frostbite risk per hour exposed without shelter."),
        (1,  "cold rain",     "Brief icy rain. Travel pace -25% for the day."),
        (1,  "hard frost",    "Morning frost on rocks. DEX check on rough terrain or fall."),
        (1,  "mirage haze",   "Distant landmarks waver until midday."),
    ],
}

# Arctic / tundra: always cold; season modulates from "thaw" (spring) to
# "polar night" (winter). Rain is essentially absent — replaced by snow and
# freezing-rain variants.
_ARCTIC_TABLES: dict[str, list[tuple[int, str, str]]] = {
    "spring": [
        (6, "clear cold",     "Sun strengthens but air still bites. CON check at night without proper shelter."),
        (4, "melt mud",       "Ground thaws to slush. Travel pace -25%. Wagons mire."),
        (4, "light snow",     "Fresh snow on the melt. Tracking impossible after 1 hr."),
        (3, "thaw fog",       "Visibility 60 ft. Dripping ice everywhere."),
        (2, "sudden blizzard","Winter resurges briefly. Travel impossible until it clears."),
        (1, "break-up flood", "River ice cracks. Fords impassable; ice floes a hazard for 1d3 days."),
    ],
    "summer": [
        (8, "clear mild",     "Long daylight, almost warm at noon. No effect."),
        (4, "overcast cool",  "Grey light. Midges everywhere."),
        (3, "cold drizzle",   "Cold rain. Tracking +1 difficulty. Ground swampy."),
        (2, "mosquito swarm", "Biting clouds. CON check or 1 hp/hour exposed; spell concentration broken."),
        (2, "freezing fog",   "Visibility 60 ft."),
        (1, "snow flurry",    "Even in summer. Mostly cosmetic; brief tracking impossible."),
    ],
    "autumn": [
        (5, "clear cold",     "Sky brilliant, air sharp. CON check at night."),
        (4, "first snow",     "Travel pace -10%. Tracking impossible after 1 hr."),
        (4, "freezing rain",  "Travel pace -25%. Surfaces glaze; DEX check on rough ground."),
        (3, "polar twilight", "Sun barely rises. Encounter rolls +1; navigation +1 difficulty."),
        (2, "windstorm",      "Travel pace -50%. Missile -3."),
        (2, "blizzard",       "Travel impossible. Shelter required."),
    ],
    "winter": [
        (4, "clear bitter",   "−40°. Frostbite per hour exposed without shelter."),
        (5, "heavy snow",     "Travel pace -50%. Missile -3. Visibility 30 ft."),
        (4, "blizzard",       "Travel impossible. Shelter or die."),
        (3, "polar night",    "No sun. Encounter rolls +1; navigation impossible without celestial means."),
        (2, "whiteout",       "Visibility 10 ft. Lost-in-snow risk; navigation checks at +3 difficulty."),
        (1, "ice storm",      "Glaze on every surface. DEX check or fall every 10 ft."),
        (1, "katabatic wind", "Sudden screaming wind off the ice. Tents lost; CON check or 1d4 cold dmg."),
    ],
}

# Swamp / marsh: humid year-round, frequent fog and rain, leeches and miasma
# in warm months, treacherous ice in winter.
_SWAMP_TABLES: dict[str, list[tuple[int, str, str]]] = {
    "spring": [
        (5, "clear humid",   "Mild and sticky. Insects rising."),
        (5, "light rain",    "Wet ground. Tracking +1 difficulty. Bows -1 to hit."),
        (3, "heavy rain",    "Travel pace -25%. Missile -2 to hit."),
        (3, "morning fog",   "Visibility 30 ft until late morning. Surprise checks +1 either side."),
        (2, "thunderstorm",  "Travel impossible. Lightning hazard; encounter rolls -1."),
        (2, "leech weather", "Damp warmth. Leeches in any wading. CON check or 1 hp/hour exposed."),
    ],
    "summer": [
        (4, "sweltering",     "Oppressive humid heat. CON check at noon. Double water consumption."),
        (4, "afternoon storm","Brief downpour 14:00–16:00. Missile -2 during. Lightning."),
        (4, "mosquito clouds","Biting clouds. Spell concentration broken; 1 hp/hour exposed."),
        (3, "light rain",     "Standard wet. Tracking +1."),
        (3, "miasma",         "Foul air rises. CON save or sickened (-2 to-hit, no spell components) until clear."),
        (2, "morning fog",    "Visibility 30 ft until midday."),
    ],
    "autumn": [
        (5, "overcast cool", "Grey, dripping branches."),
        (5, "light rain",    "Tracking +1. Bows -1."),
        (4, "dense fog",     "Visibility 30 ft all day."),
        (3, "heavy rain",    "Travel pace -25%. Missile -2."),
        (2, "cold rain",     "Travel pace -25%. Hypothermia risk if soaked overnight."),
        (1, "thunderstorm",  "Travel impossible."),
    ],
    "winter": [
        (6, "cold mist",      "Visibility 60 ft. Damp chill bites worse than dry cold. CON check at night."),
        (4, "freezing rain",  "Surfaces glaze. DEX check on rough ground."),
        (4, "frozen marsh",   "Ice over standing water. DEX check or fall through (waist-deep, hypothermia risk)."),
        (3, "light snow",     "Tracking impossible after 1 hr."),
        (2, "freezing fog",   "Visibility 60 ft. Surface ice."),
        (1, "thaw rain",      "Brief warm rain breaks the ice. Travel pace -50%; some routes impassable."),
    ],
}

# Mountain: altitude effects layered on top of weather. Sudden squalls,
# rockfall, avalanche risk in spring, thin-air fatigue in any warm season.
_MOUNTAIN_TABLES: dict[str, list[tuple[int, str, str]]] = {
    "spring": [
        (6, "clear cold",       "Bright sun, biting wind. CON check at altitude per 4 hours of climbing."),
        (4, "sudden squall",    "Brief violent storm. Missile -2 during. Travel pace -25%."),
        (3, "thin air haze",    "Altitude effects pronounced. CON check or fatigue every 4 hours of climbing."),
        (3, "mountain fog",     "Cloud bank rolls in. Visibility 30 ft. Slip risk on traverse."),
        (2, "avalanche risk",   "Snowpack unstable. Loud noises trigger slides; rolling DEX saves on traverse."),
        (2, "thaw rain",        "Travel pace -25%. Streams in flood; fords difficult."),
    ],
    "summer": [
        (8, "clear",            "Cool at altitude despite the season. No effect."),
        (3, "thin air",         "CON check or fatigue every 4 hours of climbing."),
        (3, "afternoon storm",  "Storm builds in afternoon. Lightning hazard on peaks/ridges."),
        (3, "mountain fog",     "Visibility 30 ft. Slip risk."),
        (2, "sudden squall",    "Brief violent storm. Missile -2 during."),
        (1, "rockfall",         "Loose rock dislodged by warmth. DEX check on traverse or 1d6 dmg."),
    ],
    "autumn": [
        (5, "clear cold",       "Crisp. CON check at night."),
        (4, "first snow",       "Travel pace -10%. Tracking impossible after 1 hr."),
        (4, "mountain fog",     "Visibility 30 ft."),
        (3, "windstorm",        "Travel pace -50%. Missile -3. Loose rock dislodged."),
        (2, "freezing rain",    "Surfaces glaze. DEX check on traverse or fall."),
        (2, "cold front",       "Temperature drops 30° in an hour. Hypothermia risk if not prepared."),
    ],
    "winter": [
        (3, "clear bitter",     "Frostbite at altitude per hour exposed."),
        (5, "heavy snow",       "Travel pace -50%. Missile -3. Avalanche risk on traverse."),
        (4, "blizzard",         "Travel impossible. Shelter or die."),
        (3, "whiteout",         "Visibility 10 ft. Navigation checks +3 difficulty."),
        (3, "ice storm",        "Glaze on every surface. DEX check or fall every 10 ft."),
        (2, "cornice danger",   "Snow overhangs the path. DEX check on traverse or break through and fall."),
    ],
}

# Jungle / rainforest: opposite of desert — frequent rain in warm seasons,
# oppressive humidity, dense canopy reducing visibility, biting fauna.
_JUNGLE_TABLES: dict[str, list[tuple[int, str, str]]] = {
    "spring": [
        (4, "humid clear",   "Sticky, dim under canopy. No effect."),
        (5, "light rain",    "Daily afternoon shower. Tracking +1. Bows -1."),
        (4, "heavy rain",    "Travel pace -25%. Missile -2. Fire spells partial fizzle."),
        (3, "thunderstorm",  "Travel impossible. Lightning. Encounter rolls -1."),
        (2, "morning fog",   "Visibility 30 ft until midday."),
        (2, "leech weather", "CON check or 1 hp/hour exposed."),
    ],
    "summer": [
        (2, "humid clear",     "Brief breaks between storms."),
        (6, "heavy rain",      "Daily. Travel pace -25%. Missile -2. Fire spells fizzle."),
        (4, "thunderstorm",    "Travel impossible. Lightning hazard."),
        (3, "mosquito clouds", "Spell concentration broken; 1 hp/hour exposed."),
        (3, "sweltering",      "Oppressive heat. Double water consumption. CON check at noon."),
        (2, "leech weather",   "CON check or 1 hp/hour exposed."),
    ],
    "autumn": [
        (5, "humid clear",  "Air still close. No effect."),
        (5, "light rain",   "Tracking +1. Bows -1."),
        (4, "heavy rain",   "Travel pace -25%. Missile -2."),
        (3, "morning fog",  "Visibility 30 ft until midday."),
        (2, "thunderstorm", "Travel impossible."),
        (1, "leech weather","CON check or 1 hp/hour."),
    ],
    "winter": [
        (8, "humid clear",     "Closer to bearable. Standard water consumption."),
        (4, "overcast",        "Grey beneath the canopy."),
        (3, "light rain",      "Tracking +1. Bows -1."),
        (2, "morning fog",     "Visibility 30 ft until midday."),
        (2, "mosquito clouds", "Spell concentration broken; 1 hp/hour exposed."),
        (1, "sweltering",      "Heat returns briefly. Double water."),
    ],
}

# Urban: cosmetic city weather. Effects mostly social (markets close, fog
# emboldens cutpurses) rather than mechanical penalties.
_URBAN_TABLES: dict[str, list[tuple[int, str, str]]] = {
    "spring": [
        (6, "clear bright", "Pleasant. Markets full. No effect."),
        (4, "light rain",   "Cobbles slick. Outdoor markets thin out."),
        (3, "overcast",     "Grey skies. No effect."),
        (3, "morning fog",  "Visibility 30 ft in lower city until late morning. Cutpurses bold."),
        (2, "heavy rain",   "Streets running. Some shops shuttered. Bows -1 outdoors."),
        (2, "spring wind",  "Loose tiles, smoke from chimneys. Cosmetic."),
    ],
    "summer": [
        (8, "clear hot",      "Stench rises from sewers and middens. Reaction rolls -1 in poor quarters."),
        (4, "sweltering",     "Markets sluggish after noon. Indoor work suspended in heatwave."),
        (3, "afternoon storm","Brief cooling. Drains overwhelm; some streets flood briefly."),
        (2, "dust haze",      "Dry winds. Visibility reduced; eye irritation outdoors."),
        (2, "stench day",     "Heat plus plumbing equals miasma in poor quarters. CON save or sickened in slums."),
        (1, "fire weather",   "Hot dry wind. Watch nervous; town crier warns against open flame."),
    ],
    "autumn": [
        (5, "clear cool",  "Pleasant. No effect."),
        (5, "overcast",    "Damp, grey."),
        (4, "light rain",  "Cobbles slick."),
        (3, "heavy fog",   "Visibility 30 ft in lower city all day. Theft and assault more common."),
        (2, "windstorm",   "Loose tiles fall. 1-in-20 chance of 1d4 hazard for unlucky passers-by."),
        (1, "cold rain",   "Drives travellers indoors. Inns full; common rooms crowded."),
    ],
    "winter": [
        (4, "clear cold",    "Crisp. Frozen mud underfoot. CON check at night without shelter."),
        (5, "light snow",    "Streets quiet. Tracking impossible after 1 hr."),
        (4, "cold rain",     "Sleet. Travel uncomfortable but possible."),
        (3, "freezing fog",  "Visibility 60 ft. Surface ice — DEX check or fall."),
        (2, "heavy snow",    "Streets blocked. Most business shut. Travel pace -50%."),
        (2, "blizzard",      "Doors locked. Outsiders unwelcome at inns until storm clears."),
    ],
}

# Road: like wilderness but maintained — drainage, milestones, inns within
# a day's walk. Worst hazards are softened (paths cleared by road crews).
_ROAD_TABLES: dict[str, list[tuple[int, str, str]]] = {
    "spring": [
        (6, "clear",          "Pleasant travel. No effect."),
        (4, "overcast",       "Cool grey. No effect."),
        (4, "light rain",     "Packed earth slick. Bows -1."),
        (3, "heavy rain",     "Travel pace -15% (drainage helps)."),
        (2, "morning fog",    "Visibility 30 ft until midday on the road. Surprise checks +1."),
        (1, "thunderstorm",   "Shelter at next inn or roadside copse. Travel paused 2–4 hours."),
    ],
    "summer": [
        (8, "clear hot",      "Bright and warm. Standard water consumption."),
        (4, "dusty road",     "Dust kicked up by traffic. Visibility 1/2 normal at distance; eye irritation."),
        (3, "hot day",        "CON check at noon for armoured travellers."),
        (3, "afternoon storm","Brief downpour. Missile -2 during; most travellers wait it out."),
        (1, "drought wind",   "Dry, dusty."),
        (1, "heavy traffic",  "Caravans, pilgrims, patrols. Encounter rolls +1 (mostly social)."),
    ],
    "autumn": [
        (5, "clear",       "Crisp. No effect."),
        (4, "overcast",    "Grey and cool."),
        (5, "rain",        "Wet roads. Travel pace -10%. Missile -1."),
        (3, "fog",         "Dense morning fog. Visibility 30 ft until late morning. Bandit weather."),
        (2, "cold front",  "First chill. Travellers grumble for fires."),
        (1, "windstorm",   "Branches down. Travel pace -25% (road crews clear within a day)."),
    ],
    "winter": [
        (3, "clear cold",   "Bitter cold. CON check at night without shelter."),
        (4, "overcast cold","Wagon wheels stick on packed snow."),
        (4, "light snow",   "Travel pace -10%."),
        (4, "heavy snow",   "Travel pace -40% (slightly better than off-road). Missile -3."),
        (3, "freezing fog", "Visibility 60 ft. Surface ice — DEX check or fall."),
        (2, "blizzard",     "Travel impossible. Shelter at next inn or coaching house."),
    ],
}

# Splice all specialty tables into the main _TABLES so _terrain_table picks them up.
_SPECIALTY: dict[str, dict[str, list[tuple[int, str, str]]]] = {
    "desert":   _DESERT_TABLES,
    "arctic":   _ARCTIC_TABLES,
    "swamp":    _SWAMP_TABLES,
    "mountain": _MOUNTAIN_TABLES,
    "jungle":   _JUNGLE_TABLES,
    "urban":    _URBAN_TABLES,
    "road":     _ROAD_TABLES,
}
for _terrain, _by_season in _SPECIALTY.items():
    for _season, _entries in _by_season.items():
        _TABLES[_season][_terrain] = _entries

# Dungeon weather is more about subterranean atmosphere
_DUNGEON_TABLE = [
    (10, "stale air",    "No effect."),
    (3,  "draft",         "Cold wind from somewhere — torches gutter, -10 min."),
    (2,  "humid air",     "Damp. Bowstrings slack — bows -1 to hit until restrung."),
    (1,  "miasma",        "Foul air. CON check or take 1 hp/turn until clear of source."),
]


def _supported_terrains(season: str = "spring") -> list[str]:
    """Terrain keys with a defined table for the given season, plus dungeon."""
    season_key = season.lower() if season.lower() in SEASONS else "spring"
    season_tables = _TABLES.get(season_key, _TABLES["spring"])
    explicit = sorted(k for k, v in season_tables.items() if v is not None)
    return sorted(set(explicit) | {"dungeon"})


def _terrain_table(season: str, terrain: str) -> list[tuple[int, str, str]] | None:
    """Return the weighted weather table for (season, terrain), or None if unknown."""
    season_key = season.lower() if season.lower() in SEASONS else "spring"
    t = terrain.lower()
    if t == "dungeon":
        return _DUNGEON_TABLE
    season_tables = _TABLES.get(season_key, _TABLES["spring"])
    return season_tables.get(t)  # None if unknown — caller must handle


def _weighted_pick(table: list[tuple[int, str, str]]) -> tuple[str, str]:
    total = sum(w for w, _, _ in table)
    r = random.randint(1, total)
    acc = 0
    for w, label, effect in table:
        acc += w
        if r <= acc:
            return label, effect
    return table[-1][1], table[-1][2]


def register(mcp):

    @mcp.tool()
    def set_season(season: str) -> dict:
        """Set the current in-game season. One of: spring, summer, autumn, winter.
        Affects the weather_check tables. Default is 'spring' on a fresh campaign."""
        s = season.lower().strip()
        if s not in SEASONS:
            return {"error": f"Unknown season '{season}'. Use: {', '.join(SEASONS)}."}
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        state["current_season"] = s
        _c.save_state(cfg, state)
        return {"season": s}

    @mcp.tool()
    def weather_check(terrain: str = "wilderness") -> dict:
        """Roll weather for the current season and the given terrain.
        Call list_terrains() to see supported keys. Defaults to wilderness.
        Updates state.current_weather and returns the descriptor + mechanical effects.

        Call at session start, at dawn each travel day, and on any major terrain change.
        Don't roll silently — apply the effect to subsequent missile attacks, travel pace,
        and encounter checks."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        season = state.get("current_season", "spring")
        table = _terrain_table(season, terrain)
        if table is None:
            return {
                "error": f"Unknown terrain '{terrain}'. Call list_terrains() for supported keys.",
                "supported": _supported_terrains(season),
            }
        label, effect = _weighted_pick(table)

        descriptor = f"{label} ({terrain}, {season})"
        state["current_weather"] = descriptor
        _c.save_state(cfg, state)

        return {
            "season":  season,
            "terrain": terrain,
            "weather": label,
            "effect":  effect,
            "descriptor": descriptor,
        }

    @mcp.tool()
    def current_weather() -> dict:
        """Return the current weather (and season). Read-only — does not roll."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        return {
            "season":  state.get("current_season", "spring"),
            "weather": state.get("current_weather", ""),
        }

    @mcp.tool()
    def list_terrains() -> dict:
        """List supported terrain keys for weather_check / travel / check_encounter.
        Read-only."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        season = state.get("current_season", "spring")
        return {
            "season":   season,
            "terrains": _supported_terrains(season),
            "note":     "dungeon uses a separate atmosphere table (no seasons).",
        }
