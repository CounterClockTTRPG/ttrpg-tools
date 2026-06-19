"""Player holdings — houses (real estate) and mounts (steeds, beasts of burden).

Both kinds live in ``campaign.json`` under top-level ``houses`` / ``mounts``
dicts, keyed by slug, matching the existing pattern for ``characters``
and ``quests``.

The two kinds are intentionally separate tools rather than a unified
``add_holding(kind=...)`` because their schemas diverge enough that a
union schema would be ugly: houses have value/residents/caretaker;
mounts have HP/AC/MV/attacks and integrate with the combat tracker.

House schema::

    {
      name, owner,                    # PC slug
      kind,                            # free-form string — see SUGGESTED_HOUSE_KINDS
      location,                        # location slug or free text
      value_gp,
      description,
      caretaker,                       # NPC slug (optional)
      residents,                       # [NPC slugs]
      inventory,                       # [item strings]
      portrait, portrait_prompt
    }

Mount schema::

    {
      name, owner,                    # PC slug
      species,                         # warhorse|riding-horse|pony|mule|donkey|camel|war-pony|draft-horse
      hp_max, hp, ac, mv, thac0,
      attacks,                         # [free-text damage strings]
      morale,
      description,
      inventory,                       # [item strings — gear strapped to the mount]
      portrait, portrait_prompt
    }

Common mount species auto-default to MM stats; pass explicit values to
override. Custom species (e.g. ``griffon``, ``dire-wolf``) require the
caller to supply all the stats.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import _campaign as _c
from tools import images as _img


# --- House kinds ---------------------------------------------------------

# Suggested values, NOT a closed enum. Any kind string is accepted —
# this list exists to seed the prompt-builder, the dashboard filter,
# and the docstring so users have a starting vocabulary. Pass anything
# else (``lair``, ``warehouse``, ``wreck``, ``hideout``…) and it is
# stored as-is.
SUGGESTED_HOUSE_KINDS = (
    "cottage", "manor", "tower", "keep", "fortress", "stronghold",
    "inn", "shop", "warehouse", "farm", "hovel", "townhouse", "estate",
    "lair", "ruin", "outpost", "vault", "hideout", "shrine", "temple",
)


# --- Mount species defaults (AD&D 2e Monstrous Manual) -------------------

# Each tuple: (hp_max, ac, mv, thac0, attacks, morale).
# Custom species (e.g. owlbear, dire-bear, dragon turtle) work too —
# add_mount accepts any species string; the caller just has to provide
# stats explicitly when no entry is found here.
_MOUNT_DEFAULTS = {
    # Mundane
    "riding-horse":     (18, 7, 24, 19, ["1d3 (kick)"],                       8),
    "warhorse-light":   (27, 7, 24, 17, ["1d4/1d4/1d3 (hooves/bite)"],       11),
    "warhorse-medium":  (33, 6, 18, 16, ["1d6/1d6/1d3 (hooves/bite)"],       12),
    "warhorse-heavy":   (39, 5, 15, 15, ["1d8/1d8/1d3 (hooves/bite)"],       13),
    "warhorse":         (33, 6, 18, 16, ["1d6/1d6/1d3 (hooves/bite)"],       12),
    "draft-horse":      (18, 7, 12, 19, ["1d4 (kick)"],                       8),
    "pony":             (16, 7, 12, 19, ["1d2 (kick)"],                       7),
    "war-pony":         (20, 7, 18, 18, ["1d3/1d3 (kicks)"],                  9),
    "mule":             (15, 7, 12, 19, ["1d2 (kick)"],                       8),
    "donkey":           ( 7, 7, 12, 20, ["1d2 (kick)"],                       7),
    "camel":            (21, 7, 21, 18, ["1d4/1d4/1 (kicks/bite)"],           7),
    "riding-dog":       ( 7, 7, 15, 19, ["1d3 (bite)"],                       7),
    "dire-wolf":        (30, 6, 18, 17, ["2d4 (bite)"],                       9),
    # Flying / exotic — the canonical "I want a griffon mount" cohort
    "griffon":          (39, 3, 12, 13, ["1d4/1d4/2d8 (claws/bite)"],        13),
    "hippogriff":       (25, 5, 18, 17, ["1d6/1d6/1d10 (claws/bite)"],       11),
    "pegasus":          (25, 6, 24, 17, ["1d8/1d8/1d3 (hooves/bite)"],       13),
    "giant-eagle":      (28, 7,  3, 17, ["1d6/1d6/2d6 (claws/beak)"],        11),
    "giant-owl":        (24, 6,  3, 17, ["1d4/1d4/1d4 (claws/beak)"],        10),
    # Aquatic
    "giant-sea-horse":  (24, 7, 21, 17, ["1d3 (head-butt)"],                  7),
}

VALID_MOUNT_SPECIES = tuple(_MOUNT_DEFAULTS.keys())


# --- Helpers --------------------------------------------------------------

def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_owner(cfg: dict, owner: str) -> tuple[str | None, str | None]:
    """Return (resolved_owner_key, error). Owners must reference an
    existing PC in cfg['characters']."""
    if not owner:
        return None, "owner is required"
    key = owner.strip().lower()
    chars = cfg.get("characters", {})
    if key in chars:
        return key, None
    # Allow display-label match for ergonomics.
    for k, c in chars.items():
        if c.get("label", "").lower() == key:
            return k, None
    return None, f"owner '{owner}' is not a known PC"


def _portrait_prompt_for_house(h: dict) -> str:
    kind = h.get("kind", "cottage")
    location = h.get("location", "")
    desc = (h.get("description") or "").strip()
    parts = [f"A {kind}"]
    if location:
        parts.append(f"in {location}")
    if desc:
        first = desc.split(".")[0].strip()
        if 10 < len(first) < 140:
            parts.append(first.lower())
    return ". ".join(parts)


def _portrait_prompt_for_mount(m: dict) -> str:
    species = (m.get("species") or "horse").replace("-", " ")
    name = m.get("name", "")
    desc = (m.get("description") or "").strip()
    parts = [f"A {species}"]
    if name:
        parts.append(f"named {name}")
    if desc:
        first = desc.split(".")[0].strip()
        if 10 < len(first) < 140:
            parts.append(first.lower())
    return ". ".join(parts)


# --- Tool registration ---------------------------------------------------

def register(mcp):

    # ====== HOUSES ======

    @mcp.tool()
    def add_house(
        slug: str,
        name: str,
        owner: str,
        kind: str = "cottage",
        location: str = "",
        value_gp: int = 0,
        description: str = "",
        caretaker: str = "",
        residents: list = None,
        inventory: list = None,
        portrait_prompt: str = "",
        generate_portrait: bool = False,
    ) -> dict:
        """Register a house, manor, shop, or other piece of real estate owned by a PC.

        slug:            canonical id ('oakhill_cottage', 'tomas_tower')
        name:            display name
        owner:           PC slug or label — must exist in the campaign
        kind:            free-form string. Suggested vocab: cottage, manor,
                         tower, keep, fortress, stronghold, inn, shop,
                         warehouse, farm, hovel, townhouse, estate, lair,
                         ruin, outpost, vault, hideout, shrine, temple. Any
                         other string (e.g. ``salvaged-shipwreck``) is
                         accepted — used in prompt-building and display.
        location:        location slug or free-text ("Orlane, north end")
        value_gp:        rough resale value in gp
        description:     prose description
        caretaker:       NPC slug who watches the place when the owner is away
        residents:       NPC slugs who live there (caretaker counts here too)
        inventory:       items kept inside ['Iron strongbox (locked)', ...]
        portrait_prompt: image-gen prompt; auto-built from kind+location+description if omitted
        generate_portrait: if True, calls Replicate to render the portrait now"""
        cfg = _c.load_campaign()

        kind = (kind or "").strip().lower().replace(" ", "-") or "cottage"
        slug = _slugify(slug)
        houses = cfg.setdefault("houses", {})
        if slug in houses:
            return {"error": f"House '{slug}' already exists. Use update_house."}

        owner_key, err = _resolve_owner(cfg, owner)
        if err:
            return {"error": err}

        record = {
            "name":      name,
            "owner":     owner_key,
            "kind":      kind,
            "location":  location,
            "value_gp":  int(value_gp or 0),
            "description": description,
            "caretaker": caretaker.strip().lower() if caretaker else "",
            "residents": [r.strip().lower() for r in (residents or []) if r],
            "inventory": list(inventory or []),
            "portrait":  "",
            "portrait_prompt": portrait_prompt or _portrait_prompt_for_house({
                "kind": kind, "location": location, "description": description,
            }),
            "created_at": _now_iso(),
        }

        result = {"slug": slug, "added": True, "owner": owner_key}

        if generate_portrait:
            cfg["_data_dir"] = cfg.get("_data_dir") or _c.load_campaign().get("_data_dir")
            pr = _img.generate_portrait_for(cfg, slug, record["portrait_prompt"])
            if "error" in pr:
                result["portrait_error"] = pr["error"]
            else:
                record["portrait"] = pr["filename"]
                result["portrait"] = pr["filename"]

        houses[slug] = record
        _c.save_campaign(cfg)
        return result

    @mcp.tool()
    def list_houses(owner: str = "") -> dict:
        """List all houses, optionally filtered by owner PC slug.
        Returns {houses: [{slug, name, owner, kind, location, value_gp, has_portrait}]}."""
        cfg = _c.load_campaign()
        houses = cfg.get("houses", {})
        owner_filter = owner.strip().lower() if owner else None
        out = []
        for slug, h in houses.items():
            if owner_filter and h.get("owner") != owner_filter:
                continue
            out.append({
                "slug":     slug,
                "name":     h.get("name", slug),
                "owner":    h.get("owner", ""),
                "kind":     h.get("kind", "cottage"),
                "location": h.get("location", ""),
                "value_gp": h.get("value_gp", 0),
                "has_portrait": bool(h.get("portrait")),
            })
        out.sort(key=lambda r: (r["owner"], r["name"]))
        return {"houses": out, "count": len(out)}

    @mcp.tool()
    def get_house(slug: str) -> dict:
        """Full record for a house, including residents, inventory, and portrait filename."""
        cfg = _c.load_campaign()
        slug = _slugify(slug)
        h = cfg.get("houses", {}).get(slug)
        if not h:
            return {"error": f"No house '{slug}'."}
        return {"slug": slug, **h}

    @mcp.tool()
    def update_house(
        slug: str,
        name: str = None,
        owner: str = None,
        kind: str = None,
        location: str = None,
        value_gp: int = None,
        description: str = None,
        caretaker: str = None,
        residents: list = None,
        inventory: list = None,
    ) -> dict:
        """Patch-update a house. Pass only the fields you want to change.
        ``residents`` and ``inventory`` REPLACE the existing list (not append) —
        read the current value with get_house first if you want to add.
        Changing ``owner`` re-validates against the PC roster."""
        cfg = _c.load_campaign()
        slug = _slugify(slug)
        houses = cfg.get("houses", {})
        if slug not in houses:
            return {"error": f"No house '{slug}'."}
        h = houses[slug]

        if name is not None:        h["name"] = name
        if owner is not None:
            owner_key, err = _resolve_owner(cfg, owner)
            if err:
                return {"error": err}
            h["owner"] = owner_key
        if kind is not None:
            h["kind"] = kind.strip().lower().replace(" ", "-") or "cottage"
        if location is not None:    h["location"] = location
        if value_gp is not None:    h["value_gp"] = int(value_gp)
        if description is not None: h["description"] = description
        if caretaker is not None:   h["caretaker"] = caretaker.strip().lower()
        if residents is not None:
            h["residents"] = [r.strip().lower() for r in residents if r]
        if inventory is not None:
            h["inventory"] = list(inventory)

        h["updated_at"] = _now_iso()
        _c.save_campaign(cfg)
        return {"slug": slug, "updated": True}

    @mcp.tool()
    def regenerate_house_portrait(slug: str, prompt: str = "") -> dict:
        """Re-render a house portrait. Uses the stored ``portrait_prompt`` unless
        a new one is passed. Replaces the previous portrait reference."""
        cfg = _c.load_campaign()
        slug = _slugify(slug)
        h = cfg.get("houses", {}).get(slug)
        if not h:
            return {"error": f"No house '{slug}'."}
        prompt = prompt or h.get("portrait_prompt") or _portrait_prompt_for_house(h)
        pr = _img.generate_portrait_for(cfg, slug, prompt)
        if "error" in pr:
            return pr
        h["portrait"] = pr["filename"]
        h["portrait_prompt"] = prompt
        _c.save_campaign(cfg)
        return {"slug": slug, "portrait": pr["filename"]}

    # ====== MOUNTS ======

    @mcp.tool()
    def add_mount(
        slug: str,
        name: str,
        owner: str,
        species: str,
        hp_max: int = 0,
        ac: int = 0,
        mv: int = 0,
        thac0: int = 0,
        attacks: list = None,
        morale: int = 0,
        description: str = "",
        inventory: list = None,
        portrait_prompt: str = "",
        generate_portrait: bool = False,
    ) -> dict:
        """Register a mount (steed, beast of burden) owned by a PC.

        slug:            canonical id ('shadowstride', 'old_bess')
        name:            display name
        owner:           PC slug or label
        species:         free-form string. Recognised defaults include
                         riding-horse, warhorse-light/medium/heavy, draft-horse,
                         pony, war-pony, mule, donkey, camel, riding-dog,
                         dire-wolf, griffon, hippogriff, pegasus, giant-eagle,
                         giant-owl, giant-sea-horse. Any other species
                         (owlbear, dragon-turtle, blink-dog, …) is accepted
                         but the caller must then provide explicit stats.
        hp_max/ac/mv/thac0/attacks/morale:
                         combat stats. For known species these auto-default to
                         AD&D 2e MM values; pass non-zero values to override.
                         Custom species require all stats explicitly.
        description:     prose (markings, temperament, scars)
        inventory:       gear strapped to the mount (saddle, bags, contents)
        portrait_prompt: image-gen prompt; auto-built from species+description
        generate_portrait: if True, render the portrait now via Replicate"""
        cfg = _c.load_campaign()

        slug = _slugify(slug)
        mounts = cfg.setdefault("mounts", {})
        if slug in mounts:
            return {"error": f"Mount '{slug}' already exists. Use update_mount."}

        owner_key, err = _resolve_owner(cfg, owner)
        if err:
            return {"error": err}

        species_key = species.strip().lower().replace(" ", "-")
        defaults = _MOUNT_DEFAULTS.get(species_key)
        if defaults:
            d_hp, d_ac, d_mv, d_thac0, d_attacks, d_morale = defaults
        else:
            d_hp = d_ac = d_mv = d_thac0 = d_morale = 0
            d_attacks = []

        # Use explicit values when non-zero/non-empty, else fall back to default.
        # Note: AC=10 means "no armor" (valid) and MV=0 means "stationary"
        # (valid for vehicles, treants in dormant pose, etc.) — both are
        # legitimate values, not "missing". Only HP and THAC0 are validated
        # as positive because HP<=0 is dead and THAC0<=0 is nonsensical.
        final_hp     = hp_max if hp_max > 0 else d_hp
        final_ac     = ac     if ac     != 0 else d_ac
        final_mv     = mv     if mv     != 0 else d_mv
        final_thac0  = thac0  if thac0  > 0 else d_thac0
        final_morale = morale if morale > 0 else d_morale
        final_attacks = list(attacks) if attacks else list(d_attacks)

        if not defaults and (final_hp <= 0 or final_thac0 <= 0):
            return {
                "error": f"Custom species '{species}' has no defaults. "
                         f"Pass hp_max and thac0 explicitly (AC and MV may "
                         f"be any value including 0 for stationary objects)."
            }

        record = {
            "name":     name,
            "owner":    owner_key,
            "species":  species_key,
            "hp_max":   final_hp,
            "hp":       final_hp,
            "ac":       final_ac,
            "mv":       final_mv,
            "thac0":    final_thac0,
            "attacks":  final_attacks,
            "morale":   final_morale,
            "description": description,
            "inventory": list(inventory or []),
            "portrait":  "",
            "portrait_prompt": portrait_prompt or _portrait_prompt_for_mount({
                "species": species_key, "name": name, "description": description,
            }),
            "created_at": _now_iso(),
        }

        result = {"slug": slug, "added": True, "owner": owner_key,
                  "stats": {"hp": final_hp, "ac": final_ac, "mv": final_mv, "thac0": final_thac0}}

        if generate_portrait:
            pr = _img.generate_portrait_for(cfg, slug, record["portrait_prompt"])
            if "error" in pr:
                result["portrait_error"] = pr["error"]
            else:
                record["portrait"] = pr["filename"]
                result["portrait"] = pr["filename"]

        mounts[slug] = record
        _c.save_campaign(cfg)
        return result

    @mcp.tool()
    def list_mounts(owner: str = "") -> dict:
        """List all mounts, optionally filtered by owner.
        Returns {mounts: [{slug, name, owner, species, hp, hp_max, has_portrait}]}."""
        cfg = _c.load_campaign()
        mounts = cfg.get("mounts", {})
        owner_filter = owner.strip().lower() if owner else None
        out = []
        for slug, m in mounts.items():
            if owner_filter and m.get("owner") != owner_filter:
                continue
            out.append({
                "slug":     slug,
                "name":     m.get("name", slug),
                "owner":    m.get("owner", ""),
                "species":  m.get("species", ""),
                "hp":       m.get("hp", 0),
                "hp_max":   m.get("hp_max", 0),
                "has_portrait": bool(m.get("portrait")),
            })
        out.sort(key=lambda r: (r["owner"], r["name"]))
        return {"mounts": out, "count": len(out)}

    @mcp.tool()
    def get_mount(slug: str) -> dict:
        """Full record for a mount, including stats, inventory, portrait filename."""
        cfg = _c.load_campaign()
        slug = _slugify(slug)
        m = cfg.get("mounts", {}).get(slug)
        if not m:
            return {"error": f"No mount '{slug}'."}
        return {"slug": slug, **m}

    @mcp.tool()
    def update_mount(
        slug: str,
        name: str = None,
        owner: str = None,
        species: str = None,
        hp_max: int = None,
        hp: int = None,
        ac: int = None,
        mv: int = None,
        thac0: int = None,
        attacks: list = None,
        morale: int = None,
        description: str = None,
        inventory: list = None,
    ) -> dict:
        """Patch-update a mount. Pass only the fields you want to change.
        ``attacks`` and ``inventory`` REPLACE the existing list. Changing
        ``owner`` re-validates against the PC roster."""
        cfg = _c.load_campaign()
        slug = _slugify(slug)
        mounts = cfg.get("mounts", {})
        if slug not in mounts:
            return {"error": f"No mount '{slug}'."}
        m = mounts[slug]

        if name is not None:        m["name"] = name
        if owner is not None:
            owner_key, err = _resolve_owner(cfg, owner)
            if err:
                return {"error": err}
            m["owner"] = owner_key
        if species is not None:     m["species"] = species.strip().lower().replace(" ", "-")
        if hp_max is not None:      m["hp_max"] = int(hp_max)
        if hp is not None:          m["hp"] = int(hp)
        if ac is not None:          m["ac"] = int(ac)
        if mv is not None:          m["mv"] = int(mv)
        if thac0 is not None:       m["thac0"] = int(thac0)
        if morale is not None:      m["morale"] = int(morale)
        if attacks is not None:     m["attacks"] = list(attacks)
        if description is not None: m["description"] = description
        if inventory is not None:   m["inventory"] = list(inventory)

        m["updated_at"] = _now_iso()
        _c.save_campaign(cfg)
        return {"slug": slug, "updated": True}

    @mcp.tool()
    def regenerate_mount_portrait(slug: str, prompt: str = "") -> dict:
        """Re-render a mount portrait. Uses the stored ``portrait_prompt`` unless
        a new one is passed."""
        cfg = _c.load_campaign()
        slug = _slugify(slug)
        m = cfg.get("mounts", {}).get(slug)
        if not m:
            return {"error": f"No mount '{slug}'."}
        prompt = prompt or m.get("portrait_prompt") or _portrait_prompt_for_mount(m)
        pr = _img.generate_portrait_for(cfg, slug, prompt)
        if "error" in pr:
            return pr
        m["portrait"] = pr["filename"]
        m["portrait_prompt"] = prompt
        _c.save_campaign(cfg)
        return {"slug": slug, "portrait": pr["filename"]}
