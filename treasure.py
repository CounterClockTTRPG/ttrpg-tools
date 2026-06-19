#!/usr/bin/env python3
"""AD&D 2e Treasure Generator — DMG Tables 84-110.

Treasure types come in two categories:
  Lair treasure   (A-I): hoard of the whole group/den — roll once per encounter
  Individual      (J-Z): what each creature carries — roll per monster

Monster entries show both: e.g. gnoll "D,Qx5,S (L,M)" means
  lair = D + Q×5 + S, individual per gnoll = L + M

Usage:
    python3 treasure.py A               # lair hoard, type A
    python3 treasure.py D Qx5 S         # lair hoard with Qx5 notation
    python3 treasure.py L M -n 4        # individual treasure for 4 gnolls
    python3 treasure.py H --quiet       # suppress failed-roll detail
"""

import random
import re
import sys
import argparse

# ---------------------------------------------------------------------------
# Type notation helpers
# ---------------------------------------------------------------------------

def expand_types(raw_types):
    """Expand 'Qx5' shorthand to ['Q','Q','Q','Q','Q'], uppercase all."""
    out = []
    for t in raw_types:
        m = re.match(r'^([A-Za-z])x(\d+)$', t, re.I)
        if m:
            out.extend([m.group(1).upper()] * int(m.group(2)))
        else:
            out.append(t.upper())
    return out

# ---------------------------------------------------------------------------
# Dice helpers
# ---------------------------------------------------------------------------

def roll(n, sides):
    return sum(random.randint(1, sides) for _ in range(n))

def d100():
    return random.randint(1, 100)

def rng(lo, hi):
    return random.randint(lo, hi)

def pct(chance):
    return random.randint(1, 100) <= chance

# ---------------------------------------------------------------------------
# Treasure type table (Table 84)
# Each entry: (lo, hi, chance_pct) — chance=None means automatic
# magic_type: 'any' | 'any_no_weapons' | 'armor_weapon' | 'potion' | 'scroll'
# magic_extra: extra forced item type appended ('+potion' or '+scroll')
#
# NOTE on the 'ep' key: in the 2e DMG (Table 84) the fourth coin column is a
# single "Platinum or Electrum*" column, where the footnote * reads "DM's
# choice". We store that column's range under 'ep'; at generation it resolves
# to electrum OR platinum (see PLATINUM_CHANCE). 1 pp = 5 gp (PHB Table 42).
# ---------------------------------------------------------------------------

# Odds that the "Platinum or Electrum" column (Table 84) comes up PLATINUM for a
# given hoard rather than electrum — the DMG leaves this to "DM's choice", so
# this is the automated stand-in. 0.0 = always electrum (legacy behaviour),
# 1.0 = always platinum.
PLATINUM_CHANCE = 0.5

TYPES = {
    # --- Lair Treasures (A-I) ---
    'A': dict(cp=(1000,3000,25), sp=(200,2000,30), gp=(1000,6000,40),
              ep=(300,1800,35), gems=(10,40,60), art=(2,12,50),
              magic=(3,'any',30)),
    'B': dict(cp=(1000,6000,50), sp=(1000,3000,25), gp=(200,2000,25),
              ep=(100,1000,25), gems=(1,8,30), art=(1,4,20),
              magic=(1,'armor_weapon',10)),
    'C': dict(cp=(1000,10000,20), sp=(1000,6000,30), gp=None,
              ep=(100,600,10), gems=(1,6,25), art=(1,3,20),
              magic=(2,'any',10)),
    'D': dict(cp=(1000,6000,10), sp=(1000,10000,15), gp=(1000,10000,50),
              ep=(100,600,15), gems=(1,10,30), art=(1,6,25),
              magic=(2,'any+potion',15)),
    'E': dict(cp=(1000,6000,5), sp=(1000,10000,25), gp=(1000,4000,25),
              ep=(300,1800,25), gems=(1,12,15), art=(1,6,10),
              magic=(3,'any+scroll',25)),
    'F': dict(cp=None, sp=(3000,18000,10), gp=(1000,6000,40),
              ep=(1000,4000,15), gems=(2,20,20), art=(1,8,10),
              magic=(5,'any_no_weapons',30)),
    'G': dict(cp=None, sp=None, gp=(2000,20000,50),
              ep=(1000,10000,50), gems=(3,18,30), art=(1,6,25),
              magic=(5,'any',35)),
    'H': dict(cp=(3000,18000,25), sp=(2000,20000,40), gp=(2000,20000,55),
              ep=(1000,8000,40), gems=(3,30,50), art=(2,20,50),
              magic=(6,'any',15)),
    'I': dict(cp=None, sp=None, gp=None,
              ep=(100,600,30), gems=(2,12,55), art=(2,8,50),
              magic=(1,'any',None)),  # None = automatic
    # --- Individual / Small Lair Treasures (J-Z, all automatic) ---
    'J': dict(cp=(3,24,None)),
    'K': dict(sp=(3,18,None)),
    'L': dict(ep=(2,12,None)),
    'M': dict(gp=(2,8,None)),
    'N': dict(ep=(1,6,None)),
    'O': dict(cp=(10,40,None), sp=(10,30,None)),
    'P': dict(sp=(10,60,None), ep=(1,20,None)),
    'Q': dict(gems=(1,4,None)),
    'R': dict(gp=(2,20,None), ep=(10,60,None), gems=(2,8,None), art=(1,3,None)),
    'S': dict(magic=(0,'potions_1d8',None)),   # 1d8 potions
    'T': dict(magic=(0,'scrolls_1d4',None)),   # 1d4 scrolls
    'U': dict(gems=(2,16,90), art=(1,6,80), magic=(1,'any',70)),
    'V': dict(magic=(2,'any',None)),
    'W': dict(gems=(1,8,60), art=(2,16,50), magic=(2,'any',60)),
    'X': dict(magic=(0,'potions_2',None)),     # any 2 potions
    'Y': dict(gp=(200,1200,None)),
    'Z': dict(cp=(100,300,None), sp=(100,400,None), gp=(100,600,None),
              ep=(100,400,None), gems=(1,6,55), art=(2,12,50),
              magic=(3,'any',50)),
}

# ---------------------------------------------------------------------------
# Gem tables (Table 85-86)
# ---------------------------------------------------------------------------

GEM_TIERS = [
    # (d100 max, base_gp, tier_name, [stone names])
    (25,  10,   'Ornamental',   ['Azurite','Banded Agate','Blue Quartz','Eye Agate',
                                  'Hematite','Lapis Lazuli','Malachite','Moss Agate',
                                  'Obsidian','Rhodochrosite','Tiger Eye Agate','Turquoise']),
    (50,  50,   'Semi-precious',['Bloodstone','Carnelian','Chalcedony','Chrysoprase',
                                  'Citrine','Jasper','Moonstone','Onyx','Rock Crystal',
                                  'Sardonyx','Smoky Quartz','Star Rose Quartz','Zircon']),
    (70,  100,  'Fancy',        ['Amber','Alexandrite','Amethyst','Chrysoberyl','Coral',
                                  'Jade','Jet','Tourmaline']),
    (90,  500,  'Precious',     ['Aquamarine','Garnet','Pearl','Peridot','Spinel','Topaz']),
    (99,  1000, 'Gem',          ['Black Opal','Fire Opal','Opal','Oriental Amethyst',
                                  'Oriental Topaz','Sapphire']),
    (100, 5000, 'Jewel',        ['Black Sapphire','Diamond','Emerald','Jacinth',
                                  'Oriental Emerald','Ruby','Star Ruby','Star Sapphire']),
]

GEM_BASE_VALUES = [1, 5, 10, 50, 100, 500, 1000, 5000, 10000, 20000, 40000, 80000, 100000]

def gem_base_idx(gp):
    for i, v in enumerate(GEM_BASE_VALUES):
        if v >= gp:
            return i
    return len(GEM_BASE_VALUES) - 1

def roll_gem(verbose=False):
    r = d100()
    for (max_roll, base_gp, tier, stones) in GEM_TIERS:
        if r <= max_roll:
            name = random.choice(stones)
            value = base_gp
            note = ''
            if pct(10):
                d6 = roll(1, 6)
                idx = gem_base_idx(base_gp)
                if d6 == 1:
                    extra = 1
                    while roll(1,6) == 1 and idx + extra < len(GEM_BASE_VALUES) - 1:
                        extra += 1
                    idx = min(idx + extra, len(GEM_BASE_VALUES) - 1)
                    value = GEM_BASE_VALUES[idx]
                    note = f'+{extra} tier'
                elif d6 == 2:
                    value = base_gp * 2
                    note = 'double'
                elif d6 == 3:
                    pct_up = rng(10, 60)
                    value = int(base_gp * (1 + pct_up / 100))
                    note = f'+{pct_up}%'
                elif d6 == 4:
                    pct_dn = rng(10, 40)
                    value = int(base_gp * (1 - pct_dn / 100))
                    note = f'-{pct_dn}%'
                elif d6 == 5:
                    value = base_gp // 2
                    note = 'half value'
                else:
                    extra = 1
                    while roll(1,6) == 6 and idx - extra > 0:
                        extra += 1
                    idx = max(0, idx - extra)
                    value = GEM_BASE_VALUES[idx]
                    note = f'-{extra} tier'
                value = max(1, min(100000, value))
            detail = f' ({note})' if note else ''
            return name, value, tier, detail
    return 'Unknown gem', 0, '?', ''

# ---------------------------------------------------------------------------
# Art objects table (Table 87)
# ---------------------------------------------------------------------------

ART_TABLE = [
    (10,  10,   100,  'cp'),
    (25,  30,   180,  'cp'),
    (40,  100,  600,  'cp'),
    (50,  100,  1000, 'cp'),
    (60,  200,  1200, 'cp'),
    (70,  300,  1800, 'cp'),
    (80,  400,  2400, 'cp'),
    (85,  500,  3000, 'cp'),
    (90,  1000, 4000, 'cp'),
    (95,  1000, 6000, 'cp'),
    (99,  2000, 8000, 'cp'),
    (100, 2000, 12000,'cp'),
]

ART_NAMES = [
    'silver goblet','carved ivory statuette','gold-inlaid jewelry box',
    'silk tapestry with silver thread','jeweled dagger (decorative)',
    'polished obsidian mirror','enameled brass oil lamp',
    'carved jade figurine','gilded wooden mask','crystal decanter',
    'ornate bronze shield (decorative)','fine porcelain vase',
    'tooled leather belt with gold buckle','pearl-inlaid music box',
    'alabaster idol','painted miniature portrait','silver candelabra',
    'bone-carved chess set','embroidered velvet cloak','copper incense burner',
]

def roll_art():
    r = d100()
    for (max_r, lo, hi, _) in ART_TABLE:
        if r <= max_r:
            value = rng(lo, hi)
            name = random.choice(ART_NAMES)
            return name, value

# ---------------------------------------------------------------------------
# Magic item tables (Tables 88-110)
# ---------------------------------------------------------------------------

POTIONS = [
    # subtable A (d6 1-2)
    ['Animal Control','Clairaudience','Clairvoyance','Climbing',
     'Delusion (cursed)','Delusion (cursed)','Diminution','Dragon Control',
     'Elixir of Health','Elixir of Madness (cursed)','Elixir of Madness (cursed)',
     'Elixir of Youth','ESP','Extra-healing','Extra-healing','Fire Breath',
     'Fire Resistance','Flying','Gaseous Form','DM\'s Choice'],
    # subtable B (d6 3-4)
    ['Giant Control','Giant Strength (Warrior)','Growth','Healing','Healing',
     'Heroism (Warrior)','Human Control','Invisibility','Invulnerability (Warrior)',
     'Levitation','Longevity','Oil of Acid Resistance','Oil of Disenchantment',
     'Oil of Elemental Invulnerability','Oil of Etherealness','Oil of Fiery Burning',
     'Oil of Fumbling (cursed)','Oil of Impact','Oil of Slipperiness','DM\'s Choice'],
    # subtable C (d6 5-6)
    ['Oil of Timelessness','Philter of Glibness','Philter of Love',
     'Philter of Persuasiveness','Philter of Stammering (cursed)','Plant Control',
     'Poison (cursed)','Poison (cursed)','Polymorph Self','Rainbow Hues',
     'Speed','Super-heroism (Warrior)','Super-heroism (Warrior)','Sweet Water',
     'Treasure Finding','Undead Control','Ventriloquism','Vitality',
     'Water Breathing','DM\'s Choice'],
]

SCROLLS_SPELL = [
    '1 spell (levels 1-4)', '1 spell (levels 1-4)', '1 spell (levels 1-4)',
    '1 spell (levels 1-6)', '1 spell (levels 1-6)',
    '1 spell (levels 2-9)',
    '2 spells (levels 1-4)', '2 spells (levels 2-9)',
    '3 spells (levels 1-4)', '3 spells (levels 2-9)',
    '4 spells (levels 1-6)', '4 spells (levels 1-8)',
    '5 spells (levels 1-6)', '5 spells (levels 1-8)',
    '6 spells (levels 1-6)', '6 spells (levels 3-8)',
    '7 spells (levels 1-8)', '7 spells (levels 2-9)',
    '7 spells (levels 4-9)', "DM's Choice",
]

SCROLLS_PROT = [
    'Map','Protection — Acid','Protection — Cold','Protection — Dragon Breath',
    'Protection — Electricity','Protection — Elementals','Protection — Elementals',
    'Protection — Fire','Protection — Gas','Protection — Lycanthropes',
    'Protection — Lycanthropes','Protection — Magic','Protection — Petrification',
    'Protection — Plants','Protection — Poison','Protection — Possession',
    'Protection — Undead','Protection — Water','Curse (cursed)',"DM's Choice",
]

RINGS_A = [
    'Animal Friendship','Blinking','Chameleon Power','Clumsiness (cursed)',
    'Contrariness (cursed)','Delusion (cursed)','Delusion (cursed)',
    'Djinni Summoning','Elemental Command','Feather Falling',
    'Fire Resistance','Free Action','Human Influence','Invisibility',
    'Jumping','Jumping','Mammal Control','Mind Shielding','Protection',"DM's Choice",
]
RINGS_B = [
    'Protection','Protection','Ram (Ring of the)','Regeneration',
    'Shocking Grasp','Shooting Stars','Spell Storing','Spell Turning',
    'Sustenance','Swimming','Telekinesis','Truth','Warmth','Water Walking',
    'Weakness (cursed)','Wishes — Multiple','Wishes — Three',
    'Wizardry (Wizard only)','X-Ray Vision',"DM's Choice",
]

RODS = [
    'Absorption (Priest/Wizard)','Absorption (Priest/Wizard)','Alertness','Alertness',
    'Beguiling (Priest/Wiz/Rogue)','Cancellation','Cancellation','Flailing',
    'Lordly Might (Warrior)','Passage','Resurrection (Priest)','Rulership',
    'Security','Security','Smiting (Priest/Wizard)','Smiting (Priest/Wizard)',
    'Splendor','Terror','Terror',"DM's Choice",
]

STAVES = [
    'Mace','Mace','Command (Priest/Wizard)','Curing (Priest)','Curing (Priest)',
    'Magi (Wizard)','Power (Wizard)','Serpent (Priest)','Slinging (Priest)',
    'Slinging (Priest)','Spear','Spear','Striking (Priest/Wizard)',
    'Striking (Priest/Wizard)','Swarming Insects (Priest/Wizard)',
    'Thunder & Lightning','Withering','Withering','Woodlands (Druid)',"DM's Choice",
]

WANDS = [
    'Conjuration (Wizard)','Earth and Stone','Enemy Detection','Fear (Priest/Wizard)',
    'Fire (Wizard)','Flame Extinguishing','Frost (Wizard)','Illumination',
    'Illusion (Wizard)','Lightning (Wizard)','Magic Detection','Magic Missiles',
    'Metal & Mineral Detection','Negation','Paralyzation (Wizard)',
    'Polymorphing (Wizard)','Secret Door & Trap Location','Size Alteration',
    'Wonder',"DM's Choice",
]

BOOKS = [
    "Boccob's Blessed Book (Wizard)","Boccob's Blessed Book (Wizard)","Boccob's Blessed Book (Wizard)",
    'Book of Exalted Deeds (Priest)','Book of Infinite Spells','Book of Vile Darkness (Priest)',
    'Libram of Gainful Conjuration (Wizard)','Libram of Ineffable Damnation (Wizard)',
    'Libram of Silver Magic (Wizard)','Manual of Bodily Health',
    'Manual of Gainful Exercise','Manual of Golems (Priest/Wizard)',
    'Manual of Puissant Skill at Arms (Warrior)','Manual of Quickness in Action',
    'Manual of Stealthy Pilfering (Rogue)','Tome of Clear Thought',
    'Tome of Leadership and Influence','Tome of Understanding',
    'Vacuous Grimoire (cursed)',"DM's Choice",
]

JEWELRY_A = [
    'Amulet of Inescapable Location (cursed)','Amulet of Life Protection',
    'Amulet of the Planes','Amulet vs. Detection and Location',
    'Amulet Versus Undead','Beads of Force','Brooch of Shielding',
    'Gem of Brightness','Gem of Insight','Gem of Seeing',
    'Jewel of Attacks (cursed)','Jewel of Flawlessness',
    'Medallion of ESP','Medallion of Thought Projection (cursed)',
    'Necklace of Adaptation','Necklace of Missiles','Necklace of Missiles',
    'Necklace of Prayer Beads (Priest)','Necklace of Strangulation (cursed)',"DM's Choice",
]
JEWELRY_B = [
    'Pearl of Power (Wizard)','Pearl of the Sirines','Pearl of Wisdom (Priest)',
    'Periapt of Foul Rotting (cursed)','Periapt of Health','Periapt of Proof Against Poison',
    'Periapt of Wound Closure','Phylactery of Faithfulness (Priest)',
    'Phylactery of Long Years (Priest)','Phylactery of Monstrous Attention (Priest, cursed)',
    'Scarab of Death (cursed)','Scarab of Enraging Enemies','Scarab of Insanity',
    'Scarab of Protection','Scarab Versus Golems','Talisman of Pure Good (Priest)',
    'Talisman of the Sphere (Wizard)','Talisman of Ultimate Evil (Priest)',
    'Talisman of Zagy',"DM's Choice",
]

CLOAKS = [
    'Cloak of Arachnida','Cloak of Displacement','Cloak of Elvenkind',
    'Cloak of Elvenkind','Cloak of Poisonousness (cursed)',
    'Cloak of Protection','Cloak of Protection','Cloak of Protection',
    'Cloak of the Bat','Cloak of the Manta Ray',
    'Robe of the Archmagi (Wizard)','Robe of Blending','Robe of Eyes (Wizard)',
    'Robe of Powerlessness (Wizard, cursed)','Robe of Scintillating Colors (Priest/Wizard)',
    'Robe of Stars (Wizard)','Robe of Useful Items (Wizard)','Robe of Useful Items (Wizard)',
    'Robe of Vermin (Wizard, cursed)',"DM's Choice",
]

BOOTS = [
    'Boots of Dancing (cursed)','Boots of Elvenkind','Boots of Levitation',
    'Boots of Speed','Boots of Striding and Springing','Boots of the North',
    'Boots of Varied Tracks','Winged Boots','Bracers of Archery (Warrior)',
    'Bracers of Brachiation','Bracers of Defense','Bracers of Defense',
    'Bracers of Defenselessness (cursed)','Gauntlets of Dexterity',
    'Gauntlets of Fumbling (cursed)','Gauntlets of Ogre Power (Priest/Rogue/Warrior)',
    'Gauntlets of Swimming and Climbing (Priest/Rogue/Warrior)',
    'Gloves of Missile Snaring','Slippers of Spider Climbing',"DM's Choice",
]

GIRDLES = [
    'Girdle of Dwarvenkind','Girdle of Dwarvenkind','Girdle of Dwarvenkind',
    'Girdle of Femininity/Masculinity (cursed)','Girdle of Giant Strength (Priest/Rogue/Warrior)',
    'Girdle of Giant Strength (Priest/Rogue/Warrior)','Girdle of Many Pouches',
    'Girdle of Many Pouches','Girdle of Many Pouches',
    'Hat of Disguise','Hat of Stupidity (cursed)',
    'Helm of Brilliance','Helm of Comprehending Languages and Reading Magic',
    'Helm of Comprehending Languages and Reading Magic','Helm of Opposite Alignment (cursed)',
    'Helm of Telepathy','Helm of Teleportation','Helm of Underwater Action',
    'Helm of Underwater Action',"DM's Choice",
]

BAGS = [
    'Alchemy Jug','Bag of Beans','Bag of Devouring (cursed)',
    'Bag of Holding','Bag of Holding','Bag of Holding','Bag of Holding',
    'Bag of Transmuting (cursed)','Bag of Tricks','Beaker of Plentiful Potions',
    "Bucknard's Everfull Purse","Decanter of Endless Water",'Efreeti Bottle',
    'Eversmoking Bottle','Flask of Curses (cursed)',"Heward's Handy Haversack",
    'Iron Flask','Portable Hole','Pouch of Accessibility',"DM's Choice",
]

DUSTS = [
    'Candle of Invocation (Priest)','Dust of Appearance','Dust of Disappearance',
    'Dust of Dryness','Dust of Illusion','Dust of Tracelessness',
    'Dust of Sneezing and Choking (cursed)','Incense of Meditation (Priest)',
    'Incense of Obsession (Priest, cursed)','Ioun Stones','Keoghtom\'s Ointment',
    "Nolzur's Marvelous Pigments",'Philosopher\'s Stone','Smoke Powder',
    'Sovereign Glue','Stone of Controlling Earth Elementals',
    'Stone of Good Luck (Luckstone)','Stone of Weight — Loadstone (cursed)',
    'Universal Solvent',"DM's Choice",
]

HOUSEHOLD = [
    'Brazier Commanding Fire Elementals (Wizard)','Brazier of Sleep Smoke (Wizard, cursed)',
    'Broom of Animated Attack (cursed)','Broom of Flying','Carpet of Flying',
    'Mattock of the Titans (Warrior)','Maul of the Titans (Warrior)',
    'Mirror of Life Trapping (Wizard)','Mirror of Mental Prowess','Mirror of Opposition (cursed)',
    "Murlynd's Spoon",'Rope of Climbing','Rope of Climbing','Rope of Constriction (cursed)',
    'Rope of Entanglement','Rug of Smothering (cursed)','Rug of Welcome (Wizard)',
    'Saw of Mighty Cutting (Warrior)','Spade of Colossal Excavation (Warrior)',"DM's Choice",
]

INSTRUMENTS = [
    'Chime of Interruption','Chime of Opening','Chime of Hunger (cursed)',
    'Drums of Deafening (cursed)','Drums of Panic','Harp of Charming',
    'Harp of Discord (cursed)','Horn of Blasting','Horn of Bubbles (cursed)',
    'Horn of Collapsing','Horn of Fog','Horn of Goodness/Evil',
    'Horn of the Tritons (Priest/Warrior)','Horn of Valhalla',
    'Lyre of Building','Pipes of Haunting','Pipes of Pain (cursed)',
    'Pipes of Sounding','Pipes of the Sewers',"DM's Choice",
]

WEIRD_A = [
    'Apparatus of Kwalish','Folding Boat','Folding Boat',
    'Bowl Commanding Water Elementals (Wizard)','Bowl of Watery Death (Wizard, cursed)',
    'Censer Controlling Air Elementals (Wizard)','Censer of Hostile Air Elementals (cursed)',
    'Crystal Ball (Wizard)','Crystal Ball (Wizard)','Crystal Hypnosis Ball (Wizard, cursed)',
    'Cube of Force','Cube of Frost Resistance','Cube of Frost Resistance','Cubic Gate',
    "Daern's Instant Fortress",'Deck of Illusions','Deck of Many Things (cursed)',
    'Eyes of Charming (Wizard)','Eyes of Minute Seeing',"DM's Choice",
]
WEIRD_B = [
    'Eyes of Petrification (cursed)','Eyes of the Eagle',
    'Figurine of Wondrous Power','Figurine of Wondrous Power',
    'Horseshoes of a Zephyr','Horseshoes of a Zephyr','Horseshoes of Speed',
    'Iron Bands of Bilarro','Lens of Detection',"Quaal's Feather Token",
    'Quiver of Ehlonna','Quiver of Ehlonna','Sheet of Smallness',
    'Sphere of Annihilation','Stone Horse','Well of Many Worlds',
    'Wind Fan','Wind Fan','Wings of Flying',"DM's Choice",
]

ARMOR_TYPES = [
    'Banded Mail','Brigandine','Chain Mail','Chain Mail','Chain Mail',
    'Field Plate','Full Plate','Leather Armor','Plate Mail','Plate Mail',
    'Plate Mail','Plate Mail','Ring Mail','Scale Mail',
    'Shield','Shield','Shield','Splint Mail','Studded Leather','Special',
]
ARMOR_ADJ = [
    (-1,'cursed'),(-1,'cursed'),
    (+1,'+1'),(+1,'+1'),(+1,'+1'),(+1,'+1'),(+1,'+1'),(+1,'+1'),(+1,'+1'),(+1,'+1'),
    (+2,'+2'),(+2,'+2'),(+2,'+2'),(+2,'+2'),
    (+3,'+3'),(+3,'+3'),(+3,'+3'),
    (+4,'+4'),(+4,'+4'),
    (+5,'+5'),
]
SPECIAL_ARMOR = [
    'Armor of Command +1','Armor of Command +1',
    'Armor of Blending +1','Armor of Blending +1',
    'Armor of Missile Attraction (cursed)','Armor of Missile Attraction (cursed)',
    'Armor of Rage (cursed)','Armor of Rage (cursed)',
    'Elven Chain Mail','Elven Chain Mail',
    'Plate Mail of Etherealness','Plate Mail of Etherealness',
    'Plate Mail of Fear','Plate Mail of Fear',
    'Plate Mail of Vulnerability (cursed)','Plate Mail of Vulnerability (cursed)',
    'Shield +1/+4 vs. Missiles','Shield +1/+4 vs. Missiles',
    'Shield -1, Missile Attractor (cursed)','Shield -1, Missile Attractor (cursed)',
]

WEAPON_TYPE_A = [
    'Arrow (4d6)','Arrow (3d6)','Arrow (2d6)','Axe','Axe','Battle Axe',
    'Bolt (2d10)','Bolt (2d6)','Sling Bullets (3d4)','Dagger','Dagger','Dagger',
    'Darts (3d4)','Flail','Javelin (1d2)','Knife','Lance','Mace','Mace','Special',
]
WEAPON_TYPE_B = [
    'Military Pick','Morning Star','Pole Arm','Scimitar','Scimitar',
    'Spear','Spear','Spear','Sword','Sword','Sword','Sword','Sword',
    'Sword','Sword','Sword','Sword','Trident','Warhammer','Special',
]
WEAPON_ADJ = [
    # (sword_adj, other_adj, note)
    (-1,-1,'cursed'),(-1,-1,'cursed'),
    (+1,+1,''),(+1,+1,''),(+1,+1,''),(+1,+1,''),(+1,+1,''),(+1,+1,''),(+1,+1,''),(+1,+1,''),
    (+2,+1,''),(+2,+1,''),(+2,+1,''),(+2,+1,''),
    (+3,+2,''),(+3,+2,''),(+3,+2,''),
    (+4,+2,''),(+4,+2,''),
    (+5,+3,''),
]
SPECIAL_WEAPONS_A = [
    'Arrow of Direction','Arrow of Slaying','Axe +2 Throwing','Axe of Hurling',
    'Bow +1','Bow +1','Crossbow of Accuracy +3','Crossbow of Distance',
    'Crossbow of Speed','Dagger +1/+2 vs. Tiny-Small','Dagger +1/+2 vs. Tiny-Small',
    'Dagger +3 vs. Large','Dagger +3 vs. Large','Dagger +2 Longtooth',
    'Dagger of Throwing','Dagger of Venom','Dart of Homing',
    'Hammer +3 Dwarven Thrower','Hammer of Thunderbolts',"DM's Choice",
]
SPECIAL_WEAPONS_B = [
    'Hornblade','Javelin of Lightning','Javelin of Piercing','Buckle Knife','Buckle Knife',
    'Mace of Disruption','Net of Entrapment','Net of Snaring',
    'Magical Quarterstaff','Magical Quarterstaff','Scimitar of Speed',
    'Sling of Seeking +2','Cursed Backbiter Spear',
    'Trident of Fish Command','Trident of Submission','Trident of Warning',
    'Trident of Yearning (cursed)',"DM's Choice","DM's Choice","DM's Choice",
]
SPECIAL_SWORDS_C = [
    'Sun Blade',
    'Sword +1/+2 vs. Magic-using & Enchanted','Sword +1/+2 vs. Magic-using & Enchanted',
    'Sword +1/+2 vs. Magic-using & Enchanted','Sword +1/+2 vs. Magic-using & Enchanted',
    'Sword +1/+2 vs. Magic-using & Enchanted','Sword +1/+2 vs. Magic-using & Enchanted',
    'Sword +1/+3 vs. Lycanthropes','Sword +1/+3 vs. Lycanthropes','Sword +1/+3 vs. Lycanthropes',
    'Sword +1/+3 vs. Regenerating','Sword +1/+3 vs. Regenerating',
    'Sword +1/+4 vs. Reptiles',
    'Sword +1 Cursed (cursed)','Sword +1 Cursed (cursed)',
    'Sword +1 Flame Tongue','Sword +1 Luck Blade',
    'Sword +2 Dragon Slayer','Sword +2 Giant Slayer',"DM's Choice",
]
SPECIAL_SWORDS_D = [
    'Sword +2 Nine Lives Stealer','Sword +3 Frost Brand','Sword +3 Frost Brand',
    'Sword +4 Defender','Sword +5 Defender','Sword +5 Holy Avenger',
    'Sword -2 Cursed (cursed)','Sword -2 Cursed (cursed)',
    'Sword of Dancing','Sword of Life Stealing','Sword of Sharpness',
    'Sword of the Planes','Sword of Wounding',
    'Sword Cursed Berserking (cursed)','Sword Cursed Berserking (cursed)',
    'Short Sword of Quickness +2',"DM's Choice","DM's Choice","DM's Choice","DM's Choice",
]

# ---------------------------------------------------------------------------
# Magic item generation
# ---------------------------------------------------------------------------

def roll_on(table):
    """Roll 1d(len) on a table (0-indexed)."""
    return table[roll(1, len(table)) - 1]

def roll_magic_item(magic_type='any', verbose=False):
    """Return list of (category, description) tuples."""
    results = []

    if magic_type == 'potions_1d8':
        count = roll(1, 8)
        for _ in range(count):
            results.append(roll_single_magic('potion', verbose))
        return results
    if magic_type == 'potions_2':
        for _ in range(2):
            results.append(roll_single_magic('potion', verbose))
        return results
    if magic_type == 'scrolls_1d4':
        count = roll(1, 4)
        for _ in range(count):
            results.append(roll_single_magic('scroll', verbose))
        return results

    return [roll_single_magic(magic_type, verbose)]


def roll_single_magic(magic_type='any', verbose=False):
    if magic_type == 'potion':
        subtable = (roll(1, 6) - 1) // 2   # d6 1-2→0, 3-4→1, 5-6→2
        item = roll_on(POTIONS[subtable])
        return ('Potion/Oil', item)

    if magic_type == 'scroll':
        d6 = roll(1, 6)
        if d6 <= 4:
            scroll_type = 'Spell'
            item = roll_on(SCROLLS_SPELL)
            scroll_kind = 'Wizard' if pct(70) else 'Priest'
            return ('Scroll', f'{scroll_kind} {item}')
        else:
            item = roll_on(SCROLLS_PROT)
            return ('Scroll', item)

    # Table 88: d100 for category
    r = d100()
    if r <= 20:
        return roll_single_magic('potion', verbose)
    elif r <= 35:
        return roll_single_magic('scroll', verbose)
    elif r <= 40:
        sub = roll(1, 6)
        item = roll_on(RINGS_A if sub <= 4 else RINGS_B)
        return ('Ring', item)
    elif r == 41:
        return ('Rod', roll_on(RODS))
    elif r == 42:
        return ('Staff', roll_on(STAVES))
    elif r <= 45:
        return ('Wand', roll_on(WANDS))
    elif r == 46:
        return ('Misc Magic — Book/Tome', roll_on(BOOKS))
    elif r <= 48:
        sub = roll(1, 6)
        item = roll_on(JEWELRY_A if sub <= 3 else JEWELRY_B)
        return ('Misc Magic — Jewelry', item)
    elif r <= 50:
        return ('Misc Magic — Cloak/Robe', roll_on(CLOAKS))
    elif r <= 52:
        return ('Misc Magic — Boots/Gloves', roll_on(BOOTS))
    elif r == 53:
        return ('Misc Magic — Girdle/Helm', roll_on(GIRDLES))
    elif r <= 55:
        return ('Misc Magic — Bag/Bottle', roll_on(BAGS))
    elif r == 56:
        return ('Misc Magic — Dust/Stone', roll_on(DUSTS))
    elif r == 57:
        return ('Misc Magic — Household', roll_on(HOUSEHOLD))
    elif r == 58:
        return ('Misc Magic — Instrument', roll_on(INSTRUMENTS))
    elif r <= 60:
        sub = roll(1, 6)
        item = roll_on(WEIRD_A if sub <= 3 else WEIRD_B)
        return ('Misc Magic — Weird', item)
    elif r <= 75:
        return roll_armor(magic_type == 'any_no_weapons', verbose)
    else:
        # Weapons — only if not 'any_no_weapons'
        if magic_type == 'any_no_weapons':
            return roll_armor(True, verbose)
        return roll_weapon(verbose)


def roll_armor(is_forced=False, verbose=False):
    armor = roll_on(ARMOR_TYPES)
    if armor == 'Special':
        return ('Armor/Shield', roll_on(SPECIAL_ARMOR))
    adj_roll = roll(1, 20)
    bonus, label = ARMOR_ADJ[adj_roll - 1]
    label_str = f'(cursed -1)' if label == 'cursed' else f'+{bonus}'
    return ('Armor/Shield', f'{armor} {label_str}')


def roll_weapon(verbose=False):
    sub_d6 = roll(1, 6)
    weapon = roll_on(WEAPON_TYPE_A if sub_d6 <= 2 else WEAPON_TYPE_B)
    if weapon == 'Special':
        special_d10 = roll(1, 10)
        if special_d10 <= 3:
            return ('Weapon', roll_on(SPECIAL_WEAPONS_A))
        elif special_d10 <= 6:
            return ('Weapon', roll_on(SPECIAL_WEAPONS_B))
        elif special_d10 <= 9:
            return ('Weapon', roll_on(SPECIAL_SWORDS_C))
        else:
            return ('Weapon', roll_on(SPECIAL_SWORDS_D))
    adj_roll = roll(1, 20)
    sw, ot, note = WEAPON_ADJ[adj_roll - 1]
    is_sword = 'Sword' in weapon or weapon in ('Scimitar',)
    bonus = sw if is_sword else ot
    suffix = ' (cursed)' if note == 'cursed' else f' +{bonus}'
    return ('Weapon', f'{weapon}{suffix}')


# ---------------------------------------------------------------------------
# Core generator
# ---------------------------------------------------------------------------

def generate_treasure(ttype, verbose=True):
    ttype = ttype.upper()
    if ttype not in TYPES:
        print(f"Unknown treasure type: {ttype}")
        return

    tdata = TYPES[ttype]
    lines = [f"\n=== Treasure Type {ttype} ==="]
    coin_total = 0
    found_any = False

    def coin_entry(key, label):
        nonlocal coin_total, found_any
        entry = tdata.get(key)
        if entry is None:
            return
        lo, hi, chance = entry
        if chance is not None and not pct(chance):
            if verbose:
                lines.append(f"  {label}: — (rolled {random.randint(1,100)}, needed ≤{chance})")
            return
        amount = rng(lo, hi)
        # Table 84's 4th column is "Platinum or Electrum" (DM's choice). It is
        # stored under 'ep'; resolve which metal this hoard uses here.
        if key == 'ep' and random.random() < PLATINUM_CHANCE:
            key, label = 'pp', 'Platinum (pp)'
        coin_total += amount * {'cp':0.01,'sp':0.1,'gp':1,'ep':0.5,'pp':5}[key]
        tag = f' (auto)' if chance is None else f' ({chance}% ✓)'
        lines.append(f"  {label}: {amount:,}{tag}")
        found_any = True

    # Coins
    has_coins = any(k in tdata for k in ('cp','sp','gp','ep'))
    if has_coins:
        lines.append("Coins:")
    coin_entry('cp', 'Copper (cp)')
    coin_entry('sp', 'Silver (sp)')
    coin_entry('gp', 'Gold (gp)')
    coin_entry('ep', 'Electrum (ep)')

    # Gems
    gem_entry = tdata.get('gems')
    if gem_entry:
        lo, hi, chance = gem_entry
        if chance is None or pct(chance):
            count = rng(lo, hi)
            gem_values = []
            lines.append(f"Gems ({count} total{'' if chance is None else f', {chance}% ✓'}):")
            for i in range(count):
                name, value, tier, detail = roll_gem(verbose)
                gem_values.append(value)
                lines.append(f"  {i+1:2d}. {name} — {value:,} gp [{tier}]{detail}")
            lines.append(f"  Gem subtotal: {sum(gem_values):,} gp")
            found_any = True
        elif verbose:
            lines.append(f"Gems: — ({chance}% needed)")

    # Art
    art_entry = tdata.get('art')
    if art_entry:
        lo, hi, chance = art_entry
        if chance is None or pct(chance):
            count = rng(lo, hi)
            art_values = []
            lines.append(f"Art Objects ({count} total{'' if chance is None else f', {chance}% ✓'}):")
            for i in range(count):
                name, value = roll_art()
                art_values.append(value)
                lines.append(f"  {i+1:2d}. {name} — {value:,} gp")
            lines.append(f"  Art subtotal: {sum(art_values):,} gp")
            found_any = True
        elif verbose:
            lines.append(f"Art Objects: — ({chance}% needed)")

    # Magic
    magic_entry = tdata.get('magic')
    if magic_entry:
        count, mtype, chance = magic_entry
        if chance is None or pct(chance):
            lines.append(f"Magic Items{'' if chance is None else f' ({chance}% ✓)'}:")

            items = []

            # Special types that determine their own count
            if mtype.startswith('potions_') or mtype.startswith('scrolls_') or mtype == 'potions_2':
                rolled = roll_magic_item(mtype, verbose)
                items.extend(rolled)
            else:
                # Fixed count from treasure type
                extra_potion = '+potion' in mtype
                extra_scroll = '+scroll' in mtype
                base_type = mtype.replace('+potion','').replace('+scroll','')

                for _ in range(count):
                    items.extend(roll_magic_item(base_type, verbose))
                if extra_potion:
                    items.extend(roll_magic_item('potion', verbose))
                if extra_scroll:
                    items.extend(roll_magic_item('scroll', verbose))

                if mtype == 'armor_weapon':
                    d2 = roll(1, 2)
                    items = [roll_armor() if d2 == 1 else roll_weapon()]

            for i, (cat, desc) in enumerate(items):
                lines.append(f"  {i+1}. [{cat}] {desc}")
            found_any = True
        elif verbose:
            lines.append(f"Magic Items: — ({chance}% needed)")

    if not found_any:
        lines.append("  (nothing)")

    print('\n'.join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='AD&D 2e treasure generator (DMG Table 84)')
    parser.add_argument('types', nargs='+', help='Treasure type(s): A-I=lair, J-Z=individual. Qx5 notation supported.')
    parser.add_argument('--quiet', '-q', action='store_true', help='Hide failed rolls')
    parser.add_argument('--count', '-n', type=int, default=1, metavar='N',
                        help='Roll individual treasure for N creatures (default 1)')
    args = parser.parse_args()

    types = expand_types(args.types)

    if args.count > 1:
        print(f"\n--- Individual treasure × {args.count} creatures ---")
        for i in range(1, args.count + 1):
            print(f"\n[Creature {i}]")
            for t in types:
                generate_treasure(t, verbose=not args.quiet)
    else:
        for t in types:
            generate_treasure(t, verbose=not args.quiet)


if __name__ == '__main__':
    main()
