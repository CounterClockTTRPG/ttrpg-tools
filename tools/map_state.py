"""Map state — persistent tactical/area map with fog-of-war.

The party's current map (a DungML-authored dungeon level, gatehouse,
inn, etc.) lives in `<campaign>/map_state.json`. The dashboard's /map
page reads it for the background SVG, room geometry, revealed-rooms
set, and party position. Combat overlays initiative on top via
`combat_state.json` when active — those two states are separate so a
map persists across combats and so the fog state survives end_combat.

Schema (map_state.json):

  {
    "active":          true,
    "slug":            "gatehouse",
    "name":            "Surface Level Gatehouse",
    "dungml_id":       "<uuid>",
    "source":          "<DungML DSL>",
    "svg_url":         "data:image/svg+xml;base64,...",
    "width":           30,
    "height":          24,
    "scale":           "5ft",
    "rooms":           [{"name": "main", "polygon": [[x,y], ...]}, ...],
    "revealed_rooms":  ["main"],
    "revealed_traps":  [[5, 5]],
    "party":           {"x": 5, "y": 12, "label": "Party"}
  }

Tools registered with MCP:
  load_map(slug)           fetch from DungML server, write map_state.json
  reveal_room(name)        add to revealed_rooms (idempotent)
  hide_room(name)          remove from revealed_rooms (rare; correction)
  spring_trap(x, y)        reveal a hidden trap/hazard in the player view
  reset_traps()            re-hide all traps in the player view
  set_map_focus(room|x,y)  center the /area viewport on a room/cell
  unload_map()             clear map_state (party left the area)
  current_map()            summary {slug, revealed, total_rooms, ...}
  mark_explored(names,party_at?)
                           reveal rooms/corridors + move party on the embedded
                           dungml /area play session (the live player view)

The /area page embeds dungml's session widget, which reads a server-side
play **session**, not map_state.json. So `reveal_room` (and the removed
`place_party_on_tactical_map`) no longer drive the /area view — use
``mark_explored`` to reveal spaces (rooms AND corridors) and move the party
there. map_state.json fog is now used only to *seed* the session on first use.
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

import _campaign as _c
from tools import _dungml_http as _dh
from tools.combat_map import _dungml_render, _svg_to_data_url, _extract_grid_meta


# ----- public file path -----

def _state_path(cfg: dict) -> Path:
    return cfg["_dir"] / "map_state.json"


def load_state(cfg: dict) -> dict:
    p = _state_path(cfg)
    if not p.exists():
        return {"active": False}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"active": False}


def save_state(cfg: dict, state: dict) -> None:
    _c.atomic_write_text(
        _state_path(cfg),
        json.dumps(state, ensure_ascii=False, indent=2),
    )


def clear_state(cfg: dict) -> None:
    p = _state_path(cfg)
    if p.exists():
        p.unlink()


# ----- room geometry extraction -----

# DSL is hand-authored and stable enough that a focused regex extractor
# beats fighting the parse API over `!include` resolution. The renderer
# already validates the DSL server-side; if a room block is malformed
# we fall through and just don't fog it, rather than failing the load.

_ROOM_BLOCK_RX = re.compile(
    r'room\s+"([^"]+)"\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}',
    re.DOTALL,
)
_RECT_RX = re.compile(
    r'\brect\s+(-?[\d.]+)\s*,\s*(-?[\d.]+)\s+(-?[\d.]+)\s*x\s*(-?[\d.]+)',
)
_POLY_POINTS_RX = re.compile(
    r'\bpolygon\s+((?:\(\s*-?[\d.]+\s*,\s*-?[\d.]+\s*\)\s*)+)',
)
_POLY_POINT_RX = re.compile(r'\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)')
_CIRCLE_RX = re.compile(
    r'\bcircle\s+(-?[\d.]+)\s*,\s*(-?[\d.]+)\s+radius\s+(-?[\d.]+)',
)


def _extract_rooms(source: str) -> list[dict]:
    """Pull room name + polygon (cell coords) for each `room "x" {...}` block.

    Handles rect and polygon shapes; falls back to a sampled circle. Boundary
    rooms with arc edges are best rendered server-side, so we skip them —
    they'll still draw fully on the map; they just won't take fog.
    """
    rooms: list[dict] = []
    for m in _ROOM_BLOCK_RX.finditer(source):
        name = m.group(1)
        body = m.group(2)
        poly = _shape_to_polygon(body)
        if poly is not None:
            rooms.append({"name": name, "polygon": poly})
    return rooms


def _shape_to_polygon(body: str) -> list[list[float]] | None:
    rect_m = _RECT_RX.search(body)
    if rect_m:
        x, y, w, h = (float(rect_m.group(i)) for i in (1, 2, 3, 4))
        return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]

    poly_m = _POLY_POINTS_RX.search(body)
    if poly_m:
        pts = _POLY_POINT_RX.findall(poly_m.group(1))
        if len(pts) >= 3:
            return [[float(px), float(py)] for px, py in pts]

    circ_m = _CIRCLE_RX.search(body)
    if circ_m:
        cx, cy, r = (float(circ_m.group(i)) for i in (1, 2, 3))
        # 24-segment polygon approximation — fine for fog masking
        import math
        return [
            [cx + r * math.cos(2 * math.pi * i / 24),
             cy + r * math.sin(2 * math.pi * i / 24)]
            for i in range(24)
        ]

    return None


# ----- DungML render with project includes -----

def _render_via_server(project_id: str, map_id: str) -> str:
    """Render a stored map server-side so `!include` resolution happens
    against the project's other maps (core.dmap, outdoor.dmap, etc.)."""
    import urllib.request, urllib.error
    url = f"{_dh.api_base()}/api/maps/{map_id}/render"
    req = urllib.request.Request(
        url,
        headers={
            "accept": "image/svg+xml",
            "authorization": f"Bearer {_dh._ensure_token()}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {}
        raise RuntimeError(
            f"dungml render HTTP {e.code}: {payload.get('detail', payload)}"
        ) from e
    except urllib.error.URLError as e:
        raise ConnectionError(
            f"dungml server unreachable at {url} ({e.reason})"
        ) from e


# ----- MCP tool implementations -----

def _do_load_map(slug: str, project: str | None = None) -> dict:
    """Fetch a DungML map by name and write map_state.json."""
    cfg = _c.load_campaign()

    project_hint = project or os.environ.get("DUNGML_PROJECT") or cfg.get("name")
    try:
        proj = _dh.find_project(project_hint)
        m = _dh.find_map(proj["id"], slug)
        full = _dh.get_map(m["id"])
        svg = _render_via_server(proj["id"], m["id"])
    except (ConnectionError, PermissionError) as e:
        return {"error": f"DungML server: {e}"}
    except LookupError as e:
        return {"error": str(e)}
    except (ValueError, RuntimeError) as e:
        return {"error": f"DungML error: {e}"}

    source = full.get("source", "")
    try:
        width, height, scale = _extract_grid_meta(svg, source)
    except ValueError as e:
        return {"error": f"could not read map dimensions: {e}"}

    rooms = _extract_rooms(source)

    # Preserve revealed state if reloading the same slug.
    prev = load_state(cfg)
    _same = prev.get("active") and prev.get("slug") == slug
    revealed = list(prev.get("revealed_rooms", [])) if _same else []
    revealed_traps = list(prev.get("revealed_traps", [])) if _same else []
    focus = prev.get("focus") if _same else None

    state = {
        "active":          True,
        "slug":            slug,
        "name":            m.get("name", slug),
        "dungml_id":       m["id"],
        "project_id":      proj["id"],
        "source":          source,
        "svg_url":         _svg_to_data_url(svg),
        "width":           width,
        "height":          height,
        "scale":           scale,
        "rooms":           rooms,
        "revealed_rooms":  revealed,
        "revealed_traps":  revealed_traps,
        "focus":           focus,
        "party":           prev.get("party") if prev.get("slug") == slug else None,
    }
    save_state(cfg, state)

    return {
        "slug":            slug,
        "name":            state["name"],
        "size":            f"{width}×{height}",
        "scale":           scale,
        "rooms":           len(rooms),
        "revealed":        len(revealed),
        "room_names":      [r["name"] for r in rooms],
    }


def _do_reveal(name: str, set_party: bool = False) -> dict:
    cfg = _c.load_campaign()
    state = load_state(cfg)
    if not state.get("active"):
        return {"error": "no map loaded — call load_map first"}

    rooms = state.get("rooms", [])
    known = {r["name"].lower(): r["name"] for r in rooms}
    low = name.lower().strip()

    # Exact, then prefix
    target = known.get(low)
    if target is None:
        prefix = [v for k, v in known.items() if k.startswith(low)]
        if len(prefix) == 1:
            target = prefix[0]
        elif len(prefix) > 1:
            return {"error": f"ambiguous room name '{name}': {prefix}"}
        else:
            return {
                "error":     f"unknown room '{name}'",
                "available": list(known.values()),
            }

    revealed = list(state.get("revealed_rooms", []))
    if target not in revealed:
        revealed.append(target)
    state["revealed_rooms"] = revealed
    save_state(cfg, state)

    out = {
        "revealed":   target,
        "total":      len(revealed),
        "of":         len(rooms),
    }

    # When asked, also move the party to this room on the embedded /area play
    # session (the live widget) — saves a separate `mark_explored` call. The
    # map_state reveal above always stands; session sync is best-effort.
    if set_party:
        out.update(_set_session_party(state, target))

    return out


def _set_session_party(state: dict, room_name: str) -> dict:
    """Move the party to `room_name` on the campaign's bound dungml play
    session (revealing it there too). Returns a status fragment; never raises."""
    map_id = state.get("dungml_id")
    if not map_id:
        return {"party_at": None, "session_sync": "map has no dungml id"}
    cfg = _c.load_campaign()
    source = state.get("source", "")
    try:
        sid = _dh.ensure_play_session(
            cfg["_dir"], map_id, source,
            seed_names=state.get("revealed_rooms", []),
            session_name=f"{cfg.get('name', 'Campaign')} — party",
        )
        node_id = _dh.resolve_node_ids(source).get(room_name)
        if not node_id:
            return {"party_at": None, "session_sync": "room not in map graph"}
        _dh.move_party(sid, node_id)  # move also reveals the node
        return {"party_at": room_name, "session": sid}
    except Exception as e:
        return {"party_at": None, "session_sync": f"skipped ({e})"}


def _do_hide(name: str) -> dict:
    cfg = _c.load_campaign()
    state = load_state(cfg)
    if not state.get("active"):
        return {"error": "no map loaded"}
    revealed = [r for r in state.get("revealed_rooms", []) if r.lower() != name.lower()]
    state["revealed_rooms"] = revealed
    save_state(cfg, state)
    return {"hidden": name, "total_revealed": len(revealed)}


def _do_spring_trap(x: int, y: int) -> dict:
    cfg = _c.load_campaign()
    state = load_state(cfg)
    if not state.get("active"):
        return {"error": "no map loaded — call load_map first"}
    if not (0 <= x < state.get("width", 0) and 0 <= y < state.get("height", 0)):
        return {
            "error": f"({x},{y}) outside map bounds "
                     f"({state.get('width')}×{state.get('height')})"
        }
    cell = [int(x), int(y)]
    traps = [list(c) for c in state.get("revealed_traps", [])]
    if cell not in traps:
        traps.append(cell)
    state["revealed_traps"] = traps
    save_state(cfg, state)
    return {"revealed": cell, "total_revealed": len(traps)}


def _do_reset_traps() -> dict:
    cfg = _c.load_campaign()
    state = load_state(cfg)
    if not state.get("active"):
        return {"error": "no map loaded"}
    state["revealed_traps"] = []
    save_state(cfg, state)
    return {"revealed_traps": 0}


def _do_set_focus(
    room: str | None = None,
    x: int | None = None,
    y: int | None = None,
) -> dict:
    cfg = _c.load_campaign()
    state = load_state(cfg)
    if not state.get("active"):
        return {"error": "no map loaded — call load_map first"}

    # No arguments → clear the focus (revert to fit-to-bounds).
    if room is None and x is None and y is None:
        state["focus"] = None
        save_state(cfg, state)
        return {"focus": None}

    label = ""
    if room is not None:
        rooms = state.get("rooms", [])
        known = {r["name"].lower(): r for r in rooms}
        low = room.lower().strip()
        match = known.get(low)
        if match is None:
            prefix = [r for k, r in known.items() if k.startswith(low)]
            if len(prefix) == 1:
                match = prefix[0]
            elif len(prefix) > 1:
                return {"error": f"ambiguous room '{room}': "
                                 f"{[r['name'] for r in prefix]}"}
            else:
                return {"error": f"unknown room '{room}'",
                        "available": [r["name"] for r in rooms]}
        poly = match.get("polygon") or []
        if len(poly) < 3:
            return {"error": f"room '{match['name']}' has no usable polygon"}
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        cx = (min(xs) + max(xs)) / 2.0
        cy = (min(ys) + max(ys)) / 2.0
        label = match["name"]
    else:
        if x is None or y is None:
            return {"error": "give either room=, or both x= and y="}
        cx, cy = float(x), float(y)

    w, h = state.get("width", 0), state.get("height", 0)
    if not (0 <= cx <= w and 0 <= cy <= h):
        return {"error": f"focus ({cx:g},{cy:g}) outside map bounds ({w}×{h})"}

    state["focus"] = {"x": round(cx, 2), "y": round(cy, 2), "label": label}
    save_state(cfg, state)
    return {"focus": state["focus"]}


def _do_unload() -> dict:
    cfg = _c.load_campaign()
    state = load_state(cfg)
    if not state.get("active"):
        return {"already_inactive": True}
    clear_state(cfg)
    return {"unloaded": state.get("slug"), "name": state.get("name")}


def _do_current() -> dict:
    cfg = _c.load_campaign()
    state = load_state(cfg)
    if not state.get("active"):
        return {"active": False}
    return {
        "active":         True,
        "slug":           state.get("slug"),
        "name":           state.get("name"),
        "size":           f"{state.get('width')}×{state.get('height')}",
        "scale":          state.get("scale"),
        "rooms":          [r["name"] for r in state.get("rooms", [])],
        "revealed_rooms": list(state.get("revealed_rooms", [])),
        "revealed_traps": list(state.get("revealed_traps", [])),
        "focus":          state.get("focus"),
        "party":          state.get("party"),
    }


def _do_mark_explored(
    names: list[str], party_at: str | None = None,
) -> dict:
    """Reveal rooms/corridors (and optionally move the party) on the embedded
    DungML play session bound to the current campaign + map."""
    cfg = _c.load_campaign()
    state = load_state(cfg)
    if not state.get("active"):
        return {"error": "no map loaded — call load_map first"}
    map_id = state.get("dungml_id")
    if not map_id:
        return {"error": "loaded map has no dungml id"}
    source = state.get("source", "")

    try:
        sid = _dh.ensure_play_session(
            cfg["_dir"], map_id, source,
            seed_names=state.get("revealed_rooms", []),
            session_name=f"{cfg.get('name', 'Campaign')} — party",
        )
        name_to_id = _dh.resolve_node_ids(source)
    except (ConnectionError, PermissionError) as e:
        return {"error": f"DungML server: {e}"}
    except Exception as e:
        return {"error": f"DungML error: {e}"}

    revealed: list[str] = []
    unknown: list[str] = []
    for nm in names:
        nid = name_to_id.get(nm)
        if not nid:
            unknown.append(nm)
            continue
        try:
            _dh.reveal_node(sid, nid)
            revealed.append(nm)
        except Exception:
            unknown.append(nm)

    party = None
    if party_at:
        nid = name_to_id.get(party_at)
        if not nid:
            unknown.append(party_at)
        else:
            try:
                _dh.move_party(sid, nid)
                party = party_at
            except Exception:
                pass

    return {"revealed": revealed, "unknown": unknown,
            "party_at": party, "session": sid}


def register(mcp):

    @mcp.tool()
    def load_map(slug: str, project: str | None = None) -> dict:
        """Load a DungML map into the dashboard's /map view with fog-of-war.

        Fetches the named map from the DungML server (default project =
        active campaign name; override with `project`), renders it
        server-side (so `!include` files like `core.dmap` resolve), and
        writes `map_state.json` for the dashboard.

        The map starts fully unrevealed — call `reveal_room(name)` as
        the party enters each space. Reloading the same slug preserves
        the existing revealed set and party position so loading is safe
        to repeat after edits in the DungML editor.

        Args:
          slug:    map name or UUID. Loose match: "gatehouse" finds
                   "Surface Level Gatehouse". Case-insensitive.
          project: optional project name/UUID override. Defaults to the
                   active campaign name (or DUNGML_PROJECT env var).

        Returns:  {slug, name, size, scale, rooms, revealed, room_names}
                  on success, or {error} on failure (server unreachable,
                  ambiguous match, bad credentials). Surface errors to
                  the player verbatim — don't invent a fallback map.
        """
        return _do_load_map(slug, project=project)

    @mcp.tool()
    def reveal_room(name: str, set_party: bool = False) -> dict:
        """Reveal a room on the loaded map's fog-of-war.

        Call when the party enters or directly observes a room. The
        room's polygon (in cell coords) is lifted from the loaded
        DungML source — supply the room's name (the `room "..."` label
        in the DSL). Prefix matches resolve when unambiguous.

        Idempotent: revealing an already-revealed room is a no-op.

        Args:
          set_party: when True, also move the party marker into this room on
                     the /area play widget (the embedded dungml session),
                     saving a separate `mark_explored(party_at=…)` call. Use
                     it when the party has just *moved into* the room (vs only
                     glimpsing it). Best-effort: the map_state reveal always
                     applies; if the dungml session is unreachable the result
                     carries a `session_sync` note. (For corridors, or to
                     reveal several spaces at once, use `mark_explored`.)
        """
        return _do_reveal(name, set_party=set_party)

    @mcp.tool()
    def hide_room(name: str) -> dict:
        """Remove a room from the revealed set (rare — used for
        corrections, illusions, or DM resets). The polygon goes back
        to fog. No-op if the room wasn't revealed."""
        return _do_hide(name)

    @mcp.tool()
    def spring_trap(x: int, y: int) -> dict:
        """Reveal a hidden hazard on the player's tactical map.

        Floor traps (and any other DM-only map feature) are authored in
        the DungML map normally, but stay invisible in the player view
        (the /area page opened with ?fog=1) until the party triggers or
        discovers them. Call this with the trap's cell coordinates — the
        `at X,Y` from the `feature trap at X,Y` line in the DSL — at the
        moment a PC springs it, spots it, or disarms it. From then on the
        trap renders for the players too.

        DM-facing views (plain /area, the DungML editor) always show the
        trap; this only governs the fogged player view. Idempotent.
        """
        return _do_spring_trap(x, y)

    @mcp.tool()
    def reset_traps() -> dict:
        """Re-hide every trap on the current map in the player view
        (clears the revealed-traps set). Use after a TPK retcon, an
        illusion, or to reset a re-used map. No-op if no map is loaded."""
        return _do_reset_traps()

    @mcp.tool()
    def set_map_focus(
        room: str | None = None,
        x: int | None = None,
        y: int | None = None,
    ) -> dict:
        """Center the /area tactical map on a location.

        Pass `room` (a `room "..."` name from the DSL — the view centers
        on that room's middle) OR explicit `x`/`y` cell coordinates. The
        /area page recenters its viewport on the focus point the next
        time it polls (it does not fight manual panning afterwards —
        recentering only fires when the focus actually changes).

        Call with no arguments to clear the focus and revert to the
        default whole-map view. Returns the stored focus, or {error} if
        no map is loaded or the room/coords are unknown / out of bounds.
        """
        return _do_set_focus(room=room, x=x, y=y)

    @mcp.tool()
    def unload_map() -> dict:
        """Clear the loaded tactical/area map. Use when the party
        leaves the keyed area and the /map view should go blank until
        the next `load_map`."""
        return _do_unload()

    @mcp.tool()
    def current_map() -> dict:
        """Return a summary of the currently loaded map: slug, size,
        scale, all room names, the revealed subset, and the party
        marker position if one has been set."""
        return _do_current()

    @mcp.tool()
    def mark_explored(
        names: list[str], party_at: str | None = None,
    ) -> dict:
        """Mark rooms/corridors as explored on the /area play view (the
        embedded DungML widget) and optionally move the party marker.

        The /area widget reads a DungML **session**, not map_state.json, so
        `reveal_room` no longer drives it. Call this whenever the party
        **enters or passes through** any space — and crucially **corridors as
        well as rooms** (corridors are the connective tissue the widget needs
        to draw a continuous explored map). Names are the bare DSL ids, e.g.
        `room_8`, `cave_3`, `corridor_15`.

        Args:
          names:    room/corridor ids the party has now seen. Idempotent —
                    re-marking is harmless.
          party_at: optional id to move the party marker to (also reveals it).
                    Pass the party's current location after a move.

        Returns: {revealed, unknown, party_at, session}. `unknown` lists names
        not found in the map graph (check spelling against the DSL). Binds /
        creates the campaign session automatically if needed. Surface errors
        to the DM verbatim.
        """
        return _do_mark_explored(names, party_at=party_at)
