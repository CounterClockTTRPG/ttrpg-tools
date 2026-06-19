"""Scene and portrait image generation tools using Replicate."""
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import _campaign as _c

BASE_DIR = Path(__file__).parent.parent

STYLE_BASE = (
    "Tabletop RPG game art illustration, AD&D 2nd edition cover-art aesthetic. "
    "Medieval fantasy, non-violent character portrait, safe for work. "
    "No text, no labels, no UI elements. "
    "No firearms, no modern technology."
)

TONE_STYLES = {
    "high fantasy":    "In the style of Larry Elmore and Clyde Caldwell. Painted oils, romantic heroic fantasy, heroic and luminous, epic scale. Rich colours, dramatic lighting.",
    "gritty realism":  "In the style of Brom and Jeff Easley. Painted, high-contrast, gothic. Grim, weathered, realistic. Dark tones, worn equipment.",
    "comedy":          "In the style of Tony DiTerlizzi. Inked line work with watercolor, whimsical. Lighthearted, bright colours, cartoonish.",
}

NEGATIVE_PROMPT = (
    "gun, pistol, firearm, modern clothing, modern technology, text, watermark, UI"
)


def _load_replicate():
    """Import replicate and verify token. Returns replicate module or raises ImportError."""
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
    import os
    if not os.environ.get("REPLICATE_API_TOKEN"):
        raise EnvironmentError("REPLICATE_API_TOKEN not set")
    import replicate
    return replicate


def _safe_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", s.lower()).strip("_")[:60]


def _update_index(images_dir: Path, entry: dict):
    index_file = images_dir / "index.json"
    records = []
    if index_file.exists():
        try:
            records = json.loads(index_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            records = []
    records.append(entry)
    _c.atomic_write_text(index_file, json.dumps(records, indent=2))


def _portrait_prompt_for(char: dict) -> str:
    """Build a Flux portrait prompt from a character dict."""
    race   = char.get("race", "human")
    gender = char.get("gender", "male")
    cls    = char.get("cls", "").lower()
    subject = f"{gender} {race} {cls}".strip()

    gear = []
    attacks = char.get("attacks")
    if attacks and attacks[0].get("name"):
        gear.append(f"wielding {attacks[0]['name'].lower()}")
    elif char.get("weapon") and char["weapon"] not in ("1d6", "1d4"):
        gear.append(f"wielding a {char['weapon']}")
    ac = char.get("ac", 10)
    if ac <= 4:
        gear.append("plate or chain mail armour")
    elif ac <= 7:
        gear.append("leather armour")

    bg = char.get("background", "").strip()
    bg_hint = ""
    if bg:
        first = bg.split(".")[0].strip()
        if 20 < len(first) < 80:
            bg_hint = f". {first}"

    gear_str = (", " + ", ".join(gear)) if gear else ""
    return f"Portrait of a {subject}{gear_str}{bg_hint}"


def generate_portrait_for(cfg: dict, slug: str, prompt: str) -> dict:
    """Generate and index a portrait. Returns result dict (may contain 'error' key)."""
    try:
        replicate = _load_replicate()
    except (ImportError, EnvironmentError) as exc:
        return {"error": str(exc)}

    tone = cfg.get("tone", "high fantasy")
    tone_clause = TONE_STYLES.get(tone, tone)
    full_prompt = f"{STYLE_BASE} {tone_clause}. {prompt}"

    scene_label = f"{slug} — Portrait"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{_safe_slug(scene_label)}.png"
    images_dir = cfg["_data_dir"] / "images"

    try:
        dest = _generate_image(replicate, full_prompt, images_dir, filename)
    except Exception as exc:
        return {"error": str(exc)}

    _update_index(images_dir, {
        "filename":    filename,
        "scene":       scene_label,
        "description": prompt,
        "timestamp":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "type":        "portrait",
        "slug":        slug,
    })
    return {"filename": filename, "slug": slug, "path": str(dest)}


def _generate_image(replicate, full_prompt: str, images_dir: Path, filename: str) -> Path:
    output = replicate.run(
        "black-forest-labs/flux-2-pro",
        input={
            "prompt":           full_prompt,
            "aspect_ratio":     "1:1",
            "output_format":    "png",
            "safety_tolerance": 5,
        },
    )
    images_dir.mkdir(parents=True, exist_ok=True)
    dest = images_dir / filename

    # output may be a list of URLs or file-like objects
    item = output[0] if isinstance(output, (list, tuple)) else output
    if hasattr(item, "read"):
        dest.write_bytes(item.read())
    else:
        import urllib.request
        urllib.request.urlretrieve(str(item), str(dest))

    return dest


def register(mcp):

    @mcp.tool()
    def generate_scene(prompt: str, title: str = "") -> dict:
        """Generate a scene illustration and save it to the campaign images directory.
        prompt: descriptive scene text
        title: optional display title (defaults to first 40 chars of prompt)
        Returns filename and absolute path."""
        try:
            replicate = _load_replicate()
        except (ImportError, EnvironmentError) as exc:
            return {"error": f"replicate not installed or REPLICATE_API_TOKEN not set: {exc}"}

        cfg = _c.load_campaign()
        tone = cfg.get("tone", "high fantasy")
        tone_clause = TONE_STYLES.get(tone, tone)
        full_prompt = f"{STYLE_BASE} {tone_clause}. {prompt}"

        display_title = title or prompt[:40]
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe = _safe_slug(display_title)
        filename = f"{timestamp}_{safe}.png"

        images_dir = cfg["_data_dir"] / "images"

        try:
            dest = _generate_image(replicate, full_prompt, images_dir, filename)
        except Exception as exc:
            return {"error": str(exc)}

        iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _update_index(images_dir, {
            "filename":    filename,
            "scene":       display_title,
            "description": prompt,
            "timestamp":   iso,
            "type":        "scene",
        })

        return {
            "filename": filename,
            "path":     str(dest),
        }

    @mcp.tool()
    def generate_portrait(slug: str, prompt: str, area: str = "") -> dict:
        """Generate a portrait image for a character or location.
        slug: character or location slug (used for dashboard matching)
        prompt: description of the subject
        Returns filename and slug."""
        cfg = _c.load_campaign()
        return generate_portrait_for(cfg, slug, prompt)

    @mcp.tool()
    def regenerate_portrait(key: str) -> dict:
        """Regenerate the portrait for a PC or NPC using their stored portrait_prompt.
        key: character key (from campaign.json characters or npcs)
        The stored prompt is used as-is, ensuring visual consistency across regenerations."""
        cfg = _c.load_campaign()
        char = cfg.get("characters", {}).get(key) or cfg.get("npcs", {}).get(key)
        if not char:
            return {"error": f"No stored data for '{key}'. Use add_character or set_npc_stats first."}
        prompt = char.get("portrait_prompt", "").strip()
        if not prompt:
            return {"error": f"No portrait_prompt stored for '{key}'. Set one via add_character or set_npc_stats."}
        return generate_portrait_for(cfg, key, prompt)
