"""Combat map — dungml DSL renderer, grid state, combatant positioning.

The map renderer delegates to the dungml backend (`dmap-server`) at
`DUNGML_API_BASE` (default http://127.0.0.1:8000). Rooms, corridors,
doors, windows, and features are drawn by dungml's classic-bw / hatched
/ floorplan renderers; combatants ride on top as integer-cell tokens in
the same coordinate space as the map's `grid { bounds W x H }` block.
"""
import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import _campaign as _c
from tools.combat import get_session

BASE_DIR = Path(__file__).parent.parent

DUNGML_API_BASE = os.environ.get("DUNGML_API_BASE", "http://127.0.0.1:8000").rstrip("/")
_DUNGML_RENDER_URL = f"{DUNGML_API_BASE}/api/dsl/render"
_DUNGML_TIMEOUT_S = 10.0


def _dungml_render(source: str, renderer: str | None = None) -> dict:
    """POST to dungml's /api/dsl/render. Returns the parsed JSON or raises.

    Errors are surfaced as one of:
      - ConnectionError    backend unreachable (server down / wrong URL)
      - ValueError         parse error or bad request from the backend
      - RuntimeError       any other non-200 status
    """
    payload = json.dumps(
        {"source": source, "renderer": renderer}
    ).encode("utf-8")
    req = urllib.request.Request(
        _DUNGML_RENDER_URL,
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_DUNGML_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # FastAPI puts our parse-error envelope under `detail`.
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {}
        if e.code == 400:
            detail = body.get("detail", {})
            if isinstance(detail, dict):
                raise ValueError(
                    f'parse error at line {detail.get("line", 0)}'
                    f':{detail.get("column", 0)}: '
                    f'{detail.get("message", "(no detail)")}'
                ) from e
            raise ValueError(f"bad request: {detail}") from e
        raise RuntimeError(f"dungml backend HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise ConnectionError(
            f"dungml backend unreachable at {_DUNGML_RENDER_URL} "
            f"({e.reason}). Start it with `uv run dmap-server` from the "
            f"dungml workspace, or set DUNGML_API_BASE."
        ) from e


_VIEWBOX_RX = re.compile(
    r'viewBox="0\s+0\s+([\d.]+)\s+([\d.]+)"'
)
_UNITS_RX = re.compile(
    r'units\s+([A-Za-z_-]+)\s+([\d.]+)', re.MULTILINE
)


def _extract_grid_meta(svg: str, dsl: str) -> tuple[int, int, str]:
    """Recover (width, height, scale_label) from rendered SVG + source DSL.

    Width/height come from the viewBox (which dungml sets from the
    `grid { bounds W x H }` block, even when the renderer extends it
    downward for a legend strip — we slice off any extra rows in case
    the source enabled `legend`). Scale label parses the optional
    `units NAME N` declaration; falls back to "5ft" so combat distances
    still read sensibly.
    """
    m = _VIEWBOX_RX.search(svg)
    if not m:
        raise ValueError("could not extract viewBox from rendered SVG")
    width = int(float(m.group(1)))
    height = int(float(m.group(2)))
    # The legend strip is appended below the map (LEGEND_HEIGHT = 4
    # world units in the renderer). Heuristic: if the source has
    # `legend` and the height is bigger than the declared bounds, trim
    # back to the bounds.
    bounds_m = re.search(r'bounds\s+([\d.]+)\s*x\s*([\d.]+)', dsl)
    if bounds_m:
        bw, bh = int(float(bounds_m.group(1))), int(float(bounds_m.group(2)))
        width, height = bw, bh

    units_m = _UNITS_RX.search(dsl)
    if units_m:
        name = units_m.group(1).lower()
        per_cell = units_m.group(2).rstrip("0").rstrip(".") or units_m.group(2)
        # Compact label for the dashboard ("feet 5" → "5ft", "yards 5" → "5yd").
        unit_short = {"feet": "ft", "foot": "ft", "yards": "yd", "yard": "yd",
                      "meters": "m", "metres": "m", "meter": "m"}.get(name, name)
        scale = f"{per_cell}{unit_short}"
    else:
        scale = "5ft"
    return width, height, scale


def _svg_to_data_url(svg: str) -> str:
    """Encode an SVG string as a base64 data URL. Base64 (rather than
    percent-encoding) keeps the JSON payload simple and avoids escaping
    inside data URLs."""
    enc = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{enc}"


# ----- Combatant-driven marker upsert -----
#
# Every `place_combatant` call rewrites the stored DSL to keep an
# up-to-date `marker "NAME" at X,Y tag TAG ...` line for each placed
# combatant. The DSL is then re-rendered against the dungml backend
# and the cached svg_url + dsl on the grid are swapped in atomically.
#
# Author-written markers (NPCs, environmental tokens, etc.) are
# untouched — we only rewrite lines whose marker name matches a
# combatant in the session.


def _combatant_tag(c: dict) -> str:
    """Pick a marker palette key from combat session state."""
    if c.get("hp") is not None and c["hp"] <= 0:
        return "neutral"  # downed → grey
    side = (c.get("side") or "").lower()
    if side == "party":
        return "party"
    if side == "enemy":
        return "enemy"
    return "neutral"


def _marker_initial(name: str) -> str:
    """Single-letter token glyph. Strip leading `Goblin #2` numerals."""
    base = re.sub(r"\s*#\d+\s*$", "", name).strip()
    return (base[:1] or "?").upper()


def _format_marker_line(c: dict, portrait_url: str | None = None) -> str:
    # NOTE: dungml's marker grammar does not currently accept an `image`
    # clause — we keep the portrait URL out of the DSL and inject the
    # portrait <image> into the rendered SVG via _inject_portrait_images.
    name = c["name"].replace('"', '\\"')
    x = int(c.get("x") or 0)
    y = int(c.get("y") or 0)
    tag = _combatant_tag(c)
    initial = _marker_initial(c["name"])
    return f'marker "{name}" at {x},{y}  tag {tag}  initial "{initial}"'


_MARKER_RX = re.compile(
    r'<g\s+class="marker"\s+data-name="([^"]+)"[^>]*>(.*?)</g>',
    re.DOTALL,
)
_MARKER_CIRCLE_RX = re.compile(
    r'<circle\s+cx="([^"]+)"\s+cy="([^"]+)"\s+r="([^"]+)"[^/]*/>'
)


def _inject_portrait_images(
    svg: str,
    combatants: list[dict],
    portrait_lookup: "callable | None",
) -> str:
    """Inject <image> elements into dungml-rendered marker groups so the
    map renders each combatant's portrait inside the token circle.

    The dungml backend draws markers as <circle> + <text> (initial-letter
    monogram). For combatants with a resolvable portrait URL we replace
    the text element with a clip-pathed <image> at the same position.
    """
    if not combatants or portrait_lookup is None:
        return svg

    portraits: dict[str, str] = {}
    for c in combatants:
        url = portrait_lookup(c)
        if url:
            portraits[c["name"]] = url
    if not portraits:
        return svg

    def _replace(m: "re.Match[str]") -> str:
        full = m.group(0)
        name = m.group(1).replace('\\"', '"')
        url = portraits.get(name)
        if not url:
            return full
        circle_m = _MARKER_CIRCLE_RX.search(full)
        if not circle_m:
            return full
        cx_s, cy_s, r_s = circle_m.group(1), circle_m.group(2), circle_m.group(3)
        try:
            cx, cy, r = float(cx_s), float(cy_s), float(r_s)
        except ValueError:
            return full
        clip_id = "tok-clip-" + re.sub(r'[^A-Za-z0-9]+', '-', name).strip('-')
        img_x = cx - r
        img_y = cy - r
        img_size = r * 2
        overlay = (
            f'<defs><clipPath id="{clip_id}">'
            f'<circle cx="{cx_s}" cy="{cy_s}" r="{r_s}"/>'
            f'</clipPath></defs>'
            f'<image href="{url}" x="{img_x}" y="{img_y}" '
            f'width="{img_size}" height="{img_size}" '
            f'clip-path="url(#{clip_id})" '
            f'preserveAspectRatio="xMidYMid slice"/>'
        )
        # Strip the dungml-drawn initial-letter text element since we're
        # overlaying a portrait instead.
        cleaned = re.sub(r'<text\b[^>]*>[^<]*</text>', '', full, count=1)
        # Insert the portrait overlay just before the closing </g> so it
        # paints on top of the circle.
        return cleaned.replace('</g>', overlay + '</g>', 1)

    return _MARKER_RX.sub(_replace, svg)


# Each combatant marker is tagged with a comment marker so we can find
# and rewrite our own lines without touching author-written ones.
_TAG_COMMENT = "# combat-managed"
_TAG_BLOCK_RX = re.compile(
    r"\n*# ----- combat markers -----\n(?:.*\n)*?# ----- end combat markers -----\n*",
    re.MULTILINE,
)


def _strip_combat_block(dsl: str) -> str:
    """Remove our managed marker block so the next render uses a clean slate."""
    return _TAG_BLOCK_RX.sub("\n", dsl)


def _build_combat_block(
    combatants: list[dict],
    portrait_lookup: "callable | None" = None,
) -> str:
    """Build the `# ----- combat markers -----` ... `----- end combat markers -----`
    block for every combatant that has both x and y set.

    `portrait_lookup`, if given, is called per combatant to resolve a
    portrait URL — emitted on the marker line as `image "URL"` so the
    dungml renderer paints the portrait inside the token circle.
    """
    placed = [c for c in combatants if c.get("x") is not None and c.get("y") is not None]
    if not placed:
        return ""
    lines = ["# ----- combat markers -----"]
    for c in placed:
        url = portrait_lookup(c) if portrait_lookup else None
        lines.append(_format_marker_line(c, portrait_url=url))
    lines.append("# ----- end combat markers -----")
    return "\n" + "\n".join(lines) + "\n"


def _rewrite_dsl_with_combatants(
    dsl: str,
    combatants: list[dict],
    portrait_lookup: "callable | None" = None,
) -> str:
    """Strip the existing combat-managed block (if any) and append a fresh one."""
    cleaned = _strip_combat_block(dsl).rstrip()
    block = _build_combat_block(combatants, portrait_lookup=portrait_lookup)
    if not block:
        return cleaned + "\n"
    return cleaned + "\n" + block


def _re_render_grid(
    grid: dict,
    combatants: list[dict],
    portrait_lookup: "callable | None" = None,
) -> dict | None:
    """Recompute the SVG after a combatant change. Returns an error dict
    or None on success. Updates `grid['dsl']` and `grid['svg_url']` in
    place.
    """
    src = grid.get("dsl") or ""
    if not src.strip():
        return None  # nothing to re-render — no DSL stored
    new_dsl = _rewrite_dsl_with_combatants(src, combatants, portrait_lookup=portrait_lookup)
    try:
        result = _dungml_render(new_dsl)
    except (ValueError, ConnectionError, RuntimeError) as e:
        return {"error": str(e)}
    svg = result.get("svg", "")
    svg = _inject_portrait_images(svg, combatants, portrait_lookup)
    grid["dsl"] = new_dsl
    grid["svg_url"] = _svg_to_data_url(svg)
    return None


def _names_in_combat_block(dsl: str) -> set[str]:
    """Return the set of marker names currently in the combat-managed
    block. Used by save_combat_state to flag which combatants are
    already drawn in the SVG (so the canvas skips them)."""
    block_m = _TAG_BLOCK_RX.search(dsl or "")
    if not block_m:
        return set()
    names: set[str] = set()
    for line in block_m.group(0).splitlines():
        m = re.match(r'\s*marker\s+"((?:[^"\\]|\\.)*)"', line)
        if m:
            names.add(m.group(1).replace('\\"', '"'))
    return names


def _load_portrait_index(camp_dir: Path) -> list:
    idx_path = camp_dir / "images" / "index.json"
    if not idx_path.exists():
        return []
    try:
        return json.loads(idx_path.read_text())
    except Exception:
        return []


def _portrait_url(portrait_idx: list, key: str | None, name: str, side: str) -> str | None:
    """Return a dashboard-relative portrait URL, or None.

    portrait_idx: pre-loaded images/index.json contents (avoid re-reading per combatant).
    """
    if side == "party" and key and portrait_idx:
        matches = [e for e in portrait_idx if e.get("type") == "portrait" and e.get("slug") == key]
        if matches:
            return f"/images/{matches[-1]['filename']}"

    # Monster portrait: global/monsters/<slug>.<ext>
    # Strip trailing " #N" counters ("Goblin #2" → "goblin") then normalise.
    base = re.sub(r'\s*#\d+\s*$', '', name).strip()
    slug = re.sub(r'[^a-z0-9]+', '_', base.lower()).strip('_')
    for ext in ("png", "jpg", "webp"):
        if (BASE_DIR / "global" / "monsters" / f"{slug}.{ext}").exists():
            return f"/monsters/portraits/{slug}.{ext}"

    return None


def save_combat_state(cfg: dict, session: dict) -> None:
    """Write combat_state.json for the dashboard. Called by map tools and re-exported
    so that combat.py can clear it on end_combat without importing this whole module."""
    camp_dir = cfg["_dir"]
    idx      = min(session.get("current_idx", 0), max(len(session["combatants"]) - 1, 0))
    portrait_idx = _load_portrait_index(camp_dir)

    # Combatants whose marker is already drawn into the dungml SVG get
    # an `in_svg` flag — the canvas layer skips drawing their token to
    # avoid double-vision. Author-written markers (NPCs, scene tokens)
    # aren't combatants, so they don't appear here.
    grid = session.get("grid") or {}
    svg_combat_names = _names_in_combat_block(grid.get("dsl") or "")

    combatants_out = []
    for i, c in enumerate(session["combatants"]):
        combatants_out.append({
            "name":         c["name"],
            "side":         c["side"],
            "key":          c.get("_key"),
            "hp":           c["hp"],
            "hp_max":       c["hp_max"],
            "x":            c.get("x"),
            "y":            c.get("y"),
            "init":         c.get("init"),
            "conditions":   list(c["conditions"]),
            "current":      i == idx,
            "portrait_url": _portrait_url(portrait_idx, c.get("_key"), c["name"], c["side"]),
            "movement":     c.get("movement", 6),
            "in_svg":       c["name"] in svg_combat_names,
        })

    out = {
        "active":     True,
        "round":      session["round"],
        "current":    session["combatants"][idx]["name"] if session["combatants"] else None,
        "grid":       session.get("grid"),
        "combatants": combatants_out,
    }
    _c.atomic_write_text(
        camp_dir / "combat_state.json",
        json.dumps(out, ensure_ascii=False, indent=2),
    )


def register(mcp):

    @mcp.tool()
    def create_map(dsl: str, renderer: str | None = None) -> dict:
        """Render a dungml DSL map and attach it to the active combat session.

        `dsl` is the source of a .dmap file — see the dungml language
        reference. The render runs against the dungml backend (default
        http://127.0.0.1:8000, override with the `DUNGML_API_BASE` env
        var). The SVG is embedded into combat_state.json and shows on
        the /combat dashboard. The map's `grid { bounds W x H }` becomes
        the cell coordinate space for `place_combatant`.

        Args:
          dsl:      a full .dmap source string, including a top-level
                    `map "Name" { grid { bounds W x H } ... }` block and
                    any rooms / corridors / doors / features you want.
          renderer: optional override — one of "classic-bw" (default),
                    "floorplan" (building style alias), or "hatched"
                    (architectural section drawings). If unset the
                    renderer named in the source is used.

        Returns: {size, scale, diagnostics} on success, or {error, ...}
        on parse failure / backend unreachable. `diagnostics` is a list
        of non-fatal warnings (e.g. unused names, suspicious geometry).

        Minimal working example:

            map "Crypt Room" {
              grid { bounds 20 x 14 }
            }
            room "main" {
              rect 1,1 18 x 12
              label "Tomb"
              feature pillar at 5,4
              feature pillar at 14,4
              feature pillar at 5,10
              feature pillar at 14,10
              feature altar at 10,7
            }
            door at 1,7 {
              connects room.main
              type wooden state closed
            }

        Combatants are then positioned via `place_combatant(name, x, y)`
        in the [0, W) × [0, H) cell space.
        """
        session = get_session()
        if session is None:
            return {"error": "No active combat session. Call start_combat first."}

        try:
            result = _dungml_render(dsl, renderer=renderer)
        except (ValueError, ConnectionError, RuntimeError) as e:
            return {"error": str(e)}

        svg = result.get("svg", "")
        diagnostics = result.get("diagnostics", [])
        try:
            width, height, scale = _extract_grid_meta(svg, dsl)
        except ValueError as e:
            return {"error": str(e)}

        # Inject portrait <image> elements for any author-written markers
        # whose name matches a combatant with a portrait. Subsequent
        # place_combatant calls trigger _re_render_grid which also injects.
        cfg = _c.load_campaign()
        portrait_idx = _load_portrait_index(cfg["_dir"])
        portrait_lookup = lambda c: _portrait_url(
            portrait_idx, c.get("_key"), c["name"], c["side"]
        )
        svg = _inject_portrait_images(svg, session["combatants"], portrait_lookup)

        grid = {
            "width":   width,
            "height":  height,
            "scale":   scale,
            "cells":   {},
            "styles":  {},
            "svg_url": _svg_to_data_url(svg),
            "dsl":     dsl,
        }
        session["grid"] = grid
        save_combat_state(cfg, session)

        return {
            "size":        f"{width}×{height}",
            "scale":       scale,
            "diagnostics": diagnostics,
        }

    @mcp.tool()
    def place_combatant(name: str, x: int, y: int) -> dict:
        """Place or move a combatant to grid cell (x, y).
        No movement-rate validation — DM positions tokens freely.
        Call after create_map to set initial positions for each combatant."""
        session = get_session()
        if session is None:
            return {"error": "No active combat session. Call start_combat first."}

        target = None
        low    = name.lower()
        for c in session["combatants"]:
            if c["name"].lower() == low or c["name"].lower().startswith(low):
                target = c
                break
        if target is None:
            return {"error": f"Combatant '{name}' not found."}

        grid = session.get("grid")
        if grid and not (0 <= x < grid["width"] and 0 <= y < grid["height"]):
            return {
                "error": f"({x},{y}) is outside map bounds "
                         f"({grid['width']}×{grid['height']})."
            }

        target["x"] = x
        target["y"] = y

        # Keep the rendered SVG in sync — upsert a marker line for this
        # combatant in the stored DSL and re-render against the dungml
        # backend. Errors are non-fatal: the token still moves, but the
        # SVG snapshot may lag until the backend is reachable again.
        cfg = _c.load_campaign()
        rerender_error: str | None = None
        if grid:
            portrait_idx = _load_portrait_index(cfg["_dir"])
            portrait_lookup = lambda c: _portrait_url(
                portrait_idx, c.get("_key"), c["name"], c["side"]
            )
            err = _re_render_grid(
                grid, session["combatants"], portrait_lookup=portrait_lookup
            )
            if err is not None:
                rerender_error = err.get("error")

        save_combat_state(cfg, session)

        result = {"name": target["name"], "x": x, "y": y}
        if rerender_error:
            result["warning"] = f"map not re-rendered: {rerender_error}"
        return result

    @mcp.tool()
    def get_map_state() -> dict:
        """Return map dimensions, scale, and current grid positions of all combatants."""
        session = get_session()
        if session is None:
            return {"error": "No active combat session."}

        grid = session.get("grid")
        return {
            "has_map":   grid is not None,
            "grid_size": f"{grid['width']}×{grid['height']}" if grid else None,
            "scale":     grid["scale"] if grid else None,
            "positions": [
                {
                    "name": c["name"],
                    "side": c["side"],
                    "x":    c.get("x"),
                    "y":    c.get("y"),
                }
                for c in session["combatants"]
            ],
        }
