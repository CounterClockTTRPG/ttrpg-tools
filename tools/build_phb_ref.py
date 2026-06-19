#!/usr/bin/env python3
"""Build ability_scores + proficiencies reference tables into global/2e.db.

Numeric mechanical values are sourced from the AD&D 2e ability and proficiency
tables (functional game mechanics). Column notes are written from scratch as
brief mechanical statements — no PHB prose is reproduced.

Idempotent: drops and recreates the four ref tables.
"""
import json
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "global" / "2e.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
DROP TABLE IF EXISTS ability_columns;
DROP TABLE IF EXISTS ability_scores;
DROP TABLE IF EXISTS ability_notes;
DROP TABLE IF EXISTS proficiencies;
DROP TABLE IF EXISTS proficiency_class_crossover;
DROP TABLE IF EXISTS turning_undead;

CREATE TABLE ability_notes (
    ability   TEXT PRIMARY KEY,
    headline  TEXT NOT NULL,
    xp_bonus  TEXT,
    extra     TEXT
);

CREATE TABLE ability_columns (
    id         INTEGER PRIMARY KEY,
    ability    TEXT NOT NULL,
    name       TEXT NOT NULL,
    short_name TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    note       TEXT
);
CREATE INDEX idx_ability_col ON ability_columns(ability, sort_order);

CREATE TABLE ability_scores (
    id         INTEGER PRIMARY KEY,
    ability    TEXT NOT NULL,
    score      TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    data       TEXT NOT NULL
);
CREATE INDEX idx_ability_row ON ability_scores(ability, sort_order);

CREATE TABLE proficiencies (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    slug           TEXT NOT NULL,
    group_name     TEXT NOT NULL,
    slots          INTEGER NOT NULL,
    ability        TEXT,
    check_modifier INTEGER
);
CREATE INDEX idx_prof_name  ON proficiencies(name);
CREATE INDEX idx_prof_group ON proficiencies(group_name);

CREATE TABLE proficiency_class_crossover (
    class_name TEXT PRIMARY KEY,
    groups     TEXT NOT NULL
);

CREATE TABLE turning_undead (
    id          INTEGER PRIMARY KEY,
    sort_order  INTEGER NOT NULL,
    undead_type TEXT NOT NULL,
    min_hd      INTEGER,
    max_hd      INTEGER,
    results     TEXT NOT NULL,   -- JSON list aligned to TURNING_LEVEL_COLUMNS
    aliases     TEXT NOT NULL    -- JSON list of name aliases (lowercase)
);
CREATE INDEX idx_turning_sort ON turning_undead(sort_order);
"""


# ---------------------------------------------------------------------------
# Turning Undead  (DMG Table 47)
# Rows: undead type (or HD bracket). Columns: priest level. Cell values:
#   numeric d20 target, "T" (auto-turned, no roll), "D" (destroyed),
#   "D*" (destroyed + extra 2d4), or "—" (cannot turn).
# Paladins use the priest column two lower than their level.
# ---------------------------------------------------------------------------
TURNING_LEVEL_COLUMNS = [
    "1", "2", "3", "4", "5", "6", "7", "8", "9",
    "10-11", "12-13", "14+",
]

TURNING_TABLE = [
    # sort, type, min_hd, max_hd, aliases, [12 cells]
    (1,  "Skeleton or 1 HD",  1,    1,    ["skeleton"],
     ["10","7","4","T","T","D","D","D*","D*","D*","D*","D*"]),
    (2,  "Zombie",            None, None, ["zombie"],
     ["13","10","7","4","T","T","D","D","D*","D*","D*","D*"]),
    (3,  "Ghoul or 2 HD",     2,    2,    ["ghoul"],
     ["16","13","10","7","4","T","T","D","D","D*","D*","D*"]),
    (4,  "Shadow or 3-4 HD",  3,    4,    ["shadow"],
     ["19","16","13","10","7","4","T","T","D","D","D*","D*"]),
    (5,  "Wight or 5 HD",     5,    5,    ["wight"],
     ["20","19","16","13","10","7","4","T","T","D","D","D*"]),
    (6,  "Ghast",             None, None, ["ghast"],
     ["—","20","19","16","13","10","7","4","T","T","D","D"]),
    (7,  "Wraith or 6 HD",    6,    6,    ["wraith"],
     ["—","—","20","19","16","13","10","7","4","T","T","D"]),
    (8,  "Mummy or 7 HD",     7,    7,    ["mummy"],
     ["—","—","—","20","19","16","13","10","7","4","T","T"]),
    (9,  "Spectre or 8 HD",   8,    8,    ["spectre", "specter"],
     ["—","—","—","—","20","19","16","13","10","7","4","T"]),
    (10, "Vampire or 9 HD",   9,    9,    ["vampire"],
     ["—","—","—","—","—","20","19","16","13","10","7","4"]),
    (11, "Ghost or 10 HD",    10,   10,   ["ghost"],
     ["—","—","—","—","—","—","20","19","16","13","10","7"]),
    (12, "Lich or 11+ HD",    11,   999,  ["lich"],
     ["—","—","—","—","—","—","—","20","19","16","13","10"]),
    (13, "Special",           None, None, ["special"],
     ["—","—","—","—","—","—","—","—","20","19","16","13"]),
]


# ---------------------------------------------------------------------------
# Ability notes  (one-line headlines + XP/special rules — original wording)
# ---------------------------------------------------------------------------
ABILITY_NOTES = {
    "strength": (
        "Muscle, endurance, and physical power. Prime requisite of warriors.",
        "Warriors with Strength 16+ gain a 10% XP bonus.",
        "A warrior with Strength 18 rolls percentile dice for exceptional "
        "Strength (18/01–50 through 18/00). Halfling fighters cannot roll "
        "exceptional Strength.",
    ),
    "dexterity": (
        "Agility, hand-eye coordination, reflexes, and balance. Prime "
        "requisite of rogues.",
        "Rogues with Dexterity 16+ gain a 10% XP bonus.",
        None,
    ),
    "constitution": (
        "Health, hardiness, and resistance to bodily shock.",
        None,
        "The starting Constitution score caps the number of times a "
        "character can be raised or resurrected. Only warriors gain the "
        "bracketed HP bonus at scores of 17 or higher.",
    ),
    "intelligence": (
        "Memory, reasoning, and learning capacity. Prime requisite of wizards.",
        "Wizards with Intelligence 16+ gain a 10% XP bonus.",
        None,
    ),
    "wisdom": (
        "Judgment, willpower, intuition, and common sense. Prime requisite "
        "of priests.",
        "Priests with Wisdom 16+ gain a 10% XP bonus.",
        None,
    ),
    "charisma": (
        "Persuasiveness, leadership, and personal magnetism.",
        None,
        None,
    ),
}


# ---------------------------------------------------------------------------
# Ability columns  (display name, short key, brief mechanical note)
# ---------------------------------------------------------------------------
ABILITY_COLUMNS = {
    "strength": [
        ("Hit Probability",    "hit_prob",   "Modifier to melee attack rolls."),
        ("Damage Adjustment",  "dmg_adj",    "Modifier to melee damage. Also applies to bow damage (bows must be specially made); crossbows excluded."),
        ("Weight Allowance",   "weight_allow", "Pounds carried with no encumbrance penalty."),
        ("Maximum Press",      "max_press",  "Heaviest weight (lbs) liftable overhead briefly."),
        ("Open Doors",         "open_doors", "Roll d20 ≤ this to force a stuck door. Parenthesised number applies to locked, barred, or magically held doors (one attempt only)."),
        ("Bend Bars/Lift Gates", "bend_bars", "Percentile chance to bend iron bars or lift a portcullis. One attempt per object."),
        ("Notes",              "notes",      "Equivalent monster Strength tier at this score."),
    ],
    "dexterity": [
        ("Reaction Adjustment",      "reaction_adj", "Modifier to surprise rolls."),
        ("Missile Attack Adjustment","missile_adj",  "Modifier to attack rolls with bows and thrown weapons."),
        ("Defensive Adjustment",     "defensive_adj","Modifier to AC and to saves vs attacks that can be dodged. Negative values improve AC (lower is better)."),
    ],
    "constitution": [
        ("HP Adjustment",         "hp_adj",    "Modifier applied to every hit die rolled. Warriors get the bracketed value. No die ever yields fewer than 1 HP."),
        ("System Shock",          "sys_shock", "Percent chance to survive magical transformations: polymorph, petrification, magical aging."),
        ("Resurrection Survival", "res_surv",  "Percent chance to successfully return from death via raise/resurrect magic."),
        ("Poison Save",           "poison_save","Modifier to saving throws vs poison (humans, elves, gnomes, half-elves)."),
        ("Regeneration",          "regen",     "Natural regeneration rate (1 HP per listed interval). Fire and acid damage never regenerate."),
    ],
    "intelligence": [
        ("Number of Languages",  "languages",     "Additional languages learnable beyond the native tongue. Also grants bonus nonweapon proficiency slots if that optional rule is in use."),
        ("Max Spell Level",      "max_spell_lvl", "Highest spell level a wizard with this Intelligence can ever cast."),
        ("Chance to Learn Spell","chance_learn",  "Percent chance for a wizard to successfully transcribe a newly encountered spell into their book."),
        ("Max #Spells/Level",    "max_spells",    "Maximum spells of any one level a wizard can know (optional rule)."),
        ("Illusion Immunity",    "illusion_immune","Illusion/phantasm spell levels the character automatically saves against."),
    ],
    "wisdom": [
        ("Magical Defense Adjustment", "magic_def_adj", "Modifier to saves against mind-affecting magic: charm, fear, illusion, hypnosis, suggestion, possession, beguiling."),
        ("Bonus Spells (Priest)",      "bonus_spells",  "Extra priest spell slots by spell level, on top of the normal allotment."),
        ("Chance Spell Failure",       "spell_fail",    "Percent chance any individual priest spell fizzles when cast."),
        ("Spell Immunity",             "spell_immune",  "Spells (or spell-like effects) the character is completely immune to. Cumulative as Wisdom rises."),
    ],
    "charisma": [
        ("Maximum Henchmen",   "max_henchmen", "Maximum permanent retainers a PC can attract. Does not affect mercenaries or hired servants."),
        ("Loyalty Base",       "loyalty",      "Modifier to loyalty checks for henchmen and other servitors."),
        ("Reaction Adjustment","reaction_adj", "Modifier to initial NPC reaction rolls."),
    ],
}


# ---------------------------------------------------------------------------
# Ability score rows  (numeric mechanical tables)
# Each entry: (score_label, sort_order, {short_name: value, ...})
# ---------------------------------------------------------------------------
STRENGTH_ROWS = [
    ("1",        1,  {"hit_prob": "-5",     "dmg_adj": "-4",    "weight_allow": "1",    "max_press": "3",    "open_doors": "1",      "bend_bars": "0%",  "notes": ""}),
    ("2",        2,  {"hit_prob": "-3",     "dmg_adj": "-2",    "weight_allow": "1",    "max_press": "5",    "open_doors": "1",      "bend_bars": "0%",  "notes": ""}),
    ("3",        3,  {"hit_prob": "-3",     "dmg_adj": "-1",    "weight_allow": "5",    "max_press": "10",   "open_doors": "2",      "bend_bars": "0%",  "notes": ""}),
    ("4-5",      4,  {"hit_prob": "-2",     "dmg_adj": "-1",    "weight_allow": "10",   "max_press": "25",   "open_doors": "3",      "bend_bars": "0%",  "notes": ""}),
    ("6-7",      5,  {"hit_prob": "-1",     "dmg_adj": "—","weight_allow": "20",   "max_press": "55",   "open_doors": "4",      "bend_bars": "0%",  "notes": ""}),
    ("8-9",      6,  {"hit_prob": "Normal", "dmg_adj": "—","weight_allow": "35",   "max_press": "90",   "open_doors": "5",      "bend_bars": "1%",  "notes": ""}),
    ("10-11",    7,  {"hit_prob": "Normal", "dmg_adj": "—","weight_allow": "40",   "max_press": "115",  "open_doors": "6",      "bend_bars": "2%",  "notes": ""}),
    ("12-13",    8,  {"hit_prob": "Normal", "dmg_adj": "—","weight_allow": "45",   "max_press": "140",  "open_doors": "7",      "bend_bars": "4%",  "notes": ""}),
    ("14-15",    9,  {"hit_prob": "Normal", "dmg_adj": "—","weight_allow": "55",   "max_press": "170",  "open_doors": "8",      "bend_bars": "7%",  "notes": ""}),
    ("16",       10, {"hit_prob": "Normal", "dmg_adj": "+1",    "weight_allow": "70",   "max_press": "195",  "open_doors": "9",      "bend_bars": "10%", "notes": ""}),
    ("17",       11, {"hit_prob": "+1",     "dmg_adj": "+1",    "weight_allow": "85",   "max_press": "220",  "open_doors": "10",     "bend_bars": "13%", "notes": ""}),
    ("18",       12, {"hit_prob": "+1",     "dmg_adj": "+2",    "weight_allow": "110",  "max_press": "255",  "open_doors": "11",     "bend_bars": "16%", "notes": ""}),
    ("18/01-50", 13, {"hit_prob": "+1",     "dmg_adj": "+3",    "weight_allow": "135",  "max_press": "280",  "open_doors": "12",     "bend_bars": "20%", "notes": ""}),
    ("18/51-75", 14, {"hit_prob": "+2",     "dmg_adj": "+3",    "weight_allow": "160",  "max_press": "305",  "open_doors": "13",     "bend_bars": "25%", "notes": ""}),
    ("18/76-90", 15, {"hit_prob": "+2",     "dmg_adj": "+4",    "weight_allow": "185",  "max_press": "330",  "open_doors": "14",     "bend_bars": "30%", "notes": ""}),
    ("18/91-99", 16, {"hit_prob": "+2",     "dmg_adj": "+5",    "weight_allow": "235",  "max_press": "380",  "open_doors": "15(3)",  "bend_bars": "35%", "notes": ""}),
    ("18/00",    17, {"hit_prob": "+3",     "dmg_adj": "+6",    "weight_allow": "335",  "max_press": "480",  "open_doors": "16(6)",  "bend_bars": "40%", "notes": ""}),
    ("19",       18, {"hit_prob": "+3",     "dmg_adj": "+7",    "weight_allow": "485",  "max_press": "640",  "open_doors": "16(8)",  "bend_bars": "50%", "notes": "Hill Giant"}),
    ("20",       19, {"hit_prob": "+3",     "dmg_adj": "+8",    "weight_allow": "535",  "max_press": "700",  "open_doors": "17(10)", "bend_bars": "60%", "notes": "Stone Giant"}),
    ("21",       20, {"hit_prob": "+4",     "dmg_adj": "+9",    "weight_allow": "635",  "max_press": "810",  "open_doors": "17(12)", "bend_bars": "70%", "notes": "Frost Giant"}),
    ("22",       21, {"hit_prob": "+4",     "dmg_adj": "+10",   "weight_allow": "785",  "max_press": "970",  "open_doors": "18(14)", "bend_bars": "80%", "notes": "Fire Giant"}),
    ("23",       22, {"hit_prob": "+5",     "dmg_adj": "+11",   "weight_allow": "935",  "max_press": "1,130","open_doors": "18(16)", "bend_bars": "90%", "notes": "Cloud Giant"}),
    ("24",       23, {"hit_prob": "+6",     "dmg_adj": "+12",   "weight_allow": "1,235","max_press": "1,440","open_doors": "19(17)", "bend_bars": "95%", "notes": "Storm Giant"}),
    ("25",       24, {"hit_prob": "+7",     "dmg_adj": "+14",   "weight_allow": "1,535","max_press": "1,750","open_doors": "19(18)", "bend_bars": "99%", "notes": "Titan"}),
]

DEXTERITY_ROWS = [
    ("1",     1,  {"reaction_adj": "-6", "missile_adj": "-6", "defensive_adj": "+5"}),
    ("2",     2,  {"reaction_adj": "-4", "missile_adj": "-4", "defensive_adj": "+5"}),
    ("3",     3,  {"reaction_adj": "-3", "missile_adj": "-3", "defensive_adj": "+4"}),
    ("4",     4,  {"reaction_adj": "-2", "missile_adj": "-2", "defensive_adj": "+3"}),
    ("5",     5,  {"reaction_adj": "-1", "missile_adj": "-1", "defensive_adj": "+2"}),
    ("6",     6,  {"reaction_adj": "0",  "missile_adj": "0",  "defensive_adj": "+1"}),
    ("7",     7,  {"reaction_adj": "0",  "missile_adj": "0",  "defensive_adj": "0"}),
    ("8",     8,  {"reaction_adj": "0",  "missile_adj": "0",  "defensive_adj": "0"}),
    ("9",     9,  {"reaction_adj": "0",  "missile_adj": "0",  "defensive_adj": "0"}),
    ("10-14", 10, {"reaction_adj": "0",  "missile_adj": "0",  "defensive_adj": "0"}),
    ("15",    11, {"reaction_adj": "0",  "missile_adj": "0",  "defensive_adj": "-1"}),
    ("16",    12, {"reaction_adj": "+1", "missile_adj": "+1", "defensive_adj": "-2"}),
    ("17",    13, {"reaction_adj": "+2", "missile_adj": "+2", "defensive_adj": "-3"}),
    ("18",    14, {"reaction_adj": "+2", "missile_adj": "+2", "defensive_adj": "-4"}),
    ("19",    15, {"reaction_adj": "+3", "missile_adj": "+3", "defensive_adj": "-4"}),
    ("20",    16, {"reaction_adj": "+3", "missile_adj": "+3", "defensive_adj": "-4"}),
    ("21",    17, {"reaction_adj": "+4", "missile_adj": "+4", "defensive_adj": "-5"}),
    ("22",    18, {"reaction_adj": "+4", "missile_adj": "+4", "defensive_adj": "-5"}),
    ("23",    19, {"reaction_adj": "+4", "missile_adj": "+4", "defensive_adj": "-5"}),
    ("24",    20, {"reaction_adj": "+5", "missile_adj": "+5", "defensive_adj": "-6"}),
    ("25",    21, {"reaction_adj": "+5", "missile_adj": "+5", "defensive_adj": "-6"}),
]

CONSTITUTION_ROWS = [
    ("1",  1,  {"hp_adj": "-3",     "sys_shock": "25%",  "res_surv": "30%",  "poison_save": "-2", "regen": "nil"}),
    ("2",  2,  {"hp_adj": "-2",     "sys_shock": "30%",  "res_surv": "35%",  "poison_save": "-1", "regen": "nil"}),
    ("3",  3,  {"hp_adj": "-2",     "sys_shock": "35%",  "res_surv": "40%",  "poison_save": "0",  "regen": "nil"}),
    ("4",  4,  {"hp_adj": "-1",     "sys_shock": "40%",  "res_surv": "45%",  "poison_save": "0",  "regen": "nil"}),
    ("5",  5,  {"hp_adj": "-1",     "sys_shock": "45%",  "res_surv": "50%",  "poison_save": "0",  "regen": "nil"}),
    ("6",  6,  {"hp_adj": "-1",     "sys_shock": "50%",  "res_surv": "55%",  "poison_save": "0",  "regen": "nil"}),
    ("7",  7,  {"hp_adj": "0",      "sys_shock": "55%",  "res_surv": "60%",  "poison_save": "0",  "regen": "nil"}),
    ("8",  8,  {"hp_adj": "0",      "sys_shock": "60%",  "res_surv": "65%",  "poison_save": "0",  "regen": "nil"}),
    ("9",  9,  {"hp_adj": "0",      "sys_shock": "65%",  "res_surv": "70%",  "poison_save": "0",  "regen": "nil"}),
    ("10", 10, {"hp_adj": "0",      "sys_shock": "70%",  "res_surv": "75%",  "poison_save": "0",  "regen": "nil"}),
    ("11", 11, {"hp_adj": "0",      "sys_shock": "75%",  "res_surv": "80%",  "poison_save": "0",  "regen": "nil"}),
    ("12", 12, {"hp_adj": "0",      "sys_shock": "80%",  "res_surv": "85%",  "poison_save": "0",  "regen": "nil"}),
    ("13", 13, {"hp_adj": "0",      "sys_shock": "85%",  "res_surv": "90%",  "poison_save": "0",  "regen": "nil"}),
    ("14", 14, {"hp_adj": "0",      "sys_shock": "88%",  "res_surv": "92%",  "poison_save": "0",  "regen": "nil"}),
    ("15", 15, {"hp_adj": "+1",     "sys_shock": "90%",  "res_surv": "94%",  "poison_save": "0",  "regen": "nil"}),
    ("16", 16, {"hp_adj": "+2",     "sys_shock": "95%",  "res_surv": "96%",  "poison_save": "0",  "regen": "nil"}),
    ("17", 17, {"hp_adj": "+2(+3)", "sys_shock": "97%",  "res_surv": "98%",  "poison_save": "0",  "regen": "nil"}),
    ("18", 18, {"hp_adj": "+2(+4)", "sys_shock": "99%",  "res_surv": "100%", "poison_save": "0",  "regen": "nil"}),
    ("19", 19, {"hp_adj": "+2(+5)", "sys_shock": "99%",  "res_surv": "100%", "poison_save": "+1", "regen": "nil"}),
    ("20", 20, {"hp_adj": "+2(+5)", "sys_shock": "99%",  "res_surv": "100%", "poison_save": "+1", "regen": "1 HP / 6 turns"}),
    ("21", 21, {"hp_adj": "+2(+6)", "sys_shock": "99%",  "res_surv": "100%", "poison_save": "+2", "regen": "1 HP / 5 turns"}),
    ("22", 22, {"hp_adj": "+2(+6)", "sys_shock": "99%",  "res_surv": "100%", "poison_save": "+2", "regen": "1 HP / 4 turns"}),
    ("23", 23, {"hp_adj": "+2(+6)", "sys_shock": "99%",  "res_surv": "100%", "poison_save": "+3", "regen": "1 HP / 3 turns"}),
    ("24", 24, {"hp_adj": "+2(+7)", "sys_shock": "99%",  "res_surv": "100%", "poison_save": "+3", "regen": "1 HP / 2 turns"}),
    ("25", 25, {"hp_adj": "+2(+7)", "sys_shock": "100%", "res_surv": "100%", "poison_save": "+4", "regen": "1 HP / turn"}),
]

INTELLIGENCE_ROWS = [
    ("1",  1,  {"languages": "0",  "max_spell_lvl": "—",  "chance_learn": "—",  "max_spells": "—", "illusion_immune": "—"}),
    ("2",  2,  {"languages": "1",  "max_spell_lvl": "—",  "chance_learn": "—",  "max_spells": "—", "illusion_immune": "—"}),
    ("3",  3,  {"languages": "1",  "max_spell_lvl": "—",  "chance_learn": "—",  "max_spells": "—", "illusion_immune": "—"}),
    ("4",  4,  {"languages": "1",  "max_spell_lvl": "—",  "chance_learn": "—",  "max_spells": "—", "illusion_immune": "—"}),
    ("5",  5,  {"languages": "1",  "max_spell_lvl": "—",  "chance_learn": "—",  "max_spells": "—", "illusion_immune": "—"}),
    ("6",  6,  {"languages": "1",  "max_spell_lvl": "—",  "chance_learn": "—",  "max_spells": "—", "illusion_immune": "—"}),
    ("7",  7,  {"languages": "1",  "max_spell_lvl": "—",  "chance_learn": "—",  "max_spells": "—", "illusion_immune": "—"}),
    ("8",  8,  {"languages": "1",  "max_spell_lvl": "—",  "chance_learn": "—",  "max_spells": "—", "illusion_immune": "—"}),
    ("9",  9,  {"languages": "2",  "max_spell_lvl": "4th",     "chance_learn": "35%",     "max_spells": "6",      "illusion_immune": "—"}),
    ("10", 10, {"languages": "2",  "max_spell_lvl": "5th",     "chance_learn": "40%",     "max_spells": "7",      "illusion_immune": "—"}),
    ("11", 11, {"languages": "2",  "max_spell_lvl": "5th",     "chance_learn": "45%",     "max_spells": "7",      "illusion_immune": "—"}),
    ("12", 12, {"languages": "3",  "max_spell_lvl": "6th",     "chance_learn": "50%",     "max_spells": "7",      "illusion_immune": "—"}),
    ("13", 13, {"languages": "3",  "max_spell_lvl": "6th",     "chance_learn": "55%",     "max_spells": "9",      "illusion_immune": "—"}),
    ("14", 14, {"languages": "4",  "max_spell_lvl": "7th",     "chance_learn": "60%",     "max_spells": "9",      "illusion_immune": "—"}),
    ("15", 15, {"languages": "4",  "max_spell_lvl": "7th",     "chance_learn": "65%",     "max_spells": "11",     "illusion_immune": "—"}),
    ("16", 16, {"languages": "5",  "max_spell_lvl": "8th",     "chance_learn": "70%",     "max_spells": "11",     "illusion_immune": "—"}),
    ("17", 17, {"languages": "6",  "max_spell_lvl": "8th",     "chance_learn": "75%",     "max_spells": "14",     "illusion_immune": "—"}),
    ("18", 18, {"languages": "7",  "max_spell_lvl": "9th",     "chance_learn": "85%",     "max_spells": "18",     "illusion_immune": "—"}),
    ("19", 19, {"languages": "8",  "max_spell_lvl": "9th",     "chance_learn": "95%",     "max_spells": "All",    "illusion_immune": "1st level"}),
    ("20", 20, {"languages": "9",  "max_spell_lvl": "9th",     "chance_learn": "96%",     "max_spells": "All",    "illusion_immune": "2nd level"}),
    ("21", 21, {"languages": "10", "max_spell_lvl": "9th",     "chance_learn": "97%",     "max_spells": "All",    "illusion_immune": "3rd level"}),
    ("22", 22, {"languages": "11", "max_spell_lvl": "9th",     "chance_learn": "98%",     "max_spells": "All",    "illusion_immune": "4th level"}),
    ("23", 23, {"languages": "12", "max_spell_lvl": "9th",     "chance_learn": "99%",     "max_spells": "All",    "illusion_immune": "5th level"}),
    ("24", 24, {"languages": "15", "max_spell_lvl": "9th",     "chance_learn": "100%",    "max_spells": "All",    "illusion_immune": "6th level"}),
    ("25", 25, {"languages": "20", "max_spell_lvl": "9th",     "chance_learn": "100%",    "max_spells": "All",    "illusion_immune": "7th level"}),
]

WISDOM_ROWS = [
    ("1",  1,  {"magic_def_adj": "-6", "bonus_spells": "—",    "spell_fail": "80%", "spell_immune": "—"}),
    ("2",  2,  {"magic_def_adj": "-4", "bonus_spells": "—",    "spell_fail": "60%", "spell_immune": "—"}),
    ("3",  3,  {"magic_def_adj": "-3", "bonus_spells": "—",    "spell_fail": "50%", "spell_immune": "—"}),
    ("4",  4,  {"magic_def_adj": "-2", "bonus_spells": "—",    "spell_fail": "45%", "spell_immune": "—"}),
    ("5",  5,  {"magic_def_adj": "-1", "bonus_spells": "—",    "spell_fail": "40%", "spell_immune": "—"}),
    ("6",  6,  {"magic_def_adj": "-1", "bonus_spells": "—",    "spell_fail": "35%", "spell_immune": "—"}),
    ("7",  7,  {"magic_def_adj": "-1", "bonus_spells": "—",    "spell_fail": "30%", "spell_immune": "—"}),
    ("8",  8,  {"magic_def_adj": "0",  "bonus_spells": "—",    "spell_fail": "25%", "spell_immune": "—"}),
    ("9",  9,  {"magic_def_adj": "0",  "bonus_spells": "0",         "spell_fail": "20%", "spell_immune": "—"}),
    ("10", 10, {"magic_def_adj": "0",  "bonus_spells": "0",         "spell_fail": "15%", "spell_immune": "—"}),
    ("11", 11, {"magic_def_adj": "0",  "bonus_spells": "0",         "spell_fail": "10%", "spell_immune": "—"}),
    ("12", 12, {"magic_def_adj": "0",  "bonus_spells": "0",         "spell_fail": "5%",  "spell_immune": "—"}),
    ("13", 13, {"magic_def_adj": "0",  "bonus_spells": "1st",       "spell_fail": "0%",  "spell_immune": "—"}),
    ("14", 14, {"magic_def_adj": "0",  "bonus_spells": "1st",       "spell_fail": "0%",  "spell_immune": "—"}),
    ("15", 15, {"magic_def_adj": "+1", "bonus_spells": "2nd",       "spell_fail": "0%",  "spell_immune": "—"}),
    ("16", 16, {"magic_def_adj": "+2", "bonus_spells": "2nd",       "spell_fail": "0%",  "spell_immune": "—"}),
    ("17", 17, {"magic_def_adj": "+3", "bonus_spells": "3rd",       "spell_fail": "0%",  "spell_immune": "—"}),
    ("18", 18, {"magic_def_adj": "+4", "bonus_spells": "4th",       "spell_fail": "0%",  "spell_immune": "—"}),
    ("19", 19, {"magic_def_adj": "+4", "bonus_spells": "1st, 3rd",  "spell_fail": "0%",  "spell_immune": "Cause Fear, Charm Person, Command, Friends, Hypnotism"}),
    ("20", 20, {"magic_def_adj": "+4", "bonus_spells": "2nd, 4th",  "spell_fail": "0%",  "spell_immune": "Forget, Hold Person, Ray of Enfeeblement, Scare"}),
    ("21", 21, {"magic_def_adj": "+4", "bonus_spells": "3rd, 5th",  "spell_fail": "0%",  "spell_immune": "Fear"}),
    ("22", 22, {"magic_def_adj": "+4", "bonus_spells": "4th, 5th",  "spell_fail": "0%",  "spell_immune": "Charm Monster, Confusion, Emotion, Fumble, Suggestion"}),
    ("23", 23, {"magic_def_adj": "+4", "bonus_spells": "1st, 6th",  "spell_fail": "0%",  "spell_immune": "Chaos, Feeblemind, Hold Monster, Magic Jar, Quest"}),
    ("24", 24, {"magic_def_adj": "+4", "bonus_spells": "5th, 6th",  "spell_fail": "0%",  "spell_immune": "Geas, Mass Suggestion, Rod of Rulership"}),
    ("25", 25, {"magic_def_adj": "+4", "bonus_spells": "6th, 7th",  "spell_fail": "0%",  "spell_immune": "Antipathy/Sympathy, Death Spell, Mass Charm"}),
]

CHARISMA_ROWS = [
    ("1",  1,  {"max_henchmen": "0",  "loyalty": "-8",  "reaction_adj": "-7"}),
    ("2",  2,  {"max_henchmen": "1",  "loyalty": "-7",  "reaction_adj": "-6"}),
    ("3",  3,  {"max_henchmen": "1",  "loyalty": "-6",  "reaction_adj": "-5"}),
    ("4",  4,  {"max_henchmen": "1",  "loyalty": "-5",  "reaction_adj": "-4"}),
    ("5",  5,  {"max_henchmen": "2",  "loyalty": "-4",  "reaction_adj": "-3"}),
    ("6",  6,  {"max_henchmen": "2",  "loyalty": "-3",  "reaction_adj": "-2"}),
    ("7",  7,  {"max_henchmen": "3",  "loyalty": "-2",  "reaction_adj": "-1"}),
    ("8",  8,  {"max_henchmen": "3",  "loyalty": "-1",  "reaction_adj": "0"}),
    ("9",  9,  {"max_henchmen": "4",  "loyalty": "0",   "reaction_adj": "0"}),
    ("10", 10, {"max_henchmen": "4",  "loyalty": "0",   "reaction_adj": "0"}),
    ("11", 11, {"max_henchmen": "4",  "loyalty": "0",   "reaction_adj": "0"}),
    ("12", 12, {"max_henchmen": "5",  "loyalty": "0",   "reaction_adj": "0"}),
    ("13", 13, {"max_henchmen": "5",  "loyalty": "0",   "reaction_adj": "+1"}),
    ("14", 14, {"max_henchmen": "6",  "loyalty": "+1",  "reaction_adj": "+2"}),
    ("15", 15, {"max_henchmen": "7",  "loyalty": "+3",  "reaction_adj": "+3"}),
    ("16", 16, {"max_henchmen": "8",  "loyalty": "+4",  "reaction_adj": "+5"}),
    ("17", 17, {"max_henchmen": "10", "loyalty": "+6",  "reaction_adj": "+6"}),
    ("18", 18, {"max_henchmen": "15", "loyalty": "+8",  "reaction_adj": "+7"}),
    ("19", 19, {"max_henchmen": "20", "loyalty": "+10", "reaction_adj": "+8"}),
    ("20", 20, {"max_henchmen": "25", "loyalty": "+12", "reaction_adj": "+9"}),
    ("21", 21, {"max_henchmen": "30", "loyalty": "+14", "reaction_adj": "+10"}),
    ("22", 22, {"max_henchmen": "35", "loyalty": "+16", "reaction_adj": "+11"}),
    ("23", 23, {"max_henchmen": "40", "loyalty": "+18", "reaction_adj": "+12"}),
    ("24", 24, {"max_henchmen": "45", "loyalty": "+20", "reaction_adj": "+13"}),
    ("25", 25, {"max_henchmen": "50", "loyalty": "+20", "reaction_adj": "+14"}),
]

ABILITY_ROWS = {
    "strength":     STRENGTH_ROWS,
    "dexterity":    DEXTERITY_ROWS,
    "constitution": CONSTITUTION_ROWS,
    "intelligence": INTELLIGENCE_ROWS,
    "wisdom":       WISDOM_ROWS,
    "charisma":     CHARISMA_ROWS,
}


# ---------------------------------------------------------------------------
# Proficiencies  (group, name, slots, ability, modifier)
# A modifier of None means "NA" (e.g. Blind-fighting, Mountaineering).
# ---------------------------------------------------------------------------
PROFICIENCIES = [
    # General
    ("general", "Agriculture",         1, "Intelligence", 0),
    ("general", "Animal Handling",     1, "Wisdom",       -1),
    ("general", "Animal Training",     1, "Wisdom",       0),
    ("general", "Artistic Ability",    1, "Wisdom",       0),
    ("general", "Blacksmithing",       1, "Strength",     0),
    ("general", "Brewing",             1, "Intelligence", 0),
    ("general", "Carpentry",           1, "Strength",     0),
    ("general", "Cobbling",            1, "Dexterity",    0),
    ("general", "Cooking",             1, "Intelligence", 0),
    ("general", "Dancing",             1, "Dexterity",    0),
    ("general", "Direction Sense",     1, "Wisdom",       1),
    ("general", "Etiquette",           1, "Charisma",     0),
    ("general", "Fire-building",       1, "Wisdom",       -1),
    ("general", "Fishing",             1, "Wisdom",       -1),
    ("general", "Heraldry",            1, "Intelligence", 0),
    ("general", "Languages, Modern",   1, "Intelligence", 0),
    ("general", "Leatherworking",      1, "Intelligence", 0),
    ("general", "Mining",              2, "Wisdom",       -3),
    ("general", "Pottery",             1, "Dexterity",    -2),
    ("general", "Riding, Airborne",    2, "Wisdom",       -2),
    ("general", "Riding, Land-Based",  1, "Wisdom",       3),
    ("general", "Rope Use",            1, "Dexterity",    0),
    ("general", "Seamanship",          1, "Dexterity",    1),
    ("general", "Seamstress/Tailor",   1, "Dexterity",    -1),
    ("general", "Singing",             1, "Charisma",     0),
    ("general", "Stonemasonry",        1, "Strength",     -2),
    ("general", "Swimming",            1, "Strength",     0),
    ("general", "Weather Sense",       1, "Wisdom",       -1),
    ("general", "Weaving",             1, "Intelligence", -1),
    # Priest
    ("priest",  "Ancient History",     1, "Intelligence", -1),
    ("priest",  "Astrology",           2, "Intelligence", 0),
    ("priest",  "Engineering",         2, "Intelligence", -3),
    ("priest",  "Healing",             2, "Wisdom",       -2),
    ("priest",  "Herbalism",           2, "Intelligence", -2),
    ("priest",  "Languages, Ancient",  1, "Intelligence", 0),
    ("priest",  "Local History",       1, "Charisma",     0),
    ("priest",  "Musical Instrument",  1, "Dexterity",    -1),
    ("priest",  "Navigation",          1, "Intelligence", -2),
    ("priest",  "Reading/Writing",     1, "Intelligence", 1),
    ("priest",  "Religion",            1, "Wisdom",       0),
    ("priest",  "Spellcraft",          1, "Intelligence", -2),
    # Rogue
    ("rogue",   "Ancient History",     1, "Intelligence", -1),
    ("rogue",   "Appraising",          1, "Intelligence", 0),
    ("rogue",   "Blind-fighting",      2, None,           None),
    ("rogue",   "Disguise",            1, "Charisma",     -1),
    ("rogue",   "Forgery",             1, "Dexterity",    -1),
    ("rogue",   "Gaming",              1, "Charisma",     0),
    ("rogue",   "Gem Cutting",         2, "Dexterity",    -2),
    ("rogue",   "Juggling",            1, "Dexterity",    -1),
    ("rogue",   "Jumping",             1, "Strength",     0),
    ("rogue",   "Local History",       1, "Charisma",     0),
    ("rogue",   "Musical Instrument",  1, "Dexterity",    -1),
    ("rogue",   "Reading Lips",        2, "Intelligence", -2),
    ("rogue",   "Set Snares",          1, "Dexterity",    -1),
    ("rogue",   "Tightrope Walking",   1, "Dexterity",    0),
    ("rogue",   "Tumbling",            1, "Dexterity",    0),
    ("rogue",   "Ventriloquism",       1, "Intelligence", -2),
    # Warrior
    ("warrior", "Animal Lore",         1, "Intelligence", 0),
    ("warrior", "Armorer",             2, "Intelligence", -2),
    ("warrior", "Blind-fighting",      2, None,           None),
    ("warrior", "Bowyer/Fletcher",     1, "Dexterity",    -1),
    ("warrior", "Charioteering",       1, "Dexterity",    2),
    ("warrior", "Endurance",           2, "Constitution", 0),
    ("warrior", "Gaming",              1, "Charisma",     0),
    ("warrior", "Hunting",             1, "Wisdom",       -1),
    ("warrior", "Mountaineering",      1, None,           None),
    ("warrior", "Navigation",          1, "Intelligence", -2),
    ("warrior", "Running",             1, "Constitution", -6),
    ("warrior", "Set Snares",          1, "Dexterity",    -1),
    ("warrior", "Survival",            2, "Intelligence", 0),
    ("warrior", "Tracking",            2, "Wisdom",       0),
    ("warrior", "Weaponsmithing",      3, "Intelligence", -3),
    # Wizard
    ("wizard",  "Ancient History",     1, "Intelligence", -1),
    ("wizard",  "Astrology",           2, "Intelligence", 0),
    ("wizard",  "Engineering",         2, "Intelligence", -3),
    ("wizard",  "Gem Cutting",         2, "Dexterity",    -2),
    ("wizard",  "Herbalism",           2, "Intelligence", -2),
    ("wizard",  "Languages, Ancient",  1, "Intelligence", 0),
    ("wizard",  "Navigation",          1, "Intelligence", -2),
    ("wizard",  "Reading/Writing",     1, "Intelligence", 1),
    ("wizard",  "Religion",            1, "Wisdom",       0),
    ("wizard",  "Spellcraft",          1, "Intelligence", -2),
]


# Class-to-proficiency-group crossover (PHB Table 38)
CLASS_CROSSOVER = {
    "Fighter":     ["warrior", "general"],
    "Paladin":     ["warrior", "priest", "general"],
    "Ranger":      ["warrior", "wizard", "general"],
    "Cleric":      ["priest", "general"],
    "Druid":       ["priest", "warrior", "general"],
    "Mage":        ["wizard", "general"],
    "Illusionist": ["wizard", "general"],
    "Thief":       ["rogue", "general"],
    "Bard":        ["rogue", "warrior", "wizard", "general"],
}


def slugify(s: str) -> str:
    out = []
    for ch in s.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " /,-":
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def main() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.executescript(SCHEMA)

        # ability_notes
        for ability, (headline, xp, extra) in ABILITY_NOTES.items():
            conn.execute(
                "INSERT INTO ability_notes (ability, headline, xp_bonus, extra) "
                "VALUES (?, ?, ?, ?)",
                (ability, headline, xp, extra),
            )

        # ability_columns
        for ability, cols in ABILITY_COLUMNS.items():
            for sort_order, (name, short, note) in enumerate(cols, 1):
                conn.execute(
                    "INSERT INTO ability_columns "
                    "(ability, name, short_name, sort_order, note) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (ability, name, short, sort_order, note),
                )

        # ability_scores
        for ability, rows in ABILITY_ROWS.items():
            for score, sort_order, data in rows:
                conn.execute(
                    "INSERT INTO ability_scores "
                    "(ability, score, sort_order, data) "
                    "VALUES (?, ?, ?, ?)",
                    (ability, score, sort_order, json.dumps(data)),
                )

        # proficiencies
        for group, name, slots, ability, modifier in PROFICIENCIES:
            conn.execute(
                "INSERT INTO proficiencies "
                "(name, slug, group_name, slots, ability, check_modifier) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, slugify(name), group, slots, ability, modifier),
            )

        # proficiency_class_crossover
        for cls, groups in CLASS_CROSSOVER.items():
            conn.execute(
                "INSERT INTO proficiency_class_crossover "
                "(class_name, groups) VALUES (?, ?)",
                (cls, json.dumps(groups)),
            )

        # turning_undead
        for sort_order, undead_type, min_hd, max_hd, aliases, results in TURNING_TABLE:
            assert len(results) == len(TURNING_LEVEL_COLUMNS), (
                f"row {undead_type!r} has {len(results)} cells, "
                f"expected {len(TURNING_LEVEL_COLUMNS)}"
            )
            conn.execute(
                "INSERT INTO turning_undead "
                "(sort_order, undead_type, min_hd, max_hd, results, aliases) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sort_order, undead_type, min_hd, max_hd,
                 json.dumps(results), json.dumps(aliases)),
            )

        conn.commit()

        # Summary
        for tbl in ("ability_notes", "ability_columns", "ability_scores",
                    "proficiencies", "proficiency_class_crossover",
                    "turning_undead"):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"  {tbl}: {n} rows")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
