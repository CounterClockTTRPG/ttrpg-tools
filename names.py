"""Fantasy name generator — call generate(race, female) from other modules.

Each race blends a curated bank of first names with a procedural fallback,
then attaches a surname / clan / epithet where appropriate. The blend keeps
output varied without going off-genre.

Public API (preserved): generate(race, female), RACES, GENERATORS.
"""
import random


def _pick(lst): return random.choice(lst)
def _join(*parts): return "".join(parts).capitalize()
def _maybe(p): return random.random() < p


# ─────────────────────────────────────────────────────────────────────
# Halfling
# ─────────────────────────────────────────────────────────────────────
HALFLING_FIRSTS_M = [
    "Alton","Beau","Bobbin","Cade","Corin","Dabb","Edric","Errich",
    "Fenton","Finnan","Garret","Hob","Ilbert","Jasper","Kibb","Lindo",
    "Lyle","Marbo","Merric","Milo","Nob","Osco","Perrin","Poco",
    "Quincy","Reggi","Roscoe","Sam","Tobin","Tully","Ulwin","Wellby","Wendel","Yarrow",
]
HALFLING_FIRSTS_F = [
    "Andry","Belba","Bree","Callie","Cora","Daisy","Dora","Esme","Fern","Gilly",
    "Hazel","Ida","Jillian","Kithri","Lavinia","Lily","Merla","Mirabella","Nedda",
    "Olivetta","Pansy","Pearl","Petunia","Poppy","Quenna","Rosamund","Rosie",
    "Salli","Tessa","Trym","Una","Verna","Willow","Yondalla","Zara",
]
HALFLING_SURNAMES = [
    "Brushgather","Goodbarrel","Greenbottle","High-Hill","Hilltopple","Leagallow",
    "Tealeaf","Thorngage","Tosscobble","Underbough","Whistlebrook","Brownlock",
    "Gardner","Hayward","Honeypot","Mossfoot","Quickstep","Reedwater",
    "Sandheaver","Shortstride","Smallburrow","Sunmeadow","Wilhide",
    "Goodbody","Goldworthy","Cottar","Roper","Twofoot","Burrows",
    "Chubb","Banks","Greenhand","Stoutbelly",
]

def halfling(female=False):
    if _maybe(0.5):
        first = _pick(HALFLING_FIRSTS_F if female else HALFLING_FIRSTS_M)
    else:
        onset = _pick(["B","Br","C","D","F","G","H","M","N","P","R","S","T","W",""])
        if female:
            vowel = _pick(["a","e","i","o","ab","ib","el","il","al","ee","ai"])
            end   = _pick(["ble","lla","sse","la","ra","na","tte","wyn","",""])
        else:
            vowel = _pick(["a","e","i","o","u","ip","ob","am","il","im","an","ub"])
            end   = _pick(["b","d","g","k","l","m","n","p","r","s","t","by","","",""])
        first = _join(onset + vowel + end)
    return f"{first} {_pick(HALFLING_SURNAMES)}"


# ─────────────────────────────────────────────────────────────────────
# Human
# ─────────────────────────────────────────────────────────────────────
HUMAN_FIRSTS_M = [
    "Aldous","Aren","Bartram","Beren","Caden","Cedric","Conrad","Dain","Devran",
    "Edmund","Elric","Faron","Garrick","Gideon","Halvor","Heinrich","Ivor","Jorund",
    "Kalen","Konrad","Leoric","Lothar","Magnus","Marek","Nestor","Orlan","Osric",
    "Percival","Quintus","Rainer","Roderic","Sten","Tarian","Theron","Ulric","Valen",
    "Wendel","Yorick","Adric","Brennan","Caspian","Donovan","Emrys","Fendrel",
    "Aldwin","Anselm","Bertran","Corvin","Damek","Donagh","Eamon","Ferran","Florian",
    "Garron","Hadwin","Joren","Larkin","Mathis","Niall","Norrick","Owain","Oryn",
    "Pevric","Phelan","Ranulf","Sevren","Tomas","Volker","Yoren",
]
HUMAN_FIRSTS_F = [
    "Adelle","Alyss","Brenna","Cara","Cyra","Della","Elara","Elsbeth","Faye","Gisela",
    "Halia","Ilse","Imogen","Jorah","Katrin","Lenora","Liora","Mira","Morwen","Nessa",
    "Niamh","Orla","Petra","Quenna","Rhona","Saira","Selene","Sigrid","Talia","Thea",
    "Una","Vesna","Wynne","Yara","Ysolde","Zinnia","Briony","Calla","Eira","Ravenna",
    "Adelaide","Beata","Brielle","Cosima","Demetra","Edda","Eleyna","Hesper","Ilona",
    "Isolt","Junia","Klara","Linnea","Maelle","Margery","Naima","Niven","Odelia",
    "Persis","Renata","Saskia","Tamsin","Verity","Wenda",
]
HUMAN_SURNAMES = [
    "Ashford","Barrow","Brightwater","Carrow","Coldwater","Crestwell",
    "Dane","Darkmoor","Dunmore","Eastwick","Emsworth","Farrow","Fenwick","Fairweather",
    "Godfrey","Hartwell","Holt","Ingram","Ironsides","Jance",
    "Jasper","Kemper","Lorne","Marsh","Mercer","Nighthollow","Oakhurst","Pendry",
    "Quell","Rowe","Stonefield","Tanner","Thornbury","Ulvane",
    "Vance","Whitmore","Winterborne","Wycliffe","Yorke","Ashby","Crane","Dunhollow",
    "Lockhart","Marlow","Northwood","Penrose","Tallow","Vexley",
    "Argent","Bowyer","Calvert","Cartwright","Carver","Cooper","Drewitt","Fairchild",
    "Falconer","Fletcher","Forester","Fuller","Glover","Goodfellow","Hadley","Hale",
    "Holloway","Ingleby","Locklin","Marrowby","Otterford","Pellingham","Pemberton",
    "Sadler","Sawyer","Sterling","Throgmorton","Wright","Yardley",
]

def human(female=False):
    if _maybe(0.5):
        first = _pick(HUMAN_FIRSTS_F if female else HUMAN_FIRSTS_M)
    else:
        if female:
            onset = _pick(["Al","An","Br","Ca","C","D","Em","Es","Fa","G","H","Is","J","K",
                           "L","M","N","Or","P","R","S","T","V","W","Yv",""])
            vowel = _pick(["a","e","i","o","u","ara","ella","enna","ina","ora","ira",
                           "ana","eva","aine","onne","ille"])
            end   = _pick(["","","","n","na","ra","la","sse","nne","lyn","dra","via",
                           "cia","wen","beth","ette"])
        else:
            onset = _pick(["Al","Ar","B","Br","C","D","Dr","E","F","G","H","J","K","L",
                           "M","N","Or","P","R","S","T","Tr","V","W",""])
            vowel = _pick(["a","e","i","o","u","ar","en","on","an","or","ur","el","ed"])
            end   = _pick(["","","d","k","l","m","n","nd","r","rd","s","t","th","ck",
                           "rt","win","mar","ric"])
        first = _join(onset + vowel + end)
    return f"{first} {_pick(HUMAN_SURNAMES)}"


# ─────────────────────────────────────────────────────────────────────
# Elf
# ─────────────────────────────────────────────────────────────────────
ELF_FIRSTS_M = [
    "Aelar","Aramil","Arannis","Aust","Beiro","Berrian","Caelynn","Carric","Enialis",
    "Erdan","Erevan","Galinndan","Hadarai","Heian","Himo","Immeral","Ivellios","Korfel",
    "Lamlis","Laucian","Mindartis","Naal","Nutae","Paelias","Peren","Quarion","Riardon",
    "Rolen","Soveliss","Suhnae","Thamior","Tharivol","Theren","Varis","Vanuath",
]
ELF_FIRSTS_F = [
    "Adrie","Ahinar","Althaea","Anastrianna","Andraste","Antinua","Bethrynna","Birel",
    "Caelynn","Drusilia","Enna","Felosial","Ielenia","Jelenneth","Keyleth","Leshanna",
    "Lia","Maiathah","Malquis","Meriele","Mialee","Naivara","Quelenna","Quillathe",
    "Sariel","Shanairra","Shava","Silaqui","Theirastra","Thia","Vadania","Valanthe",
    "Xanaphia","Yllanis",
]
ELF_SURNAMES = [
    "Aelasar","Aeravansel","Amakiir","Amastacia","Ariessus","Caerdonel",
    "Erlandur","Evenwood","Galanodel","Holimion","Ilphelkiir","Ilphukiir","Liadon",
    "Meliamne","Naïlo","Nightbreeze","Selevarun","Siannodel",
    "Sunblaze","Truesilver","Withersnap","Xiloscient","Yaeldrin",
    "Aloranachel","Caradoon","Duirsar","Echorion","Faerondaeril","Hawksong","Mistwalker",
    "Riversong","Thornblade","Windwalker",
]

def elf(female=False):
    if _maybe(0.5):
        first = _pick(ELF_FIRSTS_F if female else ELF_FIRSTS_M)
    else:
        onset = _pick(["Ae","Al","An","Ar","Ath","C","E","El","F","G","I","L","M","N",
                       "Q","R","S","T","V","Y",""])
        if female:
            mid = _pick(["ara","eia","ella","ira","iel","iele","ina","ora","oria","ena",
                         "ela","eva","ala","aine","yria","esi"])
            end = _pick(["","","l","n","s","ra","lis","nel","ria","via","na","sse",
                         "wyn","thea","aris"])
        else:
            mid = _pick(["ael","alar","amir","anor","ar","avel","el","elar","eron","ian",
                         "iel","ior","ir","or","ren","tha","ven"])
            end = _pick(["","","as","el","en","iel","il","ion","is","on","or","us",
                         "and","mir","seth"])
        first = _join(onset + mid + end)
    return f"{first} {_pick(ELF_SURNAMES)}"


# ─────────────────────────────────────────────────────────────────────
# Dwarf
# ─────────────────────────────────────────────────────────────────────
DWARF_FIRSTS_M = [
    "Adrik","Alberich","Baern","Barendd","Brottor","Dain","Darrak","Delg",
    "Eberk","Einkil","Fargrim","Gardain","Harbek","Kildrak","Morgran","Orsik",
    "Oskar","Rangrim","Rurik","Taklinn","Thoradin","Tordek","Traubon","Travok",
    "Ulfgar","Veit","Vondal","Borin","Korin","Norgrim",
]
DWARF_FIRSTS_F = [
    "Amber","Artin","Audhild","Bardryn","Dagnal","Diesa","Eldeth","Falkrunn","Finellen",
    "Gunnloda","Gurdis","Helja","Hlin","Kathra","Kristryd","Ilde","Liftrasa","Mardred",
    "Riswynn","Sannl","Torbera","Torgga","Vistra","Brynhild","Drudgrim","Falla","Mistress",
]
DWARF_CLANS = [
    "Boulderrock","Bronzemantle","Cragfist","Deepdelve","Fireforge",
    "Goldvein","Hammerfall","Marblebrow","Torunn",
    "Undurr","Warcrown","Anvilhand","Axeholm","Brawnanvil","Coppervein","Dankil","Frostbeard",
    "Goldhammer","Helderhall","Holderhek","Loderr","Lutgehr","Rumnaheim","Strakeln",
    "Steelfist","Stoutaxe","Torevir","Ungart","Whitebeard","Worldthrone",
]

def dwarf(female=False):
    if _maybe(0.5):
        first = _pick(DWARF_FIRSTS_F if female else DWARF_FIRSTS_M)
    else:
        onset = _pick(["B","Br","D","Dr","G","Gr","H","K","Kh","M","N","Th","Thr","V",""])
        vowel = _pick(["a","e","i","o","u","ar","or","un","am","ur","ai","oi","ek"])
        if female:
            end = _pick(["a","da","ga","ka","na","ra","ta","va","ryn","wyn","",""])
        else:
            end = _pick(["","d","g","k","l","m","n","nd","r","rd","t","th","grim","din","mar"])
        first = _join(onset + vowel + end)
    return f"{first} {_pick(DWARF_CLANS)}"


# ─────────────────────────────────────────────────────────────────────
# Gnome
# ─────────────────────────────────────────────────────────────────────
GNOME_FIRSTS_M = [
    "Alston","Alvyn","Boddynock","Brocc","Burgell","Dimble","Eldon","Erky","Fonkin",
    "Frug","Gerbo","Gimble","Glim","Jebeddo","Kellen","Namfoodle","Orryn","Roondar",
    "Seebo","Sindri","Warryn","Wrenn","Zook","Pip","Tink","Bosk",
]
GNOME_FIRSTS_F = [
    "Bimpnottin","Breena","Caramip","Carlin","Donella","Duvamil","Ella","Ellyjobell",
    "Ellywick","Lilli","Loopmottin","Lorilla","Mardnab","Roywyn","Shamil","Tana",
    "Waywocket","Zanna","Twiddle","Pippa","Mibsy","Nibble",
]
GNOME_SURNAMES = [
    "Bafflestone","Cogsworth","Dundersnap","Fizzlewhistle","Glitterstone","Hoppenbeck",
    "Kettlewick","Nimblewhistle","Puddlejump","Quickfingers","Sparklecap","Thistlewick",
    "Wonderspark","Zigwhistle","Beren","Daergel","Folkor","Garrick","Nackle","Murnig",
    "Ningel","Raulnor","Scheppen","Timbers","Turen","Tipplespring","Underbough","Glittergem",
    "Pebblewhistle","Coggle","Whirlspring","Mizzlefiz","Tinklestar","Boondiggle",
]

def gnome(female=False):
    if _maybe(0.5):
        first = _pick(GNOME_FIRSTS_F if female else GNOME_FIRSTS_M)
    else:
        onset = _pick(["B","C","D","F","G","H","J","K","L","M","N","P","R","S","T","W","Z",""])
        if female:
            vowel = _pick(["a","e","i","o","u","ia","ie","illy","ella","etta","ina","oodle"])
            end   = _pick(["","","","la","na","ra","za","wick","ette",""])
        else:
            vowel = _pick(["a","e","i","o","u","ick","ock","unk","ink","ank","ip","obb"])
            end   = _pick(["","","","n","k","t","l","r","s","ble","yn"])
        first = _join(onset + vowel + end)
    return f"{first} {_pick(GNOME_SURNAMES)}"


# ─────────────────────────────────────────────────────────────────────
# Half-elf — elven first OR human first w/ elven flourish, mixed surnames
# ─────────────────────────────────────────────────────────────────────
def halfelf(female=False):
    roll = random.random()
    if roll < 0.4:
        first = elf(female).split()[0]
        surname = _pick(HUMAN_SURNAMES)
    elif roll < 0.8:
        first = human(female).split()[0]
        surname = _pick(ELF_SURNAMES)
    else:
        first = human(female).split()[0]
        suffix = _pick(["", "ael", "el", "an", "ar", "ien", "iel", ""])
        first = (first + suffix).capitalize()
        surname = _pick(HUMAN_SURNAMES + ELF_SURNAMES)
    return f"{first} {surname}"


# ─────────────────────────────────────────────────────────────────────
# Half-orc
# ─────────────────────────────────────────────────────────────────────
HALFORC_FIRSTS_M = [
    "Dench","Feng","Gell","Henk","Holg","Imsh","Keth","Mhurren","Ront","Shump",
    "Garr","Drak","Brog","Vorn","Krang","Mok","Targ","Hroth","Morg","Korg","Skarn",
]
HALFORC_FIRSTS_F = [
    "Baggi","Emen","Engong","Kansif","Myev","Neega","Ovak","Ownka","Shautha","Sutha","Vola",
    "Volen","Yevelda","Brana","Drella","Krava","Murga","Sharra","Volga",
]
HALFORC_EPITHETS = [
    "the Bruiser","the Hunter","the Ironfist","Ironjaw","Bloodaxe","Skullsplitter",
    "Bonecrusher","the Bastard","the Wretched","Black-Eye","Half-Tusk","One-Eye",
    "the Quiet","the Cleaver","Wolfgrin","Mudblood","Riverborn","Blackmane","",
]

def halforc(female=False):
    if _maybe(0.5):
        first = _pick(HALFORC_FIRSTS_F if female else HALFORC_FIRSTS_M)
    else:
        onset = _pick(["Ag","Br","D","Gr","Gur","H","K","M","N","R","Sh","T","Ug","Ul","Ur","Vr",""])
        vowel = _pick(["a","ag","ak","ar","ok","uk","um","ur","ush","usk","org","ang"])
        if female:
            end = _pick(["a","ga","ka","ra","sha","na","",""])
        else:
            end = _pick(["","g","k","r","sh","t","th","gar","mok"])
        first = _join(onset + vowel + end)
    epithet = _pick(HALFORC_EPITHETS)
    return f"{first} {epithet}".strip() if epithet else first


# ─────────────────────────────────────────────────────────────────────
# Orc (full-blood)
# ─────────────────────────────────────────────────────────────────────
ORC_FIRSTS_M = [
    "Krug","Vorgath","Hragash","Mogru","Ognar",
    "Ulfang","Bargash","Drog","Skarl","Vrok","Morthog","Karzog","Duroth","Targosh",
    "Ghoroth","Urgath","Brokul","Skagrog","Morgath","Vrothag","Dorg","Karruk",
]
ORC_FIRSTS_F = [
    "Aggra","Draka","Garona","Geyah","Olgra","Sharga","Mograh","Krushka","Voghra",
    "Hrolga","Murga","Skarra","Ushka",
]
ORC_TRIBES = [
    "Black Spear","Bloody Skull","Broken Tusk","Crooked Fang","Dripping Blade","Iron Fang",
    "Red Hand","Severed Eye","Shattered Hand","Shadow Wolf","Skullcrusher","Splintered Skull",
    "Vile Rune","Cleaved Skull","Hateful Eye","Many-Arrows","Red Maw","Death Moon",
]

def orc(female=False):
    if _maybe(0.55):
        first = _pick(ORC_FIRSTS_F if female else ORC_FIRSTS_M)
    else:
        onset = _pick(["Bl","Br","D","Dr","Gr","Gh","H","K","Kr","M","N","R","Sh","T","Thr",
                       "Ug","Ul","Ur","Vr","Z"])
        vowel = _pick(["a","ag","ar","og","or","ok","ug","um","ur","ash","osh","usk","ang"])
        end   = _pick(["","g","gar","gh","k","mar","r","rg","rok","sh","th","z"])
        first = _join(onset + vowel + end)
    return f"{first} of the {_pick(ORC_TRIBES)}"


# ─────────────────────────────────────────────────────────────────────
# Goblin
# ─────────────────────────────────────────────────────────────────────
GOBLIN_FIRSTS = [
    "Snik","Grix","Yark","Nub","Krik","Vex","Snek","Zib","Pek","Drik","Glub","Gnar",
    "Hrek","Mug","Nix","Skel","Tug","Yip","Zog","Vrik","Wax","Bek","Chak","Drub","Fizz",
    "Grit","Klig","Lurk","Mok","Quib","Rax","Snib","Toot","Wug","Zik",
]
GOBLIN_TOTEMS = [
    "the Quick","the Sneaky","Big-Ears","Long-Tooth","Yellow-Eye","Knife-Hand","Rat-Friend",
    "Tunnel-Born","Wolf-Rider","of the Black Ear","of the Crooked Tooth","of the Howling Pit",
    "of the Rat Den","Backbiter","Throat-Slitter","Pot-Sticker","Bonepicker","",
]

def goblin(female=False):
    first = _pick(GOBLIN_FIRSTS)
    if female and not first.endswith(("a","i","y")):
        first += _pick(["a","i","ka","na",""])
    totem = _pick(GOBLIN_TOTEMS)
    return f"{first} {totem}".strip() if totem else first


# ─────────────────────────────────────────────────────────────────────
# Hobgoblin (martial, vaguely steppe-flavoured per 2e MM)
# ─────────────────────────────────────────────────────────────────────
HOBGOBLIN_FIRSTS_M = [
    "Azgar","Borgul","Drazak","Garmuk","Grokul","Hazgrim","Jugruk","Khazgun","Korgul",
    "Mukhtar","Nazgar","Orhan","Rugor","Sukar","Targul","Ugmar","Volgan","Zogur",
]
HOBGOBLIN_FIRSTS_F = [
    "Azgha","Drashka","Gorga","Khurra","Mokra","Nazhka","Orga","Sukhra","Targha","Vazha","Zugra",
]
HOBGOBLIN_RANKS = [
    "of the Iron Banner","of the Black Tower","of the Red Legion","Captain","Standard-Bearer",
    "the Disciplined","Warden of the Pass","Commander","of the Hundred Spears","the Unyielding",
    "of the Bloody Shield","Marshal","",
]

def hobgoblin(female=False):
    first = _pick(HOBGOBLIN_FIRSTS_F if female else HOBGOBLIN_FIRSTS_M)
    rank = _pick(HOBGOBLIN_RANKS)
    return f"{first} {rank}".strip() if rank else first


# ─────────────────────────────────────────────────────────────────────
# Bugbear
# ─────────────────────────────────────────────────────────────────────
BUGBEAR_FIRSTS = [
    "Brak","Brog","Drush","Grakk","Hragga","Korl","Krug","Mosh","Norg","Rakka","Skarn",
    "Snarl","Thrak","Throg","Urg","Vorg","Warg","Yorga","Zarn","Hrothka","Mograk",
]
BUGBEAR_EPITHETS = [
    "the Cruel","Bonebreaker","Skullhefter","Throat-Render","Maul-Hand","Quiet-Foot",
    "the Patient","Hide-Wearer","Hunter-in-the-Dark","Spike-Mace","the Foul","Ear-Taker","",
]

def bugbear(female=False):
    first = _pick(BUGBEAR_FIRSTS)
    epithet = _pick(BUGBEAR_EPITHETS)
    return f"{first} {epithet}".strip() if epithet else first


# ─────────────────────────────────────────────────────────────────────
# Kobold
# ─────────────────────────────────────────────────────────────────────
KOBOLD_FIRSTS = [
    "Kik","Sszik","Vex","Yip","Zik","Snek","Drik","Krik","Pip","Tix","Vrik","Wek","Skitt",
    "Glix","Hek","Mek","Nix","Quik","Rix","Tek","Vix","Yek","Zek","Krrk","Sthik","Vrrss",
]
KOBOLD_CLANS = [
    "of the Burning Den","of the Black Scale","of the Hidden Warren","Trapsetter","Pebble-Born",
    "Lurker","of the Iron Tail","Tunneler","Sparkmaker","of the Long Tunnel","Shrine-Tender",
    "of the Dragon's Shadow","",
]

def kobold(female=False):
    first = _pick(KOBOLD_FIRSTS)
    clan = _pick(KOBOLD_CLANS)
    return f"{first} {clan}".strip() if clan else first


# ─────────────────────────────────────────────────────────────────────
# Drow (dark elf)
# ─────────────────────────────────────────────────────────────────────
DROW_FIRSTS_M = [
    "Berg","Dinin","Gromph","Kimmuriel","Nimor",
    "Pharaun","Quenthel","Rizzen","Solaufein","Tos","Uthegentel","Velkyn","Xullrae","Zaylin",
    "Malagar","Veldrin","Tornin","Krenaste",
]
DROW_FIRSTS_F = [
    "Akordia","Briza","Chasrene","Drisinil","Eclavdra","Ginafae","Greyanna","Halisstra",
    "Imrae","Iyantha","Maya","Nathrae","Phaere","Qilue","Quavylene","SiNafay",
    "Triel","Umrae","Vendes","Yasraena","Zilvra","Mindra",
]
DROW_HOUSES = [
    "Hun'ett","Oblodra","Faen Tlabbar","Mizzrym","Barrison Del'Armgo",
    "Vandree","Despana","Tor'tar","Auvryndar","Eilservs","Kilsek","Tormtor","Aleval","Symryvvin",
    "Xorlarrin","Melarn","Duskryn","Noquar","Shobalar","T'orgh",
]

def drow(female=False):
    if _maybe(0.6):
        first = _pick(DROW_FIRSTS_F if female else DROW_FIRSTS_M)
    else:
        onset = _pick(["Ak","Br","Dr","El","Gh","Im","Iy","Kr","Lir","Mal","Nim","Phae","Ql",
                       "Sol","Tr","Ur","Vel","Vie","Xo","Za","Zi"])
        if female:
            mid = _pick(["a","ae","ie","oa","irae","onnia","yrra","entha","aelin","aevra"])
            end = _pick(["","","ra","na","th","ne","sse","l","ria"])
        else:
            mid = _pick(["a","ae","oth","ar","ir","aer","ezz","ant","or","aern"])
            end = _pick(["","","is","or","th","el","an","ar","el'd","yth"])
        first = _join(onset + mid + end)
    return f"{first} of House {_pick(DROW_HOUSES)}"


# ─────────────────────────────────────────────────────────────────────
# Duergar (gray dwarf)
# ─────────────────────────────────────────────────────────────────────
DUERGAR_FIRSTS_M = [
    "Bharash","Brakk","Dunmir","Gardain","Grimnir","Hagrim","Karnak","Khorgan","Maldred",
    "Morrik","Murgaz","Nargrim","Ralkir","Sklar","Tharog","Thraka","Ulgath","Volthak","Zudrok",
]
DUERGAR_FIRSTS_F = [
    "Brundryl","Dagga","Drudga","Garna","Hrolga","Khaldra","Mardun","Morga","Skraga",
    "Thrudga","Urzaa","Volga","Wruna",
]
DUERGAR_CLANS = [
    "Ashforge","Blackvein","Cinderhall","Deephold","Ironchain","Lavaforge","Mirrorshield",
    "Shadowanvil","Slatevein","Stoneblight","Thunderpit","Underdelve","Voidhammer","Greyhelm",
    "Ashbeard","Stormpick","Murkbrow","Coalspine",
]

def duergar(female=False):
    first = _pick(DUERGAR_FIRSTS_F if female else DUERGAR_FIRSTS_M)
    return f"{first} {_pick(DUERGAR_CLANS)}"


# ─────────────────────────────────────────────────────────────────────
# Lizardfolk
# ─────────────────────────────────────────────────────────────────────
LIZARDFOLK_NAMES = [
    "Sshakur","Issk-Tha","Hassrek","Vaszik","Sslan","Threkka","Ssrekka","Othokk","Ksiri",
    "Ssarvai","Vraxx","Hsstak","Tessik","Zhassk","Sslither","Krexx","Vraxik","Issaroth",
    "Sslassik","Threxxal","Hssoth",
]
LIZARDFOLK_DESCRIPTORS = [
    "of the Black Marsh","of the Sunken Reach","Salt-Tongue","Eel-Catcher","Mud-Walker",
    "Reed-Hunter","of the Drowned Tribe","Stillwater","Spawn-Singer","Swamp-King","Bone-Necklace",
    "of the Long River","",
]

def lizardfolk(female=False):
    first = _pick(LIZARDFOLK_NAMES)
    desc = _pick(LIZARDFOLK_DESCRIPTORS)
    return f"{first} {desc}".strip() if desc else first


# ─────────────────────────────────────────────────────────────────────
# Gnoll
# ─────────────────────────────────────────────────────────────────────
GNOLL_NAMES = [
    "Yegnar","Hek-Hak","Yrr","Karruk","Hyenak","Skrii","Yarra","Ghul","Vrek","Hyena-Maw",
    "Kraal","Ssvik","Throk","Ygnir","Veshnek","Yarrik","Hrolga","Skar-Hak","Yipper","Yegga",
]
GNOLL_PACKS = [
    "of the Laughing Pack","Fang of Yeenoghu","Bone-Picker","of the Red Moon","Hyena-Born",
    "Carrion-Tongue","of the Howling Hills","Pack-Leader","Bloody-Jaw","of the Death Track","",
]

def gnoll(female=False):
    first = _pick(GNOLL_NAMES)
    pack = _pick(GNOLL_PACKS)
    return f"{first} {pack}".strip() if pack else first


# ─────────────────────────────────────────────────────────────────────
# Tiefling
# ─────────────────────────────────────────────────────────────────────
TIEFLING_FIRSTS_M = [
    "Akmenos","Amnon","Barakas","Damakos","Ekemon","Iados","Kairon","Leucis","Melech",
    "Mordai","Morthos","Pelaios","Skamos","Therai","Caim","Vassago","Therion","Zariel","Mordrek",
]
TIEFLING_FIRSTS_F = [
    "Akta","Anakis","Bryseis","Criella","Damaia","Ea","Kallista","Lerissa","Makaria","Nemeia",
    "Orianna","Phelaia","Rieta","Thraxa","Velika","Karya","Mara","Lilith","Nyx",
]
TIEFLING_VIRTUE = [
    "Carrion","Despair","Glory","Mockery","Open","Pity","Quiet","Random","Reverence","Sorrow",
    "Temerity","Weary","Excellence","Hope","Ideal","Music","Nowhere","Vengeance",
]
TIEFLING_SURNAMES = [
    "Voryn","Belmoros","Hexlace","Vaelorian","Stigmal","Ashenvale","Drovak","Helborne",
    "Kazirian","Maledictus","Nightshade","Sablecrest","Veynara","Wrathwood","Zaltheris",
]

def tiefling(female=False):
    if _maybe(0.55):
        first = _pick(TIEFLING_FIRSTS_F if female else TIEFLING_FIRSTS_M)
        return f"{first} {_pick(TIEFLING_SURNAMES)}"
    return _pick(TIEFLING_VIRTUE)


# ─────────────────────────────────────────────────────────────────────
# Registry + dispatch
# ─────────────────────────────────────────────────────────────────────
GENERATORS = {
    "halfling":   halfling,
    "human":      human,
    "elf":        elf,
    "dwarf":      dwarf,
    "gnome":      gnome,
    "halfelf":    halfelf,
    "halforc":    halforc,
    "orc":        orc,
    "goblin":     goblin,
    "hobgoblin":  hobgoblin,
    "bugbear":    bugbear,
    "kobold":     kobold,
    "drow":       drow,
    "duergar":    duergar,
    "lizardfolk": lizardfolk,
    "gnoll":      gnoll,
    "tiefling":   tiefling,
}
RACES = list(GENERATORS.keys())

# Common spelling variants → canonical key
_ALIASES = {
    "highelf":    "elf",
    "woodelf":    "elf",
    "moonelf":    "elf",
    "sunelf":     "elf",
    "darkelf":    "drow",
    "deepelf":    "drow",
    "graydwarf":  "duergar",
    "greydwarf":  "duergar",
    "hilldwarf":  "dwarf",
    "mountaindwarf": "dwarf",
    "lightfoot":  "halfling",
    "stout":      "halfling",
    "rockgnome":  "gnome",
    "deepgnome":  "gnome",
    "halfdemon":  "tiefling",
    "fiendborn":  "tiefling",
    "lizardman":  "lizardfolk",
    "lizardmen":  "lizardfolk",
    "fullorc":    "orc",
    "halforch":   "halforc",
}


def generate(race: str = "", female: bool = False) -> str:
    """Generate a name. race defaults to random PC race if empty or unrecognised."""
    key = race.lower().replace("-", "").replace("_", "").replace(" ", "")
    key = _ALIASES.get(key, key)
    fn = GENERATORS.get(key)
    if fn is None:
        # Unknown race → random PC race (don't surprise callers with goblin names)
        fn = GENERATORS[random.choice(["human","elf","dwarf","halfling","gnome","halfelf","halforc"])]
    return fn(female=female)
