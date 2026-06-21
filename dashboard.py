#!/usr/bin/env python3
"""Campaign dashboard web app. Run: python3 dashboard.py [--campaign NAME] [--port 5000]"""

import argparse
import base64
import html
import json
import re
import sqlite3
import sys
from pathlib import Path

from datetime import datetime, timezone

from flask import Flask, Response, abort, jsonify, redirect, render_template_string, request, send_file, send_from_directory

import _campaign as _c
from tools import campaign_archive as _arch
from tools import export_session as _export
from tools import item_weights as _iw
from tools import homebrew_classes as _hb
from tools import dm_session as _dm
from tools import images as _img
from tools import tts as _tts
from tools import lore_facts as _facts
from tools import _dungml_http as _dh

_MONSTERS_DB  = Path(__file__).parent / "global" / "monsters.db"
_MONSTERS_DIR = Path(__file__).parent / "global" / "monsters"
_2E_DB        = Path(__file__).parent / "global" / "2e.db"
_GREYHAWK_DB  = Path(__file__).parent / "settings" / "greyhawk" / "greyhawk.db"
_GREYHAWK_IMG = Path(__file__).parent / "settings" / "greyhawk" / "images"

# Cached read-only connections. The reference DBs (monsters/2e/rules) are
# read-only at runtime, so a single connection per DB shared across Flask
# threads is safe with check_same_thread=False. Avoids the per-request
# connect cost on hot endpoints like /area-state.
_db_connections: dict[str, sqlite3.Connection] = {}


def _db(path: Path) -> sqlite3.Connection:
    """Get a cached read-only connection to a reference SQLite DB."""
    key = str(path)
    conn = _db_connections.get(key)
    if conn is None:
        conn = sqlite3.connect(key, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _db_connections[key] = conn
    return conn

# ---------------------------------------------------------------------------
# Global state — set in main()
# ---------------------------------------------------------------------------
cfg: dict = {}
app = Flask(__name__)


_cfg_mtime: float = 0.0


def _reload_cfg() -> None:
    """Re-read campaign.json so quests/characters/factions added mid-session
    via MCP tools show up without restarting the dashboard. mtime-gated:
    only re-reads when campaign.json has changed since the last refresh."""
    global _cfg_mtime
    if not cfg:
        return
    p = cfg["_dir"] / "campaign.json"
    try:
        m = p.stat().st_mtime
    except OSError:
        return
    if m == _cfg_mtime:
        return
    # Pass the name to bypass load_campaign's module cache and return a fresh
    # dict — the cached dict is shared with the MCP tools and clearing it
    # there would empty `cfg` too.
    fresh = _c.load_campaign(cfg["_name"])
    cfg.clear()
    cfg.update(fresh)
    _cfg_mtime = m


def _combat_hp_overrides() -> dict:
    """If a combat session is active, return a dict mapping party-character
    keys to live HP from combat_state.json so /party (and the status line)
    show current HP during combat instead of stale state.json values.

    Guard against stale combat_state.json: if state.json has been written
    more recently than combat_state.json, an out-of-combat tool (apply_damage,
    apply_heal, etc.) has updated HP since the last combat write. Trust the
    fresher state.json and skip the overrides — otherwise the sidebar would
    keep showing pre-fight HP after combat ended without end_combat clearing
    the active flag.
    """
    if not cfg:
        return {}
    cs_path = cfg["_dir"] / "combat_state.json"
    if not cs_path.exists():
        return {}
    try:
        cs = json.loads(cs_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not cs.get("active"):
        return {}
    st_path = cfg["_dir"] / "state.json"
    if st_path.exists() and st_path.stat().st_mtime > cs_path.stat().st_mtime:
        return {}
    out: dict = {}
    for c in cs.get("combatants", []):
        if c.get("side") != "party":
            continue
        key = c.get("key")
        if key:
            out[key] = c.get("hp")
    return out


def _combat_effects_overrides() -> dict:
    """Return {character_key: [effect_dict, ...]} for party members in an
    active combat session. Empty dict if no combat is active."""
    if not cfg:
        return {}
    cs_path = cfg["_dir"] / "combat_state.json"
    if not cs_path.exists():
        return {}
    try:
        cs = json.loads(cs_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not cs.get("active"):
        return {}
    out: dict = {}
    for c in cs.get("combatants", []):
        if c.get("side") != "party":
            continue
        key = c.get("key")
        effects = c.get("effects") or []
        if key and effects:
            out[key] = effects
    return out


_class_xp_cache: dict[str, list[int]] = {}

# 2e kits and common synonyms map onto their PHB base class for XP-table
# lookup. The kits use the base class's progression in the published rules.
_CLASS_ALIASES: dict[str, str] = {
    "barbarian":   "Fighter",   # Complete Barbarian's Handbook (Fighter kit)
    "berserker":   "Fighter",
    "cavalier":    "Fighter",
    "gladiator":   "Fighter",
    "samurai":     "Fighter",
    "wizard":      "Mage",
    "necromancer": "Specialist Mage",
    "abjurer":     "Specialist Mage",
    "conjurer":    "Specialist Mage",
    "diviner":     "Specialist Mage",
    "enchanter":   "Specialist Mage",
    "illusionist": "Specialist Mage",
    "invoker":     "Specialist Mage",
    "transmuter":  "Specialist Mage",
    "monk":        "Cleric",    # Scarlet Brotherhood / OA-style monk uses Priest table
    "assassin":    "Thief",     # 2e: Assassin kit of Thief
    "swashbuckler":"Thief",
}


def _class_xp_table(cls: str) -> list[int]:
    """Return [xp_required at level 1, level 2, ...] for a class.

    Resolution order:
      1. Homebrew classes (global/homebrew/classes/*.json) — preferred so
         home-brewed entries win over aliases.
      2. PHB classes (global/2e.db).
      3. Kit→base alias fallback (legacy; will be empty for classes whose
         homebrew JSON has been authored).
    Empty list if nothing matches. Process-cached."""
    key = (cls or "").strip().lower()
    if not key:
        return []
    if key in _class_xp_cache:
        return _class_xp_cache[key]

    table: list[int] = _hb.homebrew_xp_table(key)
    if not table:
        candidates = [key]
        if key in _CLASS_ALIASES:
            candidates.append(_CLASS_ALIASES[key].lower())
        try:
            conn = _db(_2E_DB)
            for candidate in candidates:
                row = conn.execute(
                    "SELECT id FROM classes WHERE lower(name) = ? LIMIT 1", (candidate,),
                ).fetchone()
                if row is None:
                    row = conn.execute(
                        "SELECT id FROM classes WHERE lower(name) LIKE ? LIMIT 1",
                        (candidate + "%",),
                    ).fetchone()
                if row is not None:
                    for r in conn.execute(
                        "SELECT xp_required FROM class_levels WHERE class_id=? ORDER BY level",
                        (row["id"],),
                    ).fetchall():
                        table.append(int(r["xp_required"]))
                    break
        except sqlite3.Error:
            table = []

    _class_xp_cache[key] = table
    return table


# Encumbrance bands (PHB Table 47) and computation live in tools/item_weights.py.
# These thin wrappers inject the dashboard's cached sqlite connection.
def _item_weight_resolve(name: str) -> dict:
    return _iw.resolve_weight(name, conn=_db(_2E_DB))


def _item_weight_lookup(name: str) -> float:
    return _item_weight_resolve(name)["lb"]


def _encumbrance_band(char: dict, state: dict, key: str) -> dict:
    """Compute encumbrance for a character. See item_weights.compute_encumbrance."""
    return _iw.compute_encumbrance(char, state, key, conn=_db(_2E_DB))


@app.before_request
def _refresh_before_request() -> None:
    _reload_cfg()

# ---------------------------------------------------------------------------
# Shared HTML base template
# ---------------------------------------------------------------------------
BASE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400&family=Cinzel:wght@400;500;600&family=EB+Garamond:ital,wght@0,400;0,500;0,600;1,400;1,500&family=JetBrains+Mono:wght@400;500&family=Lora:ital,wght@0,400;0,500;0,600;1,400;1,500&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg-base:        #14100b;
    --bg-deep:        #0a0805;
    --bg-card:        #221a12;
    --bg-card-hi:     #2a2018;
    --bg-rec:         #0f0b07;
    --bg-nav:         rgba(13, 10, 7, 0.92);
    --ink-body:       #e8d8b8;
    --ink-display:    #f0d9a4;
    --ink-muted:      #8e7e5e;
    --accent-gold:    #c8a96e;
    --accent-gold-hi: #f0d196;
    --accent-rust:    #b56a3a;
    --accent-blood:   #c83a2a;
    --accent-cool:    #6a9ec0;
    --rule:           #3d2c1e;
    --rule-hi:        #5a4030;
    --hp-good:        #7ab46e;
    --hp-warn:        #d4a14a;
    --hp-bad:         #c8503a;
    --shadow-deep:    0 10px 30px rgba(0,0,0,.55);
    --shadow-card:    0 2px 10px rgba(0,0,0,.30);
    --inset-warm:     inset 0 1px 0 rgba(220,180,120,0.05);
    --font-display:   'Cinzel', 'Trajan Pro', Georgia, serif;
    --font-body:      'EB Garamond', Georgia, 'Times New Roman', serif;
    --font-mono:      'JetBrains Mono', Menlo, Consolas, monospace;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  html {{ scrollbar-color: var(--rule-hi) var(--bg-deep); background: var(--bg-base); }}
  ::selection {{ background: rgba(200,169,110,0.35); color: var(--ink-display); }}

  body {{
    background: var(--bg-base);
    background-image:
      radial-gradient(ellipse at 18% -10%, rgba(120,80,40,0.16), transparent 55%),
      radial-gradient(ellipse at 85% 105%, rgba(80,50,25,0.14), transparent 50%),
      linear-gradient(180deg, #15110c 0%, #100c08 100%);
    background-attachment: fixed;
    color: var(--ink-body);
    font-family: var(--font-body);
    font-size: 18px;
    line-height: 1.62;
    font-feature-settings: "liga", "kern", "onum";
    text-rendering: optimizeLegibility;
    -webkit-font-smoothing: antialiased;
    min-height: 100vh;
  }}

  /* Subtle parchment grain over the entire viewport. */
  body::before {{
    content: '';
    position: fixed; inset: 0;
    pointer-events: none;
    z-index: 1;
    opacity: 0.045;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='240' height='240'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' seed='4'/><feColorMatrix values='0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0.6 0.6 0.6 0 0'/></filter><rect width='100%25' height='100%25' filter='url(%23n)'/></svg>");
  }}

  .container {{
    max-width: 980px;
    margin: 0 auto;
    padding: 28px 24px 60px;
    position: relative;
    z-index: 2;
  }}

  /* ---- Navigation ---- */
  nav {{
    background: var(--bg-nav);
    border-bottom: 1px solid var(--rule);
    box-shadow: 0 1px 0 rgba(200,169,110,0.04), 0 4px 16px rgba(0,0,0,0.4);
    padding: 14px 28px;
    position: sticky; top: 0;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    z-index: 50;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
  }}
  nav a {{
    color: var(--accent-gold);
    text-decoration: none;
    font-family: var(--font-display);
    font-size: 0.72em;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.22em;
    padding: 4px 16px;
    position: relative;
    transition: color 180ms ease;
  }}
  nav a:not(:last-of-type)::after {{
    content: '';
    position: absolute;
    right: 0; top: 28%; bottom: 28%;
    width: 1px;
    background: linear-gradient(to bottom, transparent, rgba(200,169,110,0.24), transparent);
  }}
  nav a:hover {{ color: var(--accent-gold-hi); }}
  /* /play-only toggles for the toolbar (subheader) and party panel.
     Hidden everywhere else via the :has() scope. */
  .nav-play-toggles {{ display: none; margin-left: auto; gap: 6px; align-items: center; }}
  body:has(.play-shell) .nav-play-toggles {{ display: inline-flex; }}
  .nav-icon-btn {{
    background: transparent;
    border: 1px solid var(--rule);
    color: var(--accent-gold);
    border-radius: 6px;
    padding: 3px 9px;
    font-size: 0.95em;
    line-height: 1;
    cursor: pointer;
    transition: color 180ms ease, border-color 180ms ease, opacity 180ms ease;
  }}
  .nav-icon-btn:hover {{ color: var(--accent-gold-hi); border-color: var(--accent-gold-hi); }}
  .nav-icon-btn.off {{ opacity: 0.4; }}
  nav a.is-active {{ color: var(--ink-display); }}
  nav a.is-active::before {{
    content: '';
    position: absolute;
    left: 14px; right: 14px; bottom: -16px;
    height: 2px;
    background: var(--accent-gold);
  }}

  /* ---- Phone: collapse the nav into one swipeable row ----
     Eleven uppercase links wrap into a tall block on a narrow screen and eat
     the viewport. Below 640px we keep them on a single line and let the bar
     scroll horizontally (momentum scroll on iOS), so the chrome stays one
     row tall and every destination is a thumb-swipe away. */
  @media (max-width: 640px) {{
    /* Mobile browsers mis-paint background-attachment:fixed on a scrollable
       page — the gradient covers only the first viewport and the rest goes
       white. Anchor the background to the (tall) body instead. */
    body {{ background-attachment: scroll; }}
    nav {{
      flex-wrap: nowrap;
      overflow-x: auto;
      overflow-y: hidden;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: none;
      padding: 10px 12px;
    }}
    nav::-webkit-scrollbar {{ display: none; }}
    nav a {{
      flex: 0 0 auto;
      white-space: nowrap;
      padding: 4px 12px;
      letter-spacing: 0.16em;
    }}
    /* Keep the active-link underline inside the shorter, clipped bar. */
    nav a.is-active::before {{ bottom: -6px; }}
    .nav-play-toggles {{ margin-left: 6px; flex: 0 0 auto; }}
    .container {{ padding: 20px 16px 48px; }}
  }}

  /* ---- Typography ---- */
  h1, h2, h3, h4 {{
    color: var(--accent-gold);
    font-family: var(--font-display);
    font-weight: 500;
    letter-spacing: 0.04em;
    margin: 22px 0 10px;
  }}
  h1 {{
    font-size: 2.05em;
    border-bottom: 1px solid var(--rule);
    padding-bottom: 12px;
    margin-bottom: 18px;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--ink-display);
  }}
  h2 {{ font-size: 1.32em; letter-spacing: 0.06em; }}
  h3 {{ font-size: 1.10em; letter-spacing: 0.05em; color: var(--accent-gold); }}
  h4 {{ font-size: 0.98em; letter-spacing: 0.08em; text-transform: uppercase; color: var(--accent-gold); }}
  p {{ margin: 8px 0; }}
  ul, ol {{ margin: 8px 0 8px 24px; }}
  a {{ color: var(--accent-gold); transition: color 160ms ease; }}
  a:hover {{ color: var(--accent-gold-hi); }}

  /* ---- Cards & layout ---- */
  .card {{
    background:
      linear-gradient(135deg, rgba(255,220,180,0.020), transparent 45%),
      linear-gradient(to bottom, var(--bg-card-hi), var(--bg-card));
    border: 1px solid var(--rule);
    border-radius: 4px;
    padding: 18px 22px;
    margin: 12px 0;
    box-shadow: var(--inset-warm), var(--shadow-card);
    position: relative;
  }}
  .card h2 {{ margin-top: 0; }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 18px;
    margin-top: 16px;
  }}
  .grid-item {{
    background: var(--bg-card);
    border: 1px solid var(--rule);
    border-radius: 4px;
    overflow: hidden;
    transition: border-color 200ms ease, transform 200ms ease;
  }}
  .grid-item:hover {{ border-color: var(--rule-hi); transform: translateY(-2px); }}
  .grid-item img {{ width: 100%; display: block; border-radius: 3px 3px 0 0; }}
  .grid-item .caption {{ padding: 12px 14px; font-size: 0.92em; color: var(--ink-muted); }}
  .grid-item .caption strong {{ display: block; color: var(--ink-body); margin-bottom: 4px; font-family: var(--font-display); font-size: 0.95em; letter-spacing: 0.04em; }}

  /* ---- HP bar (legacy text-block form, kept for back-compat) ---- */
  .hp-bar {{
    display: inline-block;
    font-family: var(--font-mono);
    letter-spacing: 1px;
    font-size: 0.88em;
  }}
  .hp-bar .filled {{ color: var(--hp-good); }}
  .hp-bar .empty  {{ color: #4a2a2a; }}

  /* ---- HP meter (graphical, used by /play sidebar and elsewhere) ---- */
  .hp-meter {{
    display: block;
    height: 8px;
    background: var(--bg-rec);
    border: 1px solid var(--rule);
    border-radius: 1px;
    overflow: hidden;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.5);
    position: relative;
  }}
  .hp-meter > i {{
    display: block;
    height: 100%;
    background: linear-gradient(to right, var(--hp-bad) 0%, var(--hp-warn) 45%, var(--hp-good) 75%);
    transition: width 380ms ease;
  }}

  /* ---- Info bar (e.g. /party header strip) ---- */
  .info-bar {{
    background: linear-gradient(to bottom, #1d160f, #16110b);
    border: 1px solid var(--rule);
    border-radius: 4px;
    padding: 12px 16px;
    margin: 0 0 18px 0;
    display: flex;
    flex-wrap: wrap;
    gap: 14px 26px;
    font-size: 0.96em;
    box-shadow: var(--inset-warm);
  }}
  .info-bar .item strong {{ color: var(--accent-gold); margin-right: 6px; font-family: var(--font-display); font-size: 0.85em; text-transform: uppercase; letter-spacing: 0.1em; }}

  /* ---- Clocks (faction timers) ---- */
  .clock {{
    display: inline-block;
    padding: 4px 11px;
    border-radius: 2px;
    border: 1px solid var(--rule);
    background: var(--bg-card);
    font-size: 0.9em;
    margin: 2px 4px 2px 0;
    font-family: var(--font-display);
    letter-spacing: 0.05em;
  }}
  .clock.urgent  {{ border-color: var(--accent-rust); color: #e8b487; background: #2e1d12; }}
  .clock.expired {{ border-color: #6a2818; color: #ff7a5a; background: #2e1410; }}

  /* ---- Truth/secret indicators (DM views) ---- */
  .truth-true        {{ color: var(--hp-good); font-size: 0.8em; }}
  .truth-partly_true {{ color: var(--accent-gold); font-size: 0.8em; }}
  .truth-false       {{ color: #c87a7a; font-size: 0.8em; }}
  .truth-fabrication {{ color: #ff5a4a; font-size: 0.8em; font-weight: 600; }}
  .secret-card {{
    background: linear-gradient(to bottom, #2a1818, #221212);
    border: 1px solid #6a3828;
    border-left: 4px solid var(--accent-blood);
    padding: 14px 18px;
    margin: 10px 0;
    border-radius: 3px;
    box-shadow: var(--shadow-card);
  }}
  .scope-warning {{ color: #e8b487; font-size: 0.85em; }}

  /* ---- Tags / chips ---- */
  .tag {{
    display: inline-block;
    background: #322012;
    border: 1px solid var(--rule-hi);
    border-radius: 2px;
    padding: 1px 8px;
    font-size: 0.78em;
    margin: 2px;
    color: var(--accent-gold);
    font-family: var(--font-display);
    letter-spacing: 0.06em;
  }}

  /* ---- Layout helpers ---- */
  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
  @media (max-width: 600px) {{ .two-col {{ grid-template-columns: 1fr; }} }}

  /* ---- Portrait (inline within prose) ---- */
  .portrait {{
    float: right;
    margin: 0 0 14px 18px;
    max-width: 180px;
    border-radius: 3px;
    border: 1px solid var(--rule-hi);
    box-shadow: var(--shadow-card);
  }}

  /* ---- Party card (used on /party page) ---- */
  .party-card {{ display: flex; gap: 18px; align-items: flex-start; }}
  .party-portrait {{
    width: 116px; height: 116px;
    object-fit: cover;
    border-radius: 3px;
    border: 1px solid var(--rule-hi);
    flex-shrink: 0;
    box-shadow: var(--shadow-card), inset 0 0 0 1px rgba(0,0,0,0.4);
  }}
  .party-body {{ flex: 1; min-width: 0; }}
  .party-body h2 {{ margin-top: 0; }}

  /* ---- Holdings panel (per-PC houses + mounts on /party) ---- */
  .holdings {{ margin-top: 14px; }}
  .holdings-label {{
    margin: 0 0 6px; font-size: 0.85em;
    color: var(--accent-gold);
    font-family: var(--font-display);
    text-transform: uppercase; letter-spacing: 0.16em;
    border-bottom: 1px dotted var(--rule); padding-bottom: 4px;
  }}
  .holdings-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 8px;
  }}
  .holding-card {{
    display: flex; gap: 10px; align-items: center;
    padding: 6px 8px;
    border: 1px solid var(--rule);
    border-radius: 3px;
    background: rgba(0,0,0,0.18);
    color: var(--ink-body);
    text-decoration: none;
    transition: background 0.15s ease, border-color 0.15s ease;
  }}
  a.holding-card:hover {{
    background: rgba(200,169,110,0.10);
    border-color: var(--accent-gold);
  }}
  a.holding-card:hover strong {{ color: var(--accent-gold-hi); }}
  .holding-card.holding-house {{ border-left: 3px solid var(--accent-gold); }}
  .holding-card.holding-mount {{ border-left: 3px solid #8aa66a; }}
  .holding-thumb {{
    width: 40px; height: 40px;
    flex-shrink: 0;
    border-radius: 3px;
    overflow: hidden;
    border: 1px solid var(--rule);
    background: var(--bg-rec);
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 1.4em;
    color: var(--ink-muted);
  }}
  .holding-thumb img {{
    width: 100%; height: 100%; object-fit: cover;
  }}
  .holding-thumb.empty {{ filter: grayscale(0.5); opacity: 0.85; }}
  .holding-body {{
    display: flex; flex-direction: column; min-width: 0; gap: 1px;
    font-size: 0.88em; line-height: 1.3;
  }}
  .holding-sub {{ font-size: 0.85em; }}
  .holding-stowed {{
    margin-top: 3px;
    font-size: 0.78em;
    color: var(--ink-muted);
    font-style: italic;
  }}
  .stowed-chip {{
    display: inline-block;
    padding: 0 5px;
    margin-right: 3px;
    border: 1px solid var(--rule);
    border-radius: 2px;
    background: rgba(0,0,0,0.18);
    color: var(--ink-body);
    font-style: normal;
    font-size: 0.92em;
  }}

  /* ---- Spells ---- */
  .spell-ready {{ color: var(--ink-body); }}
  .spell-cast  {{ color: #6a5a48; text-decoration: line-through; text-decoration-color: rgba(106,90,72,0.5); }}
  .memorized {{ margin: 8px 0; }}
  .spell-level {{ margin: 6px 0; }}
  .spell-level-head {{
    font-size: 0.82em;
    color: var(--accent-gold);
    margin: 4px 0 4px;
    border-bottom: 1px dotted var(--rule);
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.12em;
    padding-bottom: 3px;
  }}
  .spell-list {{ list-style: disc; margin: 2px 0 4px 22px; padding: 0; }}
  .spell-list li {{ margin: 1px 0; line-height: 1.42; }}

  /* ---- Effect / light chips ---- */
  .effect-chip {{
    display: inline-block;
    background: #182734;
    border: 1px solid #3a5a7a;
    border-radius: 2px;
    padding: 2px 8px;
    margin: 2px 2px 2px 0;
    font-size: 0.85em;
    color: #a0c0e0;
    font-family: var(--font-display);
    letter-spacing: 0.04em;
  }}
  .light-chip {{
    display: inline-block;
    background: #322014;
    border: 1px solid #6a4828;
    border-radius: 2px;
    padding: 2px 8px;
    margin: 2px 2px 2px 0;
    font-size: 0.85em;
    color: #e8c080;
    font-family: var(--font-display);
    letter-spacing: 0.04em;
  }}
  .light-chip.guttering {{
    background: #321810;
    border-color: var(--accent-rust);
    color: #ff8a6a;
    animation: flicker 1.4s ease-in-out infinite;
  }}
  @keyframes flicker {{
    0%, 100% {{ opacity: 1; }}
    47% {{ opacity: 0.78; }}
    52% {{ opacity: 1; }}
    78% {{ opacity: 0.86; }}
  }}

  /* ---- Encumbrance widgets ---- */
  .enc-card {{ margin-top: 16px; }}
  .enc-bar {{
    height: 10px;
    background: var(--bg-rec);
    border: 1px solid var(--rule);
    border-radius: 1px;
    overflow: hidden;
    margin: 6px 0 10px;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.5);
  }}
  .enc-fill {{ height: 100%; transition: width .35s ease; }}
  .enc-bands {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }}
  .enc-band {{
    background: var(--bg-rec);
    border: 1px solid var(--rule);
    border-radius: 2px;
    padding: 7px 8px;
    font-size: 0.85em;
    text-align: center;
  }}
  .enc-band-name {{
    display: block;
    color: var(--ink-muted);
    text-transform: uppercase;
    font-size: 0.78em;
    font-family: var(--font-display);
    letter-spacing: 0.1em;
  }}
  .enc-band-cap {{ display: block; color: var(--ink-body); font-family: var(--font-mono); font-size: 0.92em; }}
  .enc-band-active {{ border-color: var(--accent-gold); background: var(--bg-card); }}
  .enc-band-active .enc-band-name {{ color: var(--accent-gold); }}
  .enc-items {{ margin: 12px 0 4px; font-size: 0.92em; }}
  .enc-items th {{ font-weight: 500; color: var(--accent-gold); font-family: var(--font-display); text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.82em; }}
  .enc-items td, .enc-items th {{ padding: 5px 9px; }}
  .enc-items tfoot td {{ border-top: 1px solid var(--rule); background: var(--bg-rec); }}
  @media (max-width: 480px) {{
    .party-card {{ flex-direction: column; }}
    .party-portrait {{ width: 100%; height: auto; max-width: 220px; }}
  }}

  /* ---- Misc primitives ---- */
  .clearfix::after {{ content: ''; display: table; clear: both; }}
  .muted {{ color: var(--ink-muted); font-size: 0.92em; }}
  pre {{
    background: var(--bg-deep);
    padding: 14px 16px;
    border-radius: 3px;
    border: 1px solid var(--rule);
    overflow-x: auto;
    font-size: 0.86em;
    font-family: var(--font-mono);
  }}
  code {{ font-family: var(--font-mono); font-size: 0.92em; color: var(--accent-gold-hi); }}
  pre code {{ color: var(--ink-body); }}
  blockquote {{
    border-left: 3px solid var(--accent-gold);
    padding: 4px 0 4px 16px;
    color: #b8a982;
    font-style: italic;
    margin: 12px 0;
  }}
  hr {{
    border: none;
    height: 1px;
    background: linear-gradient(to right, transparent, var(--rule-hi), transparent);
    margin: 28px 0;
  }}
  table {{ width: 100%; border-collapse: collapse; margin: 14px 0; }}
  th, td {{ border: 1px solid var(--rule); padding: 7px 11px; text-align: left; }}
  th {{
    background: linear-gradient(to bottom, #2c2218, #221912);
    color: var(--accent-gold);
    font-family: var(--font-display);
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-size: 0.84em;
  }}
  table.gh-wikitable {{ width: auto; min-width: 320px; max-width: 100%; }}
  table.gh-wikitable caption {{
    caption-side: top;
    text-align: left;
    color: var(--accent-gold);
    font-family: var(--font-display);
    font-size: 0.9em;
    letter-spacing: 0.06em;
    padding: 0 0 6px;
  }}
  img:not(#lb-img) {{ cursor: zoom-in; }}

  /* ---- Character sheet (AD&D 2e style) ---- */
  .sheet {{ max-width: 940px; margin: 0 auto; }}
  .sheet-header {{
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 26px;
    padding: 22px 26px;
    background:
      linear-gradient(135deg, rgba(255,220,180,0.030), transparent 55%),
      linear-gradient(to bottom, var(--bg-card-hi), var(--bg-card));
    border: 1px solid var(--rule);
    border-radius: 4px;
    box-shadow: var(--inset-warm), var(--shadow-card);
    margin-bottom: 16px;
    align-items: start;
  }}
  .sheet-header-main {{ min-width: 0; }}
  .sheet-header h1 {{
    border: none;
    padding: 0;
    margin: 0 0 6px;
    font-size: 2.1em;
    letter-spacing: 0.16em;
    line-height: 1.05;
    color: var(--ink-display);
  }}
  .sheet-id {{
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.22em;
    font-size: 0.82em;
    color: var(--accent-gold);
  }}
  .sheet-id .sep {{ color: var(--ink-muted); margin: 0 8px; }}
  .sheet-conditions {{ margin-top: 10px; }}
  .sheet-portrait {{
    width: 170px;
    border-radius: 3px;
    border: 1px solid var(--rule-hi);
    box-shadow: var(--shadow-card);
    display: block;
  }}
  .sheet-portrait-wrap {{ text-align: center; }}
  .sheet-portrait-wrap details.portrait-desc {{ margin-top: 6px; }}
  .holding-portrait-wrap {{ position: relative; }}
  .holding-portrait-wrap.empty {{
    width: 200px; height: 200px;
    display: inline-flex; align-items: center; justify-content: center;
    border: 1px dashed var(--rule-hi); border-radius: 3px;
    background: rgba(0,0,0,0.18);
  }}
  .portrait-generate {{
    background: transparent;
    border: 1px solid var(--accent-gold);
    color: var(--accent-gold);
    padding: 8px 14px;
    border-radius: 3px;
    cursor: pointer;
    font-family: var(--font-display);
    font-size: 0.78em;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    transition: background 0.15s ease, color 0.15s ease;
  }}
  .portrait-generate:hover {{ background: var(--accent-gold); color: var(--bg-deep); }}
  .portrait-generate:disabled {{ opacity: 0.5; cursor: progress; }}
  .portrait-regen {{
    position: absolute; top: 6px; right: 6px;
    width: 28px; height: 28px; padding: 0;
    border: 1px solid var(--rule-hi);
    background: rgba(0,0,0,0.7);
    color: var(--accent-gold);
    border-radius: 50%;
    cursor: pointer;
    font-size: 1em; line-height: 1;
    opacity: 0.7;
    transition: opacity 0.15s ease, transform 0.15s ease;
  }}
  .portrait-regen:hover {{ opacity: 1; transform: rotate(60deg); border-color: var(--accent-gold); }}
  .portrait-regen:disabled {{ opacity: 0.4; cursor: progress; }}
  .holding-stats {{
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 4px 14px;
    font-size: 0.95em;
  }}
  .holding-stats dt {{ color: var(--ink-muted); font-family: var(--font-display); font-size: 0.85em;
                       text-transform: uppercase; letter-spacing: 0.12em; }}
  .holding-stats dd {{ margin: 0; }}
  .stowed-list {{ list-style: none; padding: 0; margin: 0; }}
  .stowed-list li {{ padding: 3px 0; border-bottom: 1px dotted var(--rule); }}
  .stowed-list li:last-child {{ border-bottom: none; }}

  .sheet-panel {{
    background: linear-gradient(to bottom, var(--bg-card-hi), var(--bg-card));
    border: 1px solid var(--rule);
    border-radius: 4px;
    margin-bottom: 14px;
    box-shadow: var(--inset-warm), var(--shadow-card);
    overflow: hidden;
  }}
  .sheet-panel-title {{
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.22em;
    font-size: 0.72em;
    color: var(--accent-gold);
    margin: 0;
    padding: 9px 16px;
    background: rgba(0,0,0,0.20);
    border-bottom: 1px solid var(--rule);
    display: flex;
    align-items: center;
    gap: 10px;
  }}
  .sheet-panel-title::before {{
    content: '\\25C6';   /* ◆ */
    font-size: 0.55em;
    color: var(--accent-gold);
    opacity: 0.85;
  }}
  .sheet-panel-body {{ padding: 16px 18px; }}
  .sheet-panel-body > *:first-child {{ margin-top: 0; }}
  .sheet-panel-body > *:last-child {{ margin-bottom: 0; }}

  /* Ability score grid */
  .ab-grid {{
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 8px;
  }}
  .ab-box {{
    background: var(--bg-deep);
    border: 1px solid var(--rule);
    border-radius: 3px;
    padding: 12px 6px 9px;
    text-align: center;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.4);
  }}
  .ab-box .ab-num {{
    font-family: var(--font-mono);
    font-size: 1.95em;
    color: var(--ink-display);
    line-height: 1.0;
  }}
  .ab-box .ab-label {{
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.18em;
    font-size: 0.66em;
    color: var(--ink-muted);
    margin-top: 6px;
  }}

  /* Vitals row */
  .vitals-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 12px 30px;
    align-items: center;
  }}
  .vital {{ display: flex; align-items: baseline; gap: 8px; }}
  .vital-label {{
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.22em;
    font-size: 0.66em;
    color: var(--ink-muted);
  }}
  .vital-value {{
    font-family: var(--font-mono);
    font-size: 1.16em;
    color: var(--ink-display);
  }}
  .vital-value .xp-next {{ color: var(--ink-muted); font-size: 0.84em; margin-left: 2px; }}
  .vital .hp-meter {{ width: 110px; height: 10px; }}

  /* Saves grid */
  .saves-grid {{
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 8px;
  }}
  .save-box {{
    background: var(--bg-deep);
    border: 1px solid var(--rule);
    border-radius: 3px;
    padding: 9px 4px 7px;
    text-align: center;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.4);
  }}
  .save-box .save-num {{
    font-family: var(--font-mono);
    font-size: 1.45em;
    color: var(--ink-display);
    line-height: 1.0;
  }}
  .save-box .save-label {{
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.14em;
    font-size: 0.6em;
    color: var(--ink-muted);
    margin-top: 5px;
  }}

  /* Attacks table */
  table.attacks-table {{ width: 100%; margin: 0; font-size: 0.94em; }}
  table.attacks-table th {{
    background: linear-gradient(to bottom, #2c2218, #221912);
    color: var(--accent-gold);
    font-family: var(--font-display);
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-size: 0.74em;
    padding: 6px 9px;
  }}
  table.attacks-table td {{
    font-family: var(--font-mono);
    padding: 7px 9px;
    border: 1px solid var(--rule);
  }}
  table.attacks-table td:first-child {{
    font-family: var(--font-body);
    color: var(--ink-body);
  }}

  /* Two-column inside a panel (e.g. profs / nwps) */
  .sheet-two-col {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 18px 26px;
  }}
  .sheet-two-col h4 {{
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.16em;
    font-size: 0.72em;
    color: var(--accent-gold);
    margin: 0 0 6px;
  }}

  /* Lists with diamond bullets */
  ul.sheet-list {{ list-style: none; padding: 0; margin: 0; }}
  ul.sheet-list li {{
    padding: 4px 0 4px 18px;
    position: relative;
    border-bottom: 1px dotted rgba(140,110,70,0.18);
    line-height: 1.45;
  }}
  ul.sheet-list li:last-child {{ border-bottom: none; }}
  ul.sheet-list li::before {{
    content: '\\25C7';   /* ◇ */
    position: absolute;
    left: 0;
    top: 0.45em;
    color: var(--accent-gold);
    font-size: 0.72em;
    line-height: 1;
  }}

  /* Proficiency chips */
  .prof-chips {{ display: flex; flex-wrap: wrap; gap: 5px; }}
  .prof-chip {{
    background: #322012;
    border: 1px solid var(--rule-hi);
    border-radius: 2px;
    padding: 3px 10px;
    font-family: var(--font-display);
    font-size: 0.72em;
    letter-spacing: 0.06em;
    color: var(--accent-gold);
  }}

  /* Thief / nature skills percentage grid */
  .skills-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
    gap: 4px 18px;
  }}
  .skill-row {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    border-bottom: 1px dotted rgba(140,110,70,0.20);
    padding: 4px 0;
  }}
  .skill-name {{ color: var(--ink-body); font-size: 0.92em; }}
  .skill-val {{
    font-family: var(--font-mono);
    color: var(--accent-gold-hi);
    font-size: 0.96em;
  }}

  @media (max-width: 720px) {{
    .sheet-header {{ grid-template-columns: 1fr; }}
    .sheet-portrait {{ max-width: 220px; }}
    .ab-grid {{ grid-template-columns: repeat(3, 1fr); }}
    .saves-grid {{ grid-template-columns: repeat(3, 1fr); }}
    .sheet-two-col {{ grid-template-columns: 1fr; }}
  }}

  /* ---- Spoiler accordion (rumors, DM notes, sheet descriptions) ---- */
  details.spoiler {{
    border: 1px solid var(--rule);
    border-left: 3px solid var(--accent-rust);
    background: linear-gradient(to bottom, #1d1410, #16100b);
    border-radius: 3px;
    margin: 12px 0;
    box-shadow: var(--inset-warm);
  }}
  details.spoiler[open] {{ border-left-color: var(--accent-gold); }}
  details.spoiler > summary {{
    cursor: pointer;
    padding: 11px 16px;
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.18em;
    font-size: 0.78em;
    color: var(--accent-rust);
    user-select: none;
    list-style: none;
    display: flex;
    align-items: center;
    gap: 12px;
    transition: color 180ms ease;
  }}
  details.spoiler > summary::-webkit-details-marker {{ display: none; }}
  details.spoiler > summary::before {{
    content: '▶';
    font-size: 0.7em;
    transition: transform 200ms ease;
    color: inherit;
  }}
  details.spoiler[open] > summary {{
    color: var(--accent-gold);
    border-bottom: 1px solid var(--rule);
  }}
  details.spoiler[open] > summary::before {{ transform: rotate(90deg); }}
  details.spoiler > summary .spoiler-warn {{
    color: var(--ink-muted);
    font-style: italic;
    font-family: var(--font-body);
    text-transform: none;
    letter-spacing: 0.01em;
    font-size: 0.86em;
    margin-left: auto;
    font-weight: normal;
  }}
  details.spoiler > .spoiler-body {{
    padding: 14px 18px;
  }}
  details.spoiler > .spoiler-body > *:first-child {{ margin-top: 0; }}
  details.spoiler > .spoiler-body > *:last-child {{ margin-bottom: 0; }}

  /* Compact details (e.g. portrait description on character sheets) — quieter than .spoiler. */
  details.portrait-desc {{ margin-top: 8px; text-align: left; }}
  details.portrait-desc > summary {{
    cursor: pointer;
    font-size: 0.78em;
    color: var(--ink-muted);
    font-style: italic;
    list-style: none;
    user-select: none;
    padding: 2px 0;
  }}
  details.portrait-desc > summary::-webkit-details-marker {{ display: none; }}
  details.portrait-desc > summary::before {{
    content: '▸ ';
    color: var(--accent-gold);
    font-style: normal;
  }}
  details.portrait-desc[open] > summary::before {{ content: '▾ '; }}
  details.portrait-desc > p {{
    margin-top: 6px;
    color: var(--ink-muted);
    font-size: 0.84em;
    line-height: 1.5;
    text-align: left;
  }}

  /* ---- Lightbox ---- */
  #lb {{
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,.92); z-index: 9999;
    align-items: center; justify-content: center;
    padding: 20px; cursor: zoom-out;
    backdrop-filter: blur(4px);
  }}
  #lb.open {{ display: flex; }}
  #lb img {{
    max-width: 90vw; max-height: 90vh; object-fit: contain;
    border-radius: 3px; border: 1px solid var(--accent-gold);
    box-shadow: 0 12px 60px rgba(0,0,0,.95); cursor: zoom-out;
  }}
</style>
</head>
<body>
<nav>
  <a href="/play">Play</a>
  <a href="/">Gallery</a>
  <a href="/world">World</a>
  <a href="/quests">Quests</a>
  <a href="/sessions">Sessions</a>
  <a href="/characters">Cast</a>
  <a href="/locations">Locations</a>
  <a href="/reference">Reference</a>
  <a href="/area">Area</a>
  <a href="/atlas">Atlas</a>
  <a href="/campaigns">Campaigns</a>
  <span class="nav-play-toggles">
    <button id="nav-toggle-toolbar" type="button" class="nav-icon-btn" title="Show/hide the play toolbar" aria-label="Toggle play toolbar">⚙</button>
    <button id="nav-toggle-party" type="button" class="nav-icon-btn" title="Show/hide the party panel" aria-label="Toggle party panel">👤</button>
  </span>
</nav>
<div class="container">
{body}
</div>
<div id="lb"><img id="lb-img" src="" alt=""></div>
<script>
(function(){{
  var lb=document.getElementById('lb');
  var li=document.getElementById('lb-img');
  document.addEventListener('click',function(e){{
    var t=e.target;
    if(lb.classList.contains('open')){{lb.classList.remove('open');return;}}
    if(t.tagName==='IMG'&&t.id!=='lb-img'){{li.src=t.src;li.alt=t.alt;lb.classList.add('open');}}
  }});
  document.addEventListener('keydown',function(e){{if(e.key==='Escape')lb.classList.remove('open');}});
  // Highlight the active nav link based on the current path.
  var p = location.pathname;
  document.querySelectorAll('nav a').forEach(function(a){{
    var href = a.getAttribute('href');
    if (href === '/' ? p === '/' : p.indexOf(href) === 0) a.classList.add('is-active');
  }});
}})();
</script>
</body>
</html>"""


def render(title: str, body: str) -> str:
    return BASE_TEMPLATE.format(title=html.escape(title), body=body)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_image_index() -> list:
    idx = cfg["_data_dir"] / "images" / "index.json"
    if not idx.exists():
        return []
    try:
        return json.loads(idx.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _find_portrait(name: str, index: list, slug: str = "") -> dict | None:
    """Return the most recent portrait matching by slug (preferred) or name substring."""
    slug_lower = slug.lower()
    name_lower = name.lower()
    # Iterate reversed so the most recently added entry wins
    for entry in reversed(index):
        if entry.get("type") != "portrait":
            continue
        if slug_lower and entry.get("slug", "").lower() == slug_lower:
            return entry
    for entry in reversed(index):
        if entry.get("type") != "portrait":
            continue
        if name_lower in entry.get("scene", "").lower():
            return entry
    return None


def _inline(s: str) -> str:
    """Render inline Markdown (bold, italic, code, links) in an already-escaped string."""
    s = re.sub(r'\*\*\*(.+?)\*\*\*', r'<strong><em>\1</em></strong>', s)
    s = re.sub(r'\*\*(.+?)\*\*',     r'<strong>\1</strong>', s)
    s = re.sub(r'\*(.+?)\*',          r'<em>\1</em>', s)
    s = re.sub(r'_(.+?)_',            r'<em>\1</em>', s)
    s = re.sub(r'`(.+?)`',            r'<code>\1</code>', s)
    s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', s)
    return s


def _markdown_to_html(md: str) -> str:
    """Minimal Markdown → HTML converter (headings, bold, italic, lists, tables, hr, paragraphs)."""
    lines = md.split("\n")
    out = []
    in_ul = False
    in_ol = False
    in_pre = False
    in_table = False
    table_header_done = False
    buf: list[str] = []

    def flush_para():
        nonlocal buf
        if buf:
            text = " ".join(buf).strip()
            if text:
                out.append(f"<p>{_inline(text)}</p>")
            buf = []

    def _table_cells(line: str, tag: str) -> str:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        return "<tr>" + "".join(f"<{tag}>{_inline(html.escape(c))}</{tag}>" for c in cells) + "</tr>"

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def close_table():
        nonlocal in_table, table_header_done
        if in_table:
            out.append("</tbody></table>")
            in_table = False
            table_header_done = False

    for line in lines:
        # Fenced code blocks
        if line.startswith("```"):
            if in_pre:
                out.append("</code></pre>")
                in_pre = False
            else:
                flush_para()
                close_lists()
                close_table()
                out.append("<pre><code>")
                in_pre = True
            continue
        if in_pre:
            out.append(html.escape(line))
            continue

        # Table rows: lines starting and ending with | (or containing |)
        is_table_row = line.strip().startswith("|") and "|" in line.strip()[1:]
        is_separator = is_table_row and re.match(r'^[\|\s\-:]+$', line)

        if is_table_row:
            flush_para()
            close_lists()
            if is_separator:
                # Separator row — switch from thead to tbody
                if in_table and not table_header_done:
                    out.append("</thead><tbody>")
                    table_header_done = True
            elif not in_table:
                out.append('<table><thead>')
                in_table = True
                table_header_done = False
                out.append(_table_cells(line, "th"))
            elif not table_header_done:
                out.append(_table_cells(line, "th"))
            else:
                out.append(_table_cells(line, "td"))
            continue
        else:
            close_table()

        # Headings
        m = re.match(r'^(#{1,6})\s+(.*)', line)
        if m:
            flush_para()
            close_lists()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(html.escape(m.group(2)))}</h{lvl}>")
            continue

        # HR
        if re.match(r'^(-{3,}|_{3,}|\*{3,})$', line.strip()):
            flush_para()
            close_lists()
            out.append("<hr>")
            continue

        # Blockquote
        if line.startswith("> "):
            flush_para()
            close_lists()
            out.append(f"<blockquote>{_inline(html.escape(line[2:]))}</blockquote>")
            continue

        # Unordered list
        m = re.match(r'^[\*\-\+]\s+(.*)', line)
        if m:
            flush_para()
            if not in_ul:
                if in_ol:
                    out.append("</ol>")
                    in_ol = False
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline(html.escape(m.group(1)))}</li>")
            continue

        # Ordered list
        m = re.match(r'^\d+\.\s+(.*)', line)
        if m:
            flush_para()
            if not in_ol:
                if in_ul:
                    out.append("</ul>")
                    in_ul = False
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{_inline(html.escape(m.group(1)))}</li>")
            continue

        close_lists()

        if not line.strip():
            flush_para()
        else:
            buf.append(html.escape(line))

    flush_para()
    close_lists()
    close_table()
    return "\n".join(out)


def _period_for_hour(h: int) -> str:
    if h < 5:    return "night"
    if h < 8:    return "dawn"
    if h < 12:   return "morning"
    if h < 14:   return "midday"
    if h < 18:   return "afternoon"
    if h < 21:   return "evening"
    return "night"


def _world_info_bar(state: dict) -> str:
    """Render the day/time/weather/season/clocks summary used on multiple pages."""
    day = state.get("current_day", 1)
    session = state.get("current_session", 0)
    h = state.get("current_hour", 6)
    m = state.get("current_minute", 0)
    period = _period_for_hour(h)
    season = state.get("current_season", "spring")
    weather = state.get("current_weather", "—")
    weather_label = weather.split(" (", 1)[0] if weather else "—"

    coin = state.get("coin", {})
    coin_parts = []
    for denom in ("pp", "gp", "ep", "sp", "cp"):
        v = coin.get(denom, 0)
        if v:
            coin_parts.append(f"{v} {denom}")
    coin_str = ", ".join(coin_parts) if coin_parts else "0 gp"

    return (
        '<div class="info-bar">'
        f'<span class="item"><strong>Day:</strong>{day}</span>'
        f'<span class="item"><strong>Session:</strong>{session}</span>'
        f'<span class="item"><strong>Time:</strong>{h:02d}:{m:02d} <em class="muted">({period})</em></span>'
        f'<span class="item"><strong>Season:</strong>{html.escape(season)}</span>'
        f'<span class="item"><strong>Weather:</strong>{html.escape(weather_label)}</span>'
        f'<span class="item"><strong>Coin:</strong>{html.escape(coin_str)}</span>'
        '</div>'
    )


def _hp_bar(current: int, maximum: int, width: int = 10) -> str:
    if maximum <= 0:
        return ""
    filled = round(width * max(current, 0) / maximum)
    empty = width - filled
    bar = f'<span class="hp-bar"><span class="filled">{"█" * filled}</span><span class="empty">{"░" * empty}</span></span>'
    return bar


def _render_holdings_panel(houses: list, mounts: list) -> str:
    """Render a small "Holdings" panel for a party card. Empty list → empty
    string so the card stays compact for PCs with no property."""
    if not houses and not mounts:
        return ""

    def _thumb(filename: str, alt: str, glyph: str) -> str:
        if filename:
            return (
                f'<div class="holding-thumb"><img src="/images/{html.escape(filename)}" '
                f'alt="{html.escape(alt)}"></div>'
            )
        return f'<div class="holding-thumb empty">{glyph}</div>'

    items = []
    for h in houses:
        kind = h.get("kind", "house")
        location = h.get("location", "")
        sub_parts = [html.escape(kind)]
        if location:
            sub_parts.append(html.escape(location))
        sub = " · ".join(sub_parts)
        residents = h.get("residents") or []
        caretaker = h.get("caretaker", "")
        extra = ""
        if caretaker:
            extra = f' <span class="muted">(kept by {html.escape(caretaker)})</span>'
        elif residents:
            extra = f' <span class="muted">({len(residents)} resident{"s" if len(residents)!=1 else ""})</span>'
        items.append(
            f'<a class="holding-card holding-house" href="/houses/{html.escape(h["slug"])}">'
            f'{_thumb(h.get("portrait", ""), h.get("name", ""), "🏠")}'
            f'<div class="holding-body">'
            f'<strong>{html.escape(h.get("name", h["slug"]))}</strong>{extra}'
            f'<span class="holding-sub muted">{sub}</span>'
            f'</div></a>'
        )
    for m in mounts:
        species = (m.get("species") or "").replace("-", " ")
        hp_cur = m.get("hp", m.get("hp_max", 0))
        hp_max_m = m.get("hp_max", hp_cur)
        hp_str = f"{hp_cur}/{hp_max_m} HP" if hp_max_m else ""
        sub_parts = [html.escape(species)] if species else []
        if hp_str:
            sub_parts.append(html.escape(hp_str))
        sub = " · ".join(sub_parts)
        # HP bar only when injured — keeps the layout quiet for healthy mounts.
        bar = ""
        if hp_max_m and hp_cur < hp_max_m:
            bar = " " + _hp_bar(hp_cur, hp_max_m, width=8)
        # Stowed-pool footer: vehicle-consumables held by this mount/cart.
        stowed = m.get("stowed") or {}
        stowed_html = ""
        if stowed:
            chips = " ".join(
                f'<span class="stowed-chip">{html.escape(k)} ×{int(v)}</span>'
                for k, v in sorted(stowed.items()) if int(v) > 0
            )
            if chips:
                stowed_html = f'<span class="holding-stowed">stowed: {chips}</span>'
        # Cart icon for stationary "vehicles" (mv 0); horse for everything else.
        glyph = "🛒" if (m.get("mv") or 0) == 0 else "🐎"
        items.append(
            f'<a class="holding-card holding-mount" href="/mounts/{html.escape(m["slug"])}">'
            f'{_thumb(m.get("portrait", ""), m.get("name", ""), glyph)}'
            f'<div class="holding-body">'
            f'<strong>{html.escape(m.get("name", m["slug"]))}</strong>{bar}'
            f'<span class="holding-sub muted">{sub}</span>'
            f'{stowed_html}'
            f'</div></a>'
        )
    return (
        '<div class="holdings"><p class="holdings-label"><strong>Holdings</strong></p>'
        f'<div class="holdings-grid">{"".join(items)}</div></div>'
    )


def _read_first_heading(path: Path) -> str:
    if not path.exists():
        return path.stem
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return path.stem


def _read_first_paragraph(path: Path) -> str:
    if not path.exists():
        return ""
    in_heading = True
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            in_heading = True
            continue
        if line.strip() == "":
            if not in_heading:
                break
            continue
        in_heading = False
        return line.strip()
    return ""


# ---------------------------------------------------------------------------
# Monster portrait helpers
# ---------------------------------------------------------------------------

def _monster_portrait_path(slug: str) -> Path | None:
    """Return path to an existing monster portrait file, or None."""
    for ext in ("png", "jpg", "jpeg", "webp"):
        p = _MONSTERS_DIR / f"{slug}.{ext}"
        if p.exists():
            return p
    return None


def _monster_portraits_dict() -> dict:
    """Return {slug: filename} for every monster that has a portrait."""
    if not _MONSTERS_DIR.exists():
        return {}
    exts = {".png", ".jpg", ".jpeg", ".webp"}
    return {p.stem: p.name for p in _MONSTERS_DIR.iterdir() if p.suffix.lower() in exts}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def gallery():
    index = _load_image_index()
    scenes = [e for e in index if e.get("type") == "scene"]
    campaign_name = cfg.get("name", "Campaign")

    if not scenes:
        body = f"<h1>{html.escape(campaign_name)} — Gallery</h1><p class='muted'>No scene images yet.</p>"
        return render(campaign_name, body)

    items = []
    for e in reversed(scenes):
        fn = e.get("filename", "")
        fn_safe = html.escape(fn)
        scene = html.escape(e.get("scene", ""))
        desc = html.escape(e.get("description", ""))
        items.append(
            f'<div class="grid-item" data-filename="{fn_safe}">'
            f'<button class="gallery-delete" type="button" '
            f'data-filename="{fn_safe}" '
            f'title="Delete this picture" aria-label="Delete this picture">×</button>'
            f'<img src="/images/{fn_safe}" alt="{scene}" loading="lazy">'
            f'<div class="caption"><strong>{scene}</strong>{desc}</div>'
            f'</div>'
        )

    body = (
        "<style>"
        ".grid-item { position: relative; }"
        ".gallery-delete {"
        "  position: absolute; top: 8px; right: 8px; z-index: 5;"
        "  width: 30px; height: 30px; padding: 0;"
        "  border-radius: 50%;"
        "  background: rgba(10,8,5,0.78); border: 1px solid var(--accent-rust);"
        "  color: #e8a090;"
        "  font-family: var(--font-body); font-size: 1.15em; line-height: 1;"
        "  cursor: pointer;"
        "  display: flex; align-items: center; justify-content: center;"
        "  opacity: 0; transition: opacity 180ms ease, transform 180ms ease, background 180ms ease, color 180ms ease;"
        "}"
        ".grid-item:hover .gallery-delete, "
        ".gallery-delete:focus { opacity: 1; }"
        ".gallery-delete:hover {"
        "  background: rgba(70,20,15,0.92); color: #ffb09a;"
        "  transform: scale(1.08);"
        "}"
        ".gallery-delete:disabled { opacity: 0.5; cursor: not-allowed; }"
        "</style>"
        f"<h1>{html.escape(campaign_name)} — Gallery</h1>"
        f'<div class="grid">{"".join(items)}</div>'
        "<script>(function(){"
        "document.querySelectorAll('.gallery-delete').forEach(function(btn){"
        "  btn.addEventListener('click', async function(e){"
        "    e.stopPropagation();"  # don't bubble to lightbox handler
        "    const fn = btn.dataset.filename;"
        "    if (!fn) return;"
        "    if (!confirm('Delete this picture?')) return;"
        "    btn.disabled = true;"
        "    try {"
        "      const r = await fetch('/api/images/' + encodeURIComponent(fn), {method:'DELETE'});"
        "      if (!r.ok) {"
        "        const d = await r.json().catch(()=>({}));"
        "        alert('Delete failed: ' + (d.error || ('HTTP '+r.status)));"
        "        return;"
        "      }"
        "      const card = btn.closest('.grid-item');"
        "      if (card) card.remove();"
        "    } catch (err) {"
        "      alert('Delete failed: ' + err);"
        "    } finally {"
        "      btn.disabled = false;"
        "    }"
        "  });"
        "});"
        "})();</script>"
    )
    return render(f"{campaign_name} — Gallery", body)


def _party_body(heading_tag: str = "h1", heading_text: str | None = None) -> str:
    """Build the live party panel: world-info bar + rich PC cards + light /
    consumables / loot. Shared by the combined /characters ("Cast") page,
    where it renders as an <h2> section above the full cast grid. The heading
    is parameterised so the same markup serves both contexts."""
    state = _c.load_state(cfg)
    campaign_name = cfg.get("name", "Campaign")
    characters = cfg.get("characters", {})
    combat_hp = _combat_hp_overrides()
    combat_effects = _combat_effects_overrides()
    image_index = _load_image_index()
    light_sources = state.get("light_sources", []) or []

    # Holdings (houses + mounts) grouped by owner PC key, so each
    # character card can render its own panel without re-scanning.
    vehicle_pools = state.get("vehicle_consumables") or {}
    houses_by_owner: dict[str, list] = {}
    for slug, h in (cfg.get("houses") or {}).items():
        houses_by_owner.setdefault(h.get("owner", ""), []).append({"slug": slug, **h})
    for v in houses_by_owner.values():
        v.sort(key=lambda r: r.get("name", ""))
    mounts_by_owner: dict[str, list] = {}
    for slug, m in (cfg.get("mounts") or {}).items():
        rec = {"slug": slug, **m}
        # Attach the stowed-pool counts so the Holdings panel can show them.
        if slug in vehicle_pools:
            rec["stowed"] = vehicle_pools[slug]
        mounts_by_owner.setdefault(m.get("owner", ""), []).append(rec)
    for v in mounts_by_owner.values():
        v.sort(key=lambda r: r.get("name", ""))

    coin = state.get("coin", {})
    coin_parts = []
    for denom in ("pp", "gp", "ep", "sp", "cp"):
        v = coin.get(denom, 0)
        if v:
            coin_parts.append(f"{v} {denom}")
    coin_str = ", ".join(coin_parts) if coin_parts else "0 gp"

    day = state.get("current_day", 1)
    session = state.get("current_session", 0)

    cards = []
    for key, char in characters.items():
        cstate = state.get("characters", {}).get(key, {})
        hp_max = char.get("hp_max", 0)
        hp_cur = combat_hp.get(key, cstate.get("hp", hp_max))
        label = char.get("label", key)
        cls = char.get("cls", "")
        level = int(char.get("level", 1) or 1)
        ac = char.get("ac")
        thac0 = char.get("thac0")
        xp = cstate.get("xp", 0)
        conditions = cstate.get("conditions", [])

        bar = _hp_bar(hp_cur, hp_max)
        cond_tags = "".join(f'<span class="tag">{html.escape(c)}</span>' for c in conditions)

        # Portrait
        portrait = _find_portrait(label, image_index, key)
        portrait_html = ""
        if portrait:
            fn = html.escape(portrait["filename"])
            portrait_html = (
                f'<img src="/images/{fn}" alt="{html.escape(label)}" '
                f'class="party-portrait">'
            )

        # Class line: "Cleric 3"
        cls_line = html.escape(cls)
        if cls and level:
            cls_line = f"{html.escape(cls)} {level}"

        # Combat stats line
        stats_parts = []
        if ac is not None:
            stats_parts.append(f"<strong>AC:</strong> {html.escape(str(ac))}")
        if thac0 is not None:
            stats_parts.append(f"<strong>THAC0:</strong> {html.escape(str(thac0))}")
        stats_html = ""
        if stats_parts:
            stats_html = "<p>" + " &nbsp;·&nbsp; ".join(stats_parts) + "</p>"

        # XP / next-level threshold
        xp_table = _class_xp_table(cls)
        next_xp_str = ""
        if xp_table and level < len(xp_table):
            next_xp = xp_table[level]   # xp_required for (level+1) — table is 0-indexed by level
            if next_xp > xp:
                next_xp_str = f' <span class="muted">/ {next_xp:,}</span>'
            else:
                next_xp_str = ' <span class="tag" style="background:#1a3a18;color:#6db86d">level up</span>'

        # Active effects (combat-only)
        effects = combat_effects.get(key, [])
        effects_html = ""
        if effects:
            chips = []
            for eff in effects:
                name = html.escape(eff.get("name", "?"))
                dur = eff.get("duration", 0)
                mods = []
                for k, sym in (("to_hit", "hit"), ("ac", "AC"), ("dmg", "dmg"), ("save", "save")):
                    v = eff.get(k)
                    if v:
                        mods.append(f"{sym}{int(v):+d}")
                mod_str = f" <span class='muted'>({', '.join(mods)})</span>" if mods else ""
                chips.append(
                    f'<span class="effect-chip">{name} '
                    f'<span class="muted">{dur}r</span>{mod_str}</span>'
                )
            effects_html = "<p><strong>Effects:</strong> " + " ".join(chips) + "</p>"

        # Encumbrance — only show when not "light" (the common case)
        enc = _encumbrance_band(char, state, key)
        enc_html = ""
        if enc["band"] != "light":
            color = {
                "moderate":   "#c8a96e",
                "heavy":      "#e0a060",
                "severe":     "#e08060",
                "overloaded": "#ff6a4a",
            }.get(enc["band"], "#c8a96e")
            penalty_str = f" ({enc['penalty']:+d} move)" if enc["penalty"] else ""
            enc_html = (
                f'<p><strong>Load:</strong> '
                f'<span style="color:{color}">{html.escape(enc["band"])}</span> '
                f'<span class="muted">— {enc["weight"]} lb{penalty_str}</span></p>'
            )

        # Light source carried by this character
        carried_lit = [s for s in light_sources
                       if s.get("lit") and s.get("holder") == key]
        light_chip_html = ""
        if carried_lit:
            chips = []
            for s in carried_lit:
                mins = s.get("minutes_remaining", 0)
                cls_chip = "light-chip guttering" if mins <= 5 else "light-chip"
                stype = html.escape(s.get("type", "?"))
                chips.append(f'<span class="{cls_chip}">{stype} · {mins} min</span>')
            light_chip_html = "<p><strong>Carrying:</strong> " + " ".join(chips) + "</p>"

        # Spell slots
        slot_cfg = char.get("spell_slots") or {}
        slot_state = cstate.get("spell_slots") or {}
        slot_lines = []
        for lvl, max_count in slot_cfg.items():
            remaining = slot_state.get(str(lvl), max_count)
            slot_lines.append(f"L{lvl}: {remaining}/{max_count}")
        slots_html = ""
        if slot_lines:
            slots_html = "<p><strong>Slots:</strong> " + " &nbsp; ".join(slot_lines) + "</p>"

        # Memorized spells, itemized and grouped by spell level.
        memorized = cstate.get("memorized_spells") or []
        mem_html = ""
        if memorized:
            # Resolve missing levels from the spell DB (entries are often
            # stored with level=0 by the MCP layer).
            unknown_names = sorted({
                (s.get("name") if isinstance(s, dict) else str(s)).strip()
                for s in memorized
                if not (isinstance(s, dict) and int(s.get("level", 0) or 0) > 0)
            })
            unknown_names = [n for n in unknown_names if n]
            level_map: dict[str, int] = {}
            if unknown_names:
                placeholders = ",".join("?" * len(unknown_names))
                rows = _db(_2E_DB).execute(
                    f"SELECT name, level FROM spells WHERE lower(name) IN ({placeholders})",
                    [n.lower() for n in unknown_names],
                ).fetchall()
                level_map = {r["name"].lower(): int(r["level"]) for r in rows}

            by_level: dict[int, list[dict]] = {}
            for s in memorized:
                if isinstance(s, dict):
                    name = (s.get("name") or "").strip()
                    lvl = int(s.get("level", 0) or 0)
                    cast = bool(s.get("cast"))
                else:
                    name, lvl, cast = str(s).strip(), 0, False
                if lvl <= 0:
                    lvl = level_map.get(name.lower(), 0)
                by_level.setdefault(lvl, []).append({"name": name, "cast": cast})

            def _render_spell_li(s: dict) -> str:
                name_esc = html.escape(s["name"])
                if s["cast"]:
                    return (
                        f'<li class="spell-cast" title="cast">'
                        f'<s>{name_esc}</s></li>'
                    )
                return f'<li class="spell-ready">{name_esc}</li>'

            blocks = []
            for lvl in sorted(by_level):
                spells_at_level = by_level[lvl]
                ready_at_level = sum(1 for s in spells_at_level if not s["cast"])
                total_at_level = len(spells_at_level)
                label_lvl = f"Level {lvl}" if lvl > 0 else "Unleveled"
                items = "".join(_render_spell_li(s) for s in spells_at_level)
                blocks.append(
                    f'<div class="spell-level">'
                    f'<div class="spell-level-head">{label_lvl} '
                    f'<span class="muted">({ready_at_level}/{total_at_level})</span></div>'
                    f'<ul class="spell-list">{items}</ul>'
                    f'</div>'
                )

            ready_count = sum(1 for s in memorized
                              if not (isinstance(s, dict) and s.get("cast")))
            total_count = len(memorized)
            mem_html = (
                f'<div class="memorized">'
                f'<p style="margin-bottom:4px"><strong>Memorized</strong> '
                f'<span class="muted">({ready_count}/{total_count} ready)</span></p>'
                f'{"".join(blocks)}'
                f'</div>'
            )

        holdings_html = _render_holdings_panel(
            houses_by_owner.get(key, []),
            mounts_by_owner.get(key, []),
        )

        cards.append(
            f'<div class="card party-card clearfix">'
            f'{portrait_html}'
            f'<div class="party-body">'
            f'<h2><a href="/sheets/{html.escape(key)}">{html.escape(label)}</a></h2>'
            f"<p class='muted'>{cls_line}</p>"
            f"<p><strong>HP:</strong> {html.escape(str(hp_cur))}/{html.escape(str(hp_max))} {bar}</p>"
            f"{stats_html}"
            f"<p><strong>XP:</strong> {xp:,}{next_xp_str}</p>"
            f"{enc_html}"
            f"{light_chip_html}"
            f"{effects_html}"
            f"{slots_html}"
            f"{mem_html}"
            f'<p>{cond_tags}</p>'
            f'{holdings_html}'
            f'</div>'
            f"</div>"
        )

    # Loot pile staged from award_treasure
    loot_pile = state.get("loot_pile", [])
    loot_html = ""
    if loot_pile:
        rows = []
        for i, item in enumerate(loot_pile):
            kind = html.escape(item.get("kind", "?"))
            desc = html.escape(item.get("description", "?"))
            val = item.get("value_gp", 0)
            cat = html.escape(item.get("category", ""))
            cat_str = f' <span class="tag">{cat}</span>' if cat else ""
            rows.append(
                f'<li>[{i}] <strong>{desc}</strong> '
                f'<span class="muted">— {kind}, {val} gp</span>{cat_str}</li>'
            )
        loot_html = (
            f'<div class="card"><h2>Loot Pile <span class="muted">'
            f'({len(loot_pile)} items)</span></h2>'
            f'<ul>{"".join(rows)}</ul>'
            f'<p class="muted" style="font-size:0.85em">'
            f'Distribute via <code>claim_loot(index, character)</code>.</p></div>'
        )

    # Tracked consumables per character
    consumables = state.get("consumables", {})
    consumables_rows = []
    for key, items in consumables.items():
        if not items:
            continue
        char = cfg.get("characters", {}).get(key) or cfg.get("npcs", {}).get(key, {})
        label = char.get("label", key)
        item_strs = ", ".join(f"{html.escape(name)}×{qty}" for name, qty in items.items())
        consumables_rows.append(f"<li><strong>{html.escape(label)}:</strong> {item_strs}</li>")
    consumables_html = ""
    if consumables_rows:
        consumables_html = (
            f'<div class="card"><h2>Consumables</h2>'
            f'<ul>{"".join(consumables_rows)}</ul></div>'
        )

    # Light sources
    lights = state.get("light_sources", [])
    lit = [s for s in lights if s.get("lit")]
    light_html = ""
    if lit:
        rows = []
        for s in lit:
            mins = s.get("minutes_remaining", 0)
            cls = "urgent" if mins <= 5 else ""
            holder = html.escape(s.get("holder", "?"))
            stype = html.escape(s.get("type", "?"))
            rows.append(f'<span class="clock {cls}">{stype} ({holder}, {mins} min)</span>')
        light_html = (
            f'<div class="card"><h2>Light Sources</h2>{"".join(rows)}</div>'
        )

    head = heading_text if heading_text is not None else f"{campaign_name} — Party"
    return (
        f"<{heading_tag}>{html.escape(head)}</{heading_tag}>"
        + _world_info_bar(state)
        + "".join(cards)
        + light_html
        + consumables_html
        + loot_html
    )


@app.route("/party")
def party():
    """Legacy route — the party view now lives on the combined Cast page."""
    return redirect("/characters")


@app.route("/world")
def world_view():
    state = _c.load_state(cfg)
    campaign_name = cfg.get("name", "Campaign")

    # Factions
    factions = cfg.get("factions", {})
    faction_state = state.get("faction_state", {})
    faction_cards = []
    for slug, f in factions.items():
        fs = faction_state.get(slug, {})
        rep = fs.get("reputation", 0)
        strength = fs.get("strength", 100)
        rep_color = "#6db86d" if rep > 20 else ("#c87a7a" if rep < -20 else "#c8a96e")
        known_tag = '<span class="tag">known</span>' if f.get("known_to_party") else \
                    '<span class="tag" style="background:#3a1818;color:#c87a7a">hidden</span>'
        faction_cards.append(
            f'<div class="card">'
            f'<h2>{html.escape(f.get("name", slug))} '
            f'<span class="muted" style="font-size:0.7em">[{html.escape(f.get("alignment",""))}]</span></h2>'
            f'<p class="muted">{html.escape(f.get("scope","local"))} scope · slug: <code>{html.escape(slug)}</code> {known_tag}</p>'
            f'<p>{html.escape(f.get("goals",""))}</p>'
            f'<p><strong>Strength:</strong> {strength}% '
            f'&nbsp;·&nbsp; <strong>Reputation:</strong> '
            f'<span style="color:{rep_color}">{rep:+d}</span></p>'
            f'</div>'
        )
    factions_html = ("".join(faction_cards) if faction_cards
                     else '<p class="muted">No factions registered. Use add_faction to seed.</p>')

    # Clocks
    clocks = state.get("faction_clocks", [])
    clocks_active = sorted(
        [c for c in clocks if c.get("days_remaining", 0) > 0],
        key=lambda c: c["days_remaining"],
    )
    clocks_done = [c for c in clocks if c.get("days_remaining", 0) <= 0]

    clock_rows = []
    for c in clocks_active:
        days = c["days_remaining"]
        cls = "urgent" if days <= 7 else ""
        fac = c.get("faction", "")
        fac_html = f' · {html.escape(fac)}' if fac else ""
        on_complete = c.get("on_complete", "")
        oc_html = f'<br><span class="muted">{html.escape(on_complete)}</span>' if on_complete else ""
        clock_rows.append(
            f'<div class="clock {cls}" style="display:block;padding:10px 14px;margin:6px 0">'
            f'<strong>{html.escape(c.get("label","?"))}</strong> '
            f'<span style="float:right">{days} days</span>{fac_html}{oc_html}'
            f'</div>'
        )
    expired_rows = []
    for c in clocks_done:
        expired_rows.append(
            f'<div class="clock expired" style="display:block;padding:10px 14px;margin:6px 0">'
            f'<strong>{html.escape(c.get("label","?"))}</strong> '
            f'<span style="float:right">expired</span>'
            f'</div>'
        )
    clocks_html = ""
    if clock_rows:
        clocks_html += "".join(clock_rows)
    if expired_rows:
        clocks_html += "<h3>Expired (consequences pending)</h3>" + "".join(expired_rows)
    if not clocks_html:
        clocks_html = '<p class="muted">No active clocks.</p>'

    # Rumors (DM view — show truth tier)
    rumors = cfg.get("rumors", [])
    rumor_rows = []
    for r in rumors[-15:]:
        truth = r.get("truth", "partly_true")
        rumor_rows.append(
            f'<li><span class="truth-{html.escape(truth)}">[{html.escape(truth)}]</span> '
            f'{html.escape(r.get("text",""))}'
            f'</li>'
        )
    rumors_html = (
        f'<ul>{"".join(rumor_rows)}</ul>' if rumor_rows
        else '<p class="muted">No rumors stored.</p>'
    )

    # Secrets (DM-only)
    secrets_path = cfg["_dir"] / "secrets.json"
    secret_rows = []
    if secrets_path.exists():
        try:
            recs = json.loads(secrets_path.read_text(encoding="utf-8"))
            for r in recs[-15:]:
                tags = r.get("tags", [])
                tag_html = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in tags)
                rel = r.get("related_to", "")
                rel_html = f' <span class="muted">→ {html.escape(rel)}</span>' if rel else ""
                revealed = r.get("revealed")
                rev_html = '<span class="tag" style="background:#1a3a18;color:#6db86d">revealed</span>' if revealed else ""
                secret_rows.append(
                    f'<div class="secret-card">'
                    f'<p>{html.escape(r.get("text",""))}{rel_html}</p>'
                    f'<p class="muted" style="font-size:0.8em">'
                    f'#{r.get("id","?")} · day {r.get("day","?")} {tag_html} {rev_html}</p>'
                    f'</div>'
                )
        except (json.JSONDecodeError, OSError):
            pass
    secrets_html = (
        "".join(secret_rows) if secret_rows
        else '<p class="muted">No DM-only notes recorded.</p>'
    )

    rumor_count = len(rumors)
    secret_count = len(secret_rows)

    body = (
        f"<h1>{html.escape(campaign_name)} — World</h1>"
        + _world_info_bar(state)
        + "<h2>Factions</h2>" + factions_html
        + "<h2>World Clocks</h2>" + clocks_html
        + '<details class="spoiler">'
        + f'<summary>Rumors <span class="muted">({rumor_count})</span>'
        + '<span class="spoiler-warn">DM view — reveals truth tier · click to expand</span>'
        + '</summary>'
        + f'<div class="spoiler-body">{rumors_html}</div>'
        + '</details>'
        + '<details class="spoiler">'
        + f'<summary>DM-Only Notes <span class="muted">({secret_count})</span>'
        + '<span class="spoiler-warn">Never narrate to the player · click to expand</span>'
        + '</summary>'
        + f'<div class="spoiler-body">{secrets_html}</div>'
        + '</details>'
        + '<h2><a href="/log">Chronicle</a></h2>'
        + '<p class="muted">Session-by-session adventure log.</p>'
    )
    return render(f"{campaign_name} — World", body)


@app.route("/quests")
def quests_view():
    state = _c.load_state(cfg)
    campaign_name = cfg.get("name", "Campaign")
    quests = cfg.get("quests", {})

    chars = cfg.get("characters", {})
    avg_level = (sum(int(c.get("level", 1)) for c in chars.values()) / len(chars)) if chars else 1.0

    def _scope_warn(scope: str) -> str:
        if scope == "regional" and avg_level < 4:
            return f"⚠ regional scope on L{avg_level:.1f} party"
        if scope == "continental" and avg_level < 8:
            return f"⚠ continental scope on L{avg_level:.1f} party"
        return ""

    by_status: dict[str, list] = {"active": [], "paused": [], "complete": [], "failed": []}
    for slug, q in quests.items():
        by_status.setdefault(q.get("status", "active"), []).append((slug, q))

    def _render_quest(slug: str, q: dict) -> str:
        scope = q.get("scope", "local")
        warn = _scope_warn(scope)
        warn_html = f'<p class="scope-warning">{html.escape(warn)}</p>' if warn else ""
        log = q.get("notes_log", [])
        log_html = ""
        if log:
            items = "".join(
                f"<li><span class='muted'>day {entry.get('day','?')}:</span> "
                f"{html.escape(entry.get('text',''))}</li>"
                for entry in log[-5:]
            )
            log_html = f"<details><summary class='muted'>Recent notes</summary><ul>{items}</ul></details>"
        related = []
        for kind in ("npcs", "factions", "locations"):
            v = q.get(f"related_{kind}", [])
            if v:
                related.append(f"<strong>{kind}:</strong> {', '.join(html.escape(s) for s in v)}")
        rel_html = f"<p class='muted'>{' &nbsp;·&nbsp; '.join(related)}</p>" if related else ""
        giver = q.get("giver", "")
        giver_html = f"<p class='muted'>Given by <code>{html.escape(giver)}</code></p>" if giver else ""
        return (
            f'<div class="card">'
            f'<h2>{html.escape(q.get("title", slug))} '
            f'<span class="tag">{html.escape(scope)}</span></h2>'
            f'{warn_html}{giver_html}'
            f'<p>{html.escape(q.get("stakes",""))}</p>'
            f'{rel_html}{log_html}'
            f'</div>'
        )

    sections = []
    for status_key in ("active", "paused", "complete", "failed"):
        items = by_status.get(status_key, [])
        if not items:
            continue
        sections.append(f"<h2>{status_key.title()}</h2>")
        for slug, q in sorted(items, key=lambda kv: kv[1].get("started_day", 0)):
            sections.append(_render_quest(slug, q))

    if not sections:
        sections.append("<p class='muted'>No quests recorded yet. Use add_quest to start.</p>")

    body = (
        f"<h1>{html.escape(campaign_name)} — Quests</h1>"
        f"<p class='muted'>Average party level: {avg_level:.1f}</p>"
        + "".join(sections)
    )
    return render(f"{campaign_name} — Quests", body)


@app.route("/log")
def log():
    campaign_name = cfg.get("name", "Campaign")
    log_file = cfg.get("session_log_file") or cfg.get("chronicle_file") or "adventure_log.md"
    log_path = cfg["_data_dir"] / log_file

    if not log_path.exists():
        body = f"<h1>{html.escape(campaign_name)} — Chronicle</h1><p class='muted'>No chronicle yet.</p>"
        return render(f"{campaign_name} — Chronicle", body)

    content = log_path.read_text(encoding="utf-8")
    content_html = _markdown_to_html(content)

    body = (
        f"<h1>{html.escape(campaign_name)} — Chronicle</h1>"
        f'<div class="card">{content_html}</div>'
    )
    return render(f"{campaign_name} — Chronicle", body)


def _sessions_dir() -> Path:
    return _export.PROJECTS_DIR / _export.encode_cwd(Path(__file__).parent)


def _active_campaign_slug() -> str | None:
    """Slug of the campaign currently bound to /play (from .active)."""
    return cfg.get("_name") if cfg else None


def _campaign_session_md_dir(slug: str) -> Path:
    """Per-campaign markdown export dir for SessionEnd hooks."""
    return Path(__file__).parent / "campaigns" / slug / "_session-logs"


def _active_campaign_session_paths() -> list[Path]:
    """JSONL paths for sids in the active campaign's manifest, newest first.

    Returns ``[]`` when no campaign is active, the manifest is empty, or
    the JSONLs have been pruned from Claude's pool dir."""
    slug = _active_campaign_slug()
    if not slug:
        return []
    sdir = _sessions_dir()
    if not sdir.is_dir():
        return []
    sids = _dm.manifest_sids(slug)
    paths = []
    for sid in sids:
        p = sdir / f"{sid}.jsonl"
        if p.is_file():
            paths.append(p)
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return paths


def _session_teaser(path: Path, max_chars: int = 140) -> str:
    """First non-empty player prompt in the session, trimmed."""
    try:
        with path.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "user":
                    continue
                content = rec.get("message", {}).get("content", "")
                if not isinstance(content, str):
                    continue
                cleaned = _export.clean_user_text(content)
                if cleaned:
                    cleaned = cleaned.replace("\n", " ").strip()
                    return cleaned[:max_chars] + ("…" if len(cleaned) > max_chars else "")
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------------
# /campaigns — list, switch, create, delete, import, export, banner
# ---------------------------------------------------------------------------

_BANNER_MAX_BYTES = 8 * 1024 * 1024
_BANNER_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _activate_and_reload(slug: str) -> None:
    """Switch the active campaign and replace the shared ``cfg`` dict in
    place so subsequent requests see the new campaign without restarting."""
    _c.set_active(slug)
    fresh = _c.load_campaign(slug)
    cfg.clear()
    cfg.update(fresh)
    global _cfg_mtime
    try:
        _cfg_mtime = (cfg["_dir"] / "campaign.json").stat().st_mtime
    except OSError:
        _cfg_mtime = 0.0


def _humansize(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n/1.0:.1f} {unit}" if n < 1024 else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


@app.route("/campaigns")
def campaigns_index():
    body = """
<style>
  .campaigns-toolbar { display: flex; gap: 10px; margin: 14px 0 18px; flex-wrap: wrap; }
  .campaigns-toolbar button {
    padding: 8px 14px; background: transparent; color: var(--ink-body);
    border: 1px solid var(--rule-hi); border-radius: 3px; cursor: pointer;
    font-family: var(--font-display); font-size: 0.82em;
    text-transform: uppercase; letter-spacing: 0.12em;
  }
  .campaigns-toolbar button:hover { border-color: var(--accent-gold); color: var(--accent-gold); }
  .camp-grid {
    display: grid; gap: 18px;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  }
  .camp-card {
    background: var(--paper);
    border: 1px solid var(--rule-hi); border-radius: 4px;
    overflow: hidden; display: flex; flex-direction: column;
    transition: transform .15s ease, border-color .15s ease;
    position: relative;
  }
  .camp-card:hover { transform: translateY(-2px); border-color: var(--accent-gold); }
  .camp-banner {
    aspect-ratio: 16 / 9; background: #1a1a1a center/cover no-repeat;
    border-bottom: 1px solid var(--rule-hi);
    display: flex; align-items: center; justify-content: center;
    color: var(--ink-faint); font-family: var(--font-display);
    font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.15em;
  }
  .camp-banner.has-image { color: transparent; }
  .camp-body { padding: 12px 14px 14px; display: flex; flex-direction: column; gap: 6px; }
  .camp-name {
    font-family: var(--font-display); font-size: 1.08em; line-height: 1.2;
    color: var(--ink-body); margin: 0;
  }
  .camp-sub { font-size: 0.82em; color: var(--ink-faint); }
  .camp-meta {
    display: grid; grid-template-columns: auto 1fr; gap: 2px 10px;
    font-size: 0.78em; color: var(--ink-faint); margin-top: 4px;
  }
  .camp-meta dt { font-family: var(--font-display); text-transform: uppercase; letter-spacing: 0.1em; }
  .camp-badges { position: absolute; top: 8px; right: 8px; display: flex; gap: 4px; }
  .camp-badge {
    padding: 2px 8px; font-size: 0.7em; border-radius: 999px;
    font-family: var(--font-display); text-transform: uppercase; letter-spacing: 0.1em;
    background: rgba(0,0,0,0.55); color: var(--ink-body);
    border: 1px solid rgba(255,255,255,0.15);
  }
  .camp-badge.active { background: var(--accent-gold); color: #2a1f00; border-color: var(--accent-gold); }
  .camp-badge.closed { background: #5a2222; color: #f0d0d0; border-color: #5a2222; }
  .camp-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .camp-actions button, .camp-actions a.btn {
    padding: 5px 10px; background: transparent; border: 1px solid var(--rule-hi);
    color: var(--ink-body); border-radius: 3px; cursor: pointer;
    font-family: var(--font-display); font-size: 0.72em;
    text-transform: uppercase; letter-spacing: 0.1em; text-decoration: none;
  }
  .camp-actions button:hover, .camp-actions a.btn:hover { border-color: var(--accent-gold); color: var(--accent-gold); }
  .camp-actions .danger { border-color: #7a3a3a; color: #b88; }
  .camp-actions .danger:hover { border-color: #c66; color: #ecc; }
  .camp-empty { text-align: center; padding: 60px 20px; color: var(--ink-faint); }

  dialog.camp-dialog {
    border: 1px solid var(--rule-hi); background: var(--paper); color: var(--ink-body);
    border-radius: 4px; padding: 22px 24px; max-width: 520px; width: 90vw;
  }
  dialog.camp-dialog::backdrop { background: rgba(0,0,0,0.55); }
  dialog.camp-dialog h3 { margin: 0 0 8px; font-family: var(--font-display); }
  dialog.camp-dialog p.muted { font-size: 0.85em; margin-bottom: 12px; }
  dialog.camp-dialog label {
    display: block; margin-top: 12px; font-family: var(--font-display);
    font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.1em;
    color: var(--ink-faint);
  }
  dialog.camp-dialog input[type=text],
  dialog.camp-dialog textarea,
  dialog.camp-dialog input[type=number],
  dialog.camp-dialog input[type=file] {
    width: 100%; box-sizing: border-box; margin-top: 4px;
    padding: 6px 8px; background: #1a1a1a; color: var(--ink-body);
    border: 1px solid var(--rule-hi); border-radius: 3px;
    font-family: inherit; font-size: 0.95em;
  }
  dialog.camp-dialog textarea { min-height: 70px; resize: vertical; }
  dialog.camp-dialog .actions {
    display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px;
  }
  dialog.camp-dialog button {
    padding: 6px 14px; background: transparent; border: 1px solid var(--rule-hi);
    color: var(--ink-body); border-radius: 3px; cursor: pointer;
    font-family: var(--font-display); font-size: 0.78em;
    text-transform: uppercase; letter-spacing: 0.12em;
  }
  dialog.camp-dialog button:hover:not(:disabled) { border-color: var(--accent-gold); color: var(--accent-gold); }
  dialog.camp-dialog button:disabled { opacity: 0.5; cursor: not-allowed; }
  dialog.camp-dialog .danger-btn { border-color: #7a3a3a; color: #b88; margin-right: auto; }
  dialog.camp-dialog .danger-btn:hover:not(:disabled) { border-color: #c66; color: #ecc; }
  dialog.camp-dialog .err { color: #d77; font-size: 0.82em; margin-top: 8px; min-height: 1em; }
  dialog.camp-dialog .banner-tabs { display: flex; gap: 4px; margin-bottom: 8px; }
  dialog.camp-dialog .banner-tabs button { flex: 1; }
  dialog.camp-dialog .banner-tabs button.on { border-color: var(--accent-gold); color: var(--accent-gold); }
</style>

<h1>Campaigns</h1>
<div class="campaigns-toolbar">
  <button id="camp-new-btn">+ New campaign</button>
  <button id="camp-import-btn">Import…</button>
</div>
<div id="camp-grid" class="camp-grid"></div>

<dialog id="camp-new-dialog" class="camp-dialog">
  <form method="dialog" id="camp-new-form">
    <h3>New campaign</h3>
    <p class="muted">Scaffolds <code>campaigns/&lt;slug&gt;/</code> and switches to it.</p>
    <label>Name<input type="text" name="name" required placeholder="e.g. The Iron Crown" autofocus></label>
    <label>World<textarea name="world" required placeholder="One or two sentences setting the scene"></textarea></label>
    <label>Tone<input type="text" name="tone" required value="high fantasy"></label>
    <label>Starting gold per PC<input type="number" name="initial_gp" value="0" min="0"></label>
    <div class="err" id="camp-new-err"></div>
    <div class="actions">
      <button type="button" data-close>Cancel</button>
      <button type="submit">Create</button>
    </div>
  </form>
</dialog>

<dialog id="camp-import-dialog" class="camp-dialog">
  <form id="camp-import-form">
    <h3>Import campaign</h3>
    <p class="muted">Drop a <code>.tgz</code> produced by export below.</p>
    <label>Archive<input type="file" name="archive" accept=".tgz,.tar.gz,application/gzip,application/x-tar" required></label>
    <label>Rename slug (optional)<input type="text" name="rename" placeholder="leave blank to use the archive's own slug"></label>
    <label style="display:flex; align-items:center; gap:8px; text-transform: none; letter-spacing: 0;">
      <input type="checkbox" name="force" style="width:auto"> Overwrite if a campaign with that slug already exists
    </label>
    <div class="err" id="camp-import-err"></div>
    <div class="actions">
      <button type="button" data-close>Cancel</button>
      <button type="submit">Import</button>
    </div>
  </form>
</dialog>

<dialog id="camp-delete-dialog" class="camp-dialog">
  <form id="camp-delete-form">
    <h3>Delete campaign</h3>
    <p class="muted">This removes <code id="camp-delete-target"></code> from disk. Files in <code>global/</code> are untouched. There is no undo.</p>
    <label>Type the slug to confirm<input type="text" name="confirm" required autocomplete="off"></label>
    <div class="err" id="camp-delete-err"></div>
    <div class="actions">
      <button type="button" data-close>Cancel</button>
      <button type="submit" class="danger-btn">Delete</button>
    </div>
  </form>
</dialog>

<dialog id="camp-banner-dialog" class="camp-dialog">
  <form id="camp-banner-form">
    <h3>Set banner — <span id="camp-banner-target"></span></h3>
    <div class="banner-tabs">
      <button type="button" data-mode="upload" class="on">Upload image</button>
      <button type="button" data-mode="generate">Generate from world</button>
    </div>
    <div data-mode-panel="upload">
      <label>Image (PNG/JPG/WebP, max 8 MB)<input type="file" name="image" accept="image/png,image/jpeg,image/webp,image/gif"></label>
    </div>
    <div data-mode-panel="generate" hidden>
      <p class="muted">Generates a landscape banner from the campaign's world + tone via Flux. Takes 20–45 seconds.</p>
      <label>Optional extra hint<input type="text" name="hint" placeholder="e.g. dawn light over the docks"></label>
    </div>
    <div class="err" id="camp-banner-err"></div>
    <div class="actions">
      <button type="button" data-close>Cancel</button>
      <button type="submit" id="camp-banner-submit">Save</button>
    </div>
  </form>
</dialog>

<dialog id="camp-instructions-dialog" class="camp-dialog">
  <form id="camp-instructions-form">
    <h3>Campaign instructions — <span id="camp-instr-target"></span></h3>
    <p class="muted">A binding per-campaign constraint (e.g. lock the campaign to its module key) injected into every DM turn at the same authority as the Hard Constraints. Takes effect on that campaign's next turn.</p>
    <label>Instructions<textarea name="text" maxlength="6000" rows="6" placeholder="e.g. Run strictly off modules/<slug>/; read the matching Level-NN.md before keying any room, encounter, NPC, or cosmology beat; do not invent substitutes for module factions, the previous-party roster, or the central mystery."></textarea></label>
    <label style="display:flex; align-items:center; gap:8px; text-transform: none; letter-spacing: 0;">
      <input type="checkbox" name="enabled" style="width:auto" checked> Enabled (uncheck to keep the text but stop injecting it)
    </label>
    <div class="err" id="camp-instr-err"></div>
    <div class="actions">
      <button type="button" data-close>Cancel</button>
      <button type="submit" id="camp-instr-submit">Save</button>
    </div>
  </form>
</dialog>

<script>
(function(){
  const grid = document.getElementById('camp-grid');
  const newBtn = document.getElementById('camp-new-btn');
  const importBtn = document.getElementById('camp-import-btn');

  const newDlg = document.getElementById('camp-new-dialog');
  const newForm = document.getElementById('camp-new-form');
  const newErr = document.getElementById('camp-new-err');

  const impDlg = document.getElementById('camp-import-dialog');
  const impForm = document.getElementById('camp-import-form');
  const impErr = document.getElementById('camp-import-err');

  const delDlg = document.getElementById('camp-delete-dialog');
  const delForm = document.getElementById('camp-delete-form');
  const delTarget = document.getElementById('camp-delete-target');
  const delErr = document.getElementById('camp-delete-err');
  let delSlug = null;

  const banDlg = document.getElementById('camp-banner-dialog');
  const banForm = document.getElementById('camp-banner-form');
  const banTarget = document.getElementById('camp-banner-target');
  const banErr = document.getElementById('camp-banner-err');
  const banSubmit = document.getElementById('camp-banner-submit');
  let banSlug = null;
  let banMode = 'upload';

  const instrDlg = document.getElementById('camp-instructions-dialog');
  const instrForm = document.getElementById('camp-instructions-form');
  const instrTarget = document.getElementById('camp-instr-target');
  const instrErr = document.getElementById('camp-instr-err');
  const instrSubmit = document.getElementById('camp-instr-submit');
  let instrSlug = null;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function fmtDate(iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      return d.toLocaleDateString(undefined, {year:'numeric', month:'short', day:'numeric'}) +
             ' · ' + d.toLocaleTimeString(undefined, {hour:'2-digit', minute:'2-digit'});
    } catch (e) { return iso; }
  }
  function fmtSize(bytes) {
    if (!bytes) return '—';
    const units = ['B','KB','MB','GB'];
    let i = 0, n = bytes;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + ' ' + units[i];
  }

  function renderCard(c) {
    const wrap = document.createElement('article');
    wrap.className = 'camp-card';
    const banner = document.createElement('div');
    banner.className = 'camp-banner' + (c.banner_url ? ' has-image' : '');
    if (c.banner_url) {
      banner.style.backgroundImage = "url('" + c.banner_url + "?t=" + Date.now() + "')";
    } else {
      banner.textContent = 'no banner';
    }
    wrap.appendChild(banner);

    const badges = document.createElement('div');
    badges.className = 'camp-badges';
    if (c.active) {
      const b = document.createElement('span'); b.className = 'camp-badge active'; b.textContent = 'Active'; badges.appendChild(b);
    }
    if (c.status === 'closed') {
      const b = document.createElement('span'); b.className = 'camp-badge closed'; b.textContent = 'Closed'; badges.appendChild(b);
    }
    wrap.appendChild(badges);

    const body = document.createElement('div');
    body.className = 'camp-body';
    body.innerHTML =
      '<h2 class="camp-name">' + esc(c.name) + '</h2>' +
      '<div class="camp-sub">' + esc(c.system || '') + (c.world ? ' · ' + esc((c.world || '').slice(0, 110)) + (c.world.length > 110 ? '…' : '') : '') + '</div>' +
      '<dl class="camp-meta">' +
        '<dt>Created</dt><dd>' + esc(fmtDate(c.created_at)) + '</dd>' +
        '<dt>Last played</dt><dd>' + esc(fmtDate(c.last_played)) + '</dd>' +
        '<dt>Size</dt><dd>' + esc(fmtSize(c.size_bytes)) + '</dd>' +
      '</dl>';
    const actions = document.createElement('div');
    actions.className = 'camp-actions';
    const open = document.createElement('button'); open.textContent = c.active ? 'Open' : 'Switch & open';
    open.addEventListener('click', () => switchAndOpen(c.slug));
    const banBtn = document.createElement('button'); banBtn.textContent = 'Banner';
    banBtn.addEventListener('click', () => openBanner(c.slug, c.name));
    const instrBtn = document.createElement('button'); instrBtn.textContent = '📜 Instructions';
    instrBtn.title = 'Binding per-campaign constraint (e.g. module lock)';
    instrBtn.addEventListener('click', () => openInstructions(c.slug, c.name));
    const exp = document.createElement('a'); exp.className = 'btn'; exp.textContent = 'Export';
    exp.href = '/campaigns/' + encodeURIComponent(c.slug) + '/export.tgz';
    const del = document.createElement('button'); del.className = 'danger'; del.textContent = 'Delete';
    del.addEventListener('click', () => openDelete(c.slug));
    if (c.active) del.disabled = true, del.title = 'Switch to another campaign before deleting this one.';
    actions.append(open, banBtn, instrBtn, exp, del);
    body.appendChild(actions);
    wrap.appendChild(body);
    return wrap;
  }

  async function refresh() {
    grid.textContent = '';
    const r = await fetch('/api/campaigns.json');
    if (!r.ok) { grid.innerHTML = '<div class="camp-empty">Failed to load campaigns.</div>'; return; }
    const d = await r.json();
    if (!d.campaigns || !d.campaigns.length) {
      grid.innerHTML = '<div class="camp-empty">No campaigns yet. Create one to get started.</div>';
      return;
    }
    for (const c of d.campaigns) grid.appendChild(renderCard(c));
  }

  async function switchAndOpen(slug) {
    const r = await fetch('/campaigns/' + encodeURIComponent(slug) + '/switch', {method: 'POST'});
    if (r.ok) window.location.href = '/play';
  }
  function openDelete(slug) {
    delSlug = slug; delTarget.textContent = slug; delErr.textContent = '';
    delForm.reset();
    delDlg.showModal();
  }
  function openBanner(slug, name) {
    banSlug = slug; banTarget.textContent = name; banErr.textContent = '';
    banMode = 'upload'; setBannerMode('upload');
    banForm.reset();
    banDlg.showModal();
  }
  async function openInstructions(slug, name) {
    instrSlug = slug; instrTarget.textContent = name; instrErr.textContent = '';
    instrForm.elements.text.value = '';
    instrForm.elements.enabled.checked = true;
    instrDlg.showModal();
    try {
      const r = await fetch('/campaigns/' + encodeURIComponent(slug) + '/instructions');
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.instructions) {
        instrForm.elements.text.value = d.instructions.text || '';
        instrForm.elements.enabled.checked = d.instructions.enabled !== false;
      }
    } catch (e) { /* leave blank on fetch failure */ }
  }
  function setBannerMode(mode) {
    banMode = mode;
    banForm.querySelectorAll('.banner-tabs button').forEach(b => {
      b.classList.toggle('on', b.dataset.mode === mode);
    });
    banForm.querySelectorAll('[data-mode-panel]').forEach(p => {
      p.hidden = p.dataset.modePanel !== mode;
    });
  }
  banForm.querySelectorAll('.banner-tabs button').forEach(b => {
    b.addEventListener('click', () => setBannerMode(b.dataset.mode));
  });

  function wireClose(dlg) {
    dlg.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => dlg.close()));
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
  }
  [newDlg, impDlg, delDlg, banDlg, instrDlg].forEach(wireClose);

  newBtn.addEventListener('click', () => {
    newErr.textContent = ''; newForm.reset();
    newForm.elements.tone.value = 'high fantasy';
    newDlg.showModal();
  });
  importBtn.addEventListener('click', () => { impErr.textContent = ''; impForm.reset(); impDlg.showModal(); });

  newForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    newErr.textContent = '';
    const fd = new FormData(newForm);
    const r = await fetch('/campaigns/create', {method: 'POST', body: fd});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { newErr.textContent = d.error || 'Failed.'; return; }
    newDlg.close();
    await refresh();
  });
  impForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    impErr.textContent = '';
    const fd = new FormData(impForm);
    if (!fd.get('force')) fd.delete('force');
    const r = await fetch('/campaigns/import', {method: 'POST', body: fd});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { impErr.textContent = d.error || 'Failed.'; return; }
    impDlg.close();
    await refresh();
  });
  delForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    delErr.textContent = '';
    if (!delSlug) return;
    if (delForm.elements.confirm.value.trim() !== delSlug) {
      delErr.textContent = 'Slug confirmation does not match.'; return;
    }
    const r = await fetch('/campaigns/' + encodeURIComponent(delSlug) + '/delete', {method: 'POST'});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { delErr.textContent = d.error || 'Failed.'; return; }
    delDlg.close();
    await refresh();
  });
  banForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    banErr.textContent = '';
    if (!banSlug) return;
    banSubmit.disabled = true;
    const origTxt = banSubmit.textContent;
    banSubmit.textContent = banMode === 'generate' ? 'Generating…' : 'Uploading…';
    try {
      let resp;
      if (banMode === 'upload') {
        const file = banForm.elements.image.files[0];
        if (!file) { banErr.textContent = 'Pick an image first.'; return; }
        const fd = new FormData(); fd.append('image', file);
        resp = await fetch('/campaigns/' + encodeURIComponent(banSlug) + '/banner', {method: 'POST', body: fd});
      } else {
        const hint = banForm.elements.hint.value.trim();
        resp = await fetch('/campaigns/' + encodeURIComponent(banSlug) + '/banner', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({mode: 'generate', hint}),
        });
      }
      const d = await resp.json().catch(() => ({}));
      if (!resp.ok) { banErr.textContent = d.error || 'Failed.'; return; }
      banDlg.close();
      await refresh();
    } finally {
      banSubmit.disabled = false;
      banSubmit.textContent = origTxt;
    }
  });

  instrForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    instrErr.textContent = '';
    if (!instrSlug) return;
    instrSubmit.disabled = true;
    const origTxt = instrSubmit.textContent;
    instrSubmit.textContent = 'Saving…';
    try {
      const r = await fetch('/campaigns/' + encodeURIComponent(instrSlug) + '/instructions', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          text: instrForm.elements.text.value,
          enabled: instrForm.elements.enabled.checked,
        }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { instrErr.textContent = d.error || 'Failed.'; return; }
      instrDlg.close();
    } finally {
      instrSubmit.disabled = false;
      instrSubmit.textContent = origTxt;
    }
  });

  refresh();
})();
</script>
"""
    return render("Campaigns", body)


@app.route("/api/campaigns.json")
def api_campaigns():
    return jsonify({"campaigns": _arch.list_summaries()})


@app.route("/campaigns/<slug>/banner")
def campaign_banner(slug: str):
    if not re.match(r"^[a-z0-9][a-z0-9\-_]*$", slug):
        abort(400)
    p = _arch.banner_path(slug)
    if not p:
        abort(404)
    return send_file(str(p))


@app.route("/campaigns/<slug>/instructions", methods=["GET", "POST"])
def campaign_instructions(slug: str):
    """Read (GET) or persist (POST) a campaign's binding instructions directive
    without needing to switch to it. POST body: {"text": <str>, "enabled":
    <bool>}. Takes effect on that campaign's next DM turn."""
    if not re.match(r"^[a-z0-9][a-z0-9\-_]*$", slug):
        return jsonify({"error": "invalid slug"}), 400
    if not (Path(__file__).parent / "campaigns" / slug / "campaign.json").is_file():
        return jsonify({"error": "campaign not found"}), 404
    if request.method == "GET":
        return jsonify({"instructions": _dm.instructions_setting(slug)})
    data = request.get_json(silent=True) or {}
    setting = _dm.set_campaign_instructions(
        text=data.get("text"), enabled=data.get("enabled"), slug=slug)
    return jsonify({"ok": True, "instructions": setting})


@app.route("/campaigns/<slug>/switch", methods=["POST"])
def campaign_switch(slug: str):
    if not re.match(r"^[a-z0-9][a-z0-9\-_]*$", slug):
        return jsonify({"error": "invalid slug"}), 400
    camp_dir = Path(__file__).parent / "campaigns" / slug
    if not (camp_dir / "campaign.json").is_file():
        return jsonify({"error": "campaign not found"}), 404
    try:
        _activate_and_reload(slug)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    # Session ids are stored per-campaign (campaigns/<slug>/.dm_session),
    # so the next /play turn naturally resumes whatever conversation was
    # last active for the destination campaign — no reset needed.
    return jsonify({"ok": True, "slug": slug})


@app.route("/campaigns/create", methods=["POST"])
def campaign_create():
    name = (request.form.get("name") or "").strip()
    world = (request.form.get("world") or "").strip()
    tone = (request.form.get("tone") or "high fantasy").strip()
    try:
        initial_gp = int(request.form.get("initial_gp") or 0)
    except ValueError:
        initial_gp = 0
    if not name or not world:
        return jsonify({"error": "name and world are required"}), 400

    slug = re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-"))
    if not slug:
        return jsonify({"error": "name does not produce a valid slug"}), 400
    camp_root = Path(__file__).parent / "campaigns"
    camp_dir = camp_root / slug
    if camp_dir.exists():
        return jsonify({"error": f"campaign already exists: {slug}"}), 409

    # Mirror the MCP create_campaign tool's scaffolding so the two paths
    # produce identical campaign.json shapes.
    from tools.campaign_mgmt import _CAMPAIGN_SCHEMA
    for sub in ("", "characters", "locations", "images"):
        (camp_dir / sub).mkdir(parents=True, exist_ok=True)
    data = dict(_CAMPAIGN_SCHEMA)
    data["name"] = name
    data["world"] = world
    data["tone"] = tone
    data["initial_coin"] = {"pp": 0, "gp": initial_gp, "ep": 0, "sp": 0, "cp": 0}
    data["encounter_tables"] = {k: list(v) for k, v in _CAMPAIGN_SCHEMA["encounter_tables"].items()}
    data["encounter_frequency"] = {k: dict(v) for k, v in _CAMPAIGN_SCHEMA["encounter_frequency"].items()}
    data["characters"] = {}
    data["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _c.atomic_write_text(
        camp_dir / "campaign.json",
        json.dumps(data, indent=2, ensure_ascii=False),
    )
    log_path = camp_dir / "adventure_log.md"
    if not log_path.exists():
        log_path.write_text(f"# {name} — Adventure Log\n\n", encoding="utf-8")
    _activate_and_reload(slug)
    # No session reset needed — the new campaign has no per-campaign
    # .dm_session yet, so the first turn will start a fresh Claude
    # session and persist its id under campaigns/<slug>/.dm_session.
    return jsonify({"ok": True, "slug": slug})


@app.route("/campaigns/<slug>/delete", methods=["POST"])
def campaign_delete(slug: str):
    if not re.match(r"^[a-z0-9][a-z0-9\-_]*$", slug):
        return jsonify({"error": "invalid slug"}), 400
    # Block deleting the active campaign — switching first prevents the
    # dashboard from holding references to a deleted directory in ``cfg``.
    try:
        active = (Path(__file__).parent / ".active").read_text(encoding="utf-8").strip()
    except OSError:
        active = ""
    if slug == active:
        return jsonify({"error": "cannot delete the active campaign — switch first"}), 409
    try:
        _arch.delete_campaign(slug)
    except FileNotFoundError:
        return jsonify({"error": "campaign not found"}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/campaigns/<slug>/export.tgz")
def campaign_export(slug: str):
    if not re.match(r"^[a-z0-9][a-z0-9\-_]*$", slug):
        abort(400)
    import tempfile
    tmp = tempfile.NamedTemporaryFile(prefix=f"{slug}-", suffix=".tgz", delete=False)
    tmp.close()
    try:
        _arch.export_campaign(slug, Path(tmp.name))
    except FileNotFoundError:
        Path(tmp.name).unlink(missing_ok=True)
        abort(404)
    resp = send_file(
        tmp.name,
        mimetype="application/gzip",
        as_attachment=True,
        download_name=f"{slug}.tgz",
    )
    # Best-effort cleanup once the response is sent — Flask streams the file
    # before we get a chance here, so schedule the unlink via call_on_close.
    @resp.call_on_close
    def _cleanup():
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except OSError:
            pass
    return resp


@app.route("/campaigns/import", methods=["POST"])
def campaign_import():
    f = request.files.get("archive")
    if not f or not f.filename:
        return jsonify({"error": "archive file is required"}), 400
    rename = (request.form.get("rename") or "").strip() or None
    force = request.form.get("force") is not None
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
        f.save(tmp.name)
        path = Path(tmp.name)
    try:
        slug = _arch.import_campaign(path, rename=rename, force=force)
    except FileExistsError as exc:
        return jsonify({"error": str(exc)}), 409
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        path.unlink(missing_ok=True)
    return jsonify({"ok": True, "slug": slug})


@app.route("/campaigns/<slug>/banner", methods=["POST"])
def campaign_set_banner(slug: str):
    if not re.match(r"^[a-z0-9][a-z0-9\-_]*$", slug):
        return jsonify({"error": "invalid slug"}), 400
    camp_root = Path(__file__).parent / "campaigns" / slug
    if not (camp_root / "campaign.json").is_file():
        return jsonify({"error": "campaign not found"}), 404

    if request.is_json:
        # AI-generated banner path: use the campaign's world+tone as the
        # painter's brief, plus optional player-side hint. Reuses the same
        # Flux pipeline as /play/generate_scene so the visual style stays
        # consistent with scene illustrations.
        data = request.get_json(silent=True) or {}
        if data.get("mode") != "generate":
            return jsonify({"error": "unsupported JSON mode"}), 400
        camp_json = json.loads((camp_root / "campaign.json").read_text(encoding="utf-8"))
        world = (camp_json.get("world") or "").strip()
        tone = (camp_json.get("tone") or "high fantasy").strip()
        hint = (data.get("hint") or "").strip()
        if not world:
            return jsonify({"error": "campaign has no world description to generate from"}), 400
        try:
            replicate = _img._load_replicate()
        except (ImportError, EnvironmentError) as exc:
            return jsonify({"error": str(exc)}), 500
        tone_clause = _img.TONE_STYLES.get(tone, tone)
        brief = (
            f"Wide cinematic landscape banner, no characters in foreground. "
            f"Setting: {world}"
        )
        if hint:
            brief += f" Emphasis: {hint}"
        full_prompt = f"{_img.STYLE_BASE} {tone_clause}. {brief}"[:1500]
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_dir = Path(td)
            tmp_name = "banner.png"
            try:
                _img._generate_image(replicate, full_prompt, tmp_dir, tmp_name)
            except Exception as exc:
                return jsonify({"error": str(exc)}), 500
            try:
                rel = _arch.set_banner(slug, (tmp_dir / tmp_name).read_bytes(), ".png")
            except (FileNotFoundError, ValueError) as exc:
                return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "banner": rel, "prompt": brief})

    # Multipart upload path.
    f = request.files.get("image")
    if not f or not f.filename:
        return jsonify({"error": "image file is required"}), 400
    mime = (f.mimetype or "").lower()
    suffix = _BANNER_EXT_BY_MIME.get(mime)
    if not suffix:
        return jsonify({"error": f"unsupported image type: {mime}"}), 400
    blob = f.read(_BANNER_MAX_BYTES + 1)
    if len(blob) > _BANNER_MAX_BYTES:
        return jsonify({"error": "image exceeds 8 MB limit"}), 413
    try:
        rel = _arch.set_banner(slug, blob, suffix)
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "banner": rel})


@app.route("/sessions")
def sessions_index():
    sdir = _sessions_dir()
    if not sdir.is_dir():
        body = "<h1>Sessions</h1><p class='muted'>No transcript directory found.</p>"
        return render("Sessions", body)

    slug = _active_campaign_slug()
    if not slug:
        body = "<h1>Sessions</h1><p class='muted'>No active campaign — switch one in on /campaigns first.</p>"
        return render("Sessions", body)
    files = _active_campaign_session_paths()
    campaign_name = cfg.get("name", slug) if cfg else slug
    rows = []
    for p in files:
        st = p.stat()
        when = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        size_kb = st.st_size / 1024
        teaser = html.escape(_session_teaser(p))
        sid = p.stem
        sid_safe = html.escape(sid)
        rows.append(
            f'<tr>'
            f'<td><a href="/sessions/{sid_safe}"><code>{html.escape(sid[:8])}</code></a></td>'
            f'<td class="muted">{when}</td>'
            f'<td class="muted" style="text-align:right">{size_kb:,.1f} KB</td>'
            f'<td>{teaser or "<span class=muted>—</span>"}</td>'
            f'<td style="text-align:center;width:36px;padding:4px">'
            f'<button class="session-delete" type="button" data-sid="{sid_safe}" '
            f'title="Delete session" aria-label="Delete this session">×</button>'
            f'</td>'
            f'</tr>'
        )

    if not rows:
        body = (
            f"<h1>Sessions — {html.escape(campaign_name)}</h1>"
            f"<p class='muted'>No sessions recorded for this campaign yet.</p>"
        )
    else:
        body = (
            "<style>"
            ".session-delete {"
            "  width: 24px; height: 24px; padding: 0;"
            "  border-radius: 50%;"
            "  background: transparent;"
            "  border: 1px solid var(--rule);"
            "  color: var(--ink-muted);"
            "  font-family: var(--font-body); font-size: 1.0em; line-height: 1;"
            "  cursor: pointer;"
            "  display: inline-flex; align-items: center; justify-content: center;"
            "  opacity: 0.55;"
            "  transition: opacity 180ms ease, color 180ms ease, border-color 180ms ease, transform 180ms ease;"
            "}"
            ".session-delete:hover {"
            "  opacity: 1; color: #e8a090; border-color: var(--accent-rust);"
            "  transform: scale(1.08);"
            "}"
            ".session-delete:disabled { opacity: 0.3; cursor: not-allowed; }"
            ".session-search-bar {"
            "  display: flex; align-items: baseline; gap: 12px;"
            "  margin: 8px 0 14px;"
            "}"
            ".session-search-bar input[type=search] {"
            "  flex: 1; max-width: 480px;"
            "  background: var(--bg-rec); color: var(--ink-body);"
            "  border: 1px solid var(--rule); border-radius: 3px;"
            "  padding: 6px 10px; font-family: var(--font-body); font-size: 0.95em;"
            "}"
            ".session-search-bar input[type=search]:focus {"
            "  outline: none; border-color: var(--accent-gold);"
            "}"
            "#session-search-status { font-size: 0.85em; }"
            ".search-session { margin-bottom: 12px; padding: 10px 14px; }"
            ".search-snippets { list-style: none; padding: 0; margin: 6px 0 0; }"
            ".search-snippets li {"
            "  padding: 4px 0; border-top: 1px solid var(--rule);"
            "  font-size: 0.92em; line-height: 1.45;"
            "}"
            ".search-snippets li:first-child { border-top: none; }"
            ".search-snippets .role-tag {"
            "  display: inline-block; min-width: 52px;"
            "  font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.1em;"
            "  color: var(--ink-muted); margin-right: 6px;"
            "}"
            ".search-snippets mark {"
            "  background: rgba(212,175,55,0.28); color: inherit;"
            "  padding: 0 2px; border-radius: 2px;"
            "}"
            "</style>"
            f"<h1>Sessions — {html.escape(campaign_name)}</h1>"
            f"<p class='muted'>{len(rows)} session(s) in this campaign. Newest first.</p>"
            '<div class="session-search-bar">'
            '  <input type="search" id="session-search" placeholder="Search across all sessions…" autocomplete="off">'
            '  <span id="session-search-status" class="muted"></span>'
            '</div>'
            '<div id="session-search-results" hidden></div>'
            '<div id="session-table-wrap" class="card"><table style="width:100%;border-collapse:collapse">'
            "<thead><tr>"
            '<th style="text-align:left;padding:6px 8px">ID</th>'
            '<th style="text-align:left;padding:6px 8px">When</th>'
            '<th style="text-align:right;padding:6px 8px">Size</th>'
            '<th style="text-align:left;padding:6px 8px">First prompt</th>'
            '<th style="width:36px"></th>'
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div>"
            "<script>(function(){"
            "document.querySelectorAll('.session-delete').forEach(function(btn){"
            "  btn.addEventListener('click', async function(e){"
            "    e.stopPropagation();"
            "    const sid = btn.dataset.sid;"
            "    if (!sid) return;"
            "    if (!confirm('Delete session ' + sid.slice(0,8) + '?')) return;"
            "    btn.disabled = true;"
            "    try {"
            "      const r = await fetch('/api/sessions/' + encodeURIComponent(sid), {method:'DELETE'});"
            "      if (!r.ok) {"
            "        const d = await r.json().catch(()=>({}));"
            "        alert('Delete failed: ' + (d.error || ('HTTP '+r.status)));"
            "        return;"
            "      }"
            "      const row = btn.closest('tr');"
            "      if (row) row.remove();"
            "    } catch (err) {"
            "      alert('Delete failed: ' + err);"
            "    } finally {"
            "      btn.disabled = false;"
            "    }"
            "  });"
            "});"
            "function escapeHtml(s){"
            "  return String(s).replace(/[&<>\"']/g, function(c){"
            "    return ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'})[c];"
            "  });"
            "}"
            "function highlight(text, terms){"
            "  let out = escapeHtml(text);"
            "  for (const t of terms) {"
            "    if (!t) continue;"
            "    const esc = t.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');"
            "    out = out.replace(new RegExp('(' + esc + ')', 'gi'), '<mark>$1</mark>');"
            "  }"
            "  return out;"
            "}"
            "const search = document.getElementById('session-search');"
            "const status = document.getElementById('session-search-status');"
            "const results = document.getElementById('session-search-results');"
            "const tableWrap = document.getElementById('session-table-wrap');"
            "let timer = null, lastReq = 0;"
            "function showTable(){ results.hidden = true; results.innerHTML = ''; tableWrap.hidden = false; status.textContent = ''; }"
            "function render(data){"
            "  if (!data.results || data.results.length === 0) {"
            "    results.innerHTML = '<p class=\"muted\">No matches for <em>' + escapeHtml(data.query) + '</em>.</p>';"
            "    results.hidden = false; return;"
            "  }"
            "  const terms = data.terms || [];"
            "  const blocks = data.results.map(function(r){"
            "    const items = r.matches.map(function(m){"
            "      const label = m.role === 'dm' ? 'DM' : 'Player';"
            "      return '<li><span class=\"role-tag\">' + label + '</span>' + highlight(m.snippet, terms) + '</li>';"
            "    }).join('');"
            "    const more = r.match_count > r.matches.length"
            "      ? ' <span class=\"muted\">(+' + (r.match_count - r.matches.length) + ' more)</span>'"
            "      : '';"
            "    return '<div class=\"search-session card\">'"
            "      + '<div><a href=\"/sessions/' + encodeURIComponent(r.sid) + '\"><code>'"
            "      + escapeHtml(r.sid.slice(0, 8)) + '</code></a> '"
            "      + '<span class=\"muted\">' + escapeHtml(r.when) + '</span>' + more + '</div>'"
            "      + '<ul class=\"search-snippets\">' + items + '</ul></div>';"
            "  }).join('');"
            "  results.innerHTML = '<p class=\"muted\">' + data.results.length + ' session(s) match.</p>' + blocks;"
            "  results.hidden = false;"
            "}"
            "search.addEventListener('input', function(){"
            "  const q = search.value.trim();"
            "  clearTimeout(timer);"
            "  if (q.length < 2) { showTable(); return; }"
            "  status.textContent = 'searching…';"
            "  timer = setTimeout(async function(){"
            "    const reqId = ++lastReq;"
            "    try {"
            "      const r = await fetch('/api/sessions/search?q=' + encodeURIComponent(q));"
            "      if (reqId !== lastReq) return;"
            "      if (!r.ok) { status.textContent = 'error'; return; }"
            "      const data = await r.json();"
            "      tableWrap.hidden = true;"
            "      status.textContent = '';"
            "      render(data);"
            "    } catch (err) { status.textContent = 'error'; }"
            "  }, 250);"
            "});"
            "})();</script>"
        )
    return render("Sessions", body)


@app.route("/sessions/<sid>")
def session_view(sid: str):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", sid):
        abort(404)
    sdir = _sessions_dir()
    path = sdir / f"{sid}.jsonl"
    if not path.is_file():
        abort(404)

    include_tools = request.args.get("tools", "0") not in ("0", "", "false")
    dm_only = request.args.get("dm_only", "0") not in ("0", "", "false")

    turns = _export.parse_session(path)
    md = _export.render(
        turns,
        include_tools=include_tools,
        dm_only=dm_only,
        player_only=False,
        show_timestamps=True,
    )
    content_html = _markdown_to_html(md)

    when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    toggle = (
        f'<a href="/sessions/{html.escape(sid)}?tools={"0" if include_tools else "1"}'
        f'{"&dm_only=1" if dm_only else ""}">'
        f'{"Hide" if include_tools else "Show"} tool calls</a> · '
        f'<a href="/sessions/{html.escape(sid)}?dm_only={"0" if dm_only else "1"}'
        f'{"&tools=1" if include_tools else ""}">'
        f'{"Show all turns" if dm_only else "DM only"}</a>'
    )

    is_active = _dm.session_id() == sid
    resume_label = "Already active in /play" if is_active else "Resume in /play"
    resume_btn = (
        f'<button id="resume-sess" data-sid="{html.escape(sid)}" '
        f'style="padding:5px 12px; background:transparent; '
        f'border:1px solid var(--rule-hi); color:var(--ink-body); '
        f'border-radius:3px; cursor:pointer; font-family:var(--font-display); '
        f'font-size:0.78em; text-transform:uppercase; letter-spacing:0.12em;"'
        f'{"" if not is_active else " disabled"}>{resume_label}</button>'
    )
    resume_js = """
<script>
(function(){
  const b = document.getElementById('resume-sess');
  if (!b || b.disabled) return;
  b.addEventListener('click', async () => {
    const sid = b.dataset.sid;
    const orig = b.textContent;
    b.disabled = true; b.textContent = 'Activating…';
    try {
      const r = await fetch('/api/sessions/' + encodeURIComponent(sid) + '/activate', {method: 'POST'});
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { b.textContent = d.error || 'Failed'; setTimeout(() => { b.textContent = orig; b.disabled = false; }, 1800); return; }
      b.textContent = 'Active — opening /play…';
      setTimeout(() => { window.location.href = '/play'; }, 400);
    } catch (e) {
      b.textContent = 'Failed'; setTimeout(() => { b.textContent = orig; b.disabled = false; }, 1800);
    }
  });
})();
</script>
"""

    body = (
        f"<h1>Session <code>{html.escape(sid[:8])}</code></h1>"
        f"<p class='muted'>{when} · {toggle} · "
        f'<a href="/sessions">← back to index</a></p>'
        f'<p>{resume_btn}</p>'
        f'<div class="card">{content_html}</div>'
        f'{resume_js}'
    )
    return render(f"Session {sid[:8]}", body)


# ---------------------------------------------------------------------------
# Live play surface — drives `claude -p` via tools/dm_session.py
# ---------------------------------------------------------------------------

def _latest_location_name() -> str:
    """Most recent location_visited from events.json, or empty string."""
    if not cfg:
        return ""
    p = cfg["_dir"] / "events.json"
    if not p.exists():
        return ""
    try:
        events = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    for ev in reversed(events):
        if ev.get("type") == "location_visited":
            return ev.get("name") or ev.get("slug") or ""
    return ""


# ---------------------------------------------------------------------------
# Fog of war — the player-facing view (?fog=1) hides what the party has not
# yet discovered: unvisited locations, and DM-only tactical-map detail
# (trapped/locked doors, un-sprung floor traps). The DM's plain URLs are
# unaffected, so the operator always sees everything; only links carrying
# ?fog=1 (e.g. those handed to players) get fogged.
# ---------------------------------------------------------------------------

def _fog_on() -> bool:
    """True when the current request asks for the player-facing fog view."""
    return request.args.get("fog", "").strip().lower() in ("1", "true", "yes", "on")


def _fog_qs() -> str:
    """'?fog=1' when fog is active, else '' — for propagating the mode through
    in-page links so navigation stays in player view."""
    return "?fog=1" if _fog_on() else ""


def _visited_slugs() -> set[str]:
    """Slugs of locations/areas the party has visited.

    Sourced from ``location_visited`` events (emitted by create_area /
    create_location). Both area slugs and location slugs land in the same
    set; callers disambiguate by where they look the slug up.
    """
    out: set[str] = set()
    if not cfg:
        return out
    p = cfg["_dir"] / "events.json"
    if not p.exists():
        return out
    try:
        events = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return out
    for ev in events:
        if ev.get("type") == "location_visited":
            slug = ev.get("slug")
            if slug:
                out.add(slug)
    return out


def _play_state() -> dict:
    """Assemble the live data the /play surface needs: party (with portraits
    and HP), plus the equivalent of the statusline footer (location, time,
    weather, day, session, combat, coin)."""
    if not cfg:
        return {"campaign": None, "characters": [], "session": 0, "day": 0,
                "time": "", "weather": "", "location": "", "in_combat": False, "coin": ""}

    _reload_cfg()
    state = _c.load_state(cfg)
    combat_hp = _combat_hp_overrides()
    image_index = _load_image_index()

    chars = []
    for key, char in cfg.get("characters", {}).items():
        cstate = state.get("characters", {}).get(key, {})
        hp_max = char.get("hp_max", 0)
        hp_cur = combat_hp.get(key, cstate.get("hp", hp_max))
        portrait = _find_portrait(char.get("label", key), image_index, key)
        level = int(char.get("level", 1) or 1)
        xp = int(cstate.get("xp", 0) or 0)
        xp_table = _class_xp_table(char.get("cls", ""))
        xp_floor = 0
        xp_next = None
        if xp_table and 1 <= level <= len(xp_table):
            xp_floor = xp_table[level - 1]
            if level < len(xp_table):
                xp_next = xp_table[level]
        chars.append({
            "key": key,
            "label": char.get("label", key),
            "cls": char.get("cls", ""),
            "alignment": _alignment_abbrev(char.get("alignment") or ""),
            "level": level,
            "hp": hp_cur,
            "hp_max": hp_max,
            "ac": char.get("ac"),
            "thac0": char.get("thac0"),
            "xp": xp,
            "xp_floor": xp_floor,
            "xp_next": xp_next,
            "conditions": cstate.get("conditions", []),
            "portrait": f"/images/{portrait['filename']}" if portrait else None,
        })

    coin = state.get("coin", {}) or {}
    # Show every non-zero denomination; if the party is genuinely broke,
    # surface "0 gp" rather than blank — players want to see the zero.
    coin_parts = [f"{coin[d]} {d}" for d in ("pp", "gp", "ep", "sp", "cp") if coin.get(d)]

    hour = state.get("current_hour")
    minute = state.get("current_minute")
    time_str = f"{hour:02d}:{minute:02d}" if isinstance(hour, int) and isinstance(minute, int) else ""
    weather_full = state.get("current_weather") or ""
    weather_label = weather_full.split(" (", 1)[0] if weather_full else ""

    return {
        "campaign": {"name": cfg.get("name"), "slug": cfg.get("_name")},
        "session": state.get("current_session", 0),
        "day": state.get("current_day", 1),
        "time": time_str,
        "weather": weather_label,
        "location": _latest_location_name(),
        "in_combat": bool(state.get("combat")),
        "coin": " · ".join(coin_parts) if coin_parts else "0 gp",
        "characters": chars,
    }


_PLAY_HTML = """
<style>
  /* Take the entire viewport on /play — narrative window grows to fill all
     remaining width to the left of the sidebar. The :has() check scopes this
     to the play page so other routes keep the standard 980px container. */
  .container:has(.play-shell) {
    max-width: none;
    width: 100%;
    padding: 20px 28px;
    height: calc(100vh - 50px);   /* viewport minus sticky nav */
    display: flex;
    flex-direction: column;
  }
  /* Suppress the page-level scrollbar on /play — the chat log owns its
     own scroll. */
  body:has(.play-shell) { overflow: hidden; }
  .play-shell {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 320px;
    grid-template-rows: 100%;
    gap: 18px;
    flex: 1;
    min-height: 0;               /* allow inner overflow:auto to actually scroll */
  }
  .play-main { display: flex; flex-direction: column; min-width: 0; min-height: 0; position: relative; }
  .jump-btn {
    position: absolute;
    right: 22px;
    bottom: 130px;
    z-index: 5;
    width: 34px; height: 34px;
    padding: 0;
    display: inline-flex; align-items: center; justify-content: center;
    background: var(--bg-rec);
    border: 1px solid var(--accent-gold);
    border-radius: 50%;
    color: var(--accent-gold);
    font-size: 1.05em;
    line-height: 1;
    cursor: pointer;
    box-shadow: 0 4px 14px rgba(0,0,0,0.5);
    opacity: 0;
    transform: translateY(8px);
    pointer-events: none;
    transition: opacity 0.18s ease, transform 0.18s ease;
  }
  .jump-btn.visible {
    opacity: 1;
    transform: translateY(0);
    pointer-events: auto;
  }
  .jump-btn:hover { color: var(--accent-gold-hi); border-color: var(--accent-gold-hi); }
  .play-side { min-width: 0; min-height: 0; overflow-y: auto; }

  .play-toolbar {
    display: flex;
    align-items: baseline;
    gap: 14px;
    margin-bottom: 14px;
  }
  .play-toolbar h1 {
    font-size: 1.45em;
    border-bottom: none;
    padding: 0;
    margin: 0;
    flex: 0 0 auto;
    color: var(--ink-display);
    letter-spacing: 0.18em;
  }
  .play-toolbar .sid {
    color: var(--ink-muted);
    font-size: 0.82em;
    font-family: var(--font-display);
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }
  .play-toolbar .sid code {
    color: var(--accent-gold);
    font-family: var(--font-mono);
    text-transform: none;
    letter-spacing: 0;
    margin-left: 6px;
  }
  .play-toolbar .sid .session-no {
    color: var(--ink-display);
    margin-left: 6px;
  }
  .play-toolbar .sid .session-no:empty { display: none; }
  .play-toolbar .sid code.sid-size {
    color: var(--ink-muted);
    margin-left: 8px;
  }
  .play-toolbar .sid code.sid-size:empty { display: none; }
  .play-toolbar .sid code.sid-size.near-reset { color: var(--accent-gold, #d4a14a); font-weight: 600; }
  .play-toolbar .tone-group { display: inline-flex; align-items: center; gap: 4px; }
  .play-toolbar .tone-group .tone-icon { font-size: 0.95em; opacity: 0.8; }
  .play-toolbar #play-tone {
    background: var(--panel-2, #1b1b1b);
    color: var(--ink, #e8e2d0);
    border: 1px solid var(--border, #3a3a3a);
    border-radius: 5px;
    padding: 2px 6px;
    font: inherit;
    font-size: 0.85em;
    cursor: pointer;
  }
  .play-toolbar #play-tone-custom {
    background: var(--panel-2, #1b1b1b);
    color: var(--ink, #e8e2d0);
    border: 1px solid var(--border, #3a3a3a);
    border-radius: 5px;
    padding: 2px 6px;
    font: inherit;
    font-size: 0.85em;
    width: 14ch;
  }
  .play-toolbar #play-tone-custom[hidden] { display: none; }
  .play-toolbar .detail-group { display: inline-flex; align-items: center; gap: 5px; }
  .play-toolbar .detail-group .detail-icon { font-size: 0.95em; opacity: 0.8; }
  .play-toolbar #play-detail { width: 70px; cursor: pointer; accent-color: var(--accent-gold, #c8a24a); vertical-align: middle; }
  .play-toolbar .detail-label {
    font-size: 0.8em;
    opacity: 0.75;
    min-width: 7ch;
    color: var(--ink, #e8e2d0);
  }
  .play-toolbar .instructions-group { display: inline-flex; align-items: center; gap: 5px; }
  .play-toolbar #play-instructions-btn.active { color: var(--accent-gold, #c8a24a); opacity: 1; }
  .play-toolbar #play-instructions-text {
    width: 28ch;
    max-width: 40vw;
    font-size: 0.8em;
    resize: vertical;
    vertical-align: middle;
    color: var(--ink, #e8e2d0);
    background: rgba(0,0,0,0.25);
    border: 1px solid rgba(200,162,74,0.4);
    border-radius: 4px;
    padding: 3px 5px;
  }
  .play-toolbar #play-instructions-text[hidden] { display: none; }
  .play-toolbar #play-instructions-enabled-wrap { font-size: 0.78em; opacity: 0.8; display: inline-flex; align-items: center; gap: 3px; }
  .play-toolbar #play-instructions-enabled-wrap[hidden] { display: none; }
  .play-toolbar .spacer { flex: 1; }
  .play-toolbar button {
    background: transparent;
    color: var(--accent-gold);
    border: 1px solid var(--rule);
    border-radius: 2px;
    padding: 5px 12px;
    font-family: var(--font-display);
    font-size: 0.7em;
    text-transform: uppercase;
    letter-spacing: 0.18em;
    cursor: pointer;
    transition: all 180ms ease;
  }
  .play-toolbar button:hover {
    border-color: var(--accent-gold);
    color: var(--accent-gold-hi);
  }
  .play-toolbar #play-status {
    color: var(--ink-muted);
    font-size: 0.82em;
    font-style: italic;
    min-width: 80px;
  }

  /* Toolbar view-controls: filter pills + font-size buttons clustered
     together, with a small gap between the two groups. */
  .view-controls { display: inline-flex; align-items: center; gap: 10px; }
  .filter-group, .font-group { display: inline-flex; gap: 4px; }
  /* Session id + new-session button hug together on the left. */
  .session-group { display: inline-flex; align-items: baseline; gap: 6px; }
  /* Logical clusters of icon buttons (picture, reply-effects, recovery)
     keep tight internal spacing; the toolbar's own gap separates groups. */
  .icon-group { display: inline-flex; align-items: center; gap: 4px; }
  .icon-btn {
    width: 30px;
    height: 26px;
    padding: 0;
    background: transparent;
    border: 1px solid var(--rule);
    border-radius: 2px;
    color: var(--ink-muted);
    font-family: var(--font-body);
    font-size: 0.86em;
    line-height: 1;
    cursor: pointer;
    transition: all 180ms ease;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .icon-btn:hover { color: var(--accent-gold); border-color: var(--rule-hi); }
  .icon-btn:active { background: rgba(200,169,110,0.12); }
  .icon-btn:disabled { opacity: 0.3; cursor: not-allowed; }
  .turn-toggle.icon {
    width: 30px;
    height: 26px;
    padding: 0;
    background: transparent;
    border: 1px solid var(--rule);
    border-radius: 2px;
    color: var(--ink-muted);
    font-family: var(--font-body);
    font-size: 1em;
    line-height: 1;
    letter-spacing: 0;
    cursor: pointer;
    opacity: 0.45;
    transition: all 180ms ease;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .turn-toggle.icon:hover { opacity: 0.85; border-color: var(--rule-hi); }
  .turn-toggle.icon.active {
    opacity: 1;
    color: var(--accent-gold);
    border-color: var(--accent-gold);
    background: rgba(200,169,110,0.08);
  }

  /* Visibility filters applied to .play-log via JS (toggle pills + localStorage). */
  .play-log.hide-tools .turn.tool { display: none; }
  .play-log.hide-meta  .turn.meta { display: none; }
  .play-log.hide-player .turn.player { display: none; }

  .play-log {
    flex: 1 1 0;
    min-height: 0;
    overflow-y: auto;
    background:
      linear-gradient(135deg, rgba(255,220,180,0.022), transparent 50%),
      linear-gradient(to bottom, var(--bg-card-hi), var(--bg-card));
    border: 1px solid var(--rule);
    border-radius: 4px;
    padding: 24px 30px;
    box-shadow: var(--inset-warm), var(--shadow-card);
    line-height: 1.72;
    font-size: 1.02em;
  }
  .play-log .empty-state {
    text-align: center;
    color: var(--ink-muted);
    font-style: italic;
    margin: 24px auto 0;
    max-width: 520px;
  }
  .play-log .empty-state .play-hero {
    display: block;
    width: min(360px, 70%);
    margin: 0 auto 20px;
    border-radius: 4px;
    border: 1px solid var(--rule-hi);
    box-shadow: 0 8px 28px rgba(0,0,0,0.55), inset 0 0 0 1px rgba(0,0,0,0.4);
    cursor: default;
    opacity: 0.96;
  }
  .play-log .empty-state .empty-state-text {
    font-size: 0.95em;
    letter-spacing: 0.02em;
    color: var(--ink-muted);
  }
  .play-log .empty-state .empty-state-text::before {
    content: '✦';
    color: var(--accent-gold);
    font-style: normal;
    margin-right: 8px;
    font-size: 0.9em;
  }
  .turn { margin-bottom: 18px; }
  .turn.player {
    border-left: 2px solid var(--accent-gold);
    padding: 2px 0 2px 16px;
    color: #c8b48a;
    font-style: italic;
    white-space: pre-wrap;
  }
  .turn.player::before {
    content: '◇';
    color: var(--accent-gold);
    margin-right: 8px;
    font-style: normal;
    font-size: 0.85em;
  }
  .turn.dm {
    color: var(--ink-body);
    position: relative;
  }
  .dm-controls {
    display: flex; align-items: center; gap: 8px;
    margin-top: 8px;
    font-size: 0.78em;
    color: var(--ink-muted);
    opacity: 0.4;
    transition: opacity 0.15s ease;
  }
  .turn.dm:hover .dm-controls,
  .dm-controls.active { opacity: 1; }
  .narrate-btn {
    width: 24px; height: 24px; padding: 0;
    border: 1px solid var(--rule);
    background: transparent;
    color: var(--ink-muted);
    border-radius: 50%;
    font-size: 0.85em; line-height: 1;
    cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center;
    transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
  }
  .narrate-btn:hover {
    color: var(--accent-gold);
    border-color: var(--accent-gold);
  }
  .narrate-btn[disabled] { opacity: 0.5; cursor: progress; }
  .narrate-btn.playing { color: var(--accent-gold); border-color: var(--accent-gold); }
  .narrate-meta { font-style: italic; }
  .narrate-meta .seg-tag {
    display: inline-block;
    padding: 0 4px;
    margin: 0 2px;
    border: 1px solid var(--rule);
    border-radius: 2px;
    color: var(--ink-muted);
    font-size: 0.92em;
    font-style: normal;
  }
  .narrate-meta .seg-tag.seg-narration { border-color: var(--rule-hi); }
  .narrate-meta .seg-tag.seg-speech    { border-color: var(--accent-gold); color: var(--accent-gold); }
  dialog.voice-dialog {
    background: var(--bg-rec); color: var(--ink-body);
    border: 1px solid var(--rule); border-radius: 4px;
    padding: 18px 22px; min-width: 360px; max-width: 520px;
  }
  dialog.voice-dialog::backdrop { background: rgba(0,0,0,0.55); }
  dialog.voice-dialog h3 { margin: 0 0 12px; font-family: var(--font-display); }
  dialog.voice-dialog table { width: 100%; border-collapse: collapse; }
  dialog.voice-dialog td { padding: 4px 6px; vertical-align: middle; }
  dialog.voice-dialog td.label { color: var(--ink-muted); font-size: 0.9em; }
  dialog.voice-dialog select {
    width: 100%;
    background: var(--bg-rec); color: var(--ink-body);
    border: 1px solid var(--rule); border-radius: 2px;
    padding: 3px 6px; font-family: var(--font-body); font-size: 0.92em;
  }
  dialog.voice-dialog .row-divider td {
    border-top: 1px solid var(--rule); padding-top: 8px;
  }
  dialog.voice-dialog .actions {
    display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px;
  }
  dialog.debug-dialog {
    background: var(--bg-rec); color: var(--ink-body);
    border: 1px solid var(--rule); border-radius: 4px;
    padding: 18px 22px;
    width: min(960px, 90vw);
    max-height: 80vh;
  }
  /* Only become a flex column once the dialog is actually open — leaving
     ``display`` unset on the closed state preserves the browser's default
     ``display: none`` so the dialog isn't permanently superimposed. */
  dialog.debug-dialog[open] {
    display: flex;
    flex-direction: column;
  }
  dialog.debug-dialog::backdrop { background: rgba(0,0,0,0.55); }
  dialog.debug-dialog h3 { margin: 0 0 8px; font-family: var(--font-display); }
  dialog.debug-dialog p.muted { font-size: 0.85em; margin-bottom: 10px; }
  dialog.debug-dialog .debug-pre {
    flex: 1 1 auto; min-height: 240px;
    overflow: auto;
    background: var(--bg-deep);
    border: 1px solid var(--rule);
    border-radius: 3px;
    padding: 10px 12px;
    font-family: var(--font-mono);
    font-size: 0.78em;
    line-height: 1.45;
    color: #c8b88a;
    white-space: pre-wrap;
    word-break: break-word;
  }
  dialog.debug-dialog .actions {
    display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px;
  }
  dialog.debug-dialog button {
    padding: 5px 12px;
    background: transparent;
    border: 1px solid var(--rule-hi);
    color: var(--ink-body);
    border-radius: 3px;
    cursor: pointer;
    font-family: var(--font-display);
    font-size: 0.78em;
    text-transform: uppercase;
    letter-spacing: 0.12em;
  }
  dialog.debug-dialog button:hover { border-color: var(--accent-gold); color: var(--accent-gold); }
  dialog.debug-dialog .actions { align-items: center; }
  dialog.debug-dialog #debug-force-reset {
    margin-right: auto; border-color: #7a3a3a; color: #b88;
  }
  dialog.debug-dialog #debug-force-reset:hover { border-color: #c66; color: #ecc; }
  #play-debug.debug-on { color: var(--accent-gold); border-color: var(--accent-gold); }
  #play-suggest.suggest-on { color: var(--accent-gold); border-color: var(--accent-gold); }
  #play-bell.bell-on { color: var(--accent-gold); border-color: var(--accent-gold); }
  #play-pin-scroll.pin-on { color: var(--accent-gold); border-color: var(--accent-gold); }
  /* Suggested-action chips appear under the last DM turn when the
     'suggest' toggle is on. Clicking a chip fills the input and submits. */
  .suggest-row {
    display: flex; flex-wrap: wrap; gap: 6px;
    margin: 6px 0 12px;
    align-items: center;
  }
  .suggest-chip {
    padding: 5px 12px;
    background: transparent;
    border: 1px solid var(--rule-hi);
    color: var(--ink-body);
    border-radius: 999px;
    cursor: pointer;
    font-family: inherit;
    font-size: 0.88em;
    line-height: 1.3;
    text-align: left;
    max-width: 100%;
  }
  .suggest-chip::before {
    content: '▸ ';
    color: var(--accent-gold);
    margin-right: 1px;
  }
  .suggest-chip:hover {
    border-color: var(--accent-gold);
    color: var(--accent-gold);
  }
  .suggest-chip:disabled { opacity: 0.5; cursor: progress; }
  .suggest-loading {
    font-size: 0.82em; color: var(--ink-faint); font-style: italic;
  }
  /* Markdown rendered inside DM turns — scope headings/rules/etc so they
     don't carry their full-page sizing into the chat log. */
  .turn.dm > *:first-child { margin-top: 0; }
  .turn.dm > *:last-child { margin-bottom: 0; }
  .turn.dm p { margin: 6px 0; }
  .turn.dm h1, .turn.dm h2, .turn.dm h3, .turn.dm h4 {
    margin: 12px 0 4px;
    border-bottom: none;
    padding: 0;
  }
  .turn.dm h1 { font-size: 1.20em; letter-spacing: 0.08em; }
  .turn.dm h2 { font-size: 1.08em; letter-spacing: 0.06em; }
  .turn.dm h3 { font-size: 1.0em; letter-spacing: 0.05em; }
  .turn.dm h4 { font-size: 0.95em; }
  .turn.dm hr {
    margin: 14px 0;
    height: 1px;
    background: linear-gradient(to right, transparent, var(--rule-hi), transparent);
    border: none;
  }
  /* Character speech: the DM convention is `> "..."` (occasionally
     `> *"..."*`). Style the markdown blockquote so the speech reads as
     distinct from narration — warmer gold-cream tone, subtle gold gradient,
     italic, with a decorative opening curly-quote glyph. */
  .turn.dm blockquote {
    position: relative;
    margin: 12px 0 12px 6px;
    padding: 6px 16px 6px 22px;
    border-left: 3px solid var(--accent-gold);
    background: linear-gradient(to right, rgba(200,169,110,0.07), rgba(200,169,110,0.01) 60%, transparent);
    color: #e6c98e;
    font-style: italic;
    border-radius: 0 2px 2px 0;
  }
  .turn.dm blockquote::before {
    content: '\\201C';   /* left double quote */
    position: absolute;
    left: 6px;
    top: -10px;
    font-family: 'EB Garamond', Georgia, serif;
    font-style: normal;
    font-size: 2.4em;
    line-height: 1;
    color: var(--accent-gold);
    opacity: 0.32;
    pointer-events: none;
  }
  /* Inner <em> (e.g. when the DM writes `> *"..."*`) shouldn't double up
     visually — strip the inner italic so the line stays italicised once. */
  .turn.dm blockquote em { font-style: normal; }
  .turn.dm ul, .turn.dm ol { margin: 6px 0 6px 22px; }
  .turn.dm li { margin: 1px 0; }
  .turn.dm code { color: var(--accent-gold-hi); }
  .turn.dm a { color: var(--accent-gold); }
  /* Tables inside DM turns: tighter than full-page tables, otherwise
     inherits the base styling. */
  .turn.dm table { width: auto; max-width: 100%; font-size: 0.94em; margin: 10px 0; }
  .turn.dm th, .turn.dm td { padding: 5px 10px; vertical-align: top; }
  /* Tool calls render as expandable <details>: summary shows the short
     name + a key=val preview; expanded body shows the full input JSON and
     the tool's result once it lands. */
  details.turn.tool {
    font-family: var(--font-mono);
    font-size: 0.84em;
    color: var(--ink-muted);
    background: rgba(15,11,7,0.55);
    border: 1px solid var(--rule);
    border-left: 2px solid var(--rule-hi);
    border-radius: 0 2px 2px 0;
    margin: 6px 0;
    padding: 0;
  }
  details.turn.tool > summary {
    list-style: none;
    cursor: pointer;
    padding: 5px 12px 5px 10px;
    color: var(--ink-muted);
    display: flex;
    gap: 8px;
    align-items: baseline;
    user-select: none;
    line-height: 1.45;
  }
  details.turn.tool > summary::-webkit-details-marker { display: none; }
  details.turn.tool > summary::before {
    content: '▸';
    color: var(--accent-gold);
    font-size: 0.8em;
    transition: transform 200ms ease;
    flex-shrink: 0;
    width: 10px;
  }
  details.turn.tool[open] > summary::before { transform: rotate(90deg); }
  details.turn.tool[open] > summary {
    border-bottom: 1px solid var(--rule);
    background: rgba(200,169,110,0.04);
  }
  details.turn.tool .tool-name {
    color: var(--accent-gold);
    font-weight: 500;
    flex-shrink: 0;
  }
  details.turn.tool .tool-args {
    color: var(--ink-muted);
    font-size: 0.94em;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
    min-width: 0;
  }
  /* Pretty-printed tool summary (custom formatter output). */
  details.turn.tool .tool-glyph {
    display: inline-block;
    width: 14px;
    margin-right: 6px;
    text-align: center;
    color: var(--accent-gold);
    font-family: var(--font-body);
    flex-shrink: 0;
  }
  details.turn.tool .tool-pretty {
    flex: 1;
    min-width: 0;
    color: var(--ink-body);
    font-family: var(--font-body);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  details.turn.tool .tool-pretty b {
    color: var(--accent-gold-hi);
    font-weight: 500;
  }
  details.turn.tool .tool-pretty .neg { color: #e08070; }
  details.turn.tool .tool-pretty .pos { color: #7ab46e; }
  details.turn.tool .tool-pretty .dim { color: var(--ink-muted); }
  details.turn.tool .tool-body { padding: 10px 14px 12px; }
  details.turn.tool .tool-section + .tool-section { margin-top: 10px; }
  details.turn.tool .tool-section-label {
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.18em;
    font-size: 0.66em;
    color: var(--ink-muted);
    margin-bottom: 4px;
  }
  details.turn.tool .tool-input,
  details.turn.tool .tool-result {
    background: var(--bg-deep);
    border: 1px solid var(--rule);
    border-radius: 2px;
    padding: 8px 11px;
    margin: 0;
    font-family: var(--font-mono);
    font-size: 0.92em;
    color: var(--ink-body);
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 380px;
    overflow-y: auto;
  }
  details.turn.tool .tool-pending {
    color: var(--ink-muted);
    font-style: italic;
  }
  details.turn.tool .tool-error {
    color: #e08070;
    font-style: italic;
  }
  .turn.error {
    color: #e08070;
    border-left: 2px solid var(--accent-blood);
    padding: 6px 12px;
    background: rgba(70,20,15,0.3);
  }
  .turn.meta {
    color: var(--ink-muted);
    font-size: 0.78em;
    text-align: center;
    margin: 18px 0 6px;
    font-family: var(--font-display);
    letter-spacing: 0.18em;
    text-transform: uppercase;
    position: relative;
  }
  .turn.meta::before, .turn.meta::after {
    content: '';
    position: absolute;
    top: 50%;
    width: 25%;
    height: 1px;
    background: linear-gradient(to right, transparent, var(--rule-hi), transparent);
  }
  .turn.meta::before { left: 8%; }
  .turn.meta::after { right: 8%; transform: scaleX(-1); }
  .meta-info {
    display: inline-block;
    margin-left: 0.5em;
    opacity: 0.45;
    cursor: help;
    text-transform: none;
    letter-spacing: 0;
    font-size: 1.05em;
    transition: opacity 0.15s ease;
  }
  .meta-info:hover { opacity: 1; }

  .play-form {
    margin-top: 14px;
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 10px;
  }
  .play-form textarea {
    background: var(--bg-rec);
    color: var(--ink-body);
    border: 1px solid var(--rule);
    border-radius: 3px;
    padding: 14px 16px;
    font-family: var(--font-body);
    font-size: 1.14em;
    resize: vertical;
    min-height: 68px;
    line-height: 1.55;
    transition: border-color 200ms ease, box-shadow 200ms ease;
    box-shadow: inset 0 1px 3px rgba(0,0,0,0.3);
  }
  .play-form textarea:focus {
    outline: none;
    border-color: var(--accent-gold);
    box-shadow: inset 0 1px 3px rgba(0,0,0,0.3), 0 0 0 1px rgba(200,169,110,0.25);
  }
  .play-form textarea::placeholder { color: var(--ink-muted); font-style: italic; }
  .play-form button {
    background: linear-gradient(to bottom, #4a3625, #2e2014);
    color: var(--ink-display);
    border: 1px solid var(--rule-hi);
    border-radius: 3px;
    width: 64px;
    padding: 0;
    cursor: pointer;
    box-shadow: var(--inset-warm), var(--shadow-card);
    transition: all 180ms ease;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .play-form button svg {
    width: 26px;
    height: 26px;
    transition: transform 180ms ease;
  }
  .play-form button:hover:not(:disabled) {
    border-color: var(--accent-gold);
    color: var(--accent-gold-hi);
    background: linear-gradient(to bottom, #5a4030, #3a2818);
  }
  .play-form button:hover:not(:disabled) svg { transform: translateX(2px); }
  .play-form button:disabled { opacity: 0.4; cursor: not-allowed; }

  /* Hint dialog (Make picture with a hint). Native <dialog> + theming. */
  dialog.hint-dialog {
    background: linear-gradient(to bottom, var(--bg-card-hi), var(--bg-card));
    border: 1px solid var(--accent-gold);
    border-radius: 4px;
    padding: 22px 24px;
    box-shadow: var(--shadow-deep);
    color: var(--ink-body);
    font-family: var(--font-body);
    max-width: 520px;
    width: calc(100vw - 60px);
  }
  dialog.hint-dialog::backdrop {
    background: rgba(10,8,5,0.72);
    backdrop-filter: blur(3px);
  }
  dialog.hint-dialog h3 {
    margin: 0 0 4px;
    font-family: var(--font-display);
    color: var(--accent-gold);
    text-transform: uppercase;
    letter-spacing: 0.16em;
    font-size: 1em;
  }
  dialog.hint-dialog p.muted { margin-bottom: 14px; font-size: 0.92em; }
  dialog.hint-dialog textarea {
    width: 100%;
    background: var(--bg-rec);
    color: var(--ink-body);
    border: 1px solid var(--rule);
    border-radius: 3px;
    padding: 10px 12px;
    font-family: var(--font-body);
    font-size: 1em;
    resize: vertical;
    min-height: 70px;
    box-shadow: inset 0 1px 3px rgba(0,0,0,0.3);
  }
  dialog.hint-dialog textarea:focus {
    outline: none;
    border-color: var(--accent-gold);
    box-shadow: inset 0 1px 3px rgba(0,0,0,0.3), 0 0 0 1px rgba(200,169,110,0.25);
  }
  dialog.hint-dialog .hint-actions {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
    margin-top: 14px;
  }
  dialog.hint-dialog button {
    background: linear-gradient(to bottom, #4a3625, #2e2014);
    color: var(--ink-display);
    border: 1px solid var(--rule-hi);
    border-radius: 3px;
    padding: 7px 18px;
    font-family: var(--font-display);
    font-size: 0.78em;
    text-transform: uppercase;
    letter-spacing: 0.18em;
    cursor: pointer;
    transition: all 180ms ease;
  }
  dialog.hint-dialog button:hover {
    border-color: var(--accent-gold);
    color: var(--accent-gold-hi);
  }
  dialog.hint-dialog button#hint-submit { border-color: var(--accent-gold); }

  /* "Load earlier turns" button at the top of the log. */
  .load-more-btn {
    display: block;
    margin: 0 auto 14px;
    padding: 5px 16px;
    background: transparent;
    border: 1px solid var(--rule-hi);
    border-radius: 2px;
    color: var(--accent-gold);
    font-family: var(--font-display);
    font-size: 0.7em;
    text-transform: uppercase;
    letter-spacing: 0.18em;
    cursor: pointer;
    transition: all 180ms ease;
  }
  .load-more-btn:hover { border-color: var(--accent-gold); color: var(--accent-gold-hi); }
  .load-more-btn:disabled { opacity: 0.4; cursor: not-allowed; }

  /* History tool summaries — non-interactive (no input/result available). */
  .turn.tool.history-tool {
    font-family: var(--font-mono);
    font-size: 0.82em;
    color: var(--ink-muted);
    padding: 5px 12px;
    border: 1px solid var(--rule);
    border-left: 2px solid var(--rule-hi);
    background: rgba(15,11,7,0.4);
    margin: 6px 0;
    border-radius: 0 2px 2px 0;
  }
  .turn.tool.history-tool::before {
    content: '⚙ ';
    color: var(--accent-gold);
    margin-right: 2px;
  }

  /* Generated scene image inline in the chat log. */
  .turn.scene {
    text-align: center;
    margin: 22px 0;
  }
  .turn.scene img {
    max-width: 100%;
    max-height: 460px;
    border-radius: 4px;
    border: 1px solid var(--rule-hi);
    box-shadow: var(--shadow-deep);
    display: inline-block;
  }
  .turn.scene .caption {
    display: block;
    font-family: var(--font-display);
    font-size: 0.7em;
    color: var(--ink-muted);
    text-transform: uppercase;
    letter-spacing: 0.2em;
    margin-top: 8px;
    font-style: normal;
  }
  /* Expandable prompt-detail under generated scenes. */
  .turn.scene details.scene-prompt {
    margin: 8px auto 0;
    max-width: 80%;
    text-align: left;
    font-size: 0.84em;
  }
  .turn.scene details.scene-prompt > summary {
    list-style: none;
    cursor: pointer;
    color: var(--ink-muted);
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.18em;
    font-size: 0.78em;
    text-align: center;
  }
  .turn.scene details.scene-prompt > summary::-webkit-details-marker { display: none; }
  .turn.scene details.scene-prompt > summary::before {
    content: '▸ ';
    color: var(--accent-gold);
  }
  .turn.scene details.scene-prompt[open] > summary::before { content: '▾ '; }
  .turn.scene details.scene-prompt p {
    margin: 8px 0 0;
    color: var(--ink-muted);
    font-style: italic;
    font-family: var(--font-body);
    font-size: 1em;
    text-transform: none;
    letter-spacing: 0;
    text-align: left;
    line-height: 1.5;
  }
  .turn.generating {
    text-align: center;
    color: var(--ink-muted);
    font-style: italic;
    font-size: 0.88em;
    padding: 14px;
    border: 1px dashed var(--rule);
    border-radius: 3px;
    margin: 18px 0;
  }
  .turn.generating::before {
    content: '◌';
    color: var(--accent-gold);
    font-style: normal;
    margin-right: 8px;
    display: inline-block;
    animation: spin 1.6s linear infinite;
  }
  @keyframes spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }

  /* "DM is thinking" indicator: pulsing dots, live elapsed time, tool count.
     Sits in the log between the player turn and the eventual DM reply, then
     is removed once the first text block arrives. */
  .turn.thinking {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 12px 18px;
    margin: 14px 0;
    border: 1px solid var(--rule);
    border-radius: 3px;
    background: linear-gradient(to bottom, rgba(200,169,110,0.04), transparent);
    color: var(--ink-muted);
    font-style: italic;
    font-size: 0.92em;
  }
  .turn.thinking .thinking-glyph {
    display: inline-flex;
    gap: 4px;
    flex-shrink: 0;
  }
  .turn.thinking .thinking-glyph span {
    width: 6px;
    height: 6px;
    background: var(--accent-gold);
    border-radius: 50%;
    box-shadow: 0 0 6px rgba(200,169,110,0.45);
    animation: thinkingDot 1.25s ease-in-out infinite;
  }
  .turn.thinking .thinking-glyph span:nth-child(2) { animation-delay: 0.16s; }
  .turn.thinking .thinking-glyph span:nth-child(3) { animation-delay: 0.32s; }
  @keyframes thinkingDot {
    0%, 70%, 100% { opacity: 0.2; transform: scale(0.7); }
    35%           { opacity: 1;   transform: scale(1.15); }
  }
  .turn.thinking .thinking-text { flex: 1; }
  .turn.thinking .thinking-time {
    font-family: var(--font-mono);
    font-size: 0.84em;
    font-style: normal;
    color: var(--accent-gold);
    letter-spacing: 0;
    white-space: nowrap;
  }

  /* "Did you know" lore fact beneath the thinking row, shown only while the
     DM is composing. Scoped to .has-dyk so the recovery indicator (which
     reuses .turn.thinking as a plain row) is unaffected. */
  .turn.thinking.has-dyk { flex-direction: column; align-items: stretch; gap: 10px; }
  .turn.thinking.has-dyk .thinking-row { display: flex; align-items: center; gap: 14px; }
  .turn.thinking .dyk {
    border-top: 1px dotted var(--rule);
    padding-top: 10px;
    font-style: normal;
    line-height: 1.55;
    opacity: 0;
    transition: opacity 0.55s ease;
  }
  .turn.thinking .dyk.show { opacity: 1; }
  .turn.thinking .dyk-label {
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.16em;
    font-size: 0.7em;
    color: var(--accent-gold);
    margin-right: 9px;
  }
  .turn.thinking .dyk-label::before { content: '\\2726  '; }   /* ✦ */
  .turn.thinking .dyk-fact { font-size: 0.95em; color: var(--ink-body); }
  .turn.thinking .dyk-cat {
    font-family: var(--font-mono);
    font-size: 0.7em;
    color: var(--accent-gold);
    opacity: 0.55;
    margin-left: 8px;
    white-space: nowrap;
  }

  /* ---- Sidebar ---- */
  .play-side {
    background: linear-gradient(to bottom, var(--bg-card-hi), var(--bg-card));
    border: 1px solid var(--rule);
    border-radius: 4px;
    padding: 18px 16px 14px;
    box-shadow: var(--inset-warm), var(--shadow-card);
  }

  /* Campaign banner at the very top of the sidebar. */
  .side-camp {
    font-family: var(--font-display);
    color: var(--accent-gold-hi);
    text-transform: uppercase;
    letter-spacing: 0.18em;
    text-align: center;
    font-size: 0.82em;
    font-weight: 500;
    line-height: 1.35;
    padding-bottom: 12px;
    margin-bottom: 12px;
    border-bottom: 1px solid var(--rule);
  }
  .side-camp:empty { display: none; }

  /* Stats rows: session/day/time, weather/gold, location, combat. */
  .side-stats { display: flex; flex-direction: column; gap: 9px; margin-bottom: 12px; }
  .side-row {
    display: flex;
    align-items: baseline;
    justify-content: center;
    gap: 16px;
    flex-wrap: wrap;
  }
  .side-row .pip { display: inline-flex; align-items: baseline; gap: 6px; line-height: 1; }
  .side-row .pip .pip-label {
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.18em;
    font-size: 0.62em;
    color: var(--ink-muted);
  }
  .side-row .pip .pip-val {
    font-family: var(--font-body);
    color: var(--ink-body);
    font-size: 0.96em;
  }
  .side-row.row-loc { padding-top: 4px; }
  .side-row.row-loc .pip-val {
    color: var(--accent-gold-hi);
    font-style: italic;
    font-size: 1.0em;
  }
  .side-row.row-combat {
    padding: 4px 10px;
    background: rgba(70,18,15,0.4);
    border: 1px solid #6a2818;
    border-radius: 2px;
  }
  .side-row.row-combat .pip-val {
    color: var(--accent-blood);
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.22em;
    font-size: 0.8em;
    font-weight: 500;
    animation: combatPulse 1.6s ease-in-out infinite;
  }
  @keyframes combatPulse {
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.55; }}
  }
  .pip.pip-gold .pip-val {
    color: var(--accent-gold);
    font-family: var(--font-mono);
    font-size: 0.88em;
    letter-spacing: 0;
  }

  .side-title {
    font-family: var(--font-display);
    color: var(--accent-gold);
    text-transform: uppercase;
    letter-spacing: 0.26em;
    font-size: 0.74em;
    text-align: center;
    margin: 0 0 14px;
    padding: 10px 0;
    border-top: 1px solid var(--rule);
    border-bottom: 1px solid var(--rule);
    position: relative;
  }
  .side-title::after {
    content: '◆';
    position: absolute;
    left: 50%;
    bottom: -7px;
    transform: translateX(-50%);
    background: var(--bg-card-hi);
    color: var(--accent-gold);
    padding: 0 6px;
    font-size: 0.6em;
    letter-spacing: 0;
  }
  .party-list { display: flex; flex-direction: column; gap: 10px; }
  .party-mini { display: flex; gap: 10px; align-items: flex-start; }

  /* ---- Audit dialog + toolbar pip ---- */
  /* The post-turn audit lives behind the 📋 button in the recovery/debug
     toolbar group. Findings are surfaced in a modal so the sidebar stays
     focused on party state. The button's pip turns red when there are new
     lapses since last open, so the human still gets passive notification. */
  .audit-btn {{ position: relative; }}
  .audit-btn-pip {{
    position: absolute; top: 2px; right: 2px;
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--hp-bad);
    box-shadow: 0 0 4px var(--hp-bad);
    pointer-events: none;
  }}
  .audit-btn-pip.has-warning {{ background: #d4a14a; box-shadow: 0 0 4px #d4a14a; }}

  .audit-dialog {{ max-width: 720px; }}
  .audit-toggle {{
    display: flex; align-items: flex-start; gap: 10px;
    padding: 10px 12px;
    margin: 0 0 12px 0;
    background: rgba(200,169,110,0.05);
    border: 1px solid var(--rule);
    border-radius: 2px;
    cursor: pointer;
    font-size: 0.88em;
    line-height: 1.45;
  }}
  .audit-toggle:hover {{ border-color: var(--accent-gold); }}
  .audit-toggle input {{ margin-top: 3px; }}
  .audit-toggle strong {{ color: var(--accent-gold-hi); }}

  .audit-list {{
    list-style: none; padding: 0; margin: 0;
    display: flex; flex-direction: column; gap: 8px;
    font-size: 0.88em;
    max-height: 50vh;
    overflow-y: auto;
  }}
  .audit-item {{
    padding: 8px 12px;
    border-left: 2px solid var(--rule);
    color: var(--ink-muted);
    line-height: 1.45;
  }}
  .audit-item.sev-lapse   {{ border-left-color: var(--hp-bad); color: var(--ink-body); }}
  .audit-item.sev-warning {{ border-left-color: #d4a14a; }}
  .audit-item .audit-kind {{
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.12em;
    font-size: 0.78em;
    color: var(--accent-gold-hi);
  }}
  .audit-item .audit-slug {{
    font-family: var(--font-mono, monospace);
    color: var(--ink-body);
    background: rgba(200,169,110,0.08);
    padding: 0 4px;
    border-radius: 2px;
  }}
  .portrait-frame {
    flex-shrink: 0;
    width: 50px; height: 50px;
    border-radius: 50%;
    background: var(--bg-deep);
    border: 1px solid var(--accent-gold);
    box-shadow: 0 0 0 1px var(--bg-deep), 0 0 0 2px var(--rule), 0 2px 6px rgba(0,0,0,0.5);
    overflow: hidden;
    position: relative;
    text-decoration: none;
    color: inherit;
    display: block;
    transition: box-shadow 200ms ease, transform 200ms ease;
  }
  .portrait-frame:hover {
    box-shadow: 0 0 0 1px var(--bg-deep), 0 0 0 2px var(--accent-gold-hi), 0 4px 12px rgba(200,169,110,0.25);
    transform: translateY(-1px);
  }
  .portrait-frame img { width: 100%; height: 100%; object-fit: cover; display: block; cursor: zoom-in; }
  .portrait-frame.empty {
    background: linear-gradient(135deg, #1a130c, #0f0a07);
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--rule-hi);
    font-family: var(--font-display);
    font-size: 1.4em;
    letter-spacing: 0;
  }
  .portrait-frame.kod {
    border-color: var(--hp-bad);
    box-shadow: 0 0 0 1px var(--bg-deep), 0 0 0 2px #4a1a14, 0 0 12px rgba(200,80,58,0.4);
    filter: grayscale(0.4);
  }

  .party-mini-body { flex: 1; min-width: 0; }
  .party-mini-name {
    font-family: var(--font-display);
    color: var(--ink-display);
    font-size: 0.88em;
    letter-spacing: 0.06em;
    line-height: 1.15;
    display: block;
    text-decoration: none;
    transition: color 180ms ease;
  }
  a.party-mini-name:hover {
    color: var(--accent-gold-hi);
    text-decoration: underline;
    text-decoration-color: var(--rule-hi);
    text-underline-offset: 3px;
  }
  .party-mini-class {
    font-family: var(--font-body);
    font-style: italic;
    color: var(--ink-muted);
    font-size: 0.84em;
    margin-top: 1px;
  }
  .party-mini-hp {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 3px;
  }
  .party-mini-hp .hp-meter { flex: 1; }
  .party-mini-hp-text {
    font-family: var(--font-mono);
    font-size: 0.74em;
    color: var(--ink-muted);
    min-width: 42px;
    text-align: right;
    letter-spacing: 0;
  }
  .party-mini-xp {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 2px;
    opacity: 0.75;
  }
  .party-mini-xp .xp-meter { flex: 1; }
  .xp-meter {
    display: block;
    height: 3px;
    background: var(--bg-rec);
    border: 1px solid var(--rule);
    border-radius: 1px;
    overflow: hidden;
  }
  .xp-meter > i {
    display: block;
    height: 100%;
    background: linear-gradient(to right, #5a4a26, var(--accent-gold));
    transition: width 380ms ease;
  }
  .party-mini-xp-text {
    font-family: var(--font-mono);
    font-size: 0.62em;
    color: var(--ink-muted);
    min-width: 42px;
    text-align: right;
    letter-spacing: 0;
  }
  .party-mini-conds {
    margin-top: 5px;
    display: flex;
    flex-wrap: wrap;
    gap: 3px;
  }
  .cond-chip {
    font-family: var(--font-display);
    font-size: 0.62em;
    padding: 1px 6px;
    border: 1px solid var(--accent-rust);
    background: #2e1d12;
    color: #e8b487;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    border-radius: 1px;
  }

  @media (max-width: 900px) {
    .play-shell {
      grid-template-columns: 1fr;
      height: auto;
    }
    .play-side { max-height: 480px; }
  }

  /* ---- Phone (≤640px): stack everything and let the page scroll ----
     On desktop /play locks to the viewport (body overflow hidden, log owns the
     scroll). That falls apart on a phone: the party sidebar would sit below a
     clipped, unreachable fold. Here we release the lock so the document scrolls
     naturally — toolbar, narrative log, composer, then the party panel as a
     full-width block beneath the main column. */
  @media (max-width: 640px) {
    /* Release the viewport lock (body overflow hidden + fixed-height flex
       container) and let the document scroll. The :has() rules only matter on
       browsers that support it; the block-flow stacking below uses plain
       selectors so it works everywhere. */
    body:has(.play-shell) { overflow: auto; }
    .container:has(.play-shell) {
      display: block;
      height: auto;
      min-height: 0;
      padding: 14px 14px 32px;
    }
    /* Drop the two-column grid: stack main and sidebar as ordinary blocks,
       each sized to its own content. A grid here lets the viewport-capped
       flex chain starve the sidebar row down to a sliver. */
    .play-shell {
      display: block;
      flex: none;
      height: auto;
      min-height: 0;
    }
    .play-main {
      display: flex;
      flex-direction: column;
      flex: none;
      min-height: 0;
    }

    /* Header items wrap; the front-end icon buttons fall to the row(s)
       beneath them. The session chip claims the full first line so the tone /
       detail controls and icon clusters read as a tidy second tier. */
    .play-toolbar {
      flex-wrap: wrap;
      align-items: center;
      gap: 8px 10px;
      margin-bottom: 10px;
      min-width: 0;
      max-width: 100%;
    }
    .play-toolbar .spacer { display: none; }
    /* Flex items default to min-width:auto and refuse to shrink below their
       content — which keeps a wide group on one line and overflows the row.
       Allowing them to shrink is what actually lets the wrap engage. */
    .play-toolbar > * { min-width: 0; }
    .play-toolbar .session-group { flex: 1 1 100%; min-width: 0; }
    /* The session hash is a long unbreakable mono string; if it can't break it
       pushes the page wider than the phone, and the browser shrink-to-fits the
       whole toolbar back onto one row. Force it to wrap within the line. */
    .play-toolbar .sid { min-width: 0; }
    .play-toolbar .sid a,
    .play-toolbar .sid code { overflow-wrap: anywhere; word-break: break-word; }
    /* Roomier finger targets for the icon buttons on a touchscreen. */
    .icon-btn,
    .turn-toggle.icon { width: 36px; height: 32px; font-size: 0.98em; }

    /* The log no longer flexes to fill the viewport (the page scrolls now);
       give it a comfortable fixed slice so the composer stays in reach. */
    .play-log {
      flex: none;
      height: 58vh;
      min-height: 300px;
      padding: 16px 16px;
      font-size: 1em;
    }

    /* Party sidebar: full-width block below the narrative, sized to its
       content, no inner scroll. margin-top replaces the dropped grid gap. */
    .play-side {
      display: block;
      width: 100%;
      margin-top: 14px;
      max-height: none;
      overflow: visible;
    }

    /* Composer scaled for thumbs; nudge the jump-button clear of it. */
    .play-form textarea { font-size: 1.05em; min-height: 56px; padding: 12px 14px; }
    .play-form button { width: 56px; }
    .jump-btn { right: 14px; bottom: 96px; }
  }
</style>

<div class="play-shell">
  <section class="play-main">
    <div class="play-toolbar">
      <span class="session-group">
        <span class="sid">session<span id="play-session-no" class="session-no"></span><a id="play-sid" href="#" target="_blank" rel="noopener"><code>…</code></a><code id="play-sid-size" class="sid-size"></code></span>
        <button id="play-reset" type="button" class="icon-btn" title="New session: start a fresh DM conversation (current session remains in /sessions)" aria-label="New session">✨</button>
      </span>
      <span class="tone-group" role="group" aria-label="DM narration tone" title="DM narration tone — shapes voice and texture only, never the rules">
        <span class="tone-icon" aria-hidden="true">🎭</span>
        <select id="play-tone" aria-label="DM narration tone">
          <option value="classic">Classic</option>
          <option value="grimdark">Grimdark</option>
          <option value="heroic">High-heroic</option>
          <option value="horror">Horror</option>
          <option value="comedic">Wry / comedic</option>
          <option value="custom">Custom…</option>
        </select>
        <input id="play-tone-custom" type="text" maxlength="2000" placeholder="describe your own tone…" hidden aria-label="Custom tone description" />
      </span>
      <span class="detail-group" role="group" aria-label="Narrative detail level" title="Narrative detail — how much raw mechanical detail (coin counts, ability scores, stat blocks) the DM exposes in prose. Never changes the rules or the fourth wall.">
        <span class="detail-icon" aria-hidden="true">🎚️</span>
        <input id="play-detail" type="range" min="0" max="3" step="1" value="2" aria-label="Narrative detail level" />
        <span id="play-detail-label" class="detail-label">Standard</span>
      </span>
      <span class="instructions-group" role="group" aria-label="Campaign instructions" title="Campaign instructions — a binding per-campaign constraint (e.g. lock the campaign to its module) injected into every DM turn at the same authority as the Hard Constraints.">
        <button id="play-instructions-btn" type="button" class="icon-btn" title="Campaign instructions — binding per-campaign constraint (e.g. module lock)" aria-label="Campaign instructions">📜</button>
        <textarea id="play-instructions-text" maxlength="6000" rows="3" placeholder="Binding per-campaign instructions (e.g. run strictly off modules/<slug>/, consult Level-NN.md before keying any room)…" hidden aria-label="Campaign instructions text"></textarea>
        <label id="play-instructions-enabled-wrap" hidden><input id="play-instructions-enabled" type="checkbox" checked /> enabled</label>
      </span>
      <span class="view-controls">
        <span class="filter-group" role="group" aria-label="Show message types">
          <button class="turn-toggle icon" data-filter="player" type="button" title="Show player echoes" aria-label="Show player echoes">◇</button>
          <button class="turn-toggle icon" data-filter="tools"  type="button" title="Show tool calls"     aria-label="Show tool calls">⚙</button>
          <button class="turn-toggle icon" data-filter="meta"   type="button" title="Show meta lines"     aria-label="Show meta lines">✦</button>
        </span>
        <span class="scroll-group" role="group" aria-label="Scroll behaviour">
          <button id="play-pin-scroll" class="icon-btn" type="button" title="Pin scroll position: pause auto-scroll during streaming so you can read at your own pace" aria-label="Pin scroll position">📌</button>
        </span>
        <span class="font-group" role="group" aria-label="Narrative font">
          <button id="play-font-family" class="icon-btn" type="button" title="Cycle narrative font" aria-label="Cycle narrative font">Aa</button>
          <button id="play-font-dec"    class="icon-btn" type="button" title="Smaller text" aria-label="Decrease narrative text size">A−</button>
          <button id="play-font-inc"    class="icon-btn" type="button" title="Larger text"  aria-label="Increase narrative text size">A+</button>
        </span>
      </span>
      <span class="icon-group" role="group" aria-label="Generate illustration">
        <button id="play-make-pic" type="button" class="icon-btn" title="Make picture: generate an illustration of the last DM reply" aria-label="Make picture">🖼</button>
        <button id="play-make-pic-hint" type="button" class="icon-btn" title="Make picture with a hint" aria-label="Make picture with a hint">…</button>
      </span>
      <span class="icon-group" role="group" aria-label="Per-reply effects">
        <button id="play-voices" type="button" class="icon-btn" title="Configure narration voices" aria-label="Configure narration voices">🎙</button>
        <button id="play-suggest" type="button" class="icon-btn" title="Suggest 2-4 next actions after each DM reply" aria-label="Toggle suggested actions">💡</button>
        <button id="play-bell" type="button" class="icon-btn" title="Play a chime when the DM finishes a response" aria-label="Toggle response chime">🔔</button>
      </span>
      <span class="icon-group" role="group" aria-label="Recovery and debug">
        <button id="play-fetch-last" type="button" class="icon-btn" title="Fetch the last DM reply from the transcript (recovers a response if the SSE stream dropped)" aria-label="Fetch last DM reply">↻</button>
        <button id="play-audit" type="button" class="icon-btn audit-btn" title="View procedural-fairness audit findings (toggle DM nudge from inside)" aria-label="Open audit panel">
          <span aria-hidden="true">📋</span>
          <span class="audit-btn-pip" id="audit-btn-pip" hidden></span>
        </button>
        <button id="play-debug" type="button" class="icon-btn" title="Inspect the raw stream-JSON events from claude -p (last 200)" aria-label="Open debug log">🐛</button>
      </span>
      <span class="spacer"></span>
      <span id="play-status"></span>
    </div>
    <dialog id="hint-dialog" class="hint-dialog">
      <form id="hint-form">
        <h3>Make picture with a hint</h3>
        <p class="muted">Optional extra direction for the illustrator. Combined with the last DM reply.</p>
        <textarea id="hint-text" placeholder="e.g. focus on the treasure pile · show Korrhast in the foreground · stormy mood" rows="3"></textarea>
        <div class="hint-actions">
          <button type="button" id="hint-cancel">Cancel</button>
          <button type="submit" id="hint-submit">Generate</button>
        </div>
      </form>
    </dialog>
    <dialog id="voice-dialog" class="voice-dialog">
      <form id="voice-form" method="dialog">
        <h3>Narration voices</h3>
        <p class="muted">Each voice is sampled by gpt-4o-mini-tts. Speech is detected from markdown blockquotes; speaker is matched by name.</p>
        <table id="voice-table"><tbody></tbody></table>
        <div class="actions">
          <button type="button" id="voice-cancel">Cancel</button>
          <button type="submit" id="voice-save">Save</button>
        </div>
      </form>
    </dialog>
    <dialog id="audit-dialog" class="debug-dialog audit-dialog">
      <h3>Procedural-fairness audit <span class="muted" id="audit-summary">(loading…)</span></h3>
      <p class="muted">Findings from the post-turn scan: missed reactions, freehand combat, module-canon leaks in narrative, directive language in OOC prompts. See <code>tools/audit.py</code>.</p>
      <label class="audit-toggle">
        <input type="checkbox" id="audit-inject-toggle">
        <span><strong>Inject into DM</strong> — prepend up to 8 new lapses to the next system prompt as a "don't repeat these" addendum (B4). Off by default.</span>
      </label>
      <ul class="audit-list audit-dialog-list" id="audit-dialog-list">
        <li class="muted">(no findings yet)</li>
      </ul>
      <div class="actions">
        <button type="button" id="audit-clear-shown" title="Re-surface every current lapse to the DM on the next turn. Use if you want to retry the nudge after toggling injection back on.">Reset shown</button>
        <button type="button" id="audit-close">Close</button>
      </div>
    </dialog>
    <dialog id="debug-dialog" class="debug-dialog">
      <h3>Stream-JSON events <span class="muted" id="debug-count">(0)</span></h3>
      <p class="muted">Last 200 events from <code>claude -p</code>. Per-turn token + cost are inside each <code>result</code> event's <code>usage</code> field.</p>
      <pre id="debug-pre" class="debug-pre">(none captured yet)</pre>
      <div class="actions">
        <button type="button" id="debug-force-reset" title="Kill any wedged claude -p and clear the in-flight lock. Use only if the DM is stuck.">Force-reset turn</button>
        <button type="button" id="debug-clear">Clear</button>
        <button type="button" id="debug-copy">Copy JSON</button>
        <button type="button" id="debug-close">Close</button>
      </div>
    </dialog>
    <div class="play-log" id="play-log">
      <button id="load-more" class="load-more-btn" type="button" hidden>↑ Load earlier turns</button>
      <div class="empty-state" id="empty-state">
        <img src="/static/play-hero.png" alt="" class="play-hero">
        <div class="empty-state-text">Begin when ready. Describe an action below to address the DM.</div>
      </div>
    </div>
    <button id="jump-to-bottom" class="jump-btn" type="button" aria-label="Jump to latest" title="Jump to latest">↓</button>
    <form class="play-form" id="play-form">
      <textarea id="play-input" placeholder="Describe your action…  (Enter to send, Shift+Enter for new line)" autocomplete="off"></textarea>
      <button type="submit" id="play-send" title="Send (Enter)" aria-label="Send">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M3.5 11.6 21 4l-7.5 17-2.2-7.4-7.8-2z"/>
          <path d="m11.3 13.6 4.6-4.6" opacity="0.7"/>
        </svg>
      </button>
    </form>
  </section>

  <aside class="play-side">
    <!-- <div class="side-camp" id="side-camp"></div> -->
    <div class="side-stats" id="side-stats"></div>
    <!-- <div class="side-title">The Party</div> -->
    <div class="party-list" id="party-list">
      <div class="muted" style="text-align:center; font-size:0.84em; font-style:italic">…</div>
    </div>
  </aside>
</div>

<script>
(function(){
  const log = document.getElementById('play-log');
  const input = document.getElementById('play-input');
  const send = document.getElementById('play-send');
  const form = document.getElementById('play-form');
  const sidEl = document.getElementById('play-sid');
  const sidSizeEl = document.getElementById('play-sid-size');

  function fmtSize(bytes) {
    if (typeof bytes !== 'number' || bytes < 0) return '';
    if (bytes < 1024) return bytes + 'B';
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + 'K';
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + 'M';
    return (bytes / (1024 * 1024 * 1024)).toFixed(1) + 'G';
  }

  function fmtTokens(n) {
    if (typeof n !== 'number' || n < 0) return '';
    return n < 1000 ? String(n) : (n / 1000).toFixed(n < 10000 ? 1 : 0) + 'k';
  }

  // Show live context size (tokens) on the session chip — the number that
  // actually drives cost and the auto-reset, not the inflated file bytes.
  // Turns amber as it nears the auto-reset threshold, so you can watch it climb.
  function renderSessionMeta(d) {
    const tok = d.context_tokens;
    if (typeof tok !== 'number') {
      sidSizeEl.textContent = '';
      sidSizeEl.classList.remove('near-reset');
      sidSizeEl.removeAttribute('title');
      return;
    }
    sidSizeEl.textContent = fmtTokens(tok);
    const limit = d.auto_reset_tokens;
    let title = tok.toLocaleString() + ' tokens of context';
    if (typeof limit === 'number') {
      title += ' — auto-resets at ' + limit.toLocaleString()
            + ' (' + Math.round(100 * tok / limit) + '%) when not in combat';
      sidSizeEl.classList.toggle('near-reset', tok >= 0.8 * limit);
    } else {
      title += ' — auto-reset disabled';
      sidSizeEl.classList.remove('near-reset');
    }
    if (typeof d.size_bytes === 'number') title += '; transcript ' + fmtSize(d.size_bytes);
    sidSizeEl.title = title;
  }

  async function refreshSessionSize() {
    try {
      const r = await fetch('/play/session');
      if (!r.ok) return;
      const d = await r.json();
      renderSessionMeta(d);
    } catch (e) { /* best-effort */ }
  }

  // --- DM tone selector ---------------------------------------------------
  const toneSel = document.getElementById('play-tone');
  const toneCustom = document.getElementById('play-tone-custom');

  function applyToneSetting(t) {
    if (!t || !toneSel) return;
    const known = [...toneSel.options].some(o => o.value === t.preset);
    toneSel.value = known ? t.preset : 'classic';
    toneCustom.value = t.custom || '';
    toneCustom.hidden = toneSel.value !== 'custom';
  }

  async function saveTone() {
    const preset = toneSel.value;
    const body = preset === 'custom' ? {preset, custom: toneCustom.value} : {preset};
    try {
      await fetch('/play/tone', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
    } catch (e) { /* best-effort */ }
  }

  if (toneSel) {
    toneSel.addEventListener('change', () => {
      const isCustom = toneSel.value === 'custom';
      toneCustom.hidden = !isCustom;
      if (isCustom) {
        toneCustom.focus();
        if (toneCustom.value.trim()) saveTone();  // re-applying an existing custom tone
      } else {
        saveTone();
      }
    });
    // Custom text commits on blur or Enter (change event covers both).
    toneCustom.addEventListener('change', saveTone);
    toneCustom.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); toneCustom.blur(); }
    });
  }

  // --- Narrative detail level slider --------------------------------------
  const detailSlider = document.getElementById('play-detail');
  const detailLabel = document.getElementById('play-detail-label');
  let detailLabels = {0: 'Immersive', 1: 'Light', 2: 'Standard', 3: 'Open table'};

  function applyDetailSetting(d) {
    if (!d || !detailSlider) return;
    if (Array.isArray(d.choices)) {
      detailLabels = {};
      d.choices.forEach(c => { detailLabels[c.level] = c.label; });
    }
    const lvl = (d.detail && typeof d.detail.level === 'number') ? d.detail.level : 2;
    detailSlider.value = String(lvl);
    if (detailLabel) detailLabel.textContent = detailLabels[lvl] || '';
  }

  async function saveDetail() {
    try {
      await fetch('/play/detail', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({level: parseInt(detailSlider.value, 10)}),
      });
    } catch (e) { /* best-effort */ }
  }

  if (detailSlider) {
    detailSlider.addEventListener('input', () => {
      if (detailLabel) detailLabel.textContent = detailLabels[detailSlider.value] || '';
    });
    detailSlider.addEventListener('change', saveDetail);
  }

  // --- Campaign instructions (binding per-campaign constraint) -------------
  const instrBtn = document.getElementById('play-instructions-btn');
  const instrText = document.getElementById('play-instructions-text');
  const instrEnabled = document.getElementById('play-instructions-enabled');
  const instrEnabledWrap = document.getElementById('play-instructions-enabled-wrap');

  function applyInstructionsSetting(i) {
    if (!i || !instrText) return;
    instrText.value = i.text || '';
    if (instrEnabled) instrEnabled.checked = i.enabled !== false;
    // Mark the toggle button active when a directive is set and enabled.
    if (instrBtn) {
      const active = !!(i.text && i.text.trim()) && i.enabled !== false;
      instrBtn.classList.toggle('active', active);
    }
  }

  async function saveInstructions() {
    if (!instrText) return;
    try {
      await fetch('/play/instructions', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          text: instrText.value,
          enabled: instrEnabled ? instrEnabled.checked : true,
        }),
      });
    } catch (e) { /* best-effort */ }
    applyInstructionsSetting({
      text: instrText.value,
      enabled: instrEnabled ? instrEnabled.checked : true,
    });
  }

  if (instrBtn) {
    instrBtn.addEventListener('click', () => {
      const hidden = instrText.hidden;
      instrText.hidden = !hidden;
      if (instrEnabledWrap) instrEnabledWrap.hidden = !hidden;
      if (!instrText.hidden) instrText.focus();
    });
    instrText.addEventListener('change', saveInstructions);  // commits on blur
    if (instrEnabled) instrEnabled.addEventListener('change', saveInstructions);
  }

  // --- Nav toggles: subheader (toolbar) + party panel ---------------------
  (function(){
    const shell = document.querySelector('.play-shell');
    const toolbar = document.querySelector('.play-toolbar');
    const side = document.querySelector('.play-side');
    const tBtn = document.getElementById('nav-toggle-toolbar');
    const pBtn = document.getElementById('nav-toggle-party');

    function applyToolbar(hidden){
      if (toolbar) toolbar.style.display = hidden ? 'none' : '';
      if (tBtn) tBtn.classList.toggle('off', hidden);
    }
    function applyParty(hidden){
      if (side) side.style.display = hidden ? 'none' : '';
      if (shell) shell.style.gridTemplateColumns = hidden ? 'minmax(0, 1fr)' : '';
      if (pBtn) pBtn.classList.toggle('off', hidden);
    }

    let toolbarHidden = localStorage.getItem('play.hide.toolbar') === '1';
    let partyHidden   = localStorage.getItem('play.hide.party') === '1';
    applyToolbar(toolbarHidden);
    applyParty(partyHidden);

    if (tBtn) tBtn.addEventListener('click', () => {
      toolbarHidden = !toolbarHidden;
      localStorage.setItem('play.hide.toolbar', toolbarHidden ? '1' : '0');
      applyToolbar(toolbarHidden);
    });
    if (pBtn) pBtn.addEventListener('click', () => {
      partyHidden = !partyHidden;
      localStorage.setItem('play.hide.party', partyHidden ? '1' : '0');
      applyParty(partyHidden);
    });
  })();

  const statusEl = document.getElementById('play-status');
  const resetBtn = document.getElementById('play-reset');
  const partyList = document.getElementById('party-list');
  const sideCamp = document.getElementById('side-camp');
  const sideStats = document.getElementById('side-stats');
  const makePicBtn = document.getElementById('play-make-pic');
  const jumpBtn = document.getElementById('jump-to-bottom');
  let firstAppend = true;
  let lastDmText = '';
  let lastDmTurnEl = null;     // last live DM turn DOM node — anchor for the chip row
  let suggestEl = null;        // current chip-row element, or null
  let suggestController = null; // AbortController for the in-flight suggestion fetch
  let suggestEnabled = localStorage.getItem('play.suggest-actions') === '1';
  let bellEnabled = localStorage.getItem('play.bell') === '1';
  let bellAudioCtx = null;

  // Two-note chime via WebAudio — no asset dependency. Lazily creates
  // the AudioContext on first use (browser autoplay policies require a
  // user gesture; toggling the button counts).
  function playBell() {
    try {
      if (!bellAudioCtx) bellAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const ctx = bellAudioCtx;
      const now = ctx.currentTime;
      const tones = [{ f: 988, t: 0 }, { f: 1319, t: 0.12 }]; // B5 → E6
      for (const { f, t } of tones) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.value = f;
        gain.gain.setValueAtTime(0.0001, now + t);
        gain.gain.exponentialRampToValueAtTime(0.15, now + t + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + t + 0.35);
        osc.connect(gain).connect(ctx.destination);
        osc.start(now + t);
        osc.stop(now + t + 0.4);
      }
    } catch (e) { /* silent — audio is non-essential */ }
  }
  let thinkingEl = null;
  let thinkingStart = 0;
  let thinkingTimer = null;
  let thinkingTools = 0;

  // "Did you know" lore facts — fetched once on load, then rotated with a
  // cross-fade beneath the "DM is thinking" row during the wait for a reply.
  let dykFacts = [];
  let dykTimer = null;
  let dykPos = 0;
  const DYK_ROTATE_MS = 10000;
  fetch('/api/facts/random?n=40')
    .then(r => r.ok ? r.json() : null)
    .then(d => { if (d && Array.isArray(d.facts)) dykFacts = d.facts; })
    .catch(() => {});

  function renderNextFact() {
    if (!thinkingEl || !dykFacts.length) return;
    const dyk = thinkingEl.querySelector('.dyk');
    if (!dyk) return;
    const f = dykFacts[dykPos % dykFacts.length];
    dykPos++;
    dyk.classList.remove('show');           // fade current out
    setTimeout(function(){
      if (!thinkingEl) return;              // turn ended mid-fade
      const factSpan = dyk.querySelector('.dyk-fact');
      const catSpan = dyk.querySelector('.dyk-cat');
      if (factSpan) factSpan.textContent = f.text || '';
      // Campaign facts show the campaign name; others show the category.
      const tag = (f.category === 'campaign' && f.campaign) ? f.campaign : f.category;
      if (catSpan) catSpan.textContent = tag ? ('— ' + tag) : '';
      dyk.classList.add('show');            // fade next in
    }, 280);
  }
  // Map tool_use.id → DOM element so tool_result events can fill in the
  // result section once it lands.
  const toolElements = new Map();

  // Auto-scroll only when the user is already near the bottom; otherwise
  // leave their reading position alone. The jump button is only shown
  // once the reader has scrolled away by at least a full screen — short
  // scrolls (skim back a few lines) shouldn't summon the button.
  const NEAR_BOTTOM_PX = 120;
  function distanceFromBottom() {
    return log.scrollHeight - log.scrollTop - log.clientHeight;
  }
  function isNearBottom() {
    return distanceFromBottom() < NEAR_BOTTOM_PX;
  }
  function isFarFromBottom() {
    return distanceFromBottom() >= log.clientHeight;
  }
  function refreshJumpBtn() {
    jumpBtn.classList.toggle('visible', isFarFromBottom());
  }
  // When the user pins the scroll (📌 in the toolbar), auto-scroll is
  // suppressed entirely — even from "near bottom" — so they can read
  // mid-stream without the page chasing the cursor. The jump-to-bottom
  // button still catches up on demand.
  let scrollPinned = localStorage.getItem('play.pinScroll') === '1';
  function maybeScrollToBottom() {
    if (scrollPinned) {
      refreshJumpBtn();
      return;
    }
    if (isNearBottom()) {
      log.scrollTop = log.scrollHeight;
    } else {
      refreshJumpBtn();
    }
  }
  function forceScrollToBottom() {
    log.scrollTop = log.scrollHeight;
    jumpBtn.classList.remove('visible');
  }
  const pinBtn = document.getElementById('play-pin-scroll');
  if (pinBtn) {
    function reflectPin() {
      pinBtn.classList.toggle('pin-on', scrollPinned);
      pinBtn.setAttribute('aria-pressed', String(scrollPinned));
    }
    reflectPin();
    pinBtn.addEventListener('click', () => {
      scrollPinned = !scrollPinned;
      localStorage.setItem('play.pinScroll', scrollPinned ? '1' : '0');
      reflectPin();
      // When unpinning, also snap to the live cursor — the user just
      // opted back in to "follow the response".
      if (!scrollPinned) forceScrollToBottom();
    });
  }
  log.addEventListener('scroll', refreshJumpBtn);
  // Also re-check after layout changes that aren't a scroll event:
  // window resize, or content height changing while the user holds
  // their position (e.g. an image arriving in the background).
  window.addEventListener('resize', refreshJumpBtn);
  jumpBtn.addEventListener('click', forceScrollToBottom);

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function renderInline(s) {
    // s is already HTML-escaped. Order matters: process triple-asterisk
    // before double, double before single, code spans before italic so the
    // backticks don't fight asterisk parsing.
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\\*\\*\\*(.+?)\\*\\*\\*/g, '<strong><em>$1</em></strong>');
    s = s.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>');
    s = s.replace(/\\*(.+?)\\*/g, '<em>$1</em>');
    s = s.replace(/(^|[^\\w])_(.+?)_(?!\\w)/g, '$1<em>$2</em>');
    s = s.replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, '<a href="$2">$1</a>');
    return s;
  }

  function renderMarkdown(text) {
    if (!text) return '';
    const escaped = esc(text);
    const lines = escaped.split('\\n');
    const out = [];
    let para = [];

    // A whole-paragraph quoted line (with optional italic wrap) renders as
    // <blockquote> so character speech keeps its distinctive styling even
    // when the DM omits the `> ` prefix. Matches the escaped form: optional
    // leading `*`, then a curly or straight opening quote, then anything,
    // then a matching closing quote, then an optional trailing `*`. Inner
    // quote chars are allowed — what's required is that the paragraph
    // begins and ends with a quote (no narration outside).
    const SPEECH_PARA_RE = /^\\s*\\*?\\s*(?:&quot;|&ldquo;|\\u201C)[\\s\\S]*(?:&quot;|&rdquo;|\\u201D)\\s*\\*?\\s*$/;

    function flushPara() {
      if (para.length) {
        const joined = para.join(' ');
        const tag = SPEECH_PARA_RE.test(joined) ? 'blockquote' : 'p';
        out.push('<' + tag + '>' + renderInline(joined) + '</' + tag + '>');
        para = [];
      }
    }

    let i = 0;
    while (i < lines.length) {
      const raw = lines[i];
      const t = raw.trim();

      if (!t) { flushPara(); i++; continue; }

      const h = /^(#{1,6})\\s+(.+)$/.exec(t);
      if (h) {
        flushPara();
        const lvl = Math.min(h[1].length, 4);
        out.push('<h' + lvl + '>' + renderInline(h[2]) + '</h' + lvl + '>');
        i++; continue;
      }

      if (/^(-{3,}|\\*{3,}|_{3,})$/.test(t)) {
        flushPara();
        out.push('<hr>');
        i++; continue;
      }

      // Note: block-level detection runs *after* esc(), so a markdown ">"
      // arrives here as the entity "&gt;". Match that, not literal ">".
      if (/^&gt;\\s?/.test(t)) {
        flushPara();
        const bq = [];
        while (i < lines.length && /^&gt;\\s?/.test(lines[i].trim())) {
          bq.push(lines[i].trim().replace(/^&gt;\\s?/, ''));
          i++;
        }
        out.push('<blockquote>' + bq.map(renderInline).join('<br>') + '</blockquote>');
        continue;
      }

      if (/^[-*+]\\s+/.test(t)) {
        flushPara();
        const items = [];
        while (i < lines.length && /^[-*+]\\s+/.test(lines[i].trim())) {
          items.push(lines[i].trim().replace(/^[-*+]\\s+/, ''));
          i++;
        }
        out.push('<ul>' + items.map(it => '<li>' + renderInline(it) + '</li>').join('') + '</ul>');
        continue;
      }

      if (/^\\d+\\.\\s+/.test(t)) {
        flushPara();
        const items = [];
        while (i < lines.length && /^\\d+\\.\\s+/.test(lines[i].trim())) {
          items.push(lines[i].trim().replace(/^\\d+\\.\\s+/, ''));
          i++;
        }
        out.push('<ol>' + items.map(it => '<li>' + renderInline(it) + '</li>').join('') + '</ol>');
        continue;
      }

      // Pipe table: header row | separator | data rows. The separator
      // pattern is just dashes/colons/pipes/whitespace and must contain
      // at least one dash, which distinguishes it from a single-row
      // pipe-laden line.
      if (t.startsWith('|') && t.endsWith('|') && t.length > 2) {
        const next = (i + 1 < lines.length) ? lines[i + 1].trim() : '';
        if (/^\\|[\\s\\-:|]+\\|$/.test(next) && next.indexOf('-') >= 0) {
          flushPara();
          const splitRow = (line) =>
            line.slice(1, -1).split('|').map(c => c.trim());
          const headerCells = splitRow(t);
          i += 2;  // skip header + separator
          const rows = [];
          while (i < lines.length) {
            const rl = lines[i].trim();
            if (!(rl.startsWith('|') && rl.endsWith('|'))) break;
            rows.push(splitRow(rl));
            i++;
          }
          let html = '<table><thead><tr>';
          for (const h of headerCells) html += '<th>' + renderInline(h) + '</th>';
          html += '</tr></thead><tbody>';
          for (const r of rows) {
            html += '<tr>';
            for (const c of r) html += '<td>' + renderInline(c) + '</td>';
            html += '</tr>';
          }
          html += '</tbody></table>';
          out.push(html);
          continue;
        }
      }

      para.push(t);
      i++;
    }
    flushPara();
    return out.join('\\n');
  }

  function shortToolName(name) {
    return (name || '?').replace(/^mcp__ttrpg__/, '');
  }

  // Pretty-printed summary lines for the high-frequency MCP tools. Each
  // formatter returns an HTML string that lands inside .tool-pretty.
  // Parameters: (input, result) — result is null until the matching
  // tool_result event arrives, then the summary re-renders.
  const TOOL_PRETTY = {
    'roll': {
      glyph: '\\u2682',  // ⚂
      summary: (i, r) => {
        const f = i.formula || i.expression || i.dice || 'roll';
        let s = '<b>' + esc(f) + '</b>';
        if (i.purpose) s += ' <span class="dim">' + esc(i.purpose) + '</span>';
        if (r && typeof r.total === 'number') s += ' &rarr; <b>' + r.total + '</b>';
        else if (r && typeof r.roll === 'number') s += ' &rarr; <b>' + r.roll + '</b>';
        return s;
      },
    },
    'saving_throw': {
      glyph: '\\u2756',  // ❖
      summary: (i, r) => {
        const who = i.character || '?';
        const t = (i.type || 'save').replace(/_/g, ' ');
        let s = esc(who) + ' <span class="dim">·</span> ' + esc(t);
        if (r && r.success === true)  s += ' &rarr; <b class="pos">passed</b>';
        if (r && r.success === false) s += ' &rarr; <b class="neg">failed</b>';
        if (r && typeof r.roll === 'number') s += ' <span class="dim">(' + r.roll + ')</span>';
        return s;
      },
    },
    'skill_check': {
      glyph: '\\u2756',
      summary: (i, r) => {
        const who = i.character || '?';
        const sk = i.skill || '?';
        let s = esc(who) + ' <span class="dim">·</span> ' + esc(sk);
        if (r && r.success === true)  s += ' &rarr; <b class="pos">success</b>';
        if (r && r.success === false) s += ' &rarr; <b class="neg">fail</b>';
        if (r && typeof r.roll === 'number') s += ' <span class="dim">(' + r.roll + ')</span>';
        return s;
      },
    },
    'attack': {
      glyph: '\\u2694',  // ⚔
      summary: (i, r) => {
        const a = i.attacker || '?';
        const t = i.target || i.defender || '?';
        let s = esc(a) + ' &rarr; ' + esc(t);
        if (r) {
          if (r.hits === true)  s += ' <b class="pos">hit</b>';
          if (r.hits === false) s += ' <b class="neg">miss</b>';
          if (typeof r.damage === 'number' && r.hits) s += ' <b>' + r.damage + '</b>';
          if (typeof r.roll === 'number') s += ' <span class="dim">(' + r.roll + ')</span>';
        }
        return s;
      },
    },
    'apply_damage': {
      glyph: '\\u25BC',  // ▼
      summary: (i, r) => {
        const who = i.character || i.name || '?';
        let s = '<b class="neg">&minus;' + (i.amount ?? '?') + ' HP</b> &rarr; ' + esc(who);
        if (r && typeof r.hp_after === 'number') s += ' <span class="dim">(' + r.hp_after + ')</span>';
        if (r && r.dead) s += ' <b class="neg">dead</b>';
        else if (r && r.downed) s += ' <b class="neg">downed</b>';
        return s;
      },
    },
    'apply_combat_damage': null,    // alias (filled below)
    'apply_heal': {
      glyph: '\\u25B2',  // ▲
      summary: (i, r) => {
        const who = i.character || i.name || '?';
        let s = '<b class="pos">+' + (i.amount ?? '?') + ' HP</b> &rarr; ' + esc(who);
        if (r && typeof r.hp_after === 'number') s += ' <span class="dim">(' + r.hp_after + ')</span>';
        return s;
      },
    },
    'apply_combat_heal': null,
    'apply_combat_damage_override': null, // placeholder names if any
    'award_xp': {
      glyph: '\\u2730',  // ✰
      summary: (i, r) => {
        let s = '<b class="pos">+' + (i.amount ?? '?') + ' XP</b> &rarr; ' + esc(i.character || '?');
        if (r && typeof r.new_total === 'number') s += ' <span class="dim">(' + r.new_total.toLocaleString() + ')</span>';
        return s;
      },
    },
    'award_treasure': {
      glyph: '\\u29C9',  // ⧉
      summary: (i) => {
        const parts = [];
        for (const d of ['pp','gp','ep','sp','cp']) if (i[d]) parts.push(i[d] + ' ' + d);
        return parts.length ? parts.join(', ') : 'treasure';
      },
    },
    'update_coin': {
      glyph: '\\u29C9',
      summary: (i) => {
        const parts = [];
        for (const d of ['pp','gp','ep','sp','cp']) {
          if (i[d] != null && i[d] !== 0) {
            const sign = i[d] > 0 ? 'pos' : 'neg';
            parts.push('<span class="' + sign + '">' + (i[d] > 0 ? '+' : '') + i[d] + ' ' + d + '</span>');
          }
        }
        return parts.length ? parts.join(', ') : 'coin update';
      },
    },
    'add_inventory': {
      glyph: '\\u25C8',  // ◈
      summary: (i) => {
        const who = i.character || '?';
        const item = i.item || '?';
        const qty = i.qty || i.quantity || 1;
        return esc(who) + ' &larr; ' + esc(item) + (qty > 1 ? ' <b>&times;' + qty + '</b>' : '');
      },
    },
    'consume': {
      glyph: '\\u25C8',
      summary: (i) => {
        const who = i.character || '?';
        const item = i.item || '?';
        const qty = i.qty || 1;
        return esc(who) + ' uses <b>' + esc(item) + '</b>' + (qty > 1 ? ' &times;' + qty : '');
      },
    },
    'advance_time': {
      glyph: '\\u29D6',  // ⧖
      summary: (i) => '<b>+' + (i.minutes ?? '?') + ' min</b>',
    },
    'advance_calendar': {
      glyph: '\\u29D6',
      summary: (i) => {
        const d = i.days ?? '?';
        return '<b>+' + d + ' day' + (d === 1 ? '' : 's') + '</b>';
      },
    },
    'set_time': {
      glyph: '\\u29D6',
      summary: (i) => '<b>' + (i.hour != null ? String(i.hour).padStart(2,'0') + ':' + String(i.minute || 0).padStart(2,'0') : '?') + '</b>',
    },
    'introduce_npc': {
      glyph: '\\u2766',  // ❦
      summary: (i, r) => {
        const desc = [i.race, i.gender].filter(Boolean).join(' ');
        let s = 'NPC <span class="dim">' + esc(desc || '?') + '</span>';
        if (r && r.name) s += ' &rarr; <b>' + esc(r.name) + '</b>';
        return s;
      },
    },
    'reaction': {
      glyph: '\\u2766',
      summary: (i, r) => {
        let s = 'Reaction';
        if (i.npc) s += ' <span class="dim">' + esc(i.npc) + '</span>';
        if (r && (r.attitude || r.reaction)) s += ' &rarr; <b>' + esc(r.attitude || r.reaction) + '</b>';
        return s;
      },
    },
    'morale_check': {
      glyph: '\\u2766',
      summary: (i, r) => {
        let s = 'Morale';
        if (r) {
          if (r.broke === true || r.broken === true || r.success === false) s += ' &rarr; <b class="neg">broke</b>';
          else if (r.broke === false || r.success === true) s += ' &rarr; <b class="pos">held</b>';
        }
        return s;
      },
    },
    'next_turn': {
      glyph: '\\u29D6',
      summary: (i, r) => {
        if (r && r.round != null) return 'Round <b>' + r.round + '</b>' + (r.actor ? ' <span class="dim">' + esc(r.actor) + '</span>' : '');
        return 'next turn';
      },
    },
    'add_world_map_feature': {
      glyph: '\\u2316',  // ⌖
      summary: (i) => 'map feature: <b>' + esc((i.dsl_fragment || '').slice(0, 60)) + '</b>',
    },
  };
  // Aliases: pairs that share the same renderer.
  TOOL_PRETTY.apply_combat_damage = TOOL_PRETTY.apply_damage;
  TOOL_PRETTY.apply_combat_heal   = TOOL_PRETTY.apply_heal;

  function renderPrettySummary(name, input, result) {
    const fmt = TOOL_PRETTY[name];
    if (!fmt) return null;
    return '<span class="tool-glyph">' + fmt.glyph + '</span>'
         + '<span class="tool-pretty">' + fmt.summary(input || {}, result) + '</span>';
  }

  function summarizeArgs(input) {
    if (!input || typeof input !== 'object') return '';
    const keys = Object.keys(input).slice(0, 4);
    const parts = keys.map(k => {
      const v = input[k];
      if (typeof v === 'string') {
        const s = v.length > 32 ? v.slice(0, 30) + '…' : v;
        return k + '=' + JSON.stringify(s);
      }
      if (typeof v === 'number' || typeof v === 'boolean') return k + '=' + v;
      if (Array.isArray(v)) return k + '=[' + v.length + ']';
      if (v && typeof v === 'object') return k + '={…}';
      if (v === null) return k + '=null';
      return k + '=…';
    });
    return parts.join(', ');
  }

  function renderToolResult(blockContent) {
    // tool_result.content can be a string or a list of {type:'text', text:'…'}
    if (typeof blockContent === 'string') return blockContent;
    if (Array.isArray(blockContent)) {
      return blockContent.map(c => {
        if (c && typeof c.text === 'string') return c.text;
        if (c && typeof c === 'object') return JSON.stringify(c, null, 2);
        return String(c);
      }).join('\\n');
    }
    if (blockContent == null) return '';
    return JSON.stringify(blockContent, null, 2);
  }

  function appendToolUse(block) {
    if (firstAppend) { log.innerHTML = ''; firstAppend = false; }
    const det = document.createElement('details');
    det.className = 'turn tool';
    const name = shortToolName(block.name);
    const inputJson = JSON.stringify(block.input || {}, null, 2);

    // Prefer a custom pretty summary when one exists; otherwise fall back
    // to the original name(arg-preview) layout.
    const pretty = renderPrettySummary(name, block.input, null);
    const summaryInner = pretty
      ? pretty
      : ('<span class="tool-name">' + esc(name) + '</span>'
       + '<span class="tool-args">' + esc(summarizeArgs(block.input) ? '(' + summarizeArgs(block.input) + ')' : '()') + '</span>');

    det.innerHTML =
        '<summary>' + summaryInner + '</summary>'
      + '<div class="tool-body">'
      +   '<div class="tool-section">'
      +     '<div class="tool-section-label">Input</div>'
      +     '<pre class="tool-input">' + esc(inputJson) + '</pre>'
      +   '</div>'
      +   '<div class="tool-section">'
      +     '<div class="tool-section-label">Result</div>'
      +     '<pre class="tool-result"><span class="tool-pending">awaiting…</span></pre>'
      +   '</div>'
      + '</div>';
    if (thinkingEl && thinkingEl.parentElement === log) {
      log.insertBefore(det, thinkingEl);
    } else {
      log.appendChild(det);
    }
    maybeScrollToBottom();
    if (block.id) toolElements.set(block.id, {el: det, name: name, input: block.input || {}});
    return det;
  }

  function fillToolResult(toolUseId, content, isError) {
    const entry = toolElements.get(toolUseId);
    if (!entry) return;
    const det = entry.el;

    // Body: always show the raw result text in the expandable section.
    const resultEl = det.querySelector('.tool-result');
    const text = renderToolResult(content);
    if (resultEl) {
      resultEl.textContent = text || '(empty)';
      if (isError) resultEl.classList.add('tool-error');
    }

    // Summary: re-render with the parsed result so e.g. a roll updates from
    // "1d20" to "1d20 → 17". Skip if there's no formatter for this tool.
    if (TOOL_PRETTY[entry.name]) {
      let parsed = null;
      try { parsed = text ? JSON.parse(text) : null; } catch (e) { /* leave null */ }
      const summary = det.querySelector('summary');
      if (summary) {
        const re = renderPrettySummary(entry.name, entry.input, parsed);
        if (re) summary.innerHTML = re;
      }
    }
  }

  function startThinking() {
    stopThinking();
    if (firstAppend) { log.innerHTML = ''; firstAppend = false; }
    thinkingTools = 0;
    thinkingStart = performance.now();
    thinkingEl = document.createElement('div');
    thinkingEl.className = 'turn thinking';
    thinkingEl.innerHTML =
        '<div class="thinking-row">'
      +   '<span class="thinking-glyph" aria-hidden="true"><span></span><span></span><span></span></span>'
      +   '<span class="thinking-text">DM is thinking…</span>'
      +   '<span class="thinking-time">0.0s</span>'
      + '</div>'
      + '<div class="dyk" aria-live="polite">'
      +   '<span class="dyk-label">Did you know</span>'
      +   '<span class="dyk-fact"></span><span class="dyk-cat"></span>'
      + '</div>';
    log.appendChild(thinkingEl);
    maybeScrollToBottom();
    updateThinkingTime();
    thinkingTimer = setInterval(updateThinkingTime, 200);
    // Rotate lore facts during the wait (only if any loaded).
    if (dykFacts.length) {
      thinkingEl.classList.add('has-dyk');
      renderNextFact();
      dykTimer = setInterval(renderNextFact, DYK_ROTATE_MS);
    }
  }

  function updateThinkingTime() {
    if (!thinkingEl) return;
    const sec = ((performance.now() - thinkingStart) / 1000).toFixed(1);
    const note = thinkingTools > 0
      ? '  ·  ' + thinkingTools + ' tool call' + (thinkingTools === 1 ? '' : 's')
      : '';
    const t = thinkingEl.querySelector('.thinking-time');
    if (t) t.textContent = sec + 's' + note;
  }

  function stopThinking() {
    if (thinkingTimer) { clearInterval(thinkingTimer); thinkingTimer = null; }
    if (dykTimer) { clearInterval(dykTimer); dykTimer = null; }
    if (thinkingEl && thinkingEl.parentElement) thinkingEl.remove();
    thinkingEl = null;
  }

  // --- Browser-tab activity indicator -------------------------------------
  // Reflects the DM turn state in the favicon so you can tell at a glance
  // (or from another tab) whether a response is ongoing or complete:
  //   ongoing  -> gold dot favicon
  //   complete -> favicon restored; but if the turn finishes while this tab is
  //               in the background, a green check flags it until you return.
  const FAVI_IDLE = '/static/favicon.svg';
  const FAVI_BUSY = 'data:image/svg+xml,' + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="#c8a96e"/></svg>');
  const FAVI_DONE = 'data:image/svg+xml,' + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="#3f9142"/><path d="M4.5 8.3L7 10.8l4.5-5.3" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>');

  function setFavicon(href) {
    let l = document.querySelector('link[rel="icon"]');
    if (!l) { l = document.createElement('link'); l.rel = 'icon'; document.head.appendChild(l); }
    l.type = 'image/svg+xml';
    l.href = href;
  }
  function tabBusy() { setFavicon(FAVI_BUSY); }
  function tabIdle() { setFavicon(FAVI_IDLE); }
  function tabDone() {
    // Only flag completion when the user is looking elsewhere; if they're on
    // this tab they can already see the result, so just reset.
    if (document.hidden) { setFavicon(FAVI_DONE); }
    else { tabIdle(); }
  }
  // Returning to the tab clears a lingering "complete" flag, but never while a
  // turn is still in flight (tabBusy owns the indicator then).
  document.addEventListener('visibilitychange', function() {
    if (!document.hidden && !localTurnActive) tabIdle();
  });

  function clearEmptyState() {
    if (firstAppend) {
      const placeholder = document.getElementById('empty-state');
      if (placeholder) placeholder.remove();
      firstAppend = false;
    }
  }

  function buildHistoryTurn(t) {
    const div = document.createElement('div');
    div.className = 'turn ' + t.role;
    if (t.role === 'dm') {
      div.innerHTML = renderMarkdown(t.text);
      attachDmControls(div, t.text);
    } else if (t.role === 'tool') {
      div.classList.add('history-tool');
      div.textContent = t.text;
    } else {
      div.textContent = t.text;
    }
    return div;
  }

  function rebindLastDmTurnFromLog() {
    // After history replay, find the bottom-most DM turn so the suggest
    // toggle has an anchor for its chip row even when the user hasn't
    // sent a turn yet this page-load.
    const turns = log.querySelectorAll('.turn.dm');
    lastDmTurnEl = turns.length ? turns[turns.length - 1] : null;
  }

  function append(role, text) {
    clearEmptyState();
    const div = document.createElement('div');
    div.className = 'turn ' + role;
    if (role === 'dm') {
      div.innerHTML = renderMarkdown(text);
      attachDmControls(div, text);
      lastDmTurnEl = div;
      clearSuggestions();
    }
    else {
      div.textContent = text;
    }
    // Insert before the thinking indicator (if it's still up) so the new
    // turn appears above the still-running spinner — keeps things in
    // chronological order.
    if (thinkingEl && thinkingEl.parentElement === log) {
      log.insertBefore(div, thinkingEl);
    } else {
      log.appendChild(div);
    }
    maybeScrollToBottom();
  }

  function clearSuggestions() {
    if (suggestController) {
      try { suggestController.abort(); } catch (e) {}
      suggestController = null;
    }
    if (suggestEl && suggestEl.parentElement) suggestEl.remove();
    suggestEl = null;
  }

  function ensureSuggestRow() {
    if (!lastDmTurnEl) return null;
    if (suggestEl && suggestEl.parentElement === lastDmTurnEl.parentElement) return suggestEl;
    if (suggestEl && suggestEl.parentElement) suggestEl.remove();
    suggestEl = document.createElement('div');
    suggestEl.className = 'suggest-row';
    lastDmTurnEl.after(suggestEl);
    return suggestEl;
  }

  function renderSuggestLoading() {
    const row = ensureSuggestRow();
    if (!row) return;
    row.textContent = '';
    const s = document.createElement('span');
    s.className = 'suggest-loading';
    s.textContent = 'Thinking of options…';
    row.appendChild(s);
  }

  function renderSuggestions(actions) {
    if (!suggestEnabled) return;
    if (!Array.isArray(actions) || actions.length < 2) {
      clearSuggestions();
      return;
    }
    const row = ensureSuggestRow();
    if (!row) return;
    row.textContent = '';
    for (const a of actions) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'suggest-chip';
      btn.textContent = a;
      btn.addEventListener('click', () => {
        const text = a;
        clearSuggestions();
        input.value = '';
        submitTurn(text);
      });
      row.appendChild(btn);
    }
    maybeScrollToBottom();
  }

  async function fetchSuggestions(narrative) {
    if (!suggestEnabled) return;
    if (!narrative || !lastDmTurnEl) return;
    if (suggestController) {
      try { suggestController.abort(); } catch (e) {}
    }
    suggestController = new AbortController();
    const sig = suggestController.signal;
    renderSuggestLoading();
    try {
      const r = await fetch('/play/suggest_actions', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({narrative: narrative.slice(0, 4000)}),
        signal: sig,
      });
      if (!r.ok) { clearSuggestions(); return; }
      const d = await r.json();
      if (sig.aborted) return;
      renderSuggestions(d.actions || []);
    } catch (err) {
      // AbortError or network failure — silently drop the chip row.
      clearSuggestions();
    } finally {
      if (suggestController && suggestController.signal === sig) {
        suggestController = null;
      }
    }
  }

  function attachDmControls(turnEl, text) {
    const controls = document.createElement('div');
    controls.className = 'dm-controls';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'narrate-btn';
    btn.title = 'Narrate this turn';
    btn.setAttribute('aria-label', 'Narrate this turn');
    btn.textContent = '\\u25B6';   // ▶
    const meta = document.createElement('span');
    meta.className = 'narrate-meta';
    controls.appendChild(btn);
    controls.appendChild(meta);
    btn.addEventListener('click', function(){ narrate(btn, meta, text); });
    turnEl.appendChild(controls);
  }

  // Single-instance narration: only one DM turn plays at a time. Starting
  // a new narration stops any other in-flight playback so they don't
  // overlap. Esc key stops the current one from anywhere on the page.
  let activeNarration = null;   // { btn, audio }

  function stopActiveNarration() {
    if (!activeNarration) return;
    const { btn, audio } = activeNarration;
    try { audio.pause(); audio.currentTime = 0; } catch (e) { /* ignore */ }
    btn.classList.remove('playing');
    btn.textContent = '\\u25B6';
    activeNarration = null;
  }

  async function narrate(btn, meta, text) {
    // Same-button toggle: clicking the active button stops playback.
    if (activeNarration && activeNarration.btn === btn) {
      stopActiveNarration();
      return;
    }
    // Different-button takeover: stop whatever's playing, then proceed.
    if (activeNarration) stopActiveNarration();

    if (btn.dataset.audioUrl) {
      playAudio(btn, btn.dataset.audioUrl);
      return;
    }
    btn.disabled = true;
    btn.textContent = '\\u25CB';   // ○ pending
    meta.textContent = ' synthesizing…';
    try {
      const r = await fetch('/api/tts/synthesize', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: text}),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
      btn.dataset.audioUrl = d.audio_url;
      // Render segment breakdown.
      const tags = (d.segments || []).map(function(s){
        const label = s.kind === 'speech' ? (s.speaker || 'NPC') : 'narrator';
        const cls = 'seg-tag seg-' + s.kind;
        return '<span class="' + cls + '" title="' + s.voice + ' · ' + s.char_count + ' chars">' + label + '</span>';
      }).join('');
      const cached = d.cached ? ' (cached)' : '';
      meta.innerHTML = tags + ' <span class="muted">' + d.total_chars + ' chars' + cached + '</span>';
      playAudio(btn, d.audio_url);
    } catch (err) {
      meta.textContent = ' error: ' + err.message;
      btn.textContent = '\\u25B6';
    } finally {
      btn.disabled = false;
    }
  }

  function playAudio(btn, url) {
    const audio = new Audio(url);
    activeNarration = { btn: btn, audio: audio };
    btn.classList.add('playing');
    btn.textContent = '\\u25A0';   // ■ stop
    const cleanup = function(){
      btn.classList.remove('playing');
      btn.textContent = '\\u25B6';
      if (activeNarration && activeNarration.audio === audio) activeNarration = null;
    };
    audio.addEventListener('ended', cleanup);
    audio.addEventListener('error', cleanup);
    audio.play().catch(cleanup);
  }

  // Global Esc stops narration. Don't intercept if the user is editing text.
  document.addEventListener('keydown', function(e){
    if (e.key !== 'Escape') return;
    if (!activeNarration) return;
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'TEXTAREA' || tag === 'INPUT') return;
    stopActiveNarration();
  });

  function appendMeta(text, tooltip) {
    clearEmptyState();
    const div = document.createElement('div');
    div.className = 'turn meta';
    div.textContent = text;
    if (tooltip) {
      const icon = document.createElement('span');
      icon.className = 'meta-info';
      icon.textContent = '\\u24D8';   // circled lowercase i
      icon.title = tooltip;
      div.appendChild(icon);
    }
    if (thinkingEl && thinkingEl.parentElement === log) {
      log.insertBefore(div, thinkingEl);
    } else {
      log.appendChild(div);
    }
    maybeScrollToBottom();
  }

  // Debug capture — keep the last 200 stream-json events in memory so the
  // 🐛 dialog can show what claude -p actually sent (token usage lives in
  // the result events' .usage field; tool calls are in assistant events;
  // the user's own message round-trips back as a user event with the
  // tool_result blocks). Capture is unconditional and cheap; the dialog
  // is the only thing that exposes it.
  const DEBUG_BUFFER_MAX = 1000;
  const debugEvents = [];
  // Token-streaming state. When --include-partial-messages is on, assistant
  // text arrives as stream_event/content_block_delta records; we accumulate
  // them into a single live DM bubble and skip the matching text block on
  // the final assistant event so it doesn't render twice. streamedText is a
  // per-turn flag so a missing partial stream falls back to block render.
  let currentDmEl = null;
  let currentDmText = '';     // text already revealed in the DOM
  let pendingDmText = '';     // text buffered from deltas, waiting to reveal
  let dmRafId = null;
  let dmLastTickTime = 0;
  // Typewriter pacing for streamed text. Deltas arrive in chunky bursts of
  // several words; rendering each one immediately reads as "blocks appearing".
  // We buffer into pendingDmText and drain it character-by-character on a
  // requestAnimationFrame tick, scaling speed with backlog so long responses
  // don't lag behind generation.
  const REVEAL_BASE_CPS = 80;      // baseline chars/sec on a near-empty buffer
  const REVEAL_MAX_BOOST = 4;      // multiplier ceiling when backlog is large
  let streamedText = false;
  // Tracks whether THIS tab is the one driving the current turn. Used by
  // the recovery poller so it doesn't misread "local turn streaming, but
  // spinner already dropped because text started arriving" as a reload-
  // mid-stream scenario and re-append the narrative at result time.
  let localTurnActive = false;

  function scheduleDmReveal() {
    if (dmRafId !== null) return;
    dmLastTickTime = performance.now();
    dmRafId = requestAnimationFrame(dmRevealTick);
  }

  function dmRevealTick(now) {
    dmRafId = null;
    if (!currentDmEl) {
      pendingDmText = '';
      return;
    }
    if (!pendingDmText.length) return;
    const dt = Math.max(0, (now - dmLastTickTime) / 1000);
    dmLastTickTime = now;
    // Backlog boost: when many chars are queued (slow client / fast model)
    // we speed up so the reveal doesn't run minutes behind the actual reply.
    const boost = Math.min(REVEAL_MAX_BOOST, 1 + pendingDmText.length / 60);
    const n = Math.max(1, Math.floor(REVEAL_BASE_CPS * boost * dt));
    const take = pendingDmText.slice(0, n);
    pendingDmText = pendingDmText.slice(n);
    currentDmText += take;
    currentDmEl.innerHTML = renderMarkdown(currentDmText);
    maybeScrollToBottom();
    if (pendingDmText.length > 0) {
      dmRafId = requestAnimationFrame(dmRevealTick);
    }
  }

  function flushDmReveal() {
    if (dmRafId !== null) {
      cancelAnimationFrame(dmRafId);
      dmRafId = null;
    }
    if (pendingDmText && currentDmEl) {
      currentDmText += pendingDmText;
      currentDmEl.innerHTML = renderMarkdown(currentDmText);
      maybeScrollToBottom();
    }
    pendingDmText = '';
  }

  function handleEvent(evt) {
    debugEvents.push(evt);
    if (debugEvents.length > DEBUG_BUFFER_MAX) {
      debugEvents.splice(0, debugEvents.length - DEBUG_BUFFER_MAX);
    }
    if (evt.type === 'system' && evt.session_id) {
      sidEl.querySelector('code').textContent = evt.session_id.slice(0, 8);
      sidEl.href = '/sessions/' + evt.session_id;
    } else if (evt.type === 'stream_event' && evt.event) {
      const ev = evt.event;
      if (ev.type === 'content_block_start' && ev.content_block && ev.content_block.type === 'text') {
        stopThinking();
        currentDmText = '';
        clearEmptyState();
        currentDmEl = document.createElement('div');
        currentDmEl.className = 'turn dm';
        if (thinkingEl && thinkingEl.parentElement === log) {
          log.insertBefore(currentDmEl, thinkingEl);
        } else {
          log.appendChild(currentDmEl);
        }
        maybeScrollToBottom();
      } else if (ev.type === 'content_block_delta' && ev.delta && ev.delta.type === 'text_delta') {
        streamedText = true;
        if (!currentDmEl) {
          stopThinking();
          clearEmptyState();
          currentDmEl = document.createElement('div');
          currentDmEl.className = 'turn dm';
          if (thinkingEl && thinkingEl.parentElement === log) {
            log.insertBefore(currentDmEl, thinkingEl);
          } else {
            log.appendChild(currentDmEl);
          }
        }
        pendingDmText += ev.delta.text || '';
        scheduleDmReveal();
      } else if (ev.type === 'content_block_stop' && currentDmEl) {
        flushDmReveal();
        if (currentDmText) {
          attachDmControls(currentDmEl, currentDmText);
          lastDmTurnEl = currentDmEl;
          lastDmText += currentDmText;
          clearSuggestions();
        }
        currentDmEl = null;
        currentDmText = '';
      }
    } else if (evt.type === 'assistant' && evt.message && Array.isArray(evt.message.content)) {
      for (const block of evt.message.content) {
        if (block.type === 'text' && block.text) {
          // If --include-partial-messages already streamed this turn's text
          // via stream_event deltas, skip — otherwise it would render twice.
          if (streamedText) continue;
          stopThinking();          // first prose arrives — drop the indicator
          append('dm', block.text);
          lastDmText += block.text;
        } else if (block.type === 'tool_use') {
          thinkingTools++;
          updateThinkingTime();
          appendToolUse(block);
        }
      }
    } else if (evt.type === 'user' && evt.message && Array.isArray(evt.message.content)) {
      // Tool results: pair each tool_result block with the matching tool_use
      // element by id and fill in its result section.
      for (const block of evt.message.content) {
        if (block && block.type === 'tool_result' && block.tool_use_id) {
          fillToolResult(block.tool_use_id, block.content, !!block.is_error);
        }
      }
    } else if (evt.type === 'result') {
      const parts = ['turn complete'];
      if (typeof evt.duration_ms === 'number') parts.push((evt.duration_ms / 1000).toFixed(1) + 's');
      // Cost + tokens hidden behind a hover icon: claude -p reports
      // API-equivalent dollars even on subscription, so the number is
      // informational only — but useful for sizing context growth.
      const tip = [];
      if (typeof evt.total_cost_usd === 'number') {
        tip.push('Cost (API-equivalent): $' + evt.total_cost_usd.toFixed(4));
      }
      const u = evt.usage || {};
      if (typeof u.input_tokens === 'number' || typeof u.output_tokens === 'number') {
        tip.push('Tokens: ' + (u.input_tokens || 0).toLocaleString() + ' in / '
                            + (u.output_tokens || 0).toLocaleString() + ' out');
      }
      if (typeof u.cache_read_input_tokens === 'number' || typeof u.cache_creation_input_tokens === 'number') {
        tip.push('Cache: ' + (u.cache_read_input_tokens || 0).toLocaleString() + ' read / '
                           + (u.cache_creation_input_tokens || 0).toLocaleString() + ' write');
      }
      appendMeta(parts.join(' · '), tip.join('\\n'));
      refreshState();
      refreshSessionSize();
      if (bellEnabled) playBell();
      if (suggestEnabled) fetchSuggestions(lastDmText);
    } else if (evt.type === 'error') {
      append('error', 'rc=' + evt.returncode + '  ' + (evt.stderr || '').slice(0, 500));
    }
  }

  async function submitTurn(text) {
    clearSuggestions();
    append('player', text);
    startThinking();
    tabBusy();
    send.disabled = true;
    lastDmText = '';
    streamedText = false;
    currentDmEl = null;
    currentDmText = '';
    pendingDmText = '';
    if (dmRafId !== null) { cancelAnimationFrame(dmRafId); dmRafId = null; }
    localTurnActive = true;
    try {
      const resp = await fetch('/play/turn', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: text}),
      });
      if (!resp.ok) {
        append('error', 'HTTP ' + resp.status);
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        let nl;
        while ((nl = buf.indexOf('\\n')) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          try { handleEvent(JSON.parse(line)); }
          catch (e) { append('error', 'bad json: ' + line.slice(0, 80)); }
        }
      }
    } catch (err) {
      append('error', String(err));
    } finally {
      flushDmReveal();          // make sure anything queued is shown before we exit
      stopThinking();
      localTurnActive = false;
      tabDone();
      send.disabled = false;
      statusEl.textContent = '';
      input.focus();
    }
  }

  function pip(label, value, klass) {
    return '<span class="pip ' + (klass || '') + '">'
      + (label ? '<span class="pip-label">' + esc(label) + '</span>' : '')
      + '<span class="pip-val">' + esc(value) + '</span>'
      + '</span>';
  }

  function renderParty(chars) {
    if (!chars || !chars.length) {
      partyList.innerHTML = '<div class="muted" style="text-align:center; font-size:0.82em; font-style:italic">No characters in roster.</div>';
      return;
    }
    partyList.innerHTML = chars.map(c => {
      const pct = c.hp_max > 0 ? Math.max(0, Math.min(100, (c.hp / c.hp_max) * 100)) : 0;
      const ko = (c.hp || 0) <= 0;
      const portrait = c.portrait
        ? '<img src="' + esc(c.portrait) + '" alt="' + esc(c.label) + '">'
        : '<span>' + esc((c.label || '?').slice(0, 1).toUpperCase()) + '</span>';
      const conds = (c.conditions || []).map(x =>
        '<span class="cond-chip">' + esc(x) + '</span>').join('');
      const cls = [c.alignment, c.cls, c.level].filter(Boolean).join(' ').trim();
      const acStr = (c.ac !== undefined && c.ac !== null) ? 'AC ' + c.ac : '';
      const sub = [cls, acStr].filter(Boolean).join('  \\u00b7  ');
      const sheetUrl = '/sheets/' + encodeURIComponent(c.key || '');

      // XP bar: only when the class has a level table AND the character
      // isn't at max level (xp_next === null means max). Progress is
      // measured *within* the current level: (xp - floor) / (next - floor).
      let xpHtml = '';
      if (c.xp_next != null && c.xp_next > c.xp_floor) {
        const span = c.xp_next - c.xp_floor;
        const into = Math.max(0, c.xp - c.xp_floor);
        const xpPct = Math.max(0, Math.min(100, (into / span) * 100));
        const xpFmt = (n) => Number(n || 0).toLocaleString();
        xpHtml = ''
          + '<div class="party-mini-xp" title="XP ' + xpFmt(c.xp) + ' / ' + xpFmt(c.xp_next) + ' (next level)">'
          +   '<span class="xp-meter"><i style="width:' + xpPct.toFixed(1) + '%"></i></span>'
          +   '<span class="party-mini-xp-text">' + xpFmt(c.xp) + '</span>'
          + '</div>';
      }

      return ''
        + '<article class="party-mini">'
        +   '<div class="portrait-frame ' + (ko ? 'kod ' : '') + (c.portrait ? '' : 'empty') + '"' + (c.portrait ? ' title="Click to enlarge"' : '') + '>' + portrait + '</div>'
        +   '<div class="party-mini-body">'
        +     '<a class="party-mini-name" href="' + sheetUrl + '">' + esc(c.label) + '</a>'
        +     (sub ? '<div class="party-mini-class">' + esc(sub) + '</div>' : '')
        +     xpHtml
        +     '<div class="party-mini-hp">'
        +       '<span class="hp-meter"><i style="width:' + pct.toFixed(1) + '%"></i></span>'
        +       '<span class="party-mini-hp-text">' + esc(c.hp) + '/' + esc(c.hp_max) + '</span>'
        +     '</div>'
        +     (conds ? '<div class="party-mini-conds">' + conds + '</div>' : '')
        +   '</div>'
        + '</article>';
    }).join('');
  }

  function renderSidebarStats(s) {
    // sideCamp.innerHTML = (s.campaign && s.campaign.name) ? esc(s.campaign.name) : '';
    // Session number lives in the global header now, beside the session hash.
    const sessNoEl = document.getElementById('play-session-no');
    if (sessNoEl) sessNoEl.textContent = s.session ? String(s.session) : '';
    const rows = [];
    const r1 = [];
    if (s.day) r1.push(pip('Day', s.time ? (s.day + '  ' + s.time) : String(s.day)));
    if (s.coin) r1.push(pip('', s.coin, 'pip-gold'));
    if (r1.length) rows.push('<div class="side-row">' + r1.join('') + '</div>');
    if (s.weather) rows.push('<div class="side-row">' + pip('Sky', s.weather) + '</div>');
    if (s.location) {
      rows.push('<div class="side-row row-loc">' + pip('At', s.location) + '</div>');
    }
    if (s.in_combat) {
      rows.push('<div class="side-row row-combat">' + pip('', '⚔ In Combat') + '</div>');
    }
    sideStats.innerHTML = rows.join('');
  }

  async function refreshState() {
    try {
      const r = await fetch('/api/play_state.json');
      if (!r.ok) return;
      const s = await r.json();
      renderParty(s.characters);
      renderSidebarStats(s);
    } catch (e) {
      // best-effort: leave existing render in place
    }
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    submitTurn(text);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  resetBtn.addEventListener('click', async () => {
    if (!confirm('Start a new DM session? The current conversation will no longer be resumed.')) return;
    await fetch('/play/reset', {method: 'POST'});
    sidEl.querySelector('code').textContent = '(new)';
    sidEl.href = '#';
    renderSessionMeta({context_tokens: null});
    append('meta', 'new session started');
  });

  async function runMakePicture(hint) {
    if (!lastDmText) {
      statusEl.textContent = 'no DM text yet';
      setTimeout(() => { statusEl.textContent = ''; }, 2000);
      return;
    }
    if (firstAppend) { log.innerHTML = ''; firstAppend = false; }
    const placeholder = document.createElement('div');
    placeholder.className = 'turn generating';
    placeholder.textContent = hint
      ? 'illustrating the scene with hint: "' + hint + '"…'
      : 'composing prompt and illustrating the scene…';
    log.appendChild(placeholder);
    log.scrollTop = log.scrollHeight;
    makePicBtn.disabled = true;
    hintBtn.disabled = true;
    try {
      const body = {prompt: lastDmText.slice(0, 4000)};
      if (hint) body.hint = hint;
      const r = await fetch('/play/generate_scene', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok || d.error) {
        placeholder.className = 'turn error';
        placeholder.textContent = 'image generation failed: ' + (d.error || ('HTTP ' + r.status));
        return;
      }
      placeholder.className = 'turn scene';
      const captionLabel = d.prepass ? 'scene · Claude-composed' : 'scene';
      const promptDetails = d.prompt
        ? '<details class="scene-prompt"><summary>prompt</summary><p>' + esc(d.prompt) + '</p></details>'
        : '';
      placeholder.innerHTML =
          '<img src="' + esc(d.url) + '" alt="generated scene">'
        + '<span class="caption">' + esc(captionLabel) + '</span>'
        + promptDetails;
      maybeScrollToBottom();
    } catch (err) {
      placeholder.className = 'turn error';
      placeholder.textContent = 'image generation failed: ' + String(err);
    } finally {
      makePicBtn.disabled = false;
      hintBtn.disabled = false;
    }
  }

  const hintBtn = document.getElementById('play-make-pic-hint');
  const hintDialog = document.getElementById('hint-dialog');
  const hintForm = document.getElementById('hint-form');
  const hintText = document.getElementById('hint-text');
  const hintCancel = document.getElementById('hint-cancel');

  makePicBtn.addEventListener('click', () => runMakePicture(''));

  hintBtn.addEventListener('click', () => {
    if (!lastDmText) {
      statusEl.textContent = 'no DM text yet';
      setTimeout(() => { statusEl.textContent = ''; }, 2000);
      return;
    }
    hintText.value = '';
    if (typeof hintDialog.showModal === 'function') {
      hintDialog.showModal();
    } else {
      // Older browser fallback — shouldn't happen, but degrades to prompt().
      const h = window.prompt('Hint for the illustrator:');
      if (h !== null) runMakePicture(h.trim());
      return;
    }
    setTimeout(() => hintText.focus(), 30);
  });
  hintCancel.addEventListener('click', () => hintDialog.close());
  hintForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const h = hintText.value.trim();
    hintDialog.close();
    runMakePicture(h);
  });

  // --- Voice mapping dialog ---
  const voiceBtn = document.getElementById('play-voices');
  const voiceDialog = document.getElementById('voice-dialog');
  const voiceForm = document.getElementById('voice-form');
  const voiceCancel = document.getElementById('voice-cancel');
  const voiceTbody = document.querySelector('#voice-table tbody');

  function buildVoiceSelect(name, current, options, allowEmpty) {
    let html = '<select name="' + name + '">';
    if (allowEmpty) {
      html += '<option value="">— use default NPC voice —</option>';
    }
    for (const v of options) {
      const sel = (v === current) ? ' selected' : '';
      html += '<option value="' + v + '"' + sel + '>' + v + '</option>';
    }
    html += '</select>';
    return html;
  }

  voiceBtn.addEventListener('click', async () => {
    voiceTbody.innerHTML = '<tr><td colspan="2" class="muted">loading…</td></tr>';
    if (typeof voiceDialog.showModal === 'function') voiceDialog.showModal();
    try {
      const r = await fetch('/api/tts/voices');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      const valid = d.valid_voices || [];
      const m = d.mapping || {};
      const chars = d.characters || [];
      const charMap = m.characters || {};
      let html = '';
      html += '<tr><td class="label">Narrator</td><td>'
            + buildVoiceSelect('narrator', m.narrator, valid, false) + '</td></tr>';
      html += '<tr><td class="label">Default NPC</td><td>'
            + buildVoiceSelect('default_npc', m.default_npc, valid, false) + '</td></tr>';
      if (chars.length === 0) {
        html += '<tr class="row-divider"><td colspan="2" class="muted">'
              + 'No characters yet — voices will be assignable once NPCs are introduced.'
              + '</td></tr>';
      } else {
        chars.forEach(function(slug, idx){
          const cls = idx === 0 ? 'row-divider' : '';
          html += '<tr class="' + cls + '"><td class="label">' + slug + '</td><td>'
                + buildVoiceSelect('char__' + slug, charMap[slug] || '', valid, true)
                + '</td></tr>';
        });
      }
      voiceTbody.innerHTML = html;
    } catch (err) {
      voiceTbody.innerHTML = '<tr><td colspan="2" class="muted">load failed: '
                           + err.message + '</td></tr>';
    }
  });

  voiceCancel.addEventListener('click', () => voiceDialog.close());

  voiceForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(voiceForm);
    const payload = {
      narrator: fd.get('narrator') || 'alloy',
      default_npc: fd.get('default_npc') || 'fable',
      characters: {},
    };
    for (const [k, v] of fd.entries()) {
      if (k.startsWith('char__') && v) {
        payload.characters[k.slice('char__'.length)] = v;
      }
    }
    try {
      const r = await fetch('/api/tts/voices', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        alert('Save failed: ' + (d.error || ('HTTP ' + r.status)));
        return;
      }
      voiceDialog.close();
      // Invalidate cached audio refs on the page so re-clicks resynthesize
      // with the new voice mapping.
      document.querySelectorAll('.narrate-btn').forEach(function(b){
        delete b.dataset.audioUrl;
        b.classList.remove('playing');
        b.textContent = '\\u25B6';
        b._audio = null;
      });
      document.querySelectorAll('.narrate-meta').forEach(function(m){ m.innerHTML = ''; });
    } catch (err) {
      alert('Save failed: ' + err.message);
    }
  });

  // --- Debug dialog ---
  const debugBtn    = document.getElementById('play-debug');
  const debugDialog = document.getElementById('debug-dialog');
  const debugPre    = document.getElementById('debug-pre');
  const debugCount  = document.getElementById('debug-count');
  const debugClear  = document.getElementById('debug-clear');
  const debugCopy   = document.getElementById('debug-copy');
  const debugClose  = document.getElementById('debug-close');
  const debugReset  = document.getElementById('debug-force-reset');

  function renderDebug() {
    debugCount.textContent = '(' + debugEvents.length + ')';
    debugPre.textContent = debugEvents.length === 0
      ? '(none captured yet)'
      : JSON.stringify(debugEvents, null, 2);
  }

  debugBtn.addEventListener('click', () => {
    renderDebug();
    if (typeof debugDialog.showModal === 'function') {
      debugDialog.showModal();
      // Scroll to the bottom so the most recent events are visible.
      requestAnimationFrame(() => { debugPre.scrollTop = debugPre.scrollHeight; });
    }
  });
  debugClose.addEventListener('click', () => debugDialog.close());
  debugClear.addEventListener('click', () => {
    debugEvents.length = 0;
    renderDebug();
  });
  debugCopy.addEventListener('click', async () => {
    const text = JSON.stringify(debugEvents, null, 2);
    const orig = debugCopy.textContent;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // Fallback: select the pre and execCommand-copy.
        const sel = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(debugPre);
        sel.removeAllRanges(); sel.addRange(range);
        document.execCommand('copy');
        sel.removeAllRanges();
      }
      debugCopy.textContent = 'Copied ✓';
    } catch (err) {
      debugCopy.textContent = 'Copy failed';
    }
    setTimeout(() => { debugCopy.textContent = orig; }, 1400);
  });
  debugReset.addEventListener('click', async () => {
    if (!confirm('Force-reset the in-flight DM turn? This kills any running claude -p subprocess and clears the server-side lock. Use only if a turn appears wedged.')) return;
    const orig = debugReset.textContent;
    debugReset.disabled = true;
    debugReset.textContent = 'Resetting…';
    try {
      const r = await fetch('/play/force_reset', {method: 'POST'});
      const d = await r.json().catch(() => ({}));
      stopThinking();
      stopRecovery();
      send.disabled = false;
      lockedBySrv = false;
      debugReset.textContent = d.killed_subprocess ? 'Killed ✓' : 'Cleared ✓';
    } catch (e) {
      debugReset.textContent = 'Failed';
    }
    setTimeout(() => { debugReset.textContent = orig; debugReset.disabled = false; }, 1600);
  });

  // ----- Suggested-actions toggle -----
  const suggestBtn = document.getElementById('play-suggest');
  function applySuggestVisual() {
    suggestBtn.classList.toggle('suggest-on', suggestEnabled);
    suggestBtn.title = suggestEnabled
      ? 'Suggested next actions: ON — click to disable'
      : 'Suggested next actions: OFF — click to enable';
  }
  applySuggestVisual();
  suggestBtn.addEventListener('click', () => {
    suggestEnabled = !suggestEnabled;
    localStorage.setItem('play.suggest-actions', suggestEnabled ? '1' : '0');
    applySuggestVisual();
    if (!suggestEnabled) {
      clearSuggestions();
    } else if (lastDmText) {
      if (!lastDmTurnEl) rebindLastDmTurnFromLog();
      fetchSuggestions(lastDmText);
    }
  });

  // ----- Response-chime toggle -----
  const bellBtn = document.getElementById('play-bell');
  function applyBellVisual() {
    bellBtn.classList.toggle('bell-on', bellEnabled);
    bellBtn.title = bellEnabled
      ? 'Response chime: ON — click to disable'
      : 'Response chime: OFF — click to enable';
  }
  applyBellVisual();
  bellBtn.addEventListener('click', () => {
    bellEnabled = !bellEnabled;
    localStorage.setItem('play.bell', bellEnabled ? '1' : '0');
    applyBellVisual();
    if (bellEnabled) playBell(); // immediate preview + unlocks the AudioContext
  });

  // Narrative font-family cycle: three options, from thematic to maximally
  // readable. The button itself renders in the active font as a preview.
  const FONTS = [
    {key: 'garamond', label: 'Garamond',  stack: "'EB Garamond', Georgia, 'Times New Roman', serif"},
    {key: 'lora',     label: 'Lora',      stack: "'Lora', Georgia, 'Times New Roman', serif"},
    {key: 'atkinson', label: 'Atkinson Hyperlegible', stack: "'Atkinson Hyperlegible', system-ui, sans-serif"},
  ];
  const FONT_FAMILY_KEY = 'play.font-family';
  const fontFamilyBtn = document.getElementById('play-font-family');
  function fontIdx() {
    const stored = localStorage.getItem(FONT_FAMILY_KEY);
    const i = FONTS.findIndex(f => f.key === stored);
    return i >= 0 ? i : 0;
  }
  function applyFontFamily() {
    const f = FONTS[fontIdx()];
    log.style.fontFamily = f.stack;
    fontFamilyBtn.style.fontFamily = f.stack;
    fontFamilyBtn.title = 'Font: ' + f.label + ' — click to cycle';
  }
  fontFamilyBtn.addEventListener('click', () => {
    const next = (fontIdx() + 1) % FONTS.length;
    localStorage.setItem(FONT_FAMILY_KEY, FONTS[next].key);
    applyFontFamily();
  });
  applyFontFamily();

  // Narrative font-size controls: persist scale in localStorage, clamp to a
   // sane range, refresh button-disabled state at the limits.
  const FONT_KEY = 'play.font-size';
  const FONT_MIN = 0.85, FONT_MAX = 1.55, FONT_STEP = 0.06, FONT_DEFAULT = 1.02;
  const fontDec = document.getElementById('play-font-dec');
  const fontInc = document.getElementById('play-font-inc');
  function applyFont() {
    const stored = parseFloat(localStorage.getItem(FONT_KEY));
    const v = isNaN(stored) ? FONT_DEFAULT : Math.max(FONT_MIN, Math.min(FONT_MAX, stored));
    log.style.fontSize = v.toFixed(2) + 'em';
    fontDec.disabled = v <= FONT_MIN + 0.001;
    fontInc.disabled = v >= FONT_MAX - 0.001;
  }
  function bumpFont(delta) {
    const stored = parseFloat(localStorage.getItem(FONT_KEY));
    const cur = isNaN(stored) ? FONT_DEFAULT : stored;
    const next = Math.max(FONT_MIN, Math.min(FONT_MAX, cur + delta));
    localStorage.setItem(FONT_KEY, next.toString());
    applyFont();
  }
  fontDec.addEventListener('click', () => bumpFont(-FONT_STEP));
  fontInc.addEventListener('click', () => bumpFont(+FONT_STEP));
  applyFont();

  // Filter pills: persist Show/Hide per turn-type in localStorage.
  document.querySelectorAll('.turn-toggle').forEach(btn => {
    const key = btn.dataset.filter;
    const hidden = localStorage.getItem('play.hide.' + key) === '1';
    btn.classList.toggle('active', !hidden);
    log.classList.toggle('hide-' + key, hidden);
    btn.addEventListener('click', () => {
      const wasActive = btn.classList.contains('active');
      const nowActive = !wasActive;
      btn.classList.toggle('active', nowActive);
      log.classList.toggle('hide-' + key, !nowActive);
      localStorage.setItem('play.hide.' + key, nowActive ? '0' : '1');
    });
  });

  fetch('/play/session').then(r => r.json()).then(d => {
    sidEl.querySelector('code').textContent = d.session_id ? d.session_id.slice(0, 8) : '(new)';
    sidEl.href = d.session_id ? '/sessions/' + d.session_id : '#';
    renderSessionMeta(d);
    applyToneSetting(d.tone);
    applyDetailSetting({detail: d.detail, choices: d.detail_choices});
    applyInstructionsSetting(d.instructions);
  });

  // ------------------------------------------------------------------
  // Initial replay + Load-earlier pagination.
  // On load we fetch the last few turns from the resumed session so the
  // page has context. The "↑ Load earlier turns" button at the top of
  // the chat log walks further back in pages of 20.
  // ------------------------------------------------------------------
  const loadMoreBtn = document.getElementById('load-more');
  let oldestLoadedIdx = null;

  async function initialReplay() {
    try {
      const r = await fetch('/api/history?limit=10');
      if (!r.ok) return;
      const d = await r.json();
      if (!d.turns || !d.turns.length) return;
      clearEmptyState();
      const frag = document.createDocumentFragment();
      for (const t of d.turns) frag.appendChild(buildHistoryTurn(t));
      // Insert AFTER the load-more button so the button stays at the top.
      loadMoreBtn.after(frag);
      oldestLoadedIdx = d.oldest_idx;
      loadMoreBtn.hidden = !d.has_more;
      // Seed lastDmText with the most recent DM turn for "Make picture".
      for (let i = d.turns.length - 1; i >= 0; i--) {
        if (d.turns[i].role === 'dm') { lastDmText = d.turns[i].text; break; }
      }
      rebindLastDmTurnFromLog();
      log.scrollTop = log.scrollHeight;
    } catch (e) { /* ignore */ }
  }

  async function loadEarlier() {
    if (oldestLoadedIdx == null) return;
    loadMoreBtn.disabled = true;
    const orig = loadMoreBtn.textContent;
    loadMoreBtn.textContent = 'loading…';
    try {
      const r = await fetch('/api/history?before=' + oldestLoadedIdx + '&limit=20');
      if (!r.ok) return;
      const d = await r.json();
      if (!d.turns || !d.turns.length) {
        loadMoreBtn.hidden = true;
        return;
      }
      // Preserve scroll: prepending raises content above the viewport;
      // we want the existing first-visible turn to stay where it is.
      const heightBefore = log.scrollHeight;
      const scrollBefore = log.scrollTop;

      const frag = document.createDocumentFragment();
      for (const t of d.turns) frag.appendChild(buildHistoryTurn(t));
      loadMoreBtn.after(frag);

      log.scrollTop = scrollBefore + (log.scrollHeight - heightBefore);

      oldestLoadedIdx = d.oldest_idx;
      if (!d.has_more) loadMoreBtn.hidden = true;
    } catch (e) { /* ignore */ } finally {
      loadMoreBtn.disabled = false;
      loadMoreBtn.textContent = orig;
    }
  }
  loadMoreBtn.addEventListener('click', loadEarlier);
  initialReplay();

  // ------------------------------------------------------------------
  // Server-side turn-state watcher — surfaces a "DM still thinking from
  // a previous turn" indicator after a reload / network drop, disables
  // Send while the server lock is held, and replays the new narrative
  // once the in-flight turn lands.
  // ------------------------------------------------------------------
  let recoveryEl = null;
  let lockedBySrv = false;
  function startRecovery() {
    if (firstAppend) { log.innerHTML = ''; firstAppend = false; }
    recoveryEl = document.createElement('div');
    recoveryEl.className = 'turn thinking';
    recoveryEl.innerHTML =
        '<span class="thinking-glyph" aria-hidden="true"><span></span><span></span><span></span></span>'
      + '<span class="thinking-text">DM still working on a previous turn…</span>'
      + '<span class="thinking-time"></span>';
    log.appendChild(recoveryEl);
    log.scrollTop = log.scrollHeight;
  }
  function updateRecovery(state) {
    if (!recoveryEl) return;
    const t = recoveryEl.querySelector('.thinking-time');
    if (!t) return;
    const sec = state.started_at
      ? (Date.now() / 1000 - state.started_at).toFixed(1)
      : '?';
    const tn = state.tools_used > 0
      ? '  ·  ' + state.tools_used + ' tool call' + (state.tools_used === 1 ? '' : 's')
      : '';
    t.textContent = sec + 's' + tn;
  }
  function stopRecovery() {
    if (recoveryEl && recoveryEl.parentElement) recoveryEl.remove();
    recoveryEl = null;
  }
  async function refreshLastNarrative() {
    try {
      const r = await fetch('/api/last_narrative');
      const d = await r.json();
      if (d && d.text && d.text !== lastDmText) {
        append('dm', d.text);
        lastDmText = d.text;
        refreshState();
        return 'appended';
      }
      return d && d.text ? 'unchanged' : 'empty';
    } catch (e) { return 'error'; }
  }
  const fetchLastBtn = document.getElementById('play-fetch-last');
  if (fetchLastBtn) {
    fetchLastBtn.addEventListener('click', async () => {
      fetchLastBtn.disabled = true;
      const orig = statusEl.textContent;
      statusEl.textContent = 'fetching last reply…';
      const outcome = await refreshLastNarrative();
      const msg = {
        appended: 'recovered last DM reply',
        unchanged: 'already up to date',
        empty: 'no DM reply in transcript',
        error: 'fetch failed',
      }[outcome] || '';
      statusEl.textContent = msg;
      setTimeout(() => {
        if (statusEl.textContent === msg) statusEl.textContent = orig;
        fetchLastBtn.disabled = false;
      }, 2000);
    });
  }
  async function pollTurnState() {
    try {
      const r = await fetch('/api/turn_state');
      if (!r.ok) return;
      const s = await r.json();
      // Send-disable when the server lock is held — local UI already
      // disables it during its own submit, but a recovery scenario also
      // needs the lock surface.
      if (s.in_flight !== lockedBySrv) {
        lockedBySrv = s.in_flight;
        // Don't fight the local submit's own disable.
        if (!localTurnActive) send.disabled = lockedBySrv;
      }
      // Recovery indicator: show only when the server is busy AND we're
      // not the ones running it. Checking localTurnActive (not thinkingEl)
      // matters because the spinner is dropped early once streaming text
      // arrives — the local turn is still in flight long after.
      if (s.in_flight && !localTurnActive) {
        if (!recoveryEl) startRecovery();
        updateRecovery(s);
      } else if (recoveryEl && !s.in_flight) {
        stopRecovery();
        refreshLastNarrative();
      }
    } catch (e) { /* ignore */ }
  }
  pollTurnState();
  setInterval(pollTurnState, 3000);

  refreshState();
  // Background refresh — picks up MCP-tool mutations made outside the turn flow.
  setInterval(refreshState, 20000);

  // ----- Audit dialog (procedural-fairness findings, post-turn) -----
  const auditBtn      = document.getElementById('play-audit');
  const auditBtnPip   = document.getElementById('audit-btn-pip');
  const auditDialog   = document.getElementById('audit-dialog');
  const auditSummary  = document.getElementById('audit-summary');
  const auditDlgList  = document.getElementById('audit-dialog-list');
  const auditInject   = document.getElementById('audit-inject-toggle');
  const auditClearShown = document.getElementById('audit-clear-shown');
  const auditClose    = document.getElementById('audit-close');
  let auditSnap = null;

  function renderAuditPip(snap) {
    const findings = (snap && snap.findings) || [];
    const lapses   = findings.filter(f => f.severity === 'lapse').length;
    const warnings = findings.filter(f => f.severity === 'warning').length;
    if (!lapses && !warnings) {
      auditBtnPip.hidden = true;
      auditBtnPip.className = 'audit-btn-pip';
      return;
    }
    auditBtnPip.hidden = false;
    auditBtnPip.className = 'audit-btn-pip' + (lapses ? '' : ' has-warning');
  }

  function renderAuditDialog(snap) {
    const findings = (snap && snap.findings) || [];
    const lapses   = findings.filter(f => f.severity === 'lapse').length;
    const warnings = findings.filter(f => f.severity === 'warning').length;
    const infos    = findings.filter(f => f.severity === 'info').length;
    const parts = [];
    if (lapses)   parts.push(lapses + ' lapse' + (lapses > 1 ? 's' : ''));
    if (warnings) parts.push(warnings + ' warning' + (warnings > 1 ? 's' : ''));
    if (infos)    parts.push(infos + ' note' + (infos > 1 ? 's' : ''));
    auditSummary.textContent = parts.length ? '(' + parts.join(', ') + ')' : '(no findings)';

    if (!findings.length) {
      auditDlgList.innerHTML = '<li class="muted">(no findings — DM is clean so far)</li>';
    } else {
      auditDlgList.innerHTML = findings.map(f => {
        const sev = esc(f.severity || 'info');
        const kind = esc((f.kind || '').replace(/_/g, ' '));
        const slug = f.slug ? ' <span class="audit-slug">' + esc(f.slug) + '</span>' : '';
        const fix = f.fix ? ' — ' + esc(f.fix) : '';
        return '<li class="audit-item sev-' + sev + '">'
          + '<span class="audit-kind">' + kind + '</span>:'
          + slug + fix
          + '</li>';
      }).join('');
    }

    // Sync the inject toggle from server-side settings.
    const inject = !!(snap && snap.settings && snap.settings.inject_addendum);
    auditInject.checked = inject;
  }

  async function refreshAudit() {
    try {
      const r = await fetch('/api/audit_live.json');
      if (!r.ok) return;
      auditSnap = await r.json();
      renderAuditPip(auditSnap);
      // Only refresh the dialog DOM when it's open — keep cycles down.
      if (auditDialog.open) renderAuditDialog(auditSnap);
    } catch (e) { /* best-effort */ }
  }

  auditBtn.addEventListener('click', () => {
    renderAuditDialog(auditSnap || {findings: [], settings: {}});
    if (typeof auditDialog.showModal === 'function') auditDialog.showModal();
  });
  auditClose.addEventListener('click', () => auditDialog.close());

  auditInject.addEventListener('change', async () => {
    const orig = auditInject.checked;
    try {
      const r = await fetch('/api/audit_settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({inject_addendum: orig}),
      });
      if (!r.ok) throw new Error('save failed');
      const d = await r.json();
      auditInject.checked = !!(d.settings && d.settings.inject_addendum);
    } catch (e) {
      // Revert on failure
      auditInject.checked = !orig;
    }
  });

  auditClearShown.addEventListener('click', async () => {
    const orig = auditClearShown.textContent;
    auditClearShown.disabled = true;
    auditClearShown.textContent = 'Resetting…';
    try {
      await fetch('/api/audit_clear_shown', {method: 'POST'});
      auditClearShown.textContent = 'Reset ✓';
    } catch (e) {
      auditClearShown.textContent = 'Failed';
    }
    setTimeout(() => {
      auditClearShown.textContent = orig;
      auditClearShown.disabled = false;
    }, 1400);
  });

  refreshAudit();
  setInterval(refreshAudit, 5000);

  input.focus();
})();
</script>
"""


@app.route("/play")
def play_page():
    return render("Play", _PLAY_HTML)


@app.route("/play/session")
def play_session_id():
    sid = _dm.session_id()
    size_bytes = None
    context_tokens = None
    if sid:
        p = _dm._project_jsonl_dir() / f"{sid}.jsonl"
        try:
            size_bytes = p.stat().st_size
        except OSError:
            size_bytes = None
        context_tokens = _dm._session_context_tokens(sid)
    return jsonify({
        "session_id": sid,
        "size_bytes": size_bytes,
        "context_tokens": context_tokens,
        "auto_reset_tokens": _dm._AUTO_RESET_TOKENS if _dm._AUTO_RESET_ENABLED else None,
        "tone": _dm.tone_setting(),
        "detail": _dm.detail_setting(),
        "detail_choices": _dm.detail_choices(),
        "instructions": _dm.instructions_setting(),
    })


@app.route("/play/tone", methods=["POST"])
def play_set_tone():
    """Persist the active campaign's DM narration tone. Body:
    {"preset": <key>, "custom": <text>}. The directive is injected into every
    subsequent DM turn's system prompt; takes effect on the next turn."""
    data = request.get_json(silent=True) or {}
    setting = _dm.set_tone(preset=data.get("preset"), custom=data.get("custom"))
    return jsonify({"ok": True, "tone": setting})


@app.route("/play/detail", methods=["POST"])
def play_set_detail():
    """Persist the active campaign's narrative detail level. Body:
    {"level": 0-3}. Controls how much raw mechanical detail (coin counts,
    ability scores, stat-block values) the DM exposes in prose; injected into
    every subsequent turn's system prompt. Takes effect on the next turn."""
    data = request.get_json(silent=True) or {}
    setting = _dm.set_detail_level(data.get("level"))
    return jsonify({"ok": True, "detail": setting})


@app.route("/play/instructions", methods=["POST"])
def play_set_instructions():
    """Persist the active campaign's instructions directive. Body:
    {"text": <str>, "enabled": <bool>}. A binding per-campaign canon/procedural
    constraint (e.g. a module lock) injected into every subsequent DM turn's
    system prompt at the same authority as CLAUDE.md's Hard Constraints. Takes
    effect on the next turn."""
    data = request.get_json(silent=True) or {}
    setting = _dm.set_campaign_instructions(
        text=data.get("text"), enabled=data.get("enabled"))
    return jsonify({"ok": True, "instructions": setting})


@app.route("/play/reset", methods=["POST"])
def play_reset():
    _dm.reset_session()
    return jsonify({"ok": True})


@app.route("/play/force_reset", methods=["POST"])
def play_force_reset():
    """Hard-clear in-flight DM state from the GUI escape hatch. Kills any
    running claude subprocess, replaces the turn lock so a wedged thread
    can no longer block new turns, and resets the in-flight flag."""
    info = _dm.force_reset()
    return jsonify({"ok": True, **info})


@app.route("/api/play_state.json")
def api_play_state():
    return jsonify(_play_state())


@app.route("/api/last_narrative")
def api_last_narrative():
    """Return the most recent DM text from the resumed Claude Code session,
    so the /play surface can replay it on reload. Returns {"text": null} when
    there's no session, no transcript, or no recorded DM prose yet."""
    return jsonify({"text": _dm.last_dm_text()})


@app.route("/api/audit_live.json")
def api_audit_live():
    """Return the latest post-turn audit snapshot for the active campaign.
    Refreshed asynchronously after each DM turn (see tools/dm_session.py
    `_audit_after_turn`). Returns {"findings": [], ...} when no audit has
    run yet, or an empty payload when no campaign is active."""
    snap = _dm.latest_audit()
    if snap is None:
        snap = {"findings": [], "summary": "", "session": None, "timestamp": None}
    # Attach the campaign's audit settings so the UI can render the
    # toggle state without a second roundtrip.
    snap = dict(snap)
    snap["settings"] = _dm.audit_settings()
    return jsonify(snap)


@app.route("/api/audit_settings", methods=["POST"])
def api_audit_settings():
    """Update the active campaign's audit settings. Accepts JSON body
    with any of the known keys (currently: ``inject_addendum``). Returns
    the merged result. Unknown keys are silently dropped."""
    data = request.get_json(silent=True) or {}
    result = _dm.set_audit_settings(**{k: bool(v) for k, v in data.items()})
    return jsonify({"settings": result})


@app.route("/api/audit_clear_shown", methods=["POST"])
def api_audit_clear_shown():
    """Reset the shown-findings tracker so the next turn re-surfaces every
    current lapse via the addendum. Used when the user wants to retry the
    nudge — e.g. after toggling injection back on after a quiet period."""
    _dm.clear_shown_findings()
    return jsonify({"ok": True})


@app.route("/api/history")
def api_history():
    """Return a slice of the resumed session's parsed transcript so /play
    can render context on load and walk the conversation backward in pages.

    Query params:
        before: int — return turns whose chronological index is < this.
                Defaults to the total turn count (i.e. "all turns").
        limit:  int — max turns to return (clamped to 1..100, default 20).

    Each returned turn carries its absolute ``idx`` in the session so the
    client can pass the oldest idx as the next ``before`` cursor."""
    sid = _dm.session_id()
    empty = {"turns": [], "total": 0, "has_more": False, "oldest_idx": 0}
    if not sid:
        return jsonify(empty)
    p = _dm._project_jsonl_dir() / f"{sid}.jsonl"
    if not p.exists():
        return jsonify(empty)

    all_turns = _export.parse_session(p)
    total = len(all_turns)

    before = request.args.get("before", type=int, default=total)
    limit = max(1, min(request.args.get("limit", type=int, default=20), 100))

    end = max(0, min(before, total))
    start = max(0, end - limit)
    chunk = all_turns[start:end]

    return jsonify({
        "turns": [
            {"role": t.role, "text": t.text, "idx": start + i,
             "timestamp": t.timestamp}
            for i, t in enumerate(chunk)
        ],
        "total": total,
        "has_more": start > 0,
        "oldest_idx": start,
    })


@app.route("/api/turn_state")
def api_turn_state():
    """Live state of the currently running turn (if any). Polled by /play
    so a reload during streaming can pick up the still-cooking server-side
    response, disable Send while the lock is held, and replay the DM reply
    once it lands."""
    return jsonify(_dm.turn_state())


@app.route("/play/turn", methods=["POST"])
def play_turn():
    data = request.get_json(silent=True) or {}
    msg = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "empty message"}), 400

    def gen():
        for evt in _dm.stream_turn(msg):
            yield json.dumps(evt) + "\n"

    resp = Response(gen(), mimetype="application/x-ndjson")
    # Defeat any reverse-proxy buffering (nginx etc.); harmless on the dev server.
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/play/suggest_actions", methods=["POST"])
def play_suggest_actions():
    """Return 2-4 plausible next player actions for a given DM reply.
    Used by the /play 'suggest actions' toggle to populate chips under
    the last DM turn. Returns ``{"actions": []}`` if the pass fails or
    the narrative is empty so the frontend can silently hide the row."""
    data = request.get_json(silent=True) or {}
    narrative = (data.get("narrative") or "").strip()
    if not narrative:
        return jsonify({"actions": []})
    actions = _dm.suggest_actions(narrative)
    return jsonify({"actions": actions})


@app.route("/play/generate_scene", methods=["POST"])
def play_generate_scene():
    """Generate an illustration based on the previous DM reply.

    Two-phase pipeline:

    1. Pre-pass: ``_dm.rewrite_for_image`` runs a stateless ``claude -p``
       to extract a focused image-generation prompt from the raw narrative
       (drops dialogue, mechanics, etc).
    2. Replicate Flux generates the image from that refined prompt.

    Synchronous — returns once the image is downloaded. Slow (~15-45s).
    Falls back to the raw narrative if the pre-pass fails so the user
    still gets *some* image."""
    if not cfg:
        return jsonify({"error": "no campaign loaded"}), 400
    data = request.get_json(silent=True) or {}
    raw = (data.get("prompt") or "").strip()
    hint = (data.get("hint") or "").strip()
    if not raw:
        return jsonify({"error": "empty prompt"}), 400

    # Pre-pass: translate the narrative (plus an optional player hint) into a
    # painter's brief. Falls back to truncated raw narrative on failure.
    refined = _dm.rewrite_for_image(raw[:4000], hint=hint or None)
    used_prepass = refined is not None
    image_prompt = (refined or raw)[:1500]

    try:
        replicate = _img._load_replicate()
    except (ImportError, EnvironmentError) as exc:
        return jsonify({"error": str(exc)}), 500

    tone = cfg.get("tone", "high fantasy")
    tone_clause = _img.TONE_STYLES.get(tone, tone)
    full_prompt = f"{_img.STYLE_BASE} {tone_clause}. {image_prompt}"

    title = "Live play scene"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{_img._safe_slug(title)}.png"
    images_dir = cfg["_data_dir"] / "images"

    try:
        _img._generate_image(replicate, full_prompt, images_dir, filename)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    _img._update_index(images_dir, {
        "filename":    filename,
        "scene":       title,
        "description": image_prompt[:500],
        "timestamp":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "type":        "scene",
    })
    return jsonify({
        "url": f"/images/{filename}",
        "filename": filename,
        "prompt": image_prompt,
        "prepass": used_prepass,
    })



def _encumbrance_html(char: dict, state: dict, key: str) -> str:
    """Render a 'Carrying' block: total weight, band, and STR-keyed thresholds.
    Returns an empty string if the character has no usable ability scores."""
    ab = char.get("ability_scores") or {}
    if "str" not in ab:
        return ""

    enc = _encumbrance_band(char, state, key)
    band = enc["band"]
    weight = enc["weight"]
    penalty = enc["penalty"]
    th = enc["thresholds"]
    strength = enc["strength"]

    band_color = {
        "light":      "#6db86d",
        "moderate":   "#c8a96e",
        "heavy":      "#e0a060",
        "severe":     "#e08060",
        "overloaded": "#ff6a4a",
    }.get(band, "#c8a96e")
    penalty_str = f" &nbsp;·&nbsp; {penalty:+d} move" if penalty else ""

    # Bar — fraction of severe (overloaded cap). Cap at 1.0.
    severe_cap = max(th["severe"], 1)
    pct = min(100.0, weight / severe_cap * 100.0)
    pct_color = band_color

    rows = []
    for tname in ("light", "moderate", "heavy", "severe"):
        cap = th[tname]
        active = " enc-band-active" if band == tname else ""
        rows.append(
            f'<div class="enc-band{active}">'
            f'<span class="enc-band-name">{tname}</span>'
            f'<span class="enc-band-cap">≤ {cap} lb</span>'
            f'</div>'
        )

    # Per-item breakdown
    item_rows = []
    for it in enc["items"]:
        name = html.escape(it["item"])
        qty = it["qty"]
        unit = it["unit_lb"]
        sub = it["weight_lb"]
        wsrc = it.get("source", "unknown")

        if wsrc == "unknown":
            unit_cell = '<span class="muted" title="not found in 2e item DB or PHB">— lb</span>'
            sub_cell  = '<span class="muted">—</span>'
        elif wsrc == "negligible":
            unit_cell = '<span class="muted" title="PHB lists this as negligible (10/lb)">≈ 0</span>'
            sub_cell  = '<span class="muted">—</span>'
        else:
            unit_cell = f'{unit:g} lb'
            sub_cell  = f'<strong>{sub:g} lb</strong>'

        src_chips = []
        if it.get("origin") == "consumable":
            src_chips.append('<span class="tag" style="font-size:.7em">consumable</span>')
        if wsrc == "phb":
            src_chips.append('<span class="tag" style="font-size:.7em;background:#1a2a3a;color:#a0c0e0;border-color:#3a5a7a" title="weight from PHB Chapter 6">PHB</span>')
        elif wsrc == "alias":
            src_chips.append('<span class="tag" style="font-size:.7em" title="resolved via alias to a canonical 2e item">alias</span>')
        chips_html = " ".join(src_chips)

        qty_cell = f'×{qty}' if qty > 1 else ''
        item_rows.append(
            f'<tr>'
            f'<td>{name} {chips_html}</td>'
            f'<td style="text-align:right">{qty_cell}</td>'
            f'<td style="text-align:right">{unit_cell}</td>'
            f'<td style="text-align:right">{sub_cell}</td>'
            f'</tr>'
        )
    item_table = ""
    if item_rows:
        item_table = (
            '<table class="enc-items">'
            '<thead><tr>'
            '<th>Item</th>'
            '<th style="text-align:right">Qty</th>'
            '<th style="text-align:right">Unit</th>'
            '<th style="text-align:right">Weight</th>'
            '</tr></thead>'
            f'<tbody>{"".join(item_rows)}</tbody>'
            '<tfoot><tr>'
            '<td colspan="3" style="text-align:right"><strong>Total</strong></td>'
            f'<td style="text-align:right"><strong>{weight} lb</strong></td>'
            '</tr></tfoot>'
            '</table>'
        )
    else:
        item_table = ('<p class="muted" style="font-size:.85em">'
                      'No items recorded.</p>')

    return (
        f'<div class="card enc-card">'
        f'<h3 style="margin-top:0">Carrying</h3>'
        f'<p><strong>Weight:</strong> {weight} lb '
        f'&nbsp;·&nbsp; <span style="color:{band_color}"><strong>{band}</strong></span>'
        f'{penalty_str} '
        f'<span class="muted">(STR {strength})</span></p>'
        f'<div class="enc-bar"><div class="enc-fill" '
        f'style="width:{pct:.1f}%;background:{pct_color}"></div></div>'
        f'<div class="enc-bands">{"".join(rows)}</div>'
        f'{item_table}'
        f'<p class="muted" style="font-size:.8em;margin-top:6px">'
        f'Sources: 2e item DB · <span class="tag" style="font-size:.7em">alias</span> matched to canonical name · '
        f'<span class="tag" style="font-size:.7em;background:#1a2a3a;color:#a0c0e0;border-color:#3a5a7a">PHB</span> '
        f'sourced from PHB Chapter 6 · '
        f'“≈ 0” means PHB lists the item as negligible (10 per pound) · '
        f'“— lb” means no match — override with <code>item_update</code>.</p>'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Character sheet — AD&D 2e structured layout
# ---------------------------------------------------------------------------

# Saving-throw column labels (5-column AD&D 2e order).
_SAVE_KEYS = (
    "paralysis_poison_death",
    "rod_staff_wand",
    "petrify_polymorph",
    "breath_weapon",
    "spell",
)
_SAVE_SHORT = {
    "paralysis_poison_death": "PP&D",
    "rod_staff_wand":         "RSW",
    "petrify_polymorph":      "PETR",
    "breath_weapon":          "BREATH",
    "spell":                  "SPELL",
}


def _sheet_panel(title: str, body_html: str) -> str:
    if not body_html:
        return ""
    return (
        f'<section class="sheet-panel">'
        f'<h2 class="sheet-panel-title">{html.escape(title)}</h2>'
        f'<div class="sheet-panel-body">{body_html}</div>'
        f'</section>'
    )


def _ab_grid(scores: dict) -> str:
    if not scores:
        return ""
    boxes = []
    for k in ("str", "dex", "con", "int", "wis", "cha"):
        v = scores.get(k)
        if v is None:
            continue
        boxes.append(
            f'<div class="ab-box">'
            f'<div class="ab-num">{html.escape(str(v))}</div>'
            f'<div class="ab-label">{k.upper()}</div>'
            f'</div>'
        )
    return f'<div class="ab-grid">{"".join(boxes)}</div>' if boxes else ""


def _saves_grid(saves: list) -> str:
    if not saves:
        return ""
    by_type = {s.get("type"): s.get("value") for s in saves if isinstance(s, dict)}
    boxes = []
    for key in _SAVE_KEYS:
        if key not in by_type:
            continue
        boxes.append(
            f'<div class="save-box">'
            f'<div class="save-num">{html.escape(str(by_type[key]))}</div>'
            f'<div class="save-label">{_SAVE_SHORT[key]}</div>'
            f'</div>'
        )
    return f'<div class="saves-grid">{"".join(boxes)}</div>' if boxes else ""


def _attacks_table(attacks: list) -> str:
    if not attacks:
        return ""
    rows = []
    for a in attacks:
        if not isinstance(a, dict):
            continue
        rows.append(
            "<tr>"
            f'<td>{html.escape(str(a.get("name", "?")))}</td>'
            f'<td>{html.escape(str(a.get("speed", "—")))}</td>'
            f'<td>{html.escape(str(a.get("attacks", 1)))}</td>'
            f'<td>{html.escape(str(a.get("thac0", "—")))}</td>'
            f'<td>{html.escape(str(a.get("damage_sm", "—")))}</td>'
            f'<td>{html.escape(str(a.get("damage_l", "—")))}</td>'
            "</tr>"
        )
    head = (
        "<thead><tr>"
        "<th>Weapon</th><th>Speed</th><th>#/rd</th><th>THAC0</th>"
        "<th>Dmg S/M</th><th>Dmg L</th>"
        "</tr></thead>"
    )
    return f'<table class="attacks-table">{head}<tbody>{"".join(rows)}</tbody></table>'


def _diamond_list(items: list) -> str:
    if not items:
        return ""
    return (
        '<ul class="sheet-list">'
        + "".join(f"<li>{html.escape(str(i))}</li>" for i in items if i)
        + "</ul>"
    )


def _nwps_list(nwps: list) -> str:
    if not nwps:
        return ""
    rows = []
    for n in nwps:
        if isinstance(n, dict):
            name = html.escape(str(n.get("name", "?")))
            ab = n.get("ability") or ""
            ab_html = f' <span class="muted">({html.escape(str(ab))})</span>' if ab else ""
            rows.append(f"<li>{name}{ab_html}</li>")
        elif isinstance(n, str) and n.strip():
            # Plain-string form, e.g. "Healing (WIS)" or "Herbalism (INT, 2 slots)".
            # The governing ability is usually embedded in a trailing paren —
            # split it out so it renders muted, matching the dict form.
            m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", n.strip())
            if m:
                name = html.escape(m.group(1).strip())
                ab = html.escape(m.group(2).strip())
                rows.append(f'<li>{name} <span class="muted">({ab})</span></li>')
            else:
                rows.append(f"<li>{html.escape(n.strip())}</li>")
    return f'<ul class="sheet-list">{"".join(rows)}</ul>' if rows else ""


def _prof_chips(profs: list) -> str:
    if not profs:
        return ""
    chips = "".join(f'<span class="prof-chip">{html.escape(str(p))}</span>' for p in profs)
    return f'<div class="prof-chips">{chips}</div>'


def _skills_grid(skills: dict) -> str:
    if not skills:
        return ""
    rows = []
    for k, v in skills.items():
        rows.append(
            f'<div class="skill-row">'
            f'<span class="skill-name">{html.escape(str(k))}</span>'
            f'<span class="skill-val">{html.escape(str(v))}%</span>'
            f"</div>"
        )
    return f'<div class="skills-grid">{"".join(rows)}</div>'


def _hp_meter_html(cur: int, mx: int) -> str:
    if mx <= 0:
        return ""
    pct = max(0.0, min(100.0, (cur / mx) * 100.0))
    return (
        f'<span class="hp-meter"><i style="width:{pct:.1f}%"></i></span> '
        f'<span class="vital-value">{cur}/{mx}</span>'
    )


def _vitals_row(char: dict, cstate: dict) -> str:
    items = []
    hp_max = int(char.get("hp_max", 0) or 0)
    hp_cur = int(cstate.get("hp", hp_max) or 0)
    if hp_max:
        items.append(
            f'<div class="vital"><span class="vital-label">HP</span>{_hp_meter_html(hp_cur, hp_max)}</div>'
        )
    for label, key in (("AC", "ac"), ("THAC0", "thac0")):
        v = char.get(key)
        if v is not None and v != "":
            items.append(
                f'<div class="vital"><span class="vital-label">{label}</span>'
                f'<span class="vital-value">{html.escape(str(v))}</span></div>'
            )
    level = int(char.get("level", 1) or 1)
    items.append(
        f'<div class="vital"><span class="vital-label">Level</span>'
        f'<span class="vital-value">{level}</span></div>'
    )
    xp = int(cstate.get("xp", 0) or 0)
    xp_table = _class_xp_table(char.get("cls", ""))
    next_xp_html = ""
    if xp_table and level < len(xp_table):
        next_xp = xp_table[level]
        next_xp_html = f' <span class="xp-next">/ {next_xp:,}</span>'
    items.append(
        f'<div class="vital"><span class="vital-label">XP</span>'
        f'<span class="vital-value">{xp:,}{next_xp_html}</span></div>'
    )
    return f'<div class="vitals-row">{"".join(items)}</div>'


def _extract_md_section(content: str, heading: str) -> str:
    """Return the body of a `## <heading>` section, stripped, or "" if missing."""
    pattern = re.compile(
        r'^##\s+' + re.escape(heading) + r'\s*\n(.+?)(?=^##\s|\Z)',
        re.M | re.S,
    )
    m = pattern.search(content)
    return m.group(1).strip() if m else ""


_ALIGNMENT_LABELS = (
    "Lawful Good", "Lawful Neutral", "Lawful Evil",
    "Neutral Good", "True Neutral", "Neutral Evil",
    "Chaotic Good", "Chaotic Neutral", "Chaotic Evil",
    "Neutral",  # last so longer matches win
)
_ALIGNMENT_RE = re.compile(
    # Leading guard rejects "FooAlignment" but tolerates the literal "\n"
    # escape sequences some character files carry (where a real \b fails
    # because both surrounding chars are word chars).
    r'(?<![A-Za-z])Alignment\s*[:\-—]\s*('
    + "|".join(re.escape(a) for a in _ALIGNMENT_LABELS)
    + r')\b',
    re.I,
)


def _extract_alignment(content: str) -> str:
    """Pull an alignment label out of a character markdown file. Looks for
    'Alignment: <label>' (or em-dash/hyphen variants) anywhere in the prose
    and matches against the canonical 2e set, returning the canonical-cased
    label or '' if none found."""
    # Some legacy character files contain literal "\n" escape sequences
    # rather than real newlines; normalise so the leading-anchor lookbehind
    # doesn't see "n" right before the keyword and reject the match.
    text = (content or "").replace("\\n", "\n")
    m = _ALIGNMENT_RE.search(text)
    if not m:
        return ""
    found = m.group(1).strip().lower()
    for label in _ALIGNMENT_LABELS:
        if label.lower() == found:
            return label
    return ""


_ALIGN_ABBREV_MAP = {
    "lawful good": "LG", "lawful neutral": "LN", "lawful evil": "LE",
    "neutral good": "NG", "true neutral": "TN", "neutral": "N", "neutral evil": "NE",
    "chaotic good": "CG", "chaotic neutral": "CN", "chaotic evil": "CE",
}
_VALID_ABBREVS = {"LG", "LN", "LE", "NG", "TN", "N", "NE", "CG", "CN", "CE"}


def _alignment_abbrev(value: str) -> str:
    """Normalize an alignment (full label like 'Neutral Good' or an existing
    short code like 'NG') to a compact code for sidebar display. '' if empty
    or unrecognized."""
    t = (value or "").strip()
    if not t:
        return ""
    up = t.upper().replace(".", "")
    if up in _VALID_ABBREVS:
        return up
    return _ALIGN_ABBREV_MAP.get(t.lower(), "")


def _memorized_panel(memorized: list) -> str:
    """Memorized spells grouped by level. Returns panel-body HTML (or "")."""
    if not memorized:
        return ""

    # If level isn't set on each entry, fill from the spells DB.
    unknown = {(s.get("name") or "").strip() for s in memorized
               if isinstance(s, dict) and not int(s.get("level", 0) or 0)}
    level_map = {}
    if unknown:
        placeholders = ",".join("?" * len(unknown))
        rows = _db(_2E_DB).execute(
            f"SELECT name, level FROM spells WHERE lower(name) IN ({placeholders})",
            [n.lower() for n in unknown],
        ).fetchall()
        level_map = {r["name"].lower(): int(r["level"]) for r in rows}

    by_level: dict[int, list[dict]] = {}
    for s in memorized:
        if not isinstance(s, dict):
            continue
        name = (s.get("name") or "").strip()
        lvl = int(s.get("level", 0) or 0)
        if lvl <= 0:
            lvl = level_map.get(name.lower(), 0)
        by_level.setdefault(lvl, []).append({"name": name, "cast": bool(s.get("cast"))})

    blocks = []
    for lvl in sorted(by_level):
        spells = by_level[lvl]
        ready = sum(1 for s in spells if not s["cast"])
        total = len(spells)
        head_label = f"Level {lvl}" if lvl > 0 else "Unleveled"
        items = "".join(
            f'<li class="spell-cast"><s>{html.escape(s["name"])}</s></li>'
            if s["cast"] else
            f'<li class="spell-ready">{html.escape(s["name"])}</li>'
            for s in spells
        )
        blocks.append(
            f'<div class="spell-level">'
            f'<div class="spell-level-head">{head_label} '
            f'<span class="muted">({ready}/{total} ready)</span></div>'
            f'<ul class="spell-list">{items}</ul></div>'
        )
    return "".join(blocks)


def _profs_two_col(weapon_profs: list, nwps: list) -> str:
    left = _prof_chips(weapon_profs)
    right = _nwps_list(nwps)
    if not left and not right:
        return ""
    parts = []
    if left:
        parts.append(f'<div><h4>Weapons</h4>{left}</div>')
    if right:
        parts.append(f'<div><h4>Non-weapon</h4>{right}</div>')
    return f'<div class="sheet-two-col">{"".join(parts)}</div>'


@app.route("/sheets/<slug>")
def character_sheet(slug: str):
    campaign_name = cfg.get("name", "Campaign")
    chars_dir = cfg["_data_dir"] / "characters"
    path = chars_dir / f"{slug}.md"
    if not path.exists():
        abort(404)

    md_content = path.read_text(encoding="utf-8")
    name = _read_first_heading(path)
    char = cfg.get("characters", {}).get(slug) or cfg.get("npcs", {}).get(slug) or {}
    state = _c.load_state(cfg) if char else {}
    cstate = state.get("characters", {}).get(slug, {}) if state else {}

    # Header — portrait + identity strip + conditions
    image_index = _load_image_index() if char else _load_image_index()
    portrait = _find_portrait(name, image_index, slug)
    portrait_html = ""
    regen_dialog_html = ""
    if portrait:
        fn = html.escape(portrait["filename"])
        desc = html.escape(portrait.get("description", ""))
        desc_block = (
            f'<details class="portrait-desc"><summary>portrait notes</summary>'
            f'<p>{desc}</p></details>'
        ) if desc else ""
        # The ↻ button opens a small dialog that lets you optionally add a
        # prompt. The typed text is combined with the character's stored
        # portrait_prompt (which anchors the likeness / CHA-appropriate
        # appearance); leaving it blank redraws from that base prompt as-is.
        base_prompt = (char.get("portrait_prompt") or "").strip()
        regen_btn = (
            f'<button class="portrait-regen" data-slug="{html.escape(slug)}" '
            f'type="button" title="Regenerate portrait…" '
            f'aria-label="Regenerate portrait">↻</button>'
        )
        portrait_html = (
            f'<div class="sheet-portrait-wrap holding-portrait-wrap">'
            f'<img class="sheet-portrait" src="/images/{fn}" alt="{html.escape(name)}">'
            f'{regen_btn}'
            f'{desc_block}'
            f'</div>'
        )
        if base_prompt:
            note = ("Optional extra direction — combined with this character's stored "
                    "portrait prompt. Leave blank to redraw from the base prompt as-is.")
            base_block = (
                f'<details class="portrait-base"><summary>base prompt</summary>'
                f'<p class="muted">{html.escape(base_prompt)}</p></details>'
            )
        else:
            note = ("No portrait prompt is stored for this character — describe the "
                    "portrait you want below (a blank box can't regenerate).")
            base_block = ""
        regen_dialog_html = (
            '<dialog id="portrait-regen-dialog" class="portrait-regen-dialog">'
            '<form id="portrait-regen-form" method="dialog">'
            '<h3>Regenerate portrait</h3>'
            f'<p class="muted">{note}</p>'
            '<textarea id="portrait-regen-extra" rows="3" '
            'placeholder="e.g. older, grey at the temples · a fresh scar · softer light"></textarea>'
            f'{base_block}'
            '<div class="actions">'
            '<button type="button" id="portrait-regen-cancel">Cancel</button>'
            '<button type="submit" id="portrait-regen-go">Generate</button>'
            '</div></form></dialog>'
        )

    # Identity strip: class lvl · race · gender · alignment
    id_parts = []
    race = char.get("race") or ""
    cls = char.get("cls") or ""
    level = char.get("level")
    gender = char.get("gender") or ""
    # Alignment: prefer the structured field, fall back to scanning the
    # character markdown for an "Alignment: <label>" line.
    alignment = (char.get("alignment") or "").strip() or _extract_alignment(md_content)
    if cls:
        id_parts.append(f"{html.escape(cls)} {html.escape(str(level))}" if level else html.escape(cls))
    if race:
        id_parts.append(html.escape(race.title()))
    if gender:
        id_parts.append(html.escape(gender.title()))
    if alignment:
        id_parts.append(html.escape(alignment))
    id_strip = '<span class="sep">·</span>'.join(id_parts)

    conditions = cstate.get("conditions") or []
    cond_html = ""
    if conditions:
        chips = "".join(f'<span class="tag">{html.escape(c)}</span>' for c in conditions)
        cond_html = f'<div class="sheet-conditions">{chips}</div>'

    header = (
        '<div class="sheet-header">'
        '<div class="sheet-header-main">'
        f'<h1>{html.escape(name)}</h1>'
        f'<div class="sheet-id">{id_strip}</div>'
        f'{cond_html}'
        '</div>'
        f'{portrait_html}'
        '</div>'
    )

    # Build all the structured panels (only emit non-empty ones).
    panels = []
    if char:
        panels.append(_sheet_panel("Vitals", _vitals_row(char, cstate)))
        panels.append(_sheet_panel("Ability Scores", _ab_grid(char.get("ability_scores") or {})))
        panels.append(_sheet_panel("Saving Throws", _saves_grid(char.get("saves") or [])))
        panels.append(_sheet_panel("Attacks", _attacks_table(char.get("attacks") or [])))
        panels.append(_sheet_panel("Proficiencies",
                                   _profs_two_col(char.get("weapon_profs") or [],
                                                  char.get("nwps") or [])))
        panels.append(_sheet_panel("Thief / Nature Skills", _skills_grid(char.get("skills") or {})))
        panels.append(_sheet_panel("Natural Abilities", _diamond_list(char.get("natural_abilities") or [])))
        panels.append(_sheet_panel("Memorized Spells", _memorized_panel(cstate.get("memorized_spells") or [])))
        panels.append(_sheet_panel("Inventory", _diamond_list(char.get("inventory") or [])))
        enc = _encumbrance_html(char, state, slug)
        if enc:
            panels.append(_sheet_panel("Encumbrance", enc))

    # Background panel: prefer the prose-only `## Background` section when
    # the markdown follows the standard template; otherwise fall back to the
    # whole markdown body so older / hand-written sheets keep showing.
    background_md = _extract_md_section(md_content, "Background")
    bg_html = _markdown_to_html(background_md) if background_md else _markdown_to_html(md_content)
    panels.append(_sheet_panel("Background", bg_html))

    body = '<div class="sheet">' + header + "".join(p for p in panels if p) + '</div>'
    if regen_dialog_html:
        body += regen_dialog_html + _SHEET_PORTRAIT_JS
    return render(f"{html.escape(name)} — {html.escape(campaign_name)}", body)


def _holding_portrait_html(kind: str, slug: str, current: str = "") -> str:
    """Portrait widget for house/mount detail pages. Shows the portrait
    when one exists (with a small regen button), or a "Generate portrait"
    button when none does. Generation goes through /api/holdings/.../portrait.
    ``kind`` must be "houses" or "mounts"."""
    if current:
        ipath = cfg["_data_dir"] / "images" / current
        if ipath.exists():
            return (
                f'<div class="sheet-portrait-wrap holding-portrait-wrap">'
                f'<img class="sheet-portrait" src="/images/{html.escape(current)}" alt="">'
                f'<button class="portrait-regen" data-kind="{kind}" data-slug="{html.escape(slug)}" '
                f'type="button" title="Regenerate portrait" aria-label="Regenerate portrait">↻</button>'
                f'</div>'
            )
    return (
        f'<div class="sheet-portrait-wrap empty holding-portrait-wrap">'
        f'<button class="portrait-generate" data-kind="{kind}" data-slug="{html.escape(slug)}" '
        f'type="button">🎨 Generate portrait</button>'
        f'</div>'
    )


_HOLDING_DETAIL_JS = """<script>
(function(){
  function bind(btn) {
    btn.addEventListener('click', async function(){
      const orig = btn.textContent;
      btn.disabled = true;
      btn.textContent = 'rendering…';
      try {
        const r = await fetch('/api/holdings/' + btn.dataset.kind + '/'
                            + encodeURIComponent(btn.dataset.slug) + '/portrait',
                            {method:'POST'});
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
        // Hard reload to pick up the new portrait + the index update.
        location.reload();
      } catch (err) {
        alert('Portrait generation failed: ' + err.message);
        btn.disabled = false;
        btn.textContent = orig;
      }
    });
  }
  document.querySelectorAll('.portrait-generate, .portrait-regen').forEach(bind);
})();
</script>"""


# Regenerate-portrait widget for /sheets/<slug>. The ↻ button opens a themed
# dialog with an optional prompt field; submitting POSTs the extra text to the
# character endpoint, which combines it with the stored base prompt.
_SHEET_PORTRAIT_JS = """<style>
  dialog.portrait-regen-dialog {
    border: 1px solid var(--rule-hi);
    background: linear-gradient(to bottom, var(--bg-card-hi), var(--bg-card));
    color: var(--ink-body);
    border-radius: 5px;
    padding: 0;
    max-width: 460px;
    width: calc(100% - 32px);
    box-shadow: var(--shadow-card);
  }
  dialog.portrait-regen-dialog::backdrop { background: rgba(0,0,0,0.62); }
  dialog.portrait-regen-dialog form { padding: 20px 22px; }
  dialog.portrait-regen-dialog h3 { margin: 0 0 8px; }
  dialog.portrait-regen-dialog textarea {
    width: 100%; box-sizing: border-box; margin-top: 4px;
    background: var(--bg-rec); color: var(--ink-body);
    border: 1px solid var(--rule); border-radius: 3px;
    padding: 10px 12px; font-family: var(--font-body); font-size: 1em;
    resize: vertical; min-height: 64px; line-height: 1.5;
  }
  dialog.portrait-regen-dialog textarea:focus {
    outline: none; border-color: var(--accent-gold);
    box-shadow: 0 0 0 1px rgba(200,169,110,0.25);
  }
  dialog.portrait-regen-dialog details.portrait-base { margin-top: 10px; }
  dialog.portrait-regen-dialog details.portrait-base summary {
    cursor: pointer; font-size: 0.8em; color: var(--ink-muted); font-style: italic;
  }
  dialog.portrait-regen-dialog details.portrait-base p { font-size: 0.82em; line-height: 1.5; margin-top: 6px; }
  dialog.portrait-regen-dialog .actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 14px; }
  dialog.portrait-regen-dialog .actions button {
    background: transparent; color: var(--accent-gold);
    border: 1px solid var(--rule); border-radius: 3px;
    padding: 6px 16px; font-family: var(--font-display);
    font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.12em;
    cursor: pointer; transition: all 160ms ease;
  }
  dialog.portrait-regen-dialog .actions button:hover { border-color: var(--accent-gold); color: var(--accent-gold-hi); }
  dialog.portrait-regen-dialog .actions #portrait-regen-go {
    background: linear-gradient(to bottom, #4a3625, #2e2014);
    color: var(--ink-display); border-color: var(--rule-hi);
  }
  dialog.portrait-regen-dialog .actions button:disabled { opacity: 0.5; cursor: progress; }
</style>
<script>
(function(){
  const btn = document.querySelector('.portrait-regen[data-slug]');
  const dlg = document.getElementById('portrait-regen-dialog');
  if (!btn || !dlg) return;
  const form = document.getElementById('portrait-regen-form');
  const extra = document.getElementById('portrait-regen-extra');
  const go = document.getElementById('portrait-regen-go');
  const cancel = document.getElementById('portrait-regen-cancel');

  btn.addEventListener('click', function(){
    if (typeof dlg.showModal === 'function') dlg.showModal();
    else dlg.setAttribute('open', '');
    setTimeout(function(){ extra.focus(); }, 30);
  });
  cancel.addEventListener('click', function(){ dlg.close(); });

  form.addEventListener('submit', async function(e){
    e.preventDefault();
    const txt = (extra.value || '').trim();
    go.disabled = true; cancel.disabled = true;
    const orig = go.textContent; go.textContent = 'rendering…';
    try {
      const r = await fetch('/api/characters/' + encodeURIComponent(btn.dataset.slug) + '/portrait',
                            {method:'POST',
                             headers:{'Content-Type':'application/json'},
                             body: JSON.stringify({prompt: txt})});
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
      location.reload();   // pick up the new portrait + index update
    } catch (err) {
      alert('Portrait generation failed: ' + err.message);
      go.disabled = false; cancel.disabled = false; go.textContent = orig;
    }
  });
})();
</script>"""


@app.route("/mounts/<slug>")
def mount_detail(slug: str):
    """Detail page for a single mount. Click-through from the /party
    Holdings panel. Shows structured stats, inventory, optional notes
    from ``mounts/<slug>.md``, the stowed-pool counts when this mount
    is a vehicle, and a portrait with generate/regenerate button."""
    if not cfg:
        abort(404)
    slug_clean = re.sub(r"[^a-z0-9_-]", "", slug.lower())
    mount = (cfg.get("mounts") or {}).get(slug_clean)
    if not mount:
        abort(404)

    name = mount.get("name", slug_clean)
    owner_key = mount.get("owner", "")
    owner_label = (cfg.get("characters", {}).get(owner_key) or {}).get("label", owner_key)
    species = (mount.get("species") or "").replace("-", " ")

    # Header
    sub_parts = []
    if species:
        sub_parts.append(html.escape(species))
    if owner_key:
        sub_parts.append(f"owned by <a href='/sheets/{html.escape(owner_key)}'>{html.escape(owner_label)}</a>")
    sub = '<span class="sep">·</span>'.join(sub_parts)
    portrait_html = _holding_portrait_html("mounts", slug_clean, mount.get("portrait", ""))
    header = (
        '<div class="sheet-header">'
        '<div class="sheet-header-main">'
        f'<h1>{html.escape(name)}</h1>'
        f'<div class="sheet-id">{sub}</div>'
        '</div>'
        f'{portrait_html}'
        '</div>'
    )

    panels = []

    desc = (mount.get("description") or "").strip()
    if desc:
        panels.append(_sheet_panel("Description", f'<p>{html.escape(desc)}</p>'))

    # Stat block (mirrors AD&D 2e MM blocks)
    stats_rows = []
    hp_max = int(mount.get("hp_max", 0))
    hp_cur = int(mount.get("hp", hp_max))
    stats_rows.append(f"<dt>HP</dt><dd>{hp_cur}/{hp_max}</dd>")
    stats_rows.append(f"<dt>AC</dt><dd>{mount.get('ac', 10)}</dd>")
    stats_rows.append(f"<dt>MV</dt><dd>{mount.get('mv', 0)}</dd>")
    stats_rows.append(f"<dt>THAC0</dt><dd>{mount.get('thac0', 0)}</dd>")
    stats_rows.append(f"<dt>Morale</dt><dd>{mount.get('morale', 0)}</dd>")
    stats_html = '<dl class="holding-stats">' + "".join(stats_rows) + '</dl>'
    attacks = mount.get("attacks") or []
    if attacks:
        stats_html += "<p><strong>Attacks:</strong> " + ", ".join(html.escape(str(a)) for a in attacks) + "</p>"
    panels.append(_sheet_panel("Stats", stats_html))

    inv = mount.get("inventory") or []
    if inv:
        panels.append(_sheet_panel("Gear", _diamond_list(inv)))

    state = _c.load_state(cfg)
    pool = (state.get("vehicle_consumables") or {}).get(slug_clean) or {}
    if pool:
        rows = "".join(
            f"<li>{html.escape(k)} <strong>×{int(v)}</strong></li>"
            for k, v in sorted(pool.items()) if int(v) > 0
        )
        if rows:
            panels.append(_sheet_panel("Stowed (vehicle pool)", f'<ul class="stowed-list">{rows}</ul>'))

    # Optional free-form notes from mounts/<slug>.md.
    notes_path = cfg["_data_dir"] / "mounts" / f"{slug_clean}.md"
    if notes_path.exists():
        panels.append(_sheet_panel("Notes", _markdown_to_html(notes_path.read_text(encoding="utf-8"))))
    else:
        hint = (
            f'<p class="muted">Drop a markdown file at '
            f'<code>campaigns/{html.escape(cfg.get("name", ""))}/mounts/{html.escape(slug_clean)}.md</code> '
            f'to add free-form notes (training, scars, history) below the stat block.</p>'
        )
        panels.append(_sheet_panel("Notes", hint))

    body = '<div class="sheet">' + header + "".join(panels) + '</div>'
    body += '<p class="muted" style="margin-top:18px"><a href="/characters">← back to Cast</a></p>'
    body += _HOLDING_DETAIL_JS
    return render(f"{html.escape(name)} — {html.escape(cfg.get('name','Campaign'))}", body)


@app.route("/houses/<slug>")
def house_detail(slug: str):
    """Detail page for a single house. Same structure as mount_detail:
    header + description + structured fields + optional ``houses/<slug>.md``
    notes + portrait."""
    if not cfg:
        abort(404)
    slug_clean = re.sub(r"[^a-z0-9_-]", "", slug.lower())
    house = (cfg.get("houses") or {}).get(slug_clean)
    if not house:
        abort(404)

    name = house.get("name", slug_clean)
    owner_key = house.get("owner", "")
    owner_label = (cfg.get("characters", {}).get(owner_key) or {}).get("label", owner_key)
    kind = house.get("kind", "house")
    location = house.get("location", "")
    value_gp = int(house.get("value_gp", 0))

    # Header
    sub_parts = [html.escape(kind)]
    if location:
        sub_parts.append(f"in {html.escape(location)}")
    if owner_key:
        sub_parts.append(f"owned by <a href='/sheets/{html.escape(owner_key)}'>{html.escape(owner_label)}</a>")
    sub = '<span class="sep">·</span>'.join(sub_parts)
    portrait_html = _holding_portrait_html("houses", slug_clean, house.get("portrait", ""))
    header = (
        '<div class="sheet-header">'
        '<div class="sheet-header-main">'
        f'<h1>{html.escape(name)}</h1>'
        f'<div class="sheet-id">{sub}</div>'
        '</div>'
        f'{portrait_html}'
        '</div>'
    )

    panels = []

    desc = (house.get("description") or "").strip()
    if desc:
        panels.append(_sheet_panel("Description", f'<p>{html.escape(desc)}</p>'))

    # Structured fields
    structured_rows = []
    if value_gp:
        structured_rows.append(f"<dt>Value</dt><dd>{value_gp:,} gp</dd>")
    caretaker = (house.get("caretaker") or "").strip()
    if caretaker:
        ck = caretaker.lower()
        npc = (cfg.get("characters", {}).get(ck) or cfg.get("npcs", {}).get(ck) or {})
        ck_label = npc.get("label", caretaker)
        if npc:
            structured_rows.append(f"<dt>Caretaker</dt><dd><a href='/sheets/{html.escape(ck)}'>{html.escape(ck_label)}</a></dd>")
        else:
            structured_rows.append(f"<dt>Caretaker</dt><dd>{html.escape(caretaker)}</dd>")
    residents = house.get("residents") or []
    if residents:
        chips = []
        for r in residents:
            rk = str(r).lower()
            npc = (cfg.get("characters", {}).get(rk) or cfg.get("npcs", {}).get(rk) or {})
            rl = npc.get("label", rk)
            if npc:
                chips.append(f"<a href='/sheets/{html.escape(rk)}'>{html.escape(rl)}</a>")
            else:
                chips.append(html.escape(str(r)))
        structured_rows.append(f"<dt>Residents</dt><dd>{', '.join(chips)}</dd>")
    if structured_rows:
        panels.append(_sheet_panel("Details", '<dl class="holding-stats">' + "".join(structured_rows) + '</dl>'))

    inv = house.get("inventory") or []
    if inv:
        panels.append(_sheet_panel("Inventory", _diamond_list(inv)))

    notes_path = cfg["_data_dir"] / "houses" / f"{slug_clean}.md"
    if notes_path.exists():
        panels.append(_sheet_panel("Notes", _markdown_to_html(notes_path.read_text(encoding="utf-8"))))
    else:
        hint = (
            f'<p class="muted">Drop a markdown file at '
            f'<code>campaigns/{html.escape(cfg.get("name", ""))}/houses/{html.escape(slug_clean)}.md</code> '
            f'to add layout sketches, room descriptions, history, hidden caches, or any other free-form detail.</p>'
        )
        panels.append(_sheet_panel("Notes", hint))

    body = '<div class="sheet">' + header + "".join(panels) + '</div>'
    body += '<p class="muted" style="margin-top:18px"><a href="/characters">← back to Cast</a></p>'
    body += _HOLDING_DETAIL_JS
    return render(f"{html.escape(name)} — {html.escape(cfg.get('name','Campaign'))}", body)


@app.route("/api/holdings/<kind>/<slug>/portrait", methods=["POST"])
def api_holding_portrait(kind: str, slug: str):
    """Generate (or regenerate) a portrait for a house or mount. Reuses
    the stored ``portrait_prompt`` if one exists; otherwise builds a
    default from the record's structured fields. Returns the new
    filename and URL on success."""
    if not cfg:
        return jsonify({"error": "no campaign loaded"}), 400
    if kind not in ("houses", "mounts"):
        return jsonify({"error": "kind must be 'houses' or 'mounts'"}), 400
    slug_clean = re.sub(r"[^a-z0-9_-]", "", slug.lower())
    bucket = cfg.get(kind, {}) or {}
    if slug_clean not in bucket:
        return jsonify({"error": f"no {kind[:-1]} '{slug_clean}'"}), 404
    record = bucket[slug_clean]
    prompt = (record.get("portrait_prompt") or "").strip()
    if not prompt:
        from tools.holdings import _portrait_prompt_for_house, _portrait_prompt_for_mount
        builder = _portrait_prompt_for_house if kind == "houses" else _portrait_prompt_for_mount
        prompt = builder(record)
    pr = _img.generate_portrait_for(cfg, slug_clean, prompt)
    if "error" in pr:
        return jsonify(pr), 500
    record["portrait"] = pr["filename"]
    record["portrait_prompt"] = prompt
    _c.save_campaign(cfg)
    return jsonify({
        "ok": True,
        "filename": pr["filename"],
        "url": f"/images/{pr['filename']}",
    })


@app.route("/api/characters/<slug>/portrait", methods=["POST"])
def api_character_portrait(slug: str):
    """Regenerate a PC/NPC portrait from the sheet page (the ↻ button).

    Body (optional JSON): ``{"prompt": "<extra direction>"}``. The extra text is
    appended to the character's stored ``portrait_prompt`` — the anchor that
    keeps the likeness / CHA-appropriate appearance consistent — so a hint
    refines rather than replaces it. With no stored prompt the typed text is
    used alone. An empty body just redraws from the base prompt (mirrors the
    regenerate_portrait MCP tool). The new image is indexed by slug, so
    _find_portrait picks it up on reload; the stored base prompt is left
    unchanged so future regenerations stay anchored."""
    if not cfg:
        return jsonify({"error": "no campaign loaded"}), 400
    slug_clean = re.sub(r"[^a-z0-9_-]", "", slug.lower())
    char = (cfg.get("characters", {}) or {}).get(slug_clean) \
        or (cfg.get("npcs", {}) or {}).get(slug_clean)
    if not char:
        return jsonify({"error": f"no character '{slug_clean}'"}), 404

    data = request.get_json(silent=True) or {}
    extra = (data.get("prompt") or "").strip()
    base = (char.get("portrait_prompt") or "").strip()
    if base and extra:
        prompt = f"{base}. {extra}"
    elif extra:
        prompt = extra
    elif base:
        prompt = base
    else:
        return jsonify({"error": "No portrait_prompt stored for this character and "
                                 "no prompt provided — describe the portrait you want."}), 400

    pr = _img.generate_portrait_for(cfg, slug_clean, prompt)
    if "error" in pr:
        return jsonify(pr), 500
    return jsonify({
        "ok": True,
        "filename": pr["filename"],
        "url": f"/images/{pr['filename']}",
    })


# ---------------------------------------------------------------------------
# "Did you know" lore facts — a cross-campaign trivia store shown on /play
# during the wait for the DM's reply. Schema + CRUD live in tools/lore_facts.py
# (shared with the MCP tools); these endpoints are thin HTTP wrappers.
# ---------------------------------------------------------------------------
@app.route("/api/facts", methods=["GET"])
def api_facts_list():
    """List facts. Optional ?category=, ?campaign=, ?enabled=0|1 filters."""
    en = request.args.get("enabled")
    rows = _facts.list_facts(
        category=request.args.get("category") or None,
        campaign=request.args.get("campaign") or None,
        enabled=(int(en) if en in ("0", "1") else None),
    )
    return jsonify({"facts": rows, "count": len(rows)})


@app.route("/api/facts/random", methods=["GET"])
def api_facts_random():
    """Return ?n= random enabled facts (default 1, max 100), optionally
    filtered by ?category= / ?campaign=. This is what /play pulls on load to
    fill the 'did you know' rotation."""
    rows = _facts.random_facts(
        n=request.args.get("n", 1),
        category=request.args.get("category") or None,
        campaign=request.args.get("campaign") or None,
    )
    return jsonify({"facts": rows, "count": len(rows)})


@app.route("/api/facts", methods=["POST"])
def api_facts_create():
    """Create a fact. Body: {text (required), category?, source?, campaign?}."""
    data = request.get_json(silent=True) or {}
    res = _facts.create_fact(
        text=data.get("text", ""),
        category=data.get("category", ""),
        source=data.get("source", ""),
        campaign=data.get("campaign", ""),
    )
    return (jsonify(res), 400) if "error" in res else (jsonify(res), 201)


@app.route("/api/facts/<int:fid>", methods=["PATCH", "PUT"])
def api_facts_update(fid: int):
    """Update a fact. Body may include text, category, source, campaign, enabled."""
    data = request.get_json(silent=True) or {}
    fields = {k: data[k] for k in ("text", "category", "source", "campaign", "enabled")
              if k in data}
    res = _facts.update_fact(fid, **fields)
    if "error" in res:
        return jsonify(res), 404 if res["error"].startswith("no fact") else 400
    return jsonify(res)


@app.route("/api/facts/<int:fid>", methods=["DELETE"])
def api_facts_delete(fid: int):
    """Delete a fact by id."""
    res = _facts.delete_fact(fid)
    return (jsonify(res), 404) if "error" in res else jsonify(res)


@app.route("/characters")
def characters():
    campaign_name = cfg.get("name", "Campaign")
    chars_dir = cfg["_data_dir"] / "characters"
    index = _load_image_index()

    # Live party panel (PCs) sits above the full cast grid.
    party_section = _party_body(heading_tag="h2", heading_text="Party")
    # PC record keys — so party members aren't also listed in the cast grid.
    pc_keys = set(cfg.get("characters", {}).keys())
    page_title = f"{campaign_name} — Cast"

    if not chars_dir.exists():
        body = f"<h1>{html.escape(page_title)}</h1>" + party_section
        return render(page_title, body)

    meta = _c.load_character_meta(cfg)
    npcs = cfg.get("npcs", {})

    cards = []
    locations: set[str] = set()
    chapters: set[str] = set()
    dispositions: set[str] = set()
    for md in sorted(chars_dir.glob("*.md")):
        slug = md.stem
        if slug in pc_keys:        # already shown in the live Party panel
            continue
        name = _read_first_heading(md)
        blurb = _read_first_paragraph(md)
        portrait = _find_portrait(name, index, slug)

        m = meta.get(slug, {})
        loc = (m.get("location") or "").strip()
        chap = (m.get("chapter") or "").strip()
        if loc:
            locations.add(loc)
        if chap:
            chapters.add(chap)

        # Disposition toward the party (from campaign.json["npcs"]).
        nd = npcs.get(slug) or {}
        disp_band = None
        if "disposition" in nd:
            disp_band = _c.disposition_band(nd["disposition"])
            dispositions.add(disp_band["label"])

        img_html = ""
        if portrait:
            fn = html.escape(portrait["filename"])
            img_html = f'<img src="/images/{fn}" alt="{html.escape(name)}" class="portrait">'

        tags = []
        if disp_band:
            v = disp_band["value"]
            tags.append(
                f'<span class="char-tag disp" '
                f'style="border-color:{disp_band["color"]};color:{disp_band["color"]}">'
                f'{html.escape(disp_band["label"])} ({v:+d})</span>'
            )
        if loc:
            tags.append(f'<span class="char-tag">📍 {html.escape(loc)}</span>')
        if chap:
            tags.append(f'<span class="char-tag">📖 {html.escape(chap)}</span>')
        tags_html = f'<p class="char-tags">{"".join(tags)}</p>' if tags else ""

        cards.append(
            f'<div class="card clearfix char-card" '
            f'data-name="{html.escape(name.lower())}" '
            f'data-blurb="{html.escape(blurb.lower())}" '
            f'data-location="{html.escape(loc)}" '
            f'data-chapter="{html.escape(chap)}" '
            f'data-disposition="{html.escape(disp_band["label"]) if disp_band else ""}">'
            f"{img_html}"
            f'<h2><a href="/sheets/{html.escape(slug)}">{html.escape(name)}</a></h2>'
            f"<p>{_inline(html.escape(blurb))}</p>"
            f"{tags_html}"
            f"</div>"
        )

    if not cards:
        body = (
            f"<h1>{html.escape(page_title)}</h1>"
            + party_section
            + "<h2>All Characters</h2>"
            "<p class='muted'>No other characters recorded yet.</p>"
        )
        return render(page_title, body)

    def _opts(values: set[str]) -> str:
        out = ['<option value="">All</option>']
        for v in sorted(values, key=str.lower):
            out.append(f'<option value="{html.escape(v)}">{html.escape(v)}</option>')
        return "".join(out)

    loc_filter = (
        f'<select id="char-loc" aria-label="Filter by location">{_opts(locations)}</select>'
        if locations else ""
    )
    chap_filter = (
        f'<select id="char-chap" aria-label="Filter by chapter">{_opts(chapters)}</select>'
        if chapters else ""
    )
    # Disposition options are ordered by the band scale (Devoted → Sworn
    # enemy), not alphabetically — only bands with members are listed.
    def _disp_opts(present: set[str]) -> str:
        out = ['<option value="">All</option>']
        for label in _c.disposition_band_order():
            if label in present:
                out.append(f'<option value="{html.escape(label)}">{html.escape(label)}</option>')
        return "".join(out)

    disp_filter = (
        f'<select id="char-disp" aria-label="Filter by disposition">{_disp_opts(dispositions)}</select>'
        if dispositions else ""
    )

    controls = (
        '<div class="char-controls">'
        '<input type="search" id="char-search" placeholder="Search characters…" aria-label="Search characters">'
        + (f'<label>📍 {loc_filter}</label>' if loc_filter else "")
        + (f'<label>📖 {chap_filter}</label>' if chap_filter else "")
        + (f'<label>🤝 {disp_filter}</label>' if disp_filter else "")
        + '<span id="char-count" class="muted"></span>'
        "</div>"
    )

    grid = f'<div id="char-grid">{"".join(cards)}</div>'
    no_match = '<p id="char-nomatch" class="muted" hidden>No characters match.</p>'

    script = """
<style>
  .char-controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 12px 0 20px; }
  .char-controls input[type=search] { flex: 1; min-width: 180px; padding: 6px 10px; font: inherit;
    background: var(--panel-2, #1b1b1b); color: var(--ink, #e8e2d0);
    border: 1px solid var(--border, #3a3a3a); border-radius: 6px; }
  .char-controls select { padding: 5px 8px; font: inherit; background: var(--panel-2, #1b1b1b);
    color: var(--ink, #e8e2d0); border: 1px solid var(--border, #3a3a3a); border-radius: 6px; }
  .char-controls label { display: inline-flex; align-items: center; gap: 4px; font-size: 0.9em; }
  .char-tags { margin: 6px 0 0; }
  .char-tag { display: inline-block; font-size: 0.78em; opacity: 0.8; margin-right: 8px;
    padding: 1px 6px; border: 1px solid var(--rule, #3a3a3a); border-radius: 10px; }
  .char-tag.disp { opacity: 1; font-weight: 500; }
  .char-card[hidden] { display: none; }
</style>
<script>
(function(){
  const search = document.getElementById('char-search');
  const locSel = document.getElementById('char-loc');
  const chapSel = document.getElementById('char-chap');
  const dispSel = document.getElementById('char-disp');
  const cards = Array.from(document.querySelectorAll('.char-card'));
  const count = document.getElementById('char-count');
  const noMatch = document.getElementById('char-nomatch');

  function apply(){
    const q = (search.value || '').trim().toLowerCase();
    const loc = locSel ? locSel.value : '';
    const chap = chapSel ? chapSel.value : '';
    const disp = dispSel ? dispSel.value : '';
    let shown = 0;
    cards.forEach(c => {
      const okText = !q || c.dataset.name.includes(q) || c.dataset.blurb.includes(q);
      const okLoc = !loc || c.dataset.location === loc;
      const okChap = !chap || c.dataset.chapter === chap;
      const okDisp = !disp || c.dataset.disposition === disp;
      const ok = okText && okLoc && okChap && okDisp;
      c.hidden = !ok;
      if (ok) shown++;
    });
    if (count) count.textContent = shown + ' of ' + cards.length;
    if (noMatch) noMatch.hidden = shown !== 0;
  }

  let t = null;
  search.addEventListener('input', () => { clearTimeout(t); t = setTimeout(apply, 120); });
  if (locSel) locSel.addEventListener('change', apply);
  if (chapSel) chapSel.addEventListener('change', apply);
  if (dispSel) dispSel.addEventListener('change', apply);
  apply();
})();
</script>
"""

    body = (
        f"<h1>{html.escape(page_title)}</h1>"
        + party_section
        + "<h2>All Characters</h2>"
        + controls + grid + no_match + script
    )
    return render(page_title, body)


@app.route("/locations")
def locations():
    campaign_name = cfg.get("name", "Campaign")
    locs_dir = cfg["_data_dir"] / "locations"

    if not locs_dir.exists():
        body = f"<h1>{html.escape(campaign_name)} — Locations</h1><p class='muted'>No locations yet.</p>"
        return render(f"{campaign_name} — Locations", body)

    fog = _fog_on()
    qs = _fog_qs()
    visited = _visited_slugs() if fog else set()

    areas = []
    standalone = []

    for item in sorted(locs_dir.iterdir()):
        if item.is_dir():
            children = list(item.glob("*.md"))
            if fog:
                children = [f for f in children if f.stem in visited]
                # Show the area only once the party has reached it or one of
                # the places inside it.
                if item.name not in visited and not children:
                    continue
            area_md = locs_dir / f"{item.name}.md"
            area_name = _read_first_heading(area_md) if area_md.exists() else item.name
            areas.append((item.name, area_name, len(children)))
        elif item.is_file() and item.suffix == ".md":
            if fog and item.stem not in visited:
                continue
            name = _read_first_heading(item)
            blurb = _read_first_paragraph(item)
            standalone.append((item.stem, name, blurb))

    left_html = ""
    if areas:
        area_items = "".join(
            f'<div class="card">'
            f'<h2><a href="/locations/{html.escape(slug)}{qs}">{html.escape(name)}</a></h2>'
            f'<p class="muted">{sub} location{"s" if sub != 1 else ""}</p>'
            f'</div>'
            for slug, name, sub in areas
        )
        left_html = f"<h2>Areas</h2>{area_items}"

    right_html = ""
    if standalone:
        loc_items = "".join(
            f'<div class="card">'
            f'<h2><a href="/locations/_/{html.escape(slug)}{qs}">{html.escape(name)}</a></h2>'
            f'<p>{html.escape(blurb)}</p>'
            f'</div>'
            for slug, name, blurb in standalone
        )
        right_html = f"<h2>Locations</h2>{loc_items}"

    if left_html or right_html:
        body = (
            f"<h1>{html.escape(campaign_name)} — Locations</h1>"
            f'<div class="two-col">'
            f'<div>{left_html}</div>'
            f'<div>{right_html}</div>'
            f'</div>'
        )
    else:
        body = f"<h1>{html.escape(campaign_name)} — Locations</h1><p class='muted'>No locations yet.</p>"

    return render(f"{campaign_name} — Locations", body)


@app.route("/locations/<area>")
def area_detail(area: str):
    campaign_name = cfg.get("name", "Campaign")
    locs_dir = cfg["_data_dir"] / "locations"

    area_md = locs_dir / f"{area}.md"
    area_dir = locs_dir / area

    fog = _fog_on()
    qs = _fog_qs()
    visited = _visited_slugs() if fog else set()

    if fog:
        known_child = area_dir.exists() and any(
            md.stem in visited for md in area_dir.glob("*.md")
        )
        if area not in visited and not known_child:
            abort(404)

    area_name = _read_first_heading(area_md) if area_md.exists() else area

    content_html = ""
    if area_md.exists():
        content_html = f'<div class="card">{_markdown_to_html(area_md.read_text(encoding="utf-8"))}</div>'

    sub_cards = []
    if area_dir.exists():
        for md in sorted(area_dir.glob("*.md")):
            slug = md.stem
            if fog and slug not in visited:
                continue
            name = _read_first_heading(md)
            blurb = _read_first_paragraph(md)
            sub_cards.append(
                f'<div class="card">'
                f'<h2><a href="/locations/{html.escape(area)}/{html.escape(slug)}{qs}">{html.escape(name)}</a></h2>'
                f'<p>{html.escape(blurb)}</p>'
                f'</div>'
            )

    sub_html = ""
    if sub_cards:
        sub_html = f"<h2>Locations in {html.escape(area_name)}</h2>" + "".join(sub_cards)

    body = (
        f'<p><a href="/locations{qs}">← Locations</a></p>'
        f"<h1>{html.escape(area_name)}</h1>"
        f"{content_html}"
        f"{sub_html}"
    )
    return render(f"{html.escape(area_name)} — {html.escape(campaign_name)}", body)


@app.route("/locations/_/<slug>")
def location_standalone(slug: str):
    """Serve a top-level (non-area) location."""
    campaign_name = cfg.get("name", "Campaign")
    locs_dir = cfg["_data_dir"] / "locations"
    path = locs_dir / f"{slug}.md"

    if not path.exists():
        abort(404)
    if _fog_on() and slug not in _visited_slugs():
        abort(404)

    name = _read_first_heading(path)
    content_html = _markdown_to_html(path.read_text(encoding="utf-8"))

    body = (
        f'<p><a href="/locations{_fog_qs()}">← Locations</a></p>'
        f'<div class="card">{content_html}</div>'
    )
    return render(f"{html.escape(name)} — {html.escape(campaign_name)}", body)


@app.route("/locations/<area>/<slug>")
def location_detail(area: str, slug: str):
    campaign_name = cfg.get("name", "Campaign")
    locs_dir = cfg["_data_dir"] / "locations"
    path = locs_dir / area / f"{slug}.md"

    if not path.exists():
        abort(404)
    if _fog_on() and slug not in _visited_slugs():
        abort(404)

    qs = _fog_qs()
    name = _read_first_heading(path)
    area_md = locs_dir / f"{area}.md"
    area_name = _read_first_heading(area_md) if area_md.exists() else area
    content_html = _markdown_to_html(path.read_text(encoding="utf-8"))

    body = (
        f'<p><a href="/locations{qs}">← Locations</a> / '
        f'<a href="/locations/{html.escape(area)}{qs}">{html.escape(area_name)}</a></p>'
        f'<div class="card">{content_html}</div>'
    )
    return render(f"{html.escape(name)} — {html.escape(campaign_name)}", body)


@app.route("/images/<filename>")
def serve_image(filename: str):
    images_dir = cfg["_data_dir"] / "images"
    return send_from_directory(str(images_dir), filename)


@app.route("/api/sessions/<sid>/activate", methods=["POST"])
def api_session_activate(sid: str):
    """Pin a past Claude Code session as the one /play resumes on its next
    turn. Validates the session id charset and that a matching JSONL
    transcript exists before writing ``.dm_session``."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", sid):
        return jsonify({"error": "invalid session id"}), 400
    transcript = _dm._project_jsonl_dir() / f"{sid}.jsonl"
    if not transcript.is_file():
        return jsonify({"error": "no transcript found for that session"}), 404
    _dm.set_session(sid)
    return jsonify({"ok": True, "session_id": sid})


@app.route("/api/sessions/<sid>", methods=["DELETE"])
def api_session_delete(sid: str):
    """Delete a Claude Code session JSONL transcript and the matching
    campaigns/<slug>/_session-logs/<sid>.md export if one exists.
    Also strips the sid from every campaign's sessions.json manifest.
    Validates the session id against the same charset the /sessions/<sid>
    viewer accepts."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", sid):
        return jsonify({"error": "invalid session id"}), 400

    sdir = _sessions_dir()
    target = sdir / f"{sid}.jsonl"
    if not target.is_file():
        return jsonify({"error": "session not found"}), 404
    try:
        target.unlink()
    except OSError as exc:
        return jsonify({"error": f"transcript delete failed: {exc}"}), 500

    # Strip from every manifest (sid may have been mis-attributed historically;
    # the loop keeps the index clean across campaigns).
    from tools import session_manifest as _smf
    for slug in _smf.list_campaign_slugs() + [_smf._UNSORTED_SLUG]:
        _dm.remove_session_from_manifest(slug, sid)

    # Best-effort: also remove the corresponding markdown export written by
    # the SessionEnd hook. Check the active campaign first, then any other
    # campaign-scoped dir, then the legacy shared location. Failures here
    # don't fail the response — the primary JSONL is gone.
    log_removed = False
    candidates = []
    for slug in _smf.list_campaign_slugs():
        candidates.append(_campaign_session_md_dir(slug) / f"{sid}.md")
    candidates.append(Path(__file__).parent / "campaigns" / "_session-logs" / f"{sid}.md")
    for log_md in candidates:
        if log_md.is_file():
            try:
                log_md.unlink()
                log_removed = True
            except OSError:
                pass

    return jsonify({"ok": True, "deleted": sid, "log_removed": log_removed})


def _session_line_matches(text: str, terms: list[str]) -> bool:
    low = text.lower()
    return all(t in low for t in terms)


def _session_make_snippet(text: str, terms: list[str], length: int = 160) -> str:
    text = text.replace("\n", " ").strip()
    low = text.lower()
    pos = -1
    for t in terms:
        i = low.find(t)
        if i >= 0 and (pos < 0 or i < pos):
            pos = i
    if pos < 0:
        return text[:length] + ("…" if len(text) > length else "")
    half = length // 2
    start = max(0, pos - half)
    end = min(len(text), start + length)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


@app.route("/api/sessions/search")
def api_sessions_search():
    """Full-text search across every session JSONL file in the project.
    AND-of-words: all whitespace-separated terms must appear (case-
    insensitive) in the same line. Player text and DM text are searched;
    tool calls and tool results are skipped to keep snippets meaningful."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"query": q, "results": []})
    terms = [t.lower() for t in q.split() if t.strip()]
    if not terms:
        return jsonify({"query": q, "results": []})

    sdir = _sessions_dir()
    if not sdir.is_dir():
        return jsonify({"query": q, "terms": terms, "results": []})
    if not _active_campaign_slug():
        return jsonify({"query": q, "terms": terms, "results": []})

    MAX_PER_SESSION = 5
    MAX_RESULTS = 50

    results = []
    for path in _active_campaign_session_paths():
        matches = []
        match_count = 0
        try:
            with path.open() as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    role = rec.get("type")
                    if role == "user":
                        content = rec.get("message", {}).get("content", "")
                        if isinstance(content, str):
                            text = _export.clean_user_text(content)
                            if text and _session_line_matches(text, terms):
                                match_count += 1
                                if len(matches) < MAX_PER_SESSION:
                                    matches.append({
                                        "role": "player",
                                        "snippet": _session_make_snippet(text, terms),
                                    })
                    elif role == "assistant":
                        content = rec.get("message", {}).get("content", [])
                        if not isinstance(content, list):
                            continue
                        for block in content:
                            if block.get("type") != "text":
                                continue
                            text = (block.get("text") or "").strip()
                            if text and _session_line_matches(text, terms):
                                match_count += 1
                                if len(matches) < MAX_PER_SESSION:
                                    matches.append({
                                        "role": "dm",
                                        "snippet": _session_make_snippet(text, terms),
                                    })
        except OSError:
            continue
        if matches:
            st = path.stat()
            when = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            results.append({
                "sid": path.stem,
                "when": when,
                "matches": matches,
                "match_count": match_count,
            })
            if len(results) >= MAX_RESULTS:
                break

    return jsonify({"query": q, "terms": terms, "results": results})


# --- TTS narration endpoints --------------------------------------------

@app.route("/api/tts/synthesize", methods=["POST"])
def api_tts_synthesize():
    """Render a DM-turn text to MP3 with per-character voice mapping.
    Body: ``{"text": "..."}`` Returns the segment manifest plus the URL
    to the cached audio file. Re-requests for the same text + voice
    combination hit the on-disk cache and don't re-pay."""
    if not cfg:
        return jsonify({"error": "no campaign loaded"}), 400
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "empty text"}), 400
    if len(text) > 20000:
        return jsonify({"error": "text exceeds 20k chars"}), 413
    try:
        manifest = _tts.synthesize_turn(cfg["_dir"], text)
    except EnvironmentError as exc:
        return jsonify({"error": str(exc)}), 500
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001  — surface API failures
        return jsonify({"error": f"TTS failed: {exc}"}), 502
    manifest["audio_url"] = (
        f"/api/tts/audio/{manifest['filename']}"
    )
    return jsonify(manifest)


@app.route("/api/tts/audio/<filename>")
def api_tts_audio(filename: str):
    """Serve a cached MP3 from the active campaign's audio dir."""
    if not cfg:
        abort(404)
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+\.mp3", filename):
        abort(400)
    audio_dir = cfg["_dir"] / "audio"
    target = audio_dir / filename
    if not target.is_file():
        abort(404)
    return send_from_directory(str(audio_dir), filename, mimetype="audio/mpeg")


@app.route("/api/tts/voices", methods=["GET"])
def api_tts_voices_get():
    """Read voice mapping + valid options + the list of character slugs
    the UI can offer for assignment."""
    if not cfg:
        return jsonify({"error": "no campaign loaded"}), 400
    return jsonify({
        "mapping": _tts.load_voice_map(cfg["_dir"]),
        "valid_voices": list(_tts.VALID_VOICES),
        "characters": _tts.known_character_slugs(cfg["_dir"]),
    })


@app.route("/api/tts/voices", methods=["PUT"])
def api_tts_voices_put():
    """Replace the voice mapping. Body shape matches load_voice_map's
    return value; unknown voice names are silently dropped by save."""
    if not cfg:
        return jsonify({"error": "no campaign loaded"}), 400
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "expected JSON object"}), 400
    try:
        _tts.save_voice_map(cfg["_dir"], payload)
    except OSError as exc:
        return jsonify({"error": f"save failed: {exc}"}), 500
    return jsonify({"ok": True, "mapping": _tts.load_voice_map(cfg["_dir"])})


@app.route("/api/images/<filename>", methods=["DELETE"])
def api_image_delete(filename: str):
    """Delete a campaign image and prune its row from index.json.
    Validates the filename is a basename and known to the index — refuses
    paths that traverse out of the images dir."""
    if not cfg:
        return jsonify({"error": "no campaign loaded"}), 400
    # Refuse anything that isn't a plain basename.
    if "/" in filename or "\\" in filename or filename.startswith(".") or filename in ("", ".", ".."):
        return jsonify({"error": "invalid filename"}), 400

    images_dir = cfg["_data_dir"] / "images"
    index_file = images_dir / "index.json"
    if not index_file.exists():
        return jsonify({"error": "no image index"}), 404

    try:
        records = json.loads(index_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return jsonify({"error": f"index unreadable: {exc}"}), 500

    new_records = [r for r in records if r.get("filename") != filename]
    if len(new_records) == len(records):
        return jsonify({"error": "filename not in index"}), 404

    target = images_dir / filename
    if target.exists():
        try:
            target.unlink()
        except OSError as exc:
            return jsonify({"error": f"file delete failed: {exc}"}), 500

    _c.atomic_write_text(index_file, json.dumps(new_records, indent=2))
    return jsonify({"ok": True, "deleted": filename})


@app.route("/favicon.ico")
def favicon():
    """Serve the SVG favicon for legacy /favicon.ico requests so they don't
    404 in logs. Browsers that honour the <link rel='icon'> in BASE_TEMPLATE
    will already have loaded it from /static/favicon.svg."""
    return send_from_directory(
        Path(__file__).parent / "static",
        "favicon.svg",
        mimetype="image/svg+xml",
    )


# Monster routes are defined in dashboard_monsters.py and registered at the
# bottom of this module via dashboard_monsters.init(...).


# ---------------------------------------------------------------------------
# Spells
# ---------------------------------------------------------------------------

SELECT_STYLE = (
    'style="background:#2a2018;border:1px solid #4a3828;color:#d4c5a9;'
    'padding:6px 10px;border-radius:4px;font-size:1em"'
)
# Raw CSS (no ``style="…"`` wrapper) so callers can append extra declarations,
# e.g. ``style="{_INPUT_CSS};width:100%"``. INPUT_STYLE is the ready-to-drop-in
# attribute form for the common case (a standalone ``{INPUT_STYLE}`` in a tag).
_INPUT_CSS = (
    'background:#2a2018;border:1px solid #4a3828;color:#d4c5a9;'
    'padding:6px 10px;border-radius:4px;width:220px;font-size:1em'
)
INPUT_STYLE = f'style="{_INPUT_CSS}"'
BTN_STYLE = (
    'style="background:#3a2818;border:1px solid #5a4030;color:#c8a96e;'
    'padding:6px 14px;border-radius:4px;cursor:pointer"'
)

_WIZARD_SCHOOLS = [
    "Abjuration", "Alteration", "Conjuration/Summoning", "Divination",
    "Enchantment/Charm", "Evocation", "Illusion/Phantasm", "Invocation/Evocation", "Necromancy",
]
_PRIEST_SCHOOLS = [
    "Abjuration", "Alteration", "Conjuration", "Divination",
    "Enchantment", "Evocation", "Illusion", "Necromancy",
]
_ALL_SCHOOLS = sorted(set(_WIZARD_SCHOOLS + _PRIEST_SCHOOLS))


def _spell_query(q: str, caster: str, school: str, level: str):
    if not _2E_DB.exists():
        return []
    conn = _db(_2E_DB)
    where, params = ["1=1"], []
    if q:
        where.append("name LIKE ? COLLATE NOCASE")
        params.append(f"%{q}%")
    if caster:
        where.append("lower(caster) = ?")
        params.append(caster)
    if level and level.isdigit():
        where.append("level = ?")
        params.append(int(level))
    if school:
        where.append("school LIKE ? COLLATE NOCASE")
        params.append(f"%{school}%")
    rows = conn.execute(
        f"SELECT id, name, level, school, caster, casting_time FROM spells "
        f"WHERE {' AND '.join(where)} ORDER BY level, name",
        params,
    ).fetchall()
    return rows


def _spell_card(r) -> str:
    return (
        f'<div class="card" style="padding:8px 14px">'
        f'<h2 style="margin:0 0 2px">'
        f'<a href="/spells/{r["id"]}">{html.escape(r["name"])}</a></h2>'
        f'<p class="muted" style="margin:0;font-size:.9em">'
        f'Level&nbsp;{html.escape(str(r["level"] or "—"))} &nbsp;·&nbsp; '
        f'{html.escape(r["school"] or "—")} &nbsp;·&nbsp; '
        f'CT&nbsp;{html.escape(str(r["casting_time"] or "—"))}'
        f'</p></div>'
    )


def _spell_section(title: str, rows) -> str:
    if not rows:
        return ""
    cards = "".join(_spell_card(r) for r in rows)
    count = f"<span class='muted' style='font-size:.85em;margin-left:8px'>({len(rows)})</span>"
    return f"<h2 style='margin-top:24px'>{html.escape(title)}{count}</h2>{cards}"


def _spell_results_html(rows, caster: str) -> str:
    if not rows:
        return "<p class='muted'>No results.</p>"
    if not caster:
        wizard_rows = [r for r in rows if (r["caster"] or "").lower() == "wizard"]
        priest_rows = [r for r in rows if (r["caster"] or "").lower() == "priest"]
        other_rows  = [r for r in rows if (r["caster"] or "").lower() not in ("wizard", "priest")]
        count = f"<p class='muted' style='margin-bottom:4px'>{len(rows)} spell{'s' if len(rows) != 1 else ''}</p>"
        return (count
                + _spell_section("Wizard Spells", wizard_rows)
                + _spell_section("Priest Spells", priest_rows)
                + _spell_section("Other", other_rows))
    count = f"<p class='muted' style='margin-bottom:4px'>{len(rows)} spell{'s' if len(rows) != 1 else ''}</p>"
    return count + "".join(_spell_card(r) for r in rows)


@app.route("/api/spells/cards")
def api_spell_cards():
    q      = request.args.get("q", "").strip()
    caster = request.args.get("caster", "").strip().lower()
    school = request.args.get("school", "").strip()
    level  = request.args.get("level", "").strip()
    rows   = _spell_query(q, caster, school, level)
    return Response(_spell_results_html(rows, caster), mimetype="text/html")


_SPELLS_JS = r"""
<script>
const WIZARD_SCHOOLS = """ + json.dumps(_WIZARD_SCHOOLS) + r""";
const PRIEST_SCHOOLS = """ + json.dumps(_PRIEST_SCHOOLS) + r""";
const ALL_SCHOOLS    = """ + json.dumps(_ALL_SCHOOLS) + r""";

let _timer = null;

function schoolsFor(caster) {
  if (caster === "wizard") return WIZARD_SCHOOLS;
  if (caster === "priest") return PRIEST_SCHOOLS;
  return ALL_SCHOOLS;
}

function updateSchools() {
  const caster = document.querySelector('input[name="caster"]:checked').value;
  const sel = document.getElementById("school-sel");
  const prev = sel.value;
  sel.innerHTML = '<option value="">All schools</option>';
  for (const s of schoolsFor(caster)) {
    const opt = document.createElement("option");
    opt.value = s; opt.textContent = s;
    if (s === prev) opt.selected = true;
    sel.appendChild(opt);
  }
}

async function doFetch() {
  const q      = document.getElementById("spell-q").value.trim();
  const caster = document.querySelector('input[name="caster"]:checked').value;
  const school = document.getElementById("school-sel").value;
  const level  = document.getElementById("level-sel").value;
  if (q.length > 0 && q.length < 3) return;
  const p = new URLSearchParams();
  if (q)      p.set("q",      q);
  if (caster) p.set("caster", caster);
  if (school) p.set("school", school);
  if (level)  p.set("level",  level);
  const r = await fetch("/api/spells/cards?" + p.toString());
  document.getElementById("spell-results").innerHTML = await r.text();
}

function schedule() {
  clearTimeout(_timer);
  _timer = setTimeout(doFetch, 280);
}

document.getElementById("spell-q").addEventListener("input", schedule);
document.getElementById("school-sel").addEventListener("change", schedule);
document.getElementById("level-sel").addEventListener("change", schedule);
document.querySelectorAll('input[name="caster"]').forEach(r => {
  r.addEventListener("change", () => { updateSchools(); schedule(); });
});
</script>
"""


@app.route("/spells")
def spells():
    q      = request.args.get("q", "").strip()
    caster = request.args.get("caster", "").strip().lower()
    level  = request.args.get("level", "").strip()
    school = request.args.get("school", "").strip()

    if not _2E_DB.exists():
        body = "<h1>Spells</h1><p class='muted'>2e.db not found. Run tools/build_2e_db.py.</p>"
        return render("Spells", body)

    rows = _spell_query(q, caster, school, level)

    def _radio(val, label):
        chk = "checked" if caster == val else ""
        return (
            f'<label style="display:inline-flex;align-items:center;gap:4px;'
            f'cursor:pointer;color:#d4c5a9;font-size:.95em">'
            f'<input type="radio" name="caster" value="{val}" {chk} '
            f'style="accent-color:#c8a96e"> {label}</label>'
        )

    radios = (
        f'<div style="display:flex;gap:14px;align-items:center;'
        f'background:#2a2018;border:1px solid #4a3828;border-radius:4px;padding:6px 12px">'
        + _radio("", "Both") + _radio("wizard", "Wizard") + _radio("priest", "Priest")
        + '</div>'
    )

    schools_for_caster = (
        _WIZARD_SCHOOLS if caster == "wizard"
        else _PRIEST_SCHOOLS if caster == "priest"
        else _ALL_SCHOOLS
    )
    school_opts = '<option value="">All schools</option>' + "".join(
        f'<option value="{s}" {"selected" if school == s else ""}>{s}</option>'
        for s in schools_for_caster
    )
    level_opts = '<option value="">All levels</option>' + "".join(
        f'<option value="{i}" {"selected" if level == str(i) else ""}>Level {i}</option>'
        for i in range(1, 10)
    )

    controls = (
        f'<div style="margin-bottom:20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
        f'<input id="spell-q" name="q" value="{html.escape(q)}" placeholder="Search by name…" {INPUT_STYLE} autocomplete="off">'
        f'{radios}'
        f'<select id="school-sel" name="school" {SELECT_STYLE}>{school_opts}</select>'
        f'<select id="level-sel" name="level" {SELECT_STYLE}>{level_opts}</select>'
        f'</div>'
    )

    body = (
        f"<h1>Spells</h1>{controls}"
        f'<div id="spell-results">{_spell_results_html(rows, caster)}</div>'
        + _SPELLS_JS
    )
    return render("Spells", body)


@app.route("/spells/<int:spell_id>")
def spell_detail(spell_id: int):
    if not _2E_DB.exists():
        abort(404)

    conn = _db(_2E_DB)
    row = conn.execute("SELECT * FROM spells WHERE id=?", (spell_id,)).fetchone()

    if row is None:
        abort(404)

    def stat(label, val):
        if not val and val != 0:
            return ""
        return (
            f'<tr><th style="width:160px;white-space:nowrap">{html.escape(label)}</th>'
            f'<td>{html.escape(str(val))}</td></tr>'
        )

    components = []
    if row["verbal"]:  components.append("V")
    if row["somatic"]: components.append("S")
    if row["material"]:components.append("M")
    comp_str = ", ".join(components) or "—"
    if row["materials"]:
        comp_str += f" ({html.escape(row['materials'])})"

    table = (
        "<table>"
        + stat("Caster",       (row["caster"] or "").capitalize())
        + stat("Level",        row["level"])
        + stat("School",       row["school"])
        + stat("Casting Time", row["casting_time"])
        + stat("Range",        row["range"])
        + stat("Area of Effect", row["aoe"])
        + stat("Duration",     row["duration"])
        + stat("Saving Throw", row["save"])
        + stat("Damage",       row["damage"])
        + f'<tr><th>Components</th><td>{comp_str}</td></tr>'
        + stat("Reversible",   "Yes" if row["reversible"] else None)
        + stat("Source",       f"{row['source']} p.{row['page']}" if row["source"] else None)
        + "</table>"
    )

    desc_html = _markdown_to_html(row["description"]) if row["description"] else ""

    body = (
        f'<p><a href="/spells">← Spells</a></p>'
        f'<h1>{html.escape(row["name"])}</h1>'
        f'<div class="card">{table}</div>'
        + (f'<div class="card" style="margin-top:12px">{desc_html}</div>' if desc_html else "")
    )
    return render(f'{html.escape(row["name"])} — Spells', body)


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------

@app.route("/classes")
def classes():
    # Collect (name, slug, hit_die, pr_str, is_homebrew) entries from both sources.
    entries: list[tuple[str, str, str, str, bool, str]] = []

    if _2E_DB.exists():
        conn = _db(_2E_DB)
        for r in conn.execute(
            "SELECT name, hit_die, prime_requisite FROM classes ORDER BY name"
        ).fetchall():
            slug = re.sub(r"[^a-z0-9]+", "-", r["name"].lower()).strip("-")
            try:
                pr = json.loads(r["prime_requisite"] or "[]")
                pr_str = ", ".join(p.capitalize() for p in pr) if pr else "—"
            except (json.JSONDecodeError, TypeError):
                pr_str = str(r["prime_requisite"] or "—")
            entries.append((r["name"], slug, r["hit_die"] or "—", pr_str, False, ""))

    for hb in _hb.list_homebrew():
        if hb.get("_error"):
            entries.append((
                hb.get("name", "?"), hb.get("slug", "?"),
                "—", "—", True,
                f'<span class="tag" style="background:#3a1818;color:#c87a7a">load error</span>',
            ))
            continue
        try:
            pr = json.loads(hb.get("prime_requisite") or "[]")
            pr_str = ", ".join(p.capitalize() for p in pr) if pr else "—"
        except (json.JSONDecodeError, TypeError):
            pr_str = "—"
        entries.append((
            hb["name"], hb["slug"], hb.get("hit_die") or "—",
            pr_str, True, "",
        ))

    if not entries:
        body = "<h1>Classes</h1><p class='muted'>2e.db not found. Run tools/build_2e_db.py.</p>"
        return render("Classes", body)

    entries.sort(key=lambda e: e[0].lower())

    cards = []
    for name, slug, hit_die, pr_str, is_homebrew, extra in entries:
        tag = ('<span class="tag" style="background:#1a3a18;color:#6db86d;'
               'border-color:#3a6a3a">homebrew</span>' if is_homebrew else "")
        cards.append(
            f'<div class="card" style="padding:10px 14px">'
            f'<h2 style="margin:0 0 2px">'
            f'<a href="/classes/{html.escape(slug)}">{html.escape(name)}</a> {tag}{extra}</h2>'
            f'<p class="muted" style="margin:0;font-size:.9em">'
            f'Hit Die: {html.escape(hit_die)} &nbsp;·&nbsp; '
            f'Prime Requisite: {html.escape(pr_str)}'
            f'</p></div>'
        )

    body = "<h1>Classes</h1>" + "".join(cards)
    return render("Classes", body)


@app.route("/classes/<slug>")
def class_detail(slug: str):
    # Try homebrew first; the JSON files are authoritative for any class they
    # define, so a homebrew Barbarian wins over any same-named PHB entry.
    cls_row: dict | None = None
    level_rows: list = []
    is_homebrew = False
    homebrew_extras: dict = {}

    hb = _hb.get_homebrew(slug)
    if hb is not None and not hb.get("_error"):
        cls_row = hb
        level_rows = hb.get("level_rows", [])
        is_homebrew = True
        homebrew_extras = {k: hb.get(k) for k in (
            "ability_requirements", "allowed_races", "allowed_alignments",
            "allowed_armor", "weapon_specialization", "casts_spells",
            "base_movement_rate", "weapon_proficiency_slots",
            "nonweapon_proficiency_slots", "progression_tables",
            "source", "source_url",
        ) if hb.get(k) is not None}
    elif _2E_DB.exists():
        name_pattern = slug.replace("-", " ")
        conn = _db(_2E_DB)
        cls_row = conn.execute(
            "SELECT * FROM classes WHERE lower(replace(name,' ','-')) = ? "
            "OR name LIKE ? COLLATE NOCASE LIMIT 1",
            (slug, f"%{name_pattern}%"),
        ).fetchone()
        if cls_row is not None:
            level_rows = conn.execute(
                "SELECT * FROM class_levels WHERE class_id=? ORDER BY level",
                (cls_row["id"],),
            ).fetchall()

    if cls_row is None:
        abort(404)

    def _g(row, key):
        try:
            return row[key]
        except (KeyError, IndexError):
            return None

    try:
        pr = json.loads(_g(cls_row, "prime_requisite") or "[]")
        pr_str = ", ".join(str(p).capitalize() for p in pr) if pr else "—"
    except (json.JSONDecodeError, TypeError):
        pr_str = str(_g(cls_row, "prime_requisite") or "—")

    try:
        abilities = json.loads(_g(cls_row, "special_abilities") or "[]")
    except (json.JSONDecodeError, TypeError):
        abilities = []

    # Build XP / progression table
    has_spells = any(_g(r, "spell_slots") for r in level_rows)

    header_cells = "<tr><th>Level</th><th>XP</th><th>HD</th><th>THAC0</th><th>Atk</th>"
    header_cells += "<th>Para</th><th>RSW</th><th>Pet</th><th>Brth</th><th>Spell</th>"
    if has_spells:
        header_cells += "<th>Spell Slots</th>"
    header_cells += "</tr>"

    tbody = ""
    for r in level_rows:
        slots_str = ""
        if has_spells and _g(r, "spell_slots"):
            try:
                slots = json.loads(_g(r, "spell_slots"))
                # "level" is the caster level, not a spell tier — skip it.
                slots_str = " / ".join(str(v) for k, v in sorted(slots.items()) if v and k != "level")
            except Exception:
                slots_str = "—"

        xp_v = _g(r, "xp_required")
        xp = f"{xp_v:,}" if xp_v is not None else "—"
        tbody += (
            f"<tr>"
            f"<td>{_g(r, 'level')}</td>"
            f"<td>{xp}</td>"
            f"<td>{html.escape(_g(r, 'hit_dice') or '—')}</td>"
            f"<td>{_g(r, 'thac0') or '—'}</td>"
            f"<td>{html.escape(str(_g(r, 'attacks') or '—'))}</td>"
            f"<td>{_g(r, 'save_paralysis') or '—'}</td>"
            f"<td>{_g(r, 'save_rsw') or '—'}</td>"
            f"<td>{_g(r, 'save_petrify') or '—'}</td>"
            f"<td>{_g(r, 'save_breath') or '—'}</td>"
            f"<td>{_g(r, 'save_spell') or '—'}</td>"
            + (f"<td>{slots_str or '—'}</td>" if has_spells else "")
            + "</tr>"
        )

    prog_table = f"<table><thead>{header_cells}</thead><tbody>{tbody}</tbody></table>"

    abilities_html = ""
    if abilities:
        items = "".join(f"<li>{html.escape(str(a))}</li>" for a in abilities)
        abilities_html = f"<h2>Special Abilities</h2><ul>{items}</ul>"

    desc_html = ""
    if _g(cls_row, "description"):
        paras = [p.strip() for p in (_g(cls_row, "description") or "").split("\n") if p.strip()]
        desc_html = "".join(f"<p>{html.escape(p)}</p>" for p in paras[:4])
        if len(paras) > 4:
            desc_html += f"<p class='muted'>…{len(paras)-4} more paragraphs</p>"

    # Homebrew kit metadata block
    hb_meta_html = ""
    if is_homebrew and homebrew_extras:
        meta_rows = []
        if homebrew_extras.get("ability_requirements"):
            req = ", ".join(f"{k.upper()} {v}" for k, v in homebrew_extras["ability_requirements"].items())
            meta_rows.append(("Ability Requirements", req))
        if homebrew_extras.get("allowed_races"):
            meta_rows.append(("Allowed Races", ", ".join(homebrew_extras["allowed_races"])))
        if homebrew_extras.get("allowed_alignments"):
            meta_rows.append(("Allowed Alignments", ", ".join(homebrew_extras["allowed_alignments"])))
        if homebrew_extras.get("allowed_armor"):
            meta_rows.append(("Allowed Armor", ", ".join(homebrew_extras["allowed_armor"])))
        if homebrew_extras.get("base_movement_rate") is not None:
            meta_rows.append(("Base Movement", str(homebrew_extras["base_movement_rate"])))
        if homebrew_extras.get("weapon_specialization") is not None:
            meta_rows.append(("Weapon Specialization", "yes" if homebrew_extras["weapon_specialization"] else "no"))
        if homebrew_extras.get("casts_spells") is not None:
            meta_rows.append(("Casts Spells", "yes" if homebrew_extras["casts_spells"] else "no"))
        wp = homebrew_extras.get("weapon_proficiency_slots") or {}
        if wp:
            meta_rows.append(("Weapon Profs", f"{wp.get('initial','?')} initial, +1 per {wp.get('advance_every_levels','?')} levels"))
        nwp = homebrew_extras.get("nonweapon_proficiency_slots") or {}
        if nwp:
            meta_rows.append(("Nonweapon Profs", f"{nwp.get('initial','?')} initial, +1 per {nwp.get('advance_every_levels','?')} levels"))
        if meta_rows:
            rows_html = "".join(
                f"<tr><th style='text-align:left'>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>"
                for k, v in meta_rows
            )
            hb_meta_html = f"<h2>Class Restrictions</h2><div class='card'><table>{rows_html}</table></div>"

    # Kit progression tables (climb%, leap, etc.)
    prog_extras_html = ""
    pt = (homebrew_extras.get("progression_tables") or {}) if is_homebrew else {}
    if pt:
        blocks = []
        for tname, tdata in pt.items():
            by_level = tdata.get("by_level") or {}
            if not by_level:
                continue
            sample_val = next(iter(by_level.values()))
            if isinstance(sample_val, dict):
                cols = sorted(sample_val.keys())
                head = "<th>Level</th>" + "".join(f"<th>{html.escape(c.replace('_',' ').title())}</th>" for c in cols)
                rows_h = ""
                for lvl in sorted(by_level, key=lambda k: int(k)):
                    cells = "".join(f"<td>{html.escape(str(by_level[lvl].get(c, '—')))}</td>" for c in cols)
                    rows_h += f"<tr><td>{lvl}</td>{cells}</tr>"
            else:
                head = "<th>Level</th><th>Value</th>"
                rows_h = "".join(
                    f"<tr><td>{lvl}</td><td>{html.escape(str(by_level[lvl]))}</td></tr>"
                    for lvl in sorted(by_level, key=lambda k: int(k))
                )
            desc = tdata.get("description", "")
            blocks.append(
                f"<h3 style='margin-top:14px'>{html.escape(tname.replace('_', ' ').title())}</h3>"
                + (f"<p class='muted'>{html.escape(desc)}</p>" if desc else "")
                + f"<table><thead><tr>{head}</tr></thead><tbody>{rows_h}</tbody></table>"
            )
        if blocks:
            prog_extras_html = (
                f"<h2 style='margin-top:20px'>Kit Progression Tables</h2>"
                f"<div class='card' style='overflow-x:auto'>{''.join(blocks)}</div>"
            )

    # Source citation for homebrew
    src_html = ""
    if is_homebrew:
        src_text = homebrew_extras.get("source") or "homebrew"
        src_url = homebrew_extras.get("source_url") or ""
        if src_url:
            src_html = (f"<p class='muted'>Source: "
                        f"<a href='{html.escape(src_url)}'>{html.escape(src_text)}</a></p>")
        else:
            src_html = f"<p class='muted'>Source: {html.escape(src_text)}</p>"

    homebrew_tag = ('<span class="tag" style="background:#1a3a18;color:#6db86d;'
                    'border-color:#3a6a3a">homebrew</span>' if is_homebrew else "")

    body = (
        f'<p><a href="/classes">← Classes</a></p>'
        f'<h1>{html.escape(_g(cls_row, "name"))} {homebrew_tag}</h1>'
        f'<div class="card">'
        f'<p><strong>Hit Die:</strong> {html.escape(_g(cls_row, "hit_die") or "—")} &nbsp;·&nbsp; '
        f'<strong>Prime Requisite:</strong> {html.escape(pr_str)}</p>'
        + src_html
        + (f"<hr>{desc_html}" if desc_html else "")
        + f'</div>'
        + hb_meta_html
        + f'<h2 style="margin-top:20px">Progression</h2>'
        + f'<div class="card" style="overflow-x:auto">{prog_table}</div>'
        + (f'<div class="card">{abilities_html}</div>' if abilities_html else "")
        + prog_extras_html
    )
    return render(f'{html.escape(_g(cls_row, "name"))} — Classes', body)


# Item routes are defined in dashboard_items.py and registered at the end of
# this module via dashboard_items.init(...) — keeps this file shorter without
# changing any behaviour.


# ---------------------------------------------------------------------------
# Combat map
# ---------------------------------------------------------------------------

_PLAY_CSS = """
<style>
.container{max-width:1400px}
.pm-host{position:relative;flex:1 1 auto;min-width:0;height:560px;overflow:hidden;
  border:1px solid #4a3828;border-radius:4px;background:#1a1510;cursor:grab;outline:none}
.pm-host.grabbing{cursor:grabbing}
.pm-stage{position:absolute;top:0;left:0;transform-origin:0 0;will-change:transform}
.pm-map{position:absolute;top:0;left:0}
.pm-map svg{display:block}
.pm-overlay{position:absolute;top:0;left:0;pointer-events:none}
.pm-tip{position:fixed;z-index:50;max-width:280px;background:#1c1712;
  border:1px solid #4a3828;border-radius:4px;padding:6px 9px;color:#d4c5a9;
  font-size:.82em;pointer-events:none;box-shadow:0 4px 14px rgba(0,0,0,.5)}
.pm-tip-title{color:#c8a96e;font-weight:bold;margin-bottom:2px}
.zoom-btn{background:#2a2018;border:1px solid #4a3828;color:#c8a96e;padding:2px 9px;
  border-radius:3px;cursor:pointer;font-size:1em;line-height:1.4}
.zoom-btn:hover{background:#3a3020}
.zoom-btn:disabled{opacity:.45;cursor:default}
</style>
"""

_PLAY_JS = (
    'var map = new PlayMap(document.getElementById("areamap"));\n'
    '\n'
    'map.onZoom = function(z) {\n'
    '  var el = document.getElementById("zoom-label");\n'
    '  if (el) el.textContent = Math.round(z*100)+"%";\n'
    '};\n'
    '\n'
    'document.getElementById("zoom-in").addEventListener("click", function(){ map.zoomBy(1.25); });\n'
    'document.getElementById("zoom-out").addEventListener("click", function(){ map.zoomBy(0.8); });\n'
    'document.getElementById("zoom-fit").addEventListener("click", function(){ map.fit(); });\n'
    '\n'
    '// Cached DSL from the last /area-state poll. The "Copy DSL"\n'
    '// button reads from here so the user always gets the most recent\n'
    '// version. (In the player view ?fog=1 the DSL is withheld.)\n'
    'var _currentDsl = null;\n'
    '\n'
    'var copyBtn = document.getElementById("copy-dsl-btn");\n'
    'if (copyBtn) {\n'
    '  copyBtn.addEventListener("click", async function() {\n'
    '    if (!_currentDsl) {\n'
    '      copyBtn.textContent = "No DSL";\n'
    '      setTimeout(function(){ copyBtn.textContent = "Copy DSL"; }, 1500);\n'
    '      return;\n'
    '    }\n'
    '    try {\n'
    '      await navigator.clipboard.writeText(_currentDsl);\n'
    '      copyBtn.textContent = "Copied ✓";\n'
    '    } catch(e) {\n'
    '      // Fallback for browsers/contexts without async clipboard\n'
    '      // (typically http:// without secure context). Use the legacy\n'
    '      // execCommand hack with a hidden textarea.\n'
    '      var ta = document.createElement("textarea");\n'
    '      ta.value = _currentDsl;\n'
    '      ta.style.position = "fixed";\n'
    '      ta.style.opacity = "0";\n'
    '      document.body.appendChild(ta);\n'
    '      ta.focus();\n'
    '      ta.select();\n'
    '      var ok = false;\n'
    '      try { ok = document.execCommand("copy"); } catch(_) {}\n'
    '      document.body.removeChild(ta);\n'
    '      copyBtn.textContent = ok ? "Copied ✓" : "Copy failed";\n'
    '    }\n'
    '    setTimeout(function(){ copyBtn.textContent = "Copy DSL"; }, 1500);\n'
    '  });\n'
    '}\n'
    '\n'
    'async function refresh() {\n'
    '  try {\n'
    '    var r = await fetch("/area-state" + (window.AREA_FOG_QS || ""));\n'
    '    var data = await r.json();\n'
    '    var statusEl = document.getElementById("area-status");\n'
    '    var subEl = document.getElementById("area-sub");\n'
    '    if (!data.active || !data.grid || !data.grid.svg_url) {\n'
    '      statusEl.textContent = "No map loaded for this area";\n'
    '      subEl.textContent = "Call load_map(slug) to load one";\n'
    '      _currentDsl = null;\n'
    '      if (copyBtn) copyBtn.disabled = true;\n'
    '      map.clear();\n'
    '      return;\n'
    '    }\n'
    '    var mapMeta = data.map || {};\n'
    '    statusEl.textContent = mapMeta.name || mapMeta.slug || "Area";\n'
    '    var revealedN = (data.revealed_rooms || []).length;\n'
    '    var totalN = (data.rooms || []).length;\n'
    '    subEl.textContent = totalN ? revealedN + "/" + totalN + " rooms revealed" : "";\n'
    '    _currentDsl = (data.grid && data.grid.dsl) || null;\n'
    '    if (copyBtn) copyBtn.disabled = !_currentDsl;\n'
    '    map.setState(data);\n'
    '    // Recenter on a set_map_focus point — only when it changes, so we\n'
    '    // do not yank the view back while the user is panning around.\n'
    '    if (data.focus) {\n'
    '      var fk = data.focus.x + "," + data.focus.y;\n'
    '      if (window._areaFocusKey !== fk) {\n'
    '        window._areaFocusKey = fk;\n'
    '        map.centerOn(data.focus.x, data.focus.y);\n'
    '      }\n'
    '    } else { window._areaFocusKey = null; }\n'
    '  } catch(e) {\n'
    '    var s = document.getElementById("area-status");\n'
    '    if (s) s.textContent = "Waiting for map…";\n'
    '  }\n'
    '}\n'
    'refresh();\n'
    'setInterval(refresh, 2000);\n'
)


# Drives the /area page: load the dungml play-view widget (served by the
# dungml backend) and mount it against the campaign's currently-loaded map.
# /dungml-play-config supplies the map id + a fresh dungml bearer token; the
# widget owns sessions, party movement and fog-of-war from there on, persisting
# to the dungml server. We re-poll occasionally so that swapping the area map
# (load_map) re-mounts the widget and so the bearer token never goes stale.
_AREA_WIDGET_JS = """
(function () {
  var host = document.getElementById("areamap");
  var statusEl = document.getElementById("dungml-status");
  var token = null;          // latest bearer token; getToken() reads this
  var mountedKey = null;     // "<mapId>:<sessionId>" currently mounted, or null

  function setStatus(msg) { if (statusEl) statusEl.textContent = msg || ""; }

  async function tick() {
    var cfg;
    try {
      cfg = await (await fetch("/dungml-play-config")).json();
    } catch (e) {
      setStatus("dungml backend unreachable — is the dungml service running?");
      return;
    }
    token = cfg.token;
    if (cfg.error) { setStatus("dungml: " + cfg.error); }
    if (!cfg.map_id) {
      setStatus("No map loaded for this area — use the load_map(slug) MCP tool to pick one.");
      if (mountedKey && window.DungmlPlay) { window.DungmlPlay.unmount(host); }
      mountedKey = null;
      return;
    }
    if (!window.DungmlPlay) {
      setStatus("dungml widget failed to load from " + cfg.base_url + "/dungml-play.js");
      return;
    }
    // Only (re)mount when the bound map/session changes — otherwise the
    // periodic token refresh would reset the widget every poll. The widget
    // polls internally for the party's moves once mounted.
    var key = cfg.map_id + ":" + (cfg.session_id || "");
    if (mountedKey !== key) {
      if (!cfg.error) setStatus("");
      mountedKey = key;
      window.DungmlPlay.mount(host, {
        baseUrl: cfg.base_url,
        mapId: cfg.map_id,
        sessionId: cfg.session_id || undefined,
        playerView: true,
        getToken: function () { return token; },
      });
    }
  }

  tick();
  setInterval(tick, 30000);  // pick up load_map changes + refresh the token
})();
"""


def _ensure_campaign_session(map_id: str, ms: dict) -> str:
    """Bind (creating + seeding on first use) the campaign's dungml play
    session for this map. Thin wrapper over ``_dh.ensure_play_session`` — the
    binding lives in ``<campaign>/dungml_sessions.json`` so the widget always
    opens the *campaign's* session (not a stray manual one). Seeded from the
    MCP-side room fog (``revealed_rooms``); corridors and later rooms are added
    in-play via the ``mark_explored`` MCP tool. See [[ttrpg2-area-play-viewer]].
    """
    return _dh.ensure_play_session(
        cfg.get("_dir", Path(".")),
        map_id,
        ms.get("source", ""),
        seed_names=ms.get("revealed_rooms", []),
        session_name=f"{cfg.get('name', 'Campaign')} — party",
    )


@app.route("/dungml-play-config")
def dungml_play_config():
    """Config for the embedded dungml play widget on /area.

    Resolves the active campaign's currently-loaded map (from
    ``map_state.json``, written by the ``load_map`` MCP tool) to a dungml
    map id, mints a fresh dungml bearer token, and binds (creating + seeding
    on first use) the campaign's play session. Returns ``{base_url, map_id,
    map_name, session_id, token, error?}``; ``map_id``/``session_id`` are null
    when no map is loaded or the backend is unreachable.
    """
    base_url = _dh.api_base()
    map_id = map_name = None
    ms: dict = {}
    ms_path = cfg.get("_dir", Path(".")) / "map_state.json"
    if ms_path.exists():
        try:
            ms = json.loads(ms_path.read_text(encoding="utf-8"))
            if ms.get("active"):
                map_id = ms.get("dungml_id")
                map_name = ms.get("name")
        except Exception:
            ms = {}
    try:
        token = _dh._ensure_token()
    except Exception as e:
        # Surface credential/connection problems to the page verbatim rather
        # than failing the request (the widget shows the message inline).
        return jsonify({"base_url": base_url, "map_id": map_id,
                        "map_name": map_name, "session_id": None,
                        "token": None, "error": str(e)})

    session_id = None
    error = None
    if map_id:
        try:
            session_id = _ensure_campaign_session(map_id, ms)
        except Exception as e:
            error = f"could not bind play session: {e}"

    out = {"base_url": base_url, "map_id": map_id, "map_name": map_name,
           "session_id": session_id, "token": token}
    if error:
        out["error"] = error
    return jsonify(out)


@app.route("/area")
def area_view():
    name = html.escape(cfg.get("name", "Campaign"))
    body = (
        '<h1>Area</h1>'
        '<div style="color:#8a7a60;font-size:.85em;margin-bottom:8px">'
        'dungml fog-of-war play view. Pick which map plays here with the '
        '<code>load_map(slug)</code> MCP tool; sessions, party movement and '
        'reveals are managed in the panel below and saved on the dungml server.'
        '</div>'
        '<div id="dungml-status" style="color:#c8a96e;margin-bottom:8px"></div>'
        '<div id="areamap" style="height:calc(100vh - 170px);min-height:480px;'
        'border:1px solid #4a3828;border-radius:4px;background:#0f0d0a;'
        'overflow:hidden"></div>'
        f'<script src="{html.escape(_dh.api_base())}/dungml-play.js"></script>'
        '<script>' + _AREA_WIDGET_JS + '</script>'
    )
    return render(f"Area — {name}", body)


# DM-only dungml feature types — hidden from the player SVG until the party
# triggers/discovers the cell (via spring_trap). Keyed generically by the
# renderer's data-ref, so making another feature type DM-only is a one-line
# change here rather than a special case.
_DM_ONLY_FEATURE_REFS = frozenset({"trap"})

# Door-state marks the renderer draws: door-trap (an X) and door-lock (a dot).
# Both leak DM tactical detail, so the player SVG always strips them.
_DM_DOOR_MARK_CLASSES = ("door-trap", "door-lock")

_FEATURE_INSTANCE_RX = re.compile(
    r'<g class="feature-instance"[^>]*\bdata-ref="(?P<ref>[^"]*)"[^>]*'
    r'\btransform="translate\((?P<x>-?[\d.]+),\s*(?P<y>-?[\d.]+)\)[^"]*"[^>]*>'
    r'.*?</g>',
    re.DOTALL,
)


_DM_NOTES_RX = re.compile(r'\s+data-dm-notes="[^"]*"')
_ROOM_TAG_RX = re.compile(r'<[a-zA-Z]+\b[^>]*\bdata-room="(?P<room>[^"]*)"[^>]*>')
_ROOM_TOOLTIP_ATTRS_RX = re.compile(r'\s+data-(?:label|description)="[^"]*"')


def _player_sanitize_svg(svg: str, revealed_traps: set, revealed_rooms: set) -> str:
    """Return a player-safe copy of a rendered dungml SVG.

    Strips DM-only detail the party has not earned sight of:
      - trapped/locked door marks (always);
      - DM-only feature instances (floor traps, ...) whose cell has not
        been revealed via ``spring_trap``;
      - DM notes (``data-dm-notes``) everywhere — never shown to players;
      - the label/description of any room not in ``revealed_rooms``, so a
        fogged (blacked-out) room leaks no readable contents via the tooltip
        or the DOM. Geometry is left intact; the client paints fog over it.

    Secret doors already render identically to closed doors, and hidden
    layers are excluded by the renderer, so no further masking is needed.
    """
    for cls in _DM_DOOR_MARK_CLASSES:
        svg = re.sub(
            rf'<(?:line|circle|path|rect|polygon)\b[^>]*\bclass="{cls}"[^>]*/>',
            "", svg,
        )

    def _keep(m: "re.Match") -> str:
        if m.group("ref") not in _DM_ONLY_FEATURE_REFS:
            return m.group(0)  # not DM-only — leave untouched
        cell = (round(float(m.group("x"))), round(float(m.group("y"))))
        return m.group(0) if cell in revealed_traps else ""

    svg = _FEATURE_INSTANCE_RX.sub(_keep, svg)

    # DM notes are never for players, revealed room or not.
    svg = _DM_NOTES_RX.sub("", svg)

    # Drop the readable label/description on any unrevealed room's elements.
    def _strip_room(m: "re.Match") -> str:
        tag = m.group(0)
        if m.group("room") in revealed_rooms:
            return tag
        return _ROOM_TOOLTIP_ATTRS_RX.sub("", tag)

    return _ROOM_TAG_RX.sub(_strip_room, svg)


def _revealed_trap_cells(ms: dict | None) -> set:
    """Set of (x, y) cells the party has sprung/seen, from map_state."""
    if not ms:
        return set()
    out = set()
    for c in ms.get("revealed_traps", []):
        if isinstance(c, (list, tuple)) and len(c) == 2:
            try:
                out.add((int(c[0]), int(c[1])))
            except (TypeError, ValueError):
                continue
    return out


@app.route("/area-state")
def area_state_endpoint():
    """Explore-map state for the /area page, from ``map_state.json``.

    The persistent area map (dungeon level, gatehouse, inn …) lives in
    ``map_state.json``: the background SVG, room geometry, the revealed-rooms
    set, and the party position. /area is a pure exploration view — combat is
    no longer overlaid here (it will move into dungml).

      - map active     → the explore payload (grid + rooms + fog + party)
      - otherwise      → {active: False}
    """
    camp_dir = cfg.get("_dir", Path("."))

    def _load(path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    ms = _load(camp_dir / "map_state.json")

    if not (ms and ms.get("active")):
        return jsonify({"active": False})

    out = {
        "active": True,
        "grid": {
            "dsl":     ms.get("source", ""),
            "svg_url": ms.get("svg_url"),
            "width":   ms.get("width"),
            "height":  ms.get("height"),
        },
        "map": {
            "slug":  ms.get("slug"),
            "name":  ms.get("name"),
            "scale": ms.get("scale"),
        },
        "rooms": ms.get("rooms", []),
        "revealed_rooms": ms.get("revealed_rooms", []),
        "revealed_traps": ms.get("revealed_traps", []),
    }
    if ms.get("focus"):
        out["focus"] = ms["focus"]
    if ms.get("party"):
        out["party"] = ms["party"]

    # Player view: strip DM-only tactical detail from the served SVG and
    # withhold the raw DSL (which would otherwise spell out every trap/door).
    if _fog_on():
        revealed = _revealed_trap_cells(ms)
        grid = out.get("grid")
        if isinstance(grid, dict):
            prefix = "data:image/svg+xml;base64,"
            su = grid.get("svg_url") or ""
            if su.startswith(prefix):
                try:
                    raw = base64.b64decode(su[len(prefix):]).decode("utf-8")
                    safe = _player_sanitize_svg(raw, revealed)
                    grid["svg_url"] = prefix + base64.b64encode(
                        safe.encode("utf-8")
                    ).decode("ascii")
                except Exception:
                    pass  # on any decode/parse hiccup, fall back to unmodified
            grid["dsl"] = ""

    return jsonify(out)


# ---------------------------------------------------------------------------
# Ability scores reference — numeric mechanical tables (bend bars %, system
# shock %, missile attack adjustment, etc.) sourced from the AD&D 2e ability
# tables. Tables live in 2e.db (ability_columns, ability_scores, ability_notes).
# ---------------------------------------------------------------------------
_ABILITY_ORDER = ["strength", "dexterity", "constitution",
                  "intelligence", "wisdom", "charisma"]


def _ability_load(ability: str | None = None) -> dict:
    """Return {ability: {note, columns, rows}} for one ability or all six."""
    if not _2E_DB.exists():
        return {}
    conn = _db(_2E_DB)
    targets = [ability] if ability else _ability_load_all_targets()
    out: dict = {}
    for a in targets:
        note_row = conn.execute(
            "SELECT headline, xp_bonus, extra FROM ability_notes WHERE ability=?",
            (a,),
        ).fetchone()
        if note_row is None:
            continue
        col_rows = conn.execute(
            "SELECT name, short_name, note FROM ability_columns "
            "WHERE ability=? ORDER BY sort_order", (a,),
        ).fetchall()
        score_rows = conn.execute(
            "SELECT score, data FROM ability_scores "
            "WHERE ability=? ORDER BY sort_order", (a,),
        ).fetchall()
        out[a] = {
            "headline":  note_row["headline"],
            "xp_bonus":  note_row["xp_bonus"],
            "extra":     note_row["extra"],
            "columns":   [(r["name"], r["short_name"], r["note"]) for r in col_rows],
            "rows":      [(r["score"], json.loads(r["data"])) for r in score_rows],
        }
    return out


def _ability_load_all_targets() -> list[str]:
    return list(_ABILITY_ORDER)


def _render_ability_section(ability: str, payload: dict) -> str:
    cols = payload["columns"]
    header_cells = (
        '<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #4a3828">Score</th>'
        + "".join(
            f'<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #4a3828" '
            f'title="{html.escape(note or "")}">{html.escape(name)}</th>'
            for name, _short, note in cols
        )
    )
    body_rows = []
    for score, data in payload["rows"]:
        cells = (
            f'<td style="padding:3px 10px;border-bottom:1px solid #2a2018;'
            f'white-space:nowrap;font-variant-numeric:tabular-nums">'
            f'<strong>{html.escape(score)}</strong></td>'
        )
        for _name, short, _note in cols:
            v = data.get(short, "")
            cells += (
                f'<td style="padding:3px 10px;border-bottom:1px solid #2a2018;'
                f'font-variant-numeric:tabular-nums">{html.escape(str(v))}</td>'
            )
        body_rows.append(f'<tr>{cells}</tr>')

    legend = '<ul class="muted" style="margin:6px 0 0;padding-left:18px;font-size:.88em">'
    for name, _short, note in cols:
        if note:
            legend += (
                f'<li><strong>{html.escape(name)}:</strong> {html.escape(note)}</li>'
            )
    legend += '</ul>'

    xp = (f'<p class="muted" style="margin:6px 0 0;font-size:.9em">'
          f'{html.escape(payload["xp_bonus"])}</p>') if payload["xp_bonus"] else ""
    extra = (f'<p class="muted" style="margin:6px 0 0;font-size:.9em">'
             f'{html.escape(payload["extra"])}</p>') if payload["extra"] else ""

    return (
        f'<section id="{ability}" style="margin:24px 0">'
        f'<h2 style="margin:0 0 4px;text-transform:capitalize">{ability}</h2>'
        f'<p style="margin:0 0 6px">{html.escape(payload["headline"])}</p>'
        f'{xp}{extra}'
        f'<div style="overflow-x:auto;margin-top:10px">'
        f'<table style="border-collapse:collapse;font-size:.92em;min-width:100%">'
        f'<thead><tr>{header_cells}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table></div>'
        f'{legend}</section>'
    )


@app.route("/abilities")
@app.route("/abilities/<ability>")
def abilities_view(ability: str | None = None):
    if ability:
        ability = ability.lower()
        if ability not in _ABILITY_ORDER:
            abort(404)
    data = _ability_load(ability)
    if not data:
        return render(
            "Ability Scores",
            "<h1>Ability Scores</h1><p class='muted'>2e.db not found or empty. "
            "Run <code>tools/build_phb_ref.py</code>.</p>",
        )

    if ability:
        body = (
            f'<p style="margin:0 0 8px"><a href="/abilities">&larr; All abilities</a></p>'
            f'<h1 style="margin:0 0 12px;text-transform:capitalize">{html.escape(ability)}</h1>'
            + _render_ability_section(ability, data[ability])
        )
        return render(f"{ability.capitalize()} — Ability Scores", body)

    toc = ' · '.join(
        f'<a href="#{a}" style="text-transform:capitalize">{a}</a>'
        for a in _ABILITY_ORDER if a in data
    )
    sections = "".join(
        _render_ability_section(a, data[a])
        for a in _ABILITY_ORDER if a in data
    )
    body = (
        '<h1>Ability Scores</h1>'
        '<p class="muted">Numeric derived attributes for each ability score, '
        '0–25. Hover a column header for its mechanical meaning.</p>'
        f'<p style="margin:8px 0 16px">Jump to: {toc}</p>'
        + sections
    )
    return render("Ability Scores", body)


# ---------------------------------------------------------------------------
# Proficiencies reference — Table 37 (nonweapon proficiency groups) plus the
# class-to-group crossover from Table 38. Data lives in 2e.db
# (proficiencies, proficiency_class_crossover).
# ---------------------------------------------------------------------------
_PROF_GROUPS = ["general", "priest", "rogue", "warrior", "wizard"]


def _proficiency_rows(group: str | None = None) -> list[sqlite3.Row]:
    if not _2E_DB.exists():
        return []
    conn = _db(_2E_DB)
    if group:
        return list(conn.execute(
            "SELECT * FROM proficiencies WHERE group_name=? "
            "ORDER BY name COLLATE NOCASE", (group,),
        ).fetchall())
    return list(conn.execute(
        "SELECT * FROM proficiencies "
        "ORDER BY group_name, name COLLATE NOCASE",
    ).fetchall())


# PHB Tables 34 (nonweapon) and 35 (weapon): proficiency slots progress by
# class GROUP (warrior/wizard/priest/rogue), not by individual class.
# initial = slots at level 1; every = gain +1 slot every N levels.
_SLOT_PROGRESSION: dict[str, dict[str, dict[str, int]]] = {
    "warrior": {"nwp": {"initial": 3, "every": 3}, "weapon": {"initial": 4, "every": 3}},
    "wizard":  {"nwp": {"initial": 4, "every": 3}, "weapon": {"initial": 1, "every": 6}},
    "priest":  {"nwp": {"initial": 4, "every": 3}, "weapon": {"initial": 2, "every": 4}},
    "rogue":   {"nwp": {"initial": 3, "every": 4}, "weapon": {"initial": 2, "every": 4}},
}

# Which class group each class belongs to (primary group, per PHB). A
# class spends NWP slots in its own group plus any crossover groups (see
# proficiency_class_crossover), but its slot *count* always tracks the
# primary group's progression.
_CLASS_PRIMARY_GROUP: dict[str, str] = {
    "Bard": "rogue",
    "Cleric": "priest",
    "Druid": "priest",
    "Fighter": "warrior",
    "Illusionist": "wizard",
    "Mage": "wizard",
    "Paladin": "warrior",
    "Ranger": "warrior",
    "Specialist Mage": "wizard",
    "Thief": "rogue",
}

# Levels to surface in the per-group progression table. Includes every
# milestone level for at least one group so no gain is invisible.
_SLOT_PROGRESSION_LEVELS = [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 20]


def _slot_count(initial: int, every: int, level: int) -> int:
    """PHB formula: +1 slot every ``every`` levels past 1st."""
    return initial + (level // every)


def _render_slot_progression_table() -> str:
    """8-row matrix (4 groups × NWP/Weapon) across selected levels, with
    each group's member classes listed beside the group label."""
    members: dict[str, list[str]] = {g: [] for g in _SLOT_PROGRESSION}
    for cls, group in sorted(_CLASS_PRIMARY_GROUP.items()):
        members[group].append(cls)

    head = (
        '<tr>'
        '<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #4a3828">Group</th>'
        '<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #4a3828">Type</th>'
        + "".join(
            f'<th style="text-align:center;padding:4px 8px;border-bottom:1px solid #4a3828">L{L}</th>'
            for L in _SLOT_PROGRESSION_LEVELS
        )
        + '<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #4a3828">Cadence</th>'
        + '</tr>'
    )

    body_rows = []
    for group, prog in _SLOT_PROGRESSION.items():
        member_html = (
            f'<div style="text-transform:capitalize;font-weight:600">{html.escape(group)}</div>'
            f'<div class="muted" style="font-size:.85em;margin-top:2px">'
            f'{html.escape(", ".join(members[group]) or "—")}</div>'
        )
        for type_key, type_label in (("nwp", "Nonweapon"), ("weapon", "Weapon")):
            cfg = prog[type_key]
            cells = "".join(
                f'<td style="text-align:center;padding:4px 8px;border-bottom:1px solid #2a2018;'
                f'font-variant-numeric:tabular-nums">'
                f'{_slot_count(cfg["initial"], cfg["every"], L)}</td>'
                for L in _SLOT_PROGRESSION_LEVELS
            )
            cadence = f'{cfg["initial"]} initial, +1 every {cfg["every"]} levels'
            # Only print the group label/members on the first row of each
            # group so the matrix reads as two grouped lines per cluster.
            group_cell = (
                f'<td rowspan="2" style="padding:6px 10px;border-bottom:1px solid #2a2018;'
                f'vertical-align:top">{member_html}</td>'
                if type_key == "nwp" else ""
            )
            body_rows.append(
                f'<tr>{group_cell}'
                f'<td style="padding:4px 10px;border-bottom:1px solid #2a2018">{type_label}</td>'
                f'{cells}'
                f'<td class="muted" style="padding:4px 10px;border-bottom:1px solid #2a2018;'
                f'font-size:.88em">{cadence}</td>'
                f'</tr>'
            )

    return (
        '<section id="slots" style="margin:18px 0">'
        '<h2 style="margin:0 0 8px">Slots by class &amp; level</h2>'
        '<p class="muted" style="margin:0 0 8px;font-size:.9em">'
        'Slot progression follows the class\'s primary group (PHB Tables 34 &amp; 35). '
        'NWP slots can be spent across the class\'s allowed groups (see Class crossover below); '
        'weapon slots feed weapon proficiency picks.'
        '</p>'
        '<div style="overflow-x:auto">'
        '<table style="border-collapse:collapse;font-size:.93em;min-width:720px">'
        f'<thead>{head}</thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table></div>'
        '</section>'
    )


def _proficiency_class_crossover() -> list[tuple[str, list[str]]]:
    if not _2E_DB.exists():
        return []
    conn = _db(_2E_DB)
    rows = conn.execute(
        "SELECT class_name, groups FROM proficiency_class_crossover "
        "ORDER BY class_name",
    ).fetchall()
    return [(r["class_name"], json.loads(r["groups"])) for r in rows]


def _fmt_modifier(m: int | None) -> str:
    if m is None:
        return "—"
    return f"+{m}" if m > 0 else str(m)


def _render_proficiency_table(group: str, rows: list[sqlite3.Row]) -> str:
    body_rows = []
    for r in rows:
        body_rows.append(
            '<tr>'
            f'<td style="padding:4px 10px;border-bottom:1px solid #2a2018">'
            f'{html.escape(r["name"])}</td>'
            f'<td style="padding:4px 10px;border-bottom:1px solid #2a2018;'
            f'text-align:center;font-variant-numeric:tabular-nums">'
            f'{r["slots"]}</td>'
            f'<td style="padding:4px 10px;border-bottom:1px solid #2a2018">'
            f'{html.escape(r["ability"] or "—")}</td>'
            f'<td style="padding:4px 10px;border-bottom:1px solid #2a2018;'
            f'text-align:center;font-variant-numeric:tabular-nums">'
            f'{html.escape(_fmt_modifier(r["check_modifier"]))}</td>'
            '</tr>'
        )
    header = (
        '<tr>'
        '<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #4a3828">Proficiency</th>'
        '<th style="text-align:center;padding:4px 10px;border-bottom:1px solid #4a3828">Slots</th>'
        '<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #4a3828">Relevant Ability</th>'
        '<th style="text-align:center;padding:4px 10px;border-bottom:1px solid #4a3828">Check Modifier</th>'
        '</tr>'
    )
    return (
        f'<section id="{group}" style="margin:20px 0">'
        f'<h2 style="margin:0 0 8px;text-transform:capitalize">{group}</h2>'
        f'<div style="overflow-x:auto">'
        f'<table style="border-collapse:collapse;font-size:.93em;min-width:520px">'
        f'<thead>{header}</thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table></div>'
        f'</section>'
    )


def _render_crossover_table(rows: list[tuple[str, list[str]]]) -> str:
    body_rows = []
    for cls, groups in rows:
        body_rows.append(
            '<tr>'
            f'<td style="padding:4px 10px;border-bottom:1px solid #2a2018">'
            f'<strong>{html.escape(cls)}</strong></td>'
            f'<td style="padding:4px 10px;border-bottom:1px solid #2a2018">'
            + ", ".join(
                f'<a href="#{html.escape(g)}" style="text-transform:capitalize">'
                f'{html.escape(g)}</a>' for g in groups
            )
            + '</td></tr>'
        )
    return (
        '<section style="margin:18px 0">'
        '<h2 style="margin:0 0 8px">Class crossover</h2>'
        '<p class="muted" style="margin:0 0 8px;font-size:.9em">'
        'Which proficiency groups each class can spend nonweapon slots from. '
        'A class may always pick from its own groups plus General.'
        '</p>'
        '<div style="overflow-x:auto">'
        '<table style="border-collapse:collapse;font-size:.93em;min-width:420px">'
        '<thead><tr>'
        '<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #4a3828">Class</th>'
        '<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #4a3828">Proficiency Groups</th>'
        '</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table></div>'
        '</section>'
    )


@app.route("/proficiencies")
@app.route("/proficiencies/<group>")
def proficiencies_view(group: str | None = None):
    if group:
        group = group.lower()
        if group not in _PROF_GROUPS:
            abort(404)
    if not _2E_DB.exists():
        return render(
            "Proficiencies",
            "<h1>Proficiencies</h1><p class='muted'>2e.db not found. "
            "Run <code>tools/build_phb_ref.py</code>.</p>",
        )

    if group:
        rows = _proficiency_rows(group)
        body = (
            f'<p style="margin:0 0 8px"><a href="/proficiencies">&larr; All groups</a></p>'
            f'<h1 style="margin:0 0 12px;text-transform:capitalize">'
            f'{html.escape(group)} Proficiencies</h1>'
            + _render_proficiency_table(group, rows)
        )
        return render(f"{group.capitalize()} Proficiencies", body)

    crossover = _proficiency_class_crossover()
    group_links = ' · '.join(
        f'<a href="#{g}" style="text-transform:capitalize">{g}</a>'
        for g in _PROF_GROUPS
    )
    toc = f'<a href="#slots">Slots by class</a> · {group_links}'
    sections = "".join(
        _render_proficiency_table(g, _proficiency_rows(g))
        for g in _PROF_GROUPS
    )
    body = (
        '<h1>Nonweapon Proficiencies</h1>'
        '<p class="muted">Slots required, relevant ability, and check modifier '
        'per proficiency. To make a proficiency check, roll d20 ≤ '
        '(ability score + check modifier).</p>'
        f'<p style="margin:8px 0 16px">Jump to: {toc}</p>'
        + _render_slot_progression_table()
        + (_render_crossover_table(crossover) if crossover else "")
        + sections
    )
    return render("Proficiencies", body)


# ---------------------------------------------------------------------------
# Turning Undead reference — DMG Table 47 cross-indexed by undead type vs
# priest level. Paladins use a column two levels lower than their actual level.
# ---------------------------------------------------------------------------
_TURNING_LEVEL_COLUMNS = [
    "1", "2", "3", "4", "5", "6", "7", "8", "9",
    "10-11", "12-13", "14+",
]


def _turning_cell_style(v: str) -> str:
    if v == "T":
        return "background:#1f3a1f;color:#9adb9a;font-weight:bold"
    if v.startswith("D"):
        return "background:#3a1f1f;color:#e08a8a;font-weight:bold"
    if v == "—":
        return "color:#5a4838"
    return "color:#d4c5a9;font-variant-numeric:tabular-nums"


@app.route("/turning")
def turning_view():
    if not _2E_DB.exists():
        return render(
            "Turning Undead",
            "<h1>Turning Undead</h1><p class='muted'>2e.db not found. "
            "Run <code>tools/build_phb_ref.py</code>.</p>",
        )

    conn = _db(_2E_DB)
    rows = list(conn.execute(
        "SELECT undead_type, results FROM turning_undead ORDER BY sort_order"
    ).fetchall())
    if not rows:
        return render(
            "Turning Undead",
            "<h1>Turning Undead</h1><p class='muted'>turning_undead table is "
            "empty. Run <code>tools/build_phb_ref.py</code>.</p>",
        )

    # Header
    header = (
        '<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #4a3828;'
        'border-right:1px solid #4a3828">Undead type / HD</th>'
        + "".join(
            f'<th style="text-align:center;padding:4px 8px;border-bottom:1px solid #4a3828">'
            f'{html.escape(c)}</th>'
            for c in _TURNING_LEVEL_COLUMNS
        )
    )

    body_rows = []
    for r in rows:
        cells = json.loads(r["results"])
        tds = (
            f'<td style="padding:4px 10px;border-bottom:1px solid #2a2018;'
            f'border-right:1px solid #2a2018;white-space:nowrap">'
            f'<strong>{html.escape(r["undead_type"])}</strong></td>'
        )
        for v in cells:
            style = _turning_cell_style(v)
            tds += (
                f'<td style="padding:4px 8px;border-bottom:1px solid #2a2018;'
                f'text-align:center;{style}">{html.escape(v)}</td>'
            )
        body_rows.append(f'<tr>{tds}</tr>')

    legend = (
        '<ul class="muted" style="margin:10px 0 0;padding-left:18px;font-size:.9em">'
        '<li><strong>Number</strong>: roll 1d20; result ≥ this turns the undead.</li>'
        '<li><strong>T</strong>: automatically turned, no roll needed.</li>'
        '<li><strong>D</strong>: turning destroys the undead outright.</li>'
        '<li><strong>D*</strong>: as D, plus an additional 2d4 creatures of that type are turned.</li>'
        '<li><strong>—</strong>: priest of that level cannot turn this type.</li>'
        '</ul>'
    )

    notes = (
        '<p class="muted" style="margin:10px 0 0;font-size:.9em">'
        'A successful turn or dispel affects 2d6 undead. Mixed groups: '
        'lowest HD turned first. Paladins use the priest column two lower '
        'than their actual level (a 5th-level paladin reads column 3). '
        'Druids cannot turn undead.'
        '</p>'
    )

    body = (
        '<h1>Turning Undead</h1>'
        '<p class="muted">DMG Table 47. Cross-index the undead\'s type (or HD) '
        'with the priest\'s level to find the d20 target.</p>'
        '<div style="overflow-x:auto">'
        '<table style="border-collapse:collapse;font-size:.93em;margin-top:8px">'
        f'<thead><tr>{header}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table></div>'
        + legend + notes
    )
    return render("Turning Undead", body)


# ---------------------------------------------------------------------------
# Reference index — hub for the lookup endpoints (monsters, spells, classes,
# items). Folded into one nav slot so the header doesn't have four near-
# identical reference links.
# ---------------------------------------------------------------------------
_REFERENCE_SECTIONS = [
    ("/monsters", "Monsters",
     "Monstrous Manual entries with stats, treasure types, morale, and XP values."),
    ("/spells", "Spells",
     "Wizard and priest spell catalogue: casting time, range, AOE, components, and full text."),
    ("/classes", "Classes",
     "Per-class progression: THAC0, saves, spell slots, XP requirements, and homebrew kits."),
    ("/items", "Items",
     "Equipment and magic items: cost, weight, weapon stats, armour values, and rarity."),
    ("/abilities", "Ability Scores",
     "Derived attributes per ability score: bend bars %, system shock, missile adjustments, bonus spells, henchmen caps."),
    ("/proficiencies", "Proficiencies",
     "Nonweapon proficiencies by group: slots required, relevant ability, check modifier, and class crossover."),
    ("/turning", "Turning Undead",
     "DMG Table 47: d20 target cross-indexed by undead type/HD and priest level. Paladins use level-2."),
    ("/greyhawk", "Greyhawk Setting",
     "Greyhawk wiki: deities, realms, characters, settlements, and full-text search."),
    ("/reference/facts", "Lore Facts",
     "The “did you know” trivia pool shown on /play: browse by category, add and delete entries."),
]


@app.route("/reference")
def reference_index():
    """Single hub linking to each lookup browser."""
    cards = []
    for href, title, blurb in _REFERENCE_SECTIONS:
        cards.append(
            f'<a class="card" href="{href}" '
            f'style="display:block;text-decoration:none;color:inherit">'
            f'<h2>{html.escape(title)}</h2>'
            f'<p class="muted" style="margin-bottom:0">{html.escape(blurb)}</p>'
            f'</a>'
        )
    body = (
        '<h1>Reference</h1>'
        '<p class="muted">Lookup browsers for the AD&amp;D 2e rules data.</p>'
        '<div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(280px,1fr))">'
        + ''.join(cards) +
        '</div>'
    )
    return render("Reference", body)


@app.route("/reference/facts")
def reference_facts():
    """Manage the 'did you know' lore-fact pool: browse by category, add new
    entries, delete existing ones. Add/delete go through /api/facts."""
    facts = _facts.list_facts()                      # all, ordered by id
    cat_names = sorted({f["category"] for f in facts if f["category"]})

    # Group by category; order sections by size (largest first), then name.
    groups: dict[str, list] = {}
    for f in facts:
        groups.setdefault(f["category"] or "(uncategorized)", []).append(f)
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    def esc(s):
        return html.escape("" if s is None else str(s))

    # Quick category jump-nav with counts.
    nav_chips = " ".join(
        f'<a class="tag" href="#cat-{esc(re.sub(r"[^a-z0-9]+", "-", cat.lower()))}">'
        f'{esc(cat)} <span class="muted">{len(items)}</span></a>'
        for cat, items in ordered
    )

    sections = []
    for cat, items in ordered:
        anchor = re.sub(r"[^a-z0-9]+", "-", cat.lower())
        rows = []
        for f in items:
            camp = (f.get("campaign") or "").strip()
            camp_chip = f' <span class="tag">{esc(camp)}</span>' if camp else ""
            disabled = "" if f.get("enabled", True) else ' <span class="muted">(hidden)</span>'
            rows.append(
                '<tr>'
                f'<td>{esc(f["text"])}{camp_chip}{disabled}</td>'
                f'<td class="fact-ts">{esc(f.get("created_at") or "")}</td>'
                f'<td class="fact-act"><button class="fact-del" type="button" '
                f'data-id="{f["id"]}" title="Delete entry" aria-label="Delete entry">✕</button></td>'
                '</tr>'
            )
        sections.append(
            f'<h2 id="cat-{esc(anchor)}">{esc(cat)} '
            f'<span class="muted" style="font-size:0.7em">{len(items)} entries</span></h2>'
            '<table class="fact-table"><thead><tr>'
            '<th>Fact</th><th>Added</th><th></th></tr></thead><tbody>'
            + "".join(rows) +
            '</tbody></table>'
        )

    # Category dropdown: existing categories unioned with the canonical set
    # (so unused-but-expected ones are always offered), plus a "+ new" escape
    # hatch that reveals a free-text field.
    _CANON_CATS = ["greyhawk", "monster", "rules", "spell", "magic",
                   "history", "planes", "class", "campaign"]
    cat_options = sorted(set(cat_names) | set(_CANON_CATS))
    cat_select_options = (
        '<option value="" selected>— category —</option>'
        + "".join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in cat_options)
        + '<option value="__new__">+ new category…</option>'
    )

    body = f"""
<style>
  .fact-add {{ margin: 14px 0 26px; }}
  .fact-add textarea {{
    width: 100%; box-sizing: border-box;
    background: var(--bg-rec); color: var(--ink-body);
    border: 1px solid var(--rule); border-radius: 3px;
    padding: 10px 12px; font-family: var(--font-body); font-size: 1em;
    resize: vertical; min-height: 60px; line-height: 1.5;
  }}
  .fact-add textarea:focus {{ outline: none; border-color: var(--accent-gold); }}
  .fact-add .row {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; align-items: center; }}
  .fact-add input {{
    background: var(--bg-rec); color: var(--ink-body);
    border: 1px solid var(--rule); border-radius: 3px;
    padding: 7px 10px; font-family: var(--font-body); font-size: 0.95em;
  }}
  .fact-add input:focus {{ outline: none; border-color: var(--accent-gold); }}
  .fact-add select.cat {{
    background: var(--bg-rec); color: var(--ink-body);
    border: 1px solid var(--rule); border-radius: 3px;
    padding: 7px 10px; font-family: var(--font-body); font-size: 0.95em;
    cursor: pointer;
  }}
  .fact-add select.cat:focus {{ outline: none; border-color: var(--accent-gold); }}
  .fact-add input.cat {{ width: 14ch; }}
  .fact-add input.camp {{ width: 18ch; }}
  .fact-add button {{
    background: linear-gradient(to bottom, #4a3625, #2e2014);
    color: var(--ink-display); border: 1px solid var(--rule-hi);
    border-radius: 3px; padding: 7px 18px; cursor: pointer;
    font-family: var(--font-display); font-size: 0.74em;
    text-transform: uppercase; letter-spacing: 0.14em;
    transition: all 160ms ease;
  }}
  .fact-add button:hover {{ border-color: var(--accent-gold); color: var(--accent-gold-hi); }}
  .fact-add button:disabled {{ opacity: 0.5; cursor: progress; }}
  #fact-add-status {{ margin-top: 8px; min-height: 1.2em; }}
  .cat-nav {{ margin: 10px 0 22px; line-height: 2; }}
  a.tag {{ text-decoration: none; }}
  table.fact-table td {{ vertical-align: top; }}
  table.fact-table .fact-ts {{
    font-family: var(--font-mono); font-size: 0.8em; color: var(--ink-muted);
    white-space: nowrap; width: 1%;
  }}
  table.fact-table .fact-act {{ width: 1%; text-align: center; }}
  .fact-del {{
    background: transparent; border: 1px solid var(--rule);
    color: var(--ink-muted); border-radius: 3px; cursor: pointer;
    width: 26px; height: 24px; line-height: 1; padding: 0;
    transition: all 150ms ease;
  }}
  .fact-del:hover {{ border-color: var(--accent-blood, #a33); color: #e88; }}
  .fact-del:disabled {{ opacity: 0.4; cursor: progress; }}
</style>

<h1>Lore Facts</h1>
<p class="muted">The “did you know” trivia pool shown on <a href="/play">/play</a> while the DM
composes a reply. {len(facts)} entries across {len(ordered)} categories.</p>

<div class="card fact-add">
  <h2 style="margin-top:0">Add an entry</h2>
  <form id="fact-add-form">
    <textarea id="fact-text" maxlength="600" placeholder="A short, interesting fact (one or two sentences)…"></textarea>
    <div class="row">
      <select id="fact-cat" class="cat">{cat_select_options}</select>
      <input id="fact-cat-new" class="cat" type="text" placeholder="new category" autocomplete="off" hidden>
      <input id="fact-camp" class="camp" type="text" placeholder="campaign (optional)" autocomplete="off">
      <input id="fact-source" class="camp" type="text" placeholder="source (optional)" autocomplete="off">
      <button type="submit" id="fact-add-btn">Add fact</button>
    </div>
  </form>
  <div id="fact-add-status" class="muted"></div>
</div>

<div class="cat-nav">{nav_chips}</div>

{"".join(sections) if sections else '<p class="muted">No entries yet.</p>'}

<script>
(function(){{
  const form = document.getElementById('fact-add-form');
  const text = document.getElementById('fact-text');
  const cat = document.getElementById('fact-cat');
  const catNew = document.getElementById('fact-cat-new');
  const camp = document.getElementById('fact-camp');
  const source = document.getElementById('fact-source');
  const btn = document.getElementById('fact-add-btn');
  const status = document.getElementById('fact-add-status');

  // "+ new category…" reveals a free-text field for an unlisted category.
  cat.addEventListener('change', function(){{
    const isNew = cat.value === '__new__';
    catNew.hidden = !isNew;
    if (isNew) catNew.focus();
  }});
  function chosenCategory(){{
    return cat.value === '__new__' ? catNew.value.trim() : cat.value.trim();
  }}

  form.addEventListener('submit', async function(e){{
    e.preventDefault();
    const t = (text.value || '').trim();
    if (!t) {{ text.focus(); return; }}
    btn.disabled = true; status.textContent = 'Adding…';
    try {{
      const r = await fetch('/api/facts', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{text: t, category: chosenCategory(),
                              campaign: camp.value.trim(), source: source.value.trim()}})
      }});
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
      location.reload();
    }} catch (err) {{
      status.textContent = 'Failed: ' + err.message;
      btn.disabled = false;
    }}
  }});

  document.querySelectorAll('.fact-del').forEach(function(b){{
    b.addEventListener('click', async function(){{
      if (!confirm('Delete this entry?')) return;
      b.disabled = true;
      try {{
        const r = await fetch('/api/facts/' + b.dataset.id, {{method: 'DELETE'}});
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
        const tr = b.closest('tr'); if (tr) tr.remove();
      }} catch (err) {{
        alert('Delete failed: ' + err.message);
        b.disabled = false;
      }}
    }});
  }});
}})();
</script>
"""
    return render("Lore Facts", body)


# ---------------------------------------------------------------------------
# Greyhawk setting browser — reads settings/greyhawk/greyhawk.db (the same
# DB the MCP `greyhawk_*` tools wrap). The DB ships read-only, so a cached
# connection via `_db()` is safe.
# ---------------------------------------------------------------------------
from tools import greyhawk as _greyhawk


# MediaWiki File: refs use spaces or underscores interchangeably; on disk
# the images use underscores. We also accept an exact filename match as a
# fallback in case future imports preserve the original spacing.
def _greyhawk_image_path(name: str) -> Path | None:
    """Resolve a `[[File:...]]` reference to an on-disk path, or None."""
    if not name:
        return None
    # Strip any leading File:/Image: prefix and surrounding whitespace.
    name = re.sub(r"^\s*(?:File|Image):\s*", "", name, flags=re.IGNORECASE)
    name = name.strip().lstrip("/")
    if not name or "/" in name or "\\" in name or name.startswith(".."):
        return None
    # Try the literal name and the spaces->underscores variant.
    for cand in (name, name.replace(" ", "_")):
        p = _GREYHAWK_IMG / cand
        try:
            # resolve() to defeat any sneaky relative-path escapes.
            rp = p.resolve()
        except OSError:
            continue
        try:
            rp.relative_to(_GREYHAWK_IMG.resolve())
        except ValueError:
            continue
        if rp.is_file():
            return rp
    return None


def _greyhawk_iter_file_refs(text: str):
    """Yield (start, end, inner) for each top-level `[[File:...]]` span.

    MediaWiki File refs can nest wiki links inside their caption — e.g.
    `[[File:Foo.jpg|thumb|see also [[Bar]]]]`. A flat regex over
    `[[...]]` would mis-pair the brackets, so we walk the string with a
    depth counter and yield only outermost spans.
    """
    i, n = 0, len(text)
    while i < n:
        if text.startswith("[[", i) and re.match(
            r"\[\[(File|Image):", text[i:i+10], flags=re.IGNORECASE
        ):
            depth = 0
            j = i
            while j < n:
                if text.startswith("[[", j):
                    depth += 1
                    j += 2
                elif text.startswith("]]", j):
                    depth -= 1
                    j += 2
                    if depth == 0:
                        yield (i, j, text[i+2:j-2])
                        i = j
                        break
                else:
                    j += 1
            else:
                # Unbalanced; bail out so we don't loop forever.
                return
        else:
            i += 1


_FILE_ALIGN = {"left", "right", "center", "none"}
_FILE_FRAME = {"thumb", "thumbnail", "frame", "frameless", "border"}


def _greyhawk_render_file_ref(inner: str) -> str:
    """Render the contents between the outer `[[` and `]]` of a File ref.

    `inner` is e.g. `File:Acererak DMG5e 2014.png|thumb|right|235px|A caption`.
    We escape attribute values; the caption is left raw so the downstream
    `[[link]]` regex still gets a chance to turn nested wiki links into
    anchors.
    """
    parts = inner.split("|")
    name = parts[0]
    options = parts[1:]

    width = None
    align = None
    is_thumb = False
    caption_bits: list[str] = []

    for opt in options:
        token = opt.strip()
        low = token.lower()
        if low in _FILE_ALIGN:
            align = low
        elif low in _FILE_FRAME:
            if low.startswith("thumb"):
                is_thumb = True
        elif re.fullmatch(r"\d+\s*px", low):
            width = re.sub(r"\s*px$", "", low)
        elif re.fullmatch(r"x\d+\s*px", low):
            # height-only; we don't support it, ignore
            pass
        elif re.fullmatch(r"\d+x\d+\s*px", low):
            width = low.split("x", 1)[0]
        elif low in {"upright", "link=", "alt="}:
            pass
        elif low.startswith(("link=", "alt=", "page=", "lang=", "class=")):
            pass
        else:
            caption_bits.append(token)

    caption = " | ".join(b for b in caption_bits if b).strip()

    path = _greyhawk_image_path(name)
    clean_name = re.sub(r"^\s*(?:File|Image):\s*", "", name,
                        flags=re.IGNORECASE).strip()
    on_disk = clean_name.replace(" ", "_")

    if path is None:
        # Still missing — keep a quiet placeholder so the page reads cleanly
        # while the image set is being copied in.
        return (f'<span class="muted" style="font-size:0.9em">'
                f'[image: {html.escape(clean_name)}]</span>')

    url = "/greyhawk/image/" + html.escape(on_disk, quote=True)
    style_parts = ["cursor:zoom-in"]
    if width:
        style_parts.append(f"width:{width}px")
    if align in {"left", "right"}:
        style_parts.append(f"float:{align}")
        style_parts.append("margin:4px 12px 4px 12px")
    elif align == "center":
        style_parts.append("display:block;margin:8px auto")
    img_style = ";".join(style_parts)

    # No anchor wrapper — the global lightbox handler in BASE_TEMPLATE
    # picks up any bare <img> click and opens it in the overlay.
    img_html = (f'<img src="{url}" alt="{html.escape(clean_name)}"'
                f' style="{img_style}" loading="lazy">')

    if is_thumb or caption:
        fig_style = "max-width:280px"
        if align in {"left", "right"}:
            fig_style += f";float:{align};margin:4px 12px"
        elif align == "center":
            fig_style += ";margin:8px auto"
        # Caption is left raw so later regex passes can resolve [[links]].
        cap_html = (f'<figcaption class="muted" '
                    f'style="font-size:0.9em;margin-top:4px">{caption}</figcaption>'
                    if caption else "")
        return (f'<figure class="greyhawk-figure" style="{fig_style}">'
                f'<img src="{url}" alt="{html.escape(clean_name)}" '
                f'style="width:100%;height:auto;cursor:zoom-in" '
                f'loading="lazy">'
                f'{cap_html}</figure>')
    return img_html


_GREYHAWK_INFOBOX_TEMPLATES = {
    # First-line {{Name|...}} templates we treat as page-level infoboxes
    # (the source of the right-floated sidebar). Matched case-insensitively
    # against the leading template at the top of raw_text.
    "character", "settlement", "deity", "creature", "realm", "archfiend",
    "location", "organization", "organisation", "creator", "item", "plane",
    "holiday", "building", "race",
}

# Map ENTRY_TABLES → singular display label for the infobox header. Used
# when raw_text has no leading template but a typed record exists.
_GREYHAWK_ENTRY_SINGULAR = {
    "characters": "Character", "settlements": "Settlement",
    "deities": "Deity", "creatures": "Creature", "realms": "Realm",
    "archfiends": "Archfiend", "locations": "Location",
    "organizations": "Organization", "creators": "Creator",
    "items": "Item", "planes": "Plane", "holidays": "Holiday",
    "buildings": "Building",
}


def _greyhawk_split_template_args(body: str) -> list[str]:
    """Split a wiki template body on top-level `|`.

    Respects `[[...]]` and `{{...}}` nesting so a pipe inside a wiki
    link or sub-template doesn't terminate an argument prematurely.
    """
    parts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(body)
    sq = cu = 0
    while i < n:
        if body[i:i+2] == '[[':
            sq += 1; buf.append('[['); i += 2
        elif body[i:i+2] == ']]':
            sq -= 1; buf.append(']]'); i += 2
        elif body[i:i+2] == '{{':
            cu += 1; buf.append('{{'); i += 2
        elif body[i:i+2] == '}}':
            cu -= 1; buf.append('}}'); i += 2
        elif body[i] == '|' and sq == 0 and cu == 0:
            parts.append(''.join(buf)); buf = []; i += 1
        else:
            buf.append(body[i]); i += 1
    if buf:
        parts.append(''.join(buf))
    return parts


def _greyhawk_strip_templates(text: str) -> str:
    """Remove all `{{...}}` templates with bracket-depth tracking.

    Citations (`{{cite ...}}`, `{{csb|...}}`) and infoboxes otherwise leak
    into the rendered body as raw source. We can't render arbitrary
    MediaWiki templates without a full engine, so we drop them — the
    infobox sidebar covers what we lose from the page-level template.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i:i+2] == '{{':
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if text[j:j+2] == '{{':
                    depth += 1; j += 2
                elif text[j:j+2] == '}}':
                    depth -= 1; j += 2
                else:
                    j += 1
            i = j
        else:
            out.append(text[i]); i += 1
    return ''.join(out)


def _greyhawk_extract_leading_infobox(text: str):
    """Find the first `{{Name|...}}` infobox at the top of `text`.

    Returns `(template_name_lower, {field_key_lower: raw_value, ...})` or
    `None`. Tolerates leading whitespace. Only fires when the template
    name matches `_GREYHAWK_INFOBOX_TEMPLATES` — citation templates that
    happen to come first are ignored.
    """
    m = re.match(r"\s*\{\{\s*([A-Za-z][A-Za-z0-9_\- ]*?)\s*(\||\})", text)
    if not m:
        return None
    name = m.group(1).strip().lower()
    if name not in _GREYHAWK_INFOBOX_TEMPLATES:
        return None
    if m.group(2) == '}':
        return name, {}
    i = m.end()  # right after the '|' that ends the name
    depth = 1
    body_start = i
    while i < len(text):
        if text[i:i+2] == '{{':
            depth += 1; i += 2
        elif text[i:i+2] == '}}':
            depth -= 1
            if depth == 0:
                body = text[body_start:i]
                break
            i += 2
        else:
            i += 1
    else:
        return None
    fields: dict[str, str] = {}
    for arg in _greyhawk_split_template_args(body):
        if '=' in arg:
            k, v = arg.split('=', 1)
            fields[k.strip().lower()] = v.strip()
    return name, fields


def _greyhawk_parse_image_value(v: str) -> str | None:
    """Return a portrait filename from an infobox `image=` value.

    Handles `[[File:Foo.jpg|235px]]`, `[[File:Foo.jpg]]`, `File:Foo.jpg`,
    and a bare `Foo.jpg`. The `File:`/`Image:` prefix is dropped.
    """
    if not v:
        return None
    v = v.strip()
    m = re.match(r"\[\[(?:File|Image):([^|\]]+)", v, re.I)
    if m:
        return m.group(1).strip()
    v = re.sub(r"^(?:File|Image):\s*", "", v, flags=re.I)
    return v or None


def _greyhawk_infobox_row(key: str, value: str) -> str:
    label = key.replace('_', ' ').replace('-', ' ')
    label = label[:1].upper() + label[1:]
    return (
        f'<tr><th style="text-align:left;font-weight:normal;'
        f'color:#bcae8f;padding:3px 8px 3px 0;vertical-align:top;'
        f'white-space:nowrap">{html.escape(label)}</th>'
        f'<td style="padding:3px 0;vertical-align:top">'
        f'{html.escape(value)}</td></tr>'
    )


def _greyhawk_render_infobox(template_name: str | None,
                             template_fields: dict,
                             typed_table: str | None,
                             typed_row) -> str:
    """Build the right-floated infobox card.

    Header is the template type (e.g. 'Character') — the entity name is
    surfaced as a regular `Name: ...` row, matching the user's spec. We
    prefer values from the typed DB record over re-parsing the template
    because the importer already resolved `[[wiki links]]` and stripped
    citation templates in those columns; falling back to the raw
    template only when no typed record exists.
    """
    # Title — template type takes precedence; fall back to the typed
    # table's singular when raw_text has no recognised template head.
    title = None
    if template_name:
        title = template_name.capitalize()
    elif typed_table:
        title = _GREYHAWK_ENTRY_SINGULAR.get(typed_table, typed_table.title())

    # Portrait
    image_html = ""
    img_name = _greyhawk_parse_image_value(
        (template_fields or {}).get('image', '')
    )
    if img_name:
        path = _greyhawk_image_path(img_name)
        if path:
            url = "/greyhawk/image/" + html.escape(path.name, quote=True)
            cap = _greyhawk_strip_templates(
                (template_fields or {}).get('caption', '')
            ).strip()
            cap_html = (
                f'<div style="text-align:center;font-size:0.82em;'
                f'color:#bcae8f;margin:6px 0 4px 0">{html.escape(cap)}'
                f'</div>' if cap else ''
            )
            image_html = (
                f'<img src="{url}" alt="{html.escape(img_name)}" '
                f'style="width:100%;cursor:zoom-in;display:block;'
                f'margin-top:6px" loading="lazy">' + cap_html
            )

    # Field rows — typed record first (importer-cleaned values), fall
    # back to raw template fields if there is no DB-side record.
    rows_html: list[str] = []
    seen: set[str] = set()
    if typed_row is not None:
        for col in typed_row.keys():
            if col in ('page', 'extra'):
                continue
            v = typed_row[col]
            if v in (None, ''):
                continue
            cleaned = _greyhawk_strip_templates(str(v)).strip()
            if not cleaned:
                continue
            seen.add(col.lower())
            rows_html.append(_greyhawk_infobox_row(col, cleaned))
    else:
        for k, v in (template_fields or {}).items():
            if k in ('image', 'caption'):
                continue
            cleaned = _greyhawk_strip_templates(v).strip()
            if not cleaned:
                continue
            seen.add(k.lower())
            rows_html.append(_greyhawk_infobox_row(k, cleaned))

    # `extra` JSON blob may carry infobox keys the schema didn't pre-allocate
    # (formerhome, raised, alignment3e, …). Skip extra.image / extra.caption
    # since extra.image is corrupted (often a pixel size, not a filename).
    if typed_row is not None and 'extra' in typed_row.keys() and typed_row['extra']:
        try:
            extra = json.loads(typed_row['extra'])
        except (json.JSONDecodeError, TypeError):
            extra = {}
        for k, v in extra.items():
            kl = k.lower()
            if kl in seen or kl in ('image', 'caption'):
                continue
            if not v:
                continue
            cleaned = _greyhawk_strip_templates(str(v)).strip()
            if not cleaned:
                continue
            rows_html.append(_greyhawk_infobox_row(k, cleaned))

    if not (title or image_html or rows_html):
        return ""

    title_html = (
        f'<div style="text-align:center;font-weight:bold;'
        f'font-size:1.02em;padding:4px 8px;border-bottom:1px solid '
        f'rgba(212,197,169,0.25);margin:-6px -8px 0">'
        f'{html.escape(title or "")}</div>' if title else ''
    )
    table_html = (
        f'<table style="font-size:0.88em;width:100%;border-collapse:collapse;'
        f'margin-top:6px">{"".join(rows_html)}</table>' if rows_html else ''
    )

    return (
        f'<aside class="card greyhawk-infobox" '
        f'style="float:right;width:260px;margin:0 0 16px 16px;'
        f'padding:6px 8px;clear:right">'
        f'{title_html}{image_html}{table_html}</aside>'
    )


def _greyhawk_iter_tables(text: str):
    """Yield (start, end, block) for each top-level ``{| ... |}`` table,
    tracking nesting so an inner table doesn't prematurely close the outer
    one. Bails out at the first unbalanced ``{|`` (malformed source), leaving
    the remainder untouched."""
    i, n = 0, len(text)
    while i < n:
        if text[i:i + 2] == "{|":
            depth, j = 1, i + 2
            while j < n and depth > 0:
                if text[j:j + 2] == "{|":
                    depth += 1; j += 2
                elif text[j:j + 2] == "|}":
                    depth -= 1; j += 2
                else:
                    j += 1
            if depth == 0:
                yield (i, j, text[i:j])
                i = j
            else:
                return        # unbalanced — stop, leave the rest as-is
        else:
            i += 1


def _greyhawk_inline(text: str) -> str:
    """Apply inline wiki markup — [[links]], '''bold''', ''italic'' — used by
    both the body renderer and table cells so formatting is consistent."""
    def _link(m):
        target = m.group(1).strip()
        label = (m.group(2) or target).strip()
        anchor = target.split("#", 1)[0]
        return (f'<a href="/greyhawk/page/{html.escape(anchor)}">'
                f'{html.escape(label)}</a>')
    text = re.sub(r"\[\[([^\[\]|#]+)(?:#[^\[\]|]+)?(?:\|([^\[\]]+))?\]\]",
                  _link, text)
    text = re.sub(r"'''([^']+)'''", r"<b>\1</b>", text)
    text = re.sub(r"''([^']+)''", r"<i>\1</i>", text)
    return text


def _greyhawk_strip_cell_attrs(cell: str) -> str:
    """Strip MediaWiki per-cell attributes (`style=... | content`) from a
    table cell, keeping just the content. Leaves plain cells untouched."""
    m = re.match(r"^\s*([^|]*?)\s*\|(?!\|)\s*(.*)$", cell, re.S)
    if m and "=" in m.group(1):
        return m.group(2).strip()
    return cell.strip()


def _greyhawk_render_wikitable(block: str) -> str:
    """Convert a MediaWiki `{| ... |}` table into an HTML <table>.

    Handles captions (`|+`), row breaks (`|-`), header cells (`!`, `!!`),
    data cells (`|`, `||`), per-cell attribute prefixes, and cell content
    that wraps onto a continuation line. Cell text runs through
    _greyhawk_inline so links/bold/italic render inside tables too."""
    inner = block.strip()
    inner = re.sub(r"^\{\|[^\n]*", "", inner)   # drop "{| <attrs>"
    inner = re.sub(r"\|\}\s*$", "", inner)       # drop closing "|}"

    # Pull out nested tables first (render recursively) so the pipe-based
    # cell parser below never sees their '|' / '||' separators.
    nspans = list(_greyhawk_iter_tables(inner))
    nested: list[str] = [None] * len(nspans)
    # Render forward for stable ids; splice from the end to keep offsets valid.
    for idx in range(len(nspans) - 1, -1, -1):
        s, e, b = nspans[idx]
        nested[idx] = _greyhawk_render_wikitable(b)
        inner = inner[:s] + f"\x00NT{idx}\x00" + inner[e:]

    caption = None
    rows: list[tuple[bool, list[str]]] = []
    current: list[str] = []
    current_is_header = False
    started = False

    for raw in inner.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("|+"):
            caption = line[2:].strip()
        elif line.startswith("|-"):
            if started:
                rows.append((current_is_header, current))
            current, current_is_header, started = [], False, True
        elif line.startswith("!"):
            started = True
            current_is_header = True
            current += [c.strip() for c in re.split(r"!!", line[1:])]
        elif line.startswith("|"):
            started = True
            current += [c.strip() for c in re.split(r"\|\|", line[1:])]
        elif current:                 # wrapped cell content
            current[-1] += " " + line
    if started or current:
        rows.append((current_is_header, current))

    html_rows = []
    for is_header, cells in rows:
        if not cells:
            continue
        tag = "th" if is_header else "td"
        tds = "".join(
            f"<{tag}>{_greyhawk_inline(_greyhawk_strip_cell_attrs(c))}</{tag}>"
            for c in cells
        )
        html_rows.append(f"<tr>{tds}</tr>")

    cap = f"<caption>{_greyhawk_inline(caption)}</caption>" if caption else ""
    out = f'<table class="gh-wikitable">{cap}{"".join(html_rows)}</table>'
    for idx, nested_html in enumerate(nested):
        out = out.replace(f"\x00NT{idx}\x00", nested_html)
    return out


def _greyhawk_build_list(items: list[tuple[str, str]], i: int = 0,
                         depth: int = 1):
    """Build nested <ul>/<ol> HTML from MediaWiki list items.

    items: (markers, content) where markers is a run of '*'/'#' (e.g. '*',
    '**', '*#'). Returns (html, next_index). The list kind at each level is
    set by the marker char at that depth ('#' → ordered, '*' → unordered)."""
    kind = "ol" if items[i][0][depth - 1] == "#" else "ul"
    html_parts = [f"<{kind}>"]
    while i < len(items):
        markers, content = items[i]
        d = len(markers)
        if d < depth:
            break
        if d == depth:
            html_parts.append(f"<li>{content}")
            if i + 1 < len(items) and len(items[i + 1][0]) > depth:
                sub, i = _greyhawk_build_list(items, i + 1, depth + 1)
                html_parts.append(sub)
            else:
                i += 1
            html_parts.append("</li>")
        else:                       # deeper than expected — recurse
            sub, i = _greyhawk_build_list(items, i, depth + 1)
            html_parts.append(sub)
    html_parts.append(f"</{kind}>")
    return "".join(html_parts), i


def _greyhawk_render_deflist(items: list[tuple[str, str]]) -> str:
    """Render a run of ';'/':' definition-list lines into a <dl>."""
    out = ["<dl>"]
    for mark, content in items:
        tag = "dt" if mark == ";" else "dd"
        out.append(f"<{tag}>{content}</{tag}>")
    out.append("</dl>")
    return "".join(out)


def _greyhawk_render_wiki(text: str) -> str:
    """Minimal wiki-markup renderer for raw_text.

    The Greyhawk DB stores MediaWiki-flavoured source. We don't try to be a
    full parser, but we handle the common constructs: [[links]], headings,
    '''bold'''/''italic'', {| tables |} (incl. nested), bullet/numbered/
    definition lists, horizontal rules, and [[File:]] images. Citation
    <ref>s, <gallery>s and magic words (__TOC__) are stripped. Anything else
    falls through as text so the page stays readable.
    """
    if not text:
        return ""

    # Drop all {{...}} templates first. The page's infobox is rendered
    # separately as a sidebar, and citation templates would otherwise
    # appear as raw source in the middle of paragraphs.
    text = _greyhawk_strip_templates(text)

    # Strip citation <ref>s, <gallery> blocks, and MediaWiki magic words —
    # these have no useful body rendering and otherwise leak as raw markup.
    text = re.sub(r"<ref[^>]*/>", "", text, flags=re.I)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S | re.I)
    text = re.sub(r"</?ref[^>]*>", "", text, flags=re.I)   # orphan/unclosed refs
    text = re.sub(r"<gallery[^>]*>.*?</gallery>", "", text, flags=re.S | re.I)
    text = re.sub(r"__[A-Z]+__", "", text)

    # Render MediaWiki tables to HTML (depth-aware, so nested tables don't
    # truncate the outer one) and stash them behind sentinels, so the
    # heading/inline/paragraph passes below leave the table markup intact.
    tspans = list(_greyhawk_iter_tables(text))
    _tables: list[str] = [None] * len(tspans)
    for idx in range(len(tspans) - 1, -1, -1):
        start, end, block = tspans[idx]
        _tables[idx] = _greyhawk_render_wikitable(block)
        text = text[:start] + f"\n\n\x00GHT{idx}\x00\n\n" + text[end:]

    # Expand [[File:...]] refs next so the generic [[link]] regex below
    # doesn't try to turn the filename into a wiki page anchor. We walk
    # from the end so the indices we collected stay valid as we splice.
    spans = list(_greyhawk_iter_file_refs(text))
    for start, end, inner in reversed(spans):
        text = text[:start] + _greyhawk_render_file_ref(inner) + text[end:]

    list_re = re.compile(r"^([*#]+)[ \t]*(.*)$")
    def_re = re.compile(r"^([;:])\s*(.*\S.*)$")
    lines = text.splitlines()
    out_lines: list[str] = []
    k = 0
    while k < len(lines):
        line = lines[k].rstrip()
        if re.match(r"^==+\s*[^=]+\s*==+\s*$", line):
            depth = len(line) - len(line.lstrip("="))
            heading = line.strip("= ").strip()
            tag = "h2" if depth <= 2 else "h3"
            out_lines.append(f"<{tag}>{html.escape(heading)}</{tag}>")
            k += 1
        elif re.match(r"^----+\s*$", line):
            out_lines.append("<hr>")
            k += 1
        elif list_re.match(line):
            items = []
            while k < len(lines):
                mm = list_re.match(lines[k].rstrip())
                if not mm:
                    break
                items.append((mm.group(1), mm.group(2)))
                k += 1
            html_list, _ = _greyhawk_build_list(items)
            out_lines.append(html_list)
        elif def_re.match(line):
            ditems = []
            while k < len(lines):
                mm = def_re.match(lines[k].rstrip())
                if not mm:
                    break
                ditems.append((mm.group(1), mm.group(2)))
                k += 1
            out_lines.append(_greyhawk_render_deflist(ditems))
        else:
            out_lines.append(line)
            k += 1
    body = "\n".join(out_lines)

    body = _greyhawk_inline(body)

    block_starts = ("<h2>", "<h3>", "<figure", "<ul>", "<ol>", "<dl>", "<hr>")
    paragraphs = []
    for chunk in re.split(r"\n{2,}", body):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.fullmatch(r"\x00GHT(\d+)\x00", chunk)
        if m:
            paragraphs.append(_tables[int(m.group(1))])
        elif chunk.startswith(block_starts):
            paragraphs.append(chunk)
        else:
            paragraphs.append(f"<p>{chunk}</p>")
    return "\n".join(paragraphs)


@app.route("/greyhawk/image/<path:filename>")
def greyhawk_image(filename: str):
    """Serve a Greyhawk image. Path-safe: only basenames are accepted."""
    if not _GREYHAWK_IMG.exists():
        abort(404)
    # Reject anything that looks like a traversal; send_from_directory also
    # enforces this, but bail early with a clearer 404.
    if "/" in filename or "\\" in filename or filename.startswith("."):
        abort(404)
    return send_from_directory(str(_GREYHAWK_IMG), filename)


def _greyhawk_db_or_error():
    """Return (conn, None) or (None, html_error_block)."""
    if not _GREYHAWK_DB.exists():
        return None, ('<h1>Greyhawk Setting</h1>'
                      f'<p style="color:#c66">Database not found at '
                      f'<code>{html.escape(str(_GREYHAWK_DB))}</code>.</p>')
    return _db(_GREYHAWK_DB), None


@app.route("/greyhawk")
def greyhawk_index():
    """Greyhawk hub: search bar + entry-table tiles + top wiki categories."""
    conn, err = _greyhawk_db_or_error()
    if err:
        return render("Greyhawk", err)

    q = (request.args.get("q") or "").strip()
    if q:
        return _greyhawk_search_results(conn, q)

    table_cards = []
    for t in _greyhawk.ENTRY_TABLES:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            continue
        table_cards.append(
            f'<a class="card" href="/greyhawk/category/{html.escape(t)}" '
            f'style="display:block;text-decoration:none;color:inherit">'
            f'<h3 style="margin:0 0 4px 0;text-transform:capitalize">'
            f'{html.escape(t)}</h3>'
            f'<p class="muted" style="margin:0">{n} entries</p>'
            f'</a>'
        )

    top = conn.execute(
        "SELECT category, COUNT(*) AS n FROM categories "
        "GROUP BY category ORDER BY n DESC LIMIT 20"
    ).fetchall()
    cat_items = "".join(
        f'<li><a href="/greyhawk/category/{html.escape(r["category"])}">'
        f'{html.escape(r["category"])}</a> '
        f'<span class="muted">({r["n"]})</span></li>'
        for r in top
    )
    total_pages = conn.execute(
        "SELECT COUNT(*) FROM pages WHERE is_redirect = 0"
    ).fetchone()[0]
    total_cats = conn.execute(
        "SELECT COUNT(DISTINCT category) FROM categories"
    ).fetchone()[0]

    image_count = 0
    if _GREYHAWK_IMG.exists():
        image_count = sum(
            1 for p in _GREYHAWK_IMG.iterdir()
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg",
                                                     ".gif", ".webp", ".svg"}
        )
    image_link = ""
    if image_count:
        image_link = (
            f' · <a href="/greyhawk/images">{image_count} images</a>'
        )

    body = (
        '<h1>Greyhawk Setting</h1>'
        f'<p class="muted">{total_pages} pages, {total_cats} wiki categories'
        f'{image_link}. Drawn from the Greyhawk wiki.</p>'
        '<form method="get" action="/greyhawk" style="margin:18px 0">'
        '<input name="q" placeholder="Search pages (FTS5 — e.g. Mordenkainen, '
        '&quot;Circle of Eight&quot;, Morden*)" '
        f'style="{_INPUT_CSS};width:100%;max-width:600px">'
        '</form>'
        '<h2>Entry tables</h2>'
        '<div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(200px,1fr))">'
        + "".join(table_cards) +
        '</div>'
        '<h2 style="margin-top:24px">Top wiki categories</h2>'
        f'<ul style="columns:2;max-width:700px">{cat_items}</ul>'
    )
    return render("Greyhawk", body)


@app.route("/greyhawk/images")
def greyhawk_images():
    """Thumbnail gallery of all images shipped in settings/greyhawk/images/.

    Cheap directory walk + lazy-loaded thumbnails. With a few hundred files
    this is fine; if the set grows much larger we'd want pagination.
    """
    if not _GREYHAWK_IMG.exists():
        return render(
            "Greyhawk · images",
            '<p><a href="/greyhawk">&larr; Greyhawk</a></p>'
            '<h1>Greyhawk images</h1>'
            f'<p style="color:#c66">Image directory not found at '
            f'<code>{html.escape(str(_GREYHAWK_IMG))}</code>.</p>',
        )

    q = (request.args.get("q") or "").strip().lower()
    exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
    files = sorted(
        (p for p in _GREYHAWK_IMG.iterdir()
         if p.is_file() and p.suffix.lower() in exts),
        key=lambda p: p.name.lower(),
    )
    if q:
        files = [p for p in files if q in p.name.lower()]

    tiles = []
    for p in files:
        url = "/greyhawk/image/" + html.escape(p.name, quote=True)
        label = html.escape(p.stem.replace("_", " "))
        # Bare <img> so the global lightbox in BASE_TEMPLATE catches the
        # click — no anchor needed when the only action is "view large".
        tiles.append(
            f'<div class="card" style="display:flex;flex-direction:column;'
            f'gap:6px;padding:8px">'
            f'<img src="{url}" alt="{html.escape(p.name)}" '
            f'loading="lazy" style="width:100%;height:140px;'
            f'object-fit:cover;border-radius:4px;background:#222;'
            f'cursor:zoom-in"/>'
            f'<span style="font-size:0.85em;line-height:1.2;'
            f'word-break:break-word">{label}</span></div>'
        )

    body = (
        '<p><a href="/greyhawk">&larr; Greyhawk</a></p>'
        '<h1>Greyhawk images</h1>'
        f'<p class="muted">{len(files)} files in '
        f'<code>settings/greyhawk/images/</code>.</p>'
        '<form method="get" action="/greyhawk/images" style="margin:12px 0">'
        f'<input name="q" value="{html.escape(q)}" '
        f'placeholder="Filter by filename…" '
        f'style="{_INPUT_CSS};width:100%;max-width:480px">'
        + (' <a href="/greyhawk/images" class="muted">clear</a>' if q else '')
        + '</form>'
        + (f'<div class="grid" style="grid-template-columns:'
           f'repeat(auto-fill,minmax(160px,1fr));gap:10px">'
           f'{"".join(tiles)}</div>'
           if tiles else '<p class="muted">No images match.</p>')
    )
    return render("Greyhawk · images", body)


def _greyhawk_search_results(conn, q: str) -> str:
    """Render FTS results inline on the index page."""
    escaped = q.replace('"', '""')
    try:
        rows = conn.execute(
            "SELECT p.title, p.type, "
            "snippet(pages_fts, 1, '<<', '>>', ' … ', 16) AS excerpt "
            "FROM pages_fts JOIN pages p ON p.title = pages_fts.title "
            "WHERE pages_fts MATCH ? AND p.is_redirect = 0 "
            "ORDER BY rank LIMIT 50",
            (escaped,),
        ).fetchall()
        err_html = ""
    except sqlite3.OperationalError as exc:
        rows = []
        err_html = (f'<p style="color:#c66">FTS query failed: '
                    f'{html.escape(str(exc))}</p>')

    items = []
    for r in rows:
        excerpt = (html.escape(r["excerpt"] or "")
                   .replace("&lt;&lt;", '<mark>')
                   .replace("&gt;&gt;", '</mark>'))
        items.append(
            f'<li style="margin:10px 0">'
            f'<a href="/greyhawk/page/{html.escape(r["title"])}">'
            f'<b>{html.escape(r["title"])}</b></a> '
            f'<span class="muted">({html.escape(r["type"] or "page")})</span>'
            f'<div style="margin-left:1em;color:#bcae8f">{excerpt}</div>'
            f'</li>'
        )

    body = (
        '<h1>Greyhawk Setting</h1>'
        '<form method="get" action="/greyhawk" style="margin:18px 0">'
        f'<input name="q" value="{html.escape(q)}" '
        f'style="{_INPUT_CSS};width:100%;max-width:600px">'
        ' <a href="/greyhawk" class="muted">clear</a>'
        '</form>'
        f'<h2>Search: <code>{html.escape(q)}</code> '
        f'<span class="muted">— {len(rows)} hits</span></h2>'
        + err_html
        + (f'<ul style="list-style:none;padding:0">{"".join(items)}</ul>'
           if items else
           '<p class="muted">No matches.</p>')
    )
    return render("Greyhawk · search", body)


@app.route("/greyhawk/category/<path:name>")
def greyhawk_category(name):
    """Browse a typed entry table or a wiki tag-category."""
    conn, err = _greyhawk_db_or_error()
    if err:
        return render("Greyhawk", err)

    key = name.strip()
    if key.lower() in _greyhawk.ENTRY_TABLES:
        table = key.lower()
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY page"
        ).fetchall()

        display_cols = [c for c in cols if c != "extra"][:6]
        thead = "".join(f"<th>{html.escape(c)}</th>" for c in display_cols)
        body_rows = []
        for r in rows:
            cells = []
            for c in display_cols:
                v = r[c]
                if c == "page" or c == "name":
                    page = r["page"]
                    label = v or page or ""
                    cells.append(
                        f'<td><a href="/greyhawk/page/{html.escape(page or "")}">'
                        f'{html.escape(str(label))}</a></td>'
                    )
                else:
                    cells.append(f'<td>{html.escape(str(v)) if v else ""}</td>')
            body_rows.append(f"<tr>{''.join(cells)}</tr>")

        body = (
            f'<p><a href="/greyhawk">&larr; Greyhawk</a></p>'
            f'<h1 style="text-transform:capitalize">{html.escape(table)}</h1>'
            f'<p class="muted">{len(rows)} entries. '
            f'Fields: <code>{html.escape(", ".join(cols))}</code></p>'
            f'<table><thead><tr>{thead}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table>'
        )
        return render(f"Greyhawk · {table}", body)

    cat_row = conn.execute(
        "SELECT category FROM categories WHERE category = ? COLLATE NOCASE LIMIT 1",
        (key,),
    ).fetchone()
    if cat_row is None:
        like = conn.execute(
            "SELECT category, COUNT(*) AS n FROM categories "
            "WHERE category LIKE ? COLLATE NOCASE "
            "GROUP BY category ORDER BY n DESC LIMIT 20",
            (f"%{key}%",),
        ).fetchall()
        candidates = "".join(
            f'<li><a href="/greyhawk/category/{html.escape(r["category"])}">'
            f'{html.escape(r["category"])}</a> '
            f'<span class="muted">({r["n"]})</span></li>'
            for r in like
        )
        body = (
            f'<p><a href="/greyhawk">&larr; Greyhawk</a></p>'
            f'<h1>No category &ldquo;{html.escape(key)}&rdquo;</h1>'
            + (f'<p>Did you mean:</p><ul>{candidates}</ul>'
               if candidates else '<p class="muted">No similar categories.</p>')
        )
        return render("Greyhawk · category", body)

    cat = cat_row["category"]
    pages = conn.execute(
        "SELECT page FROM categories WHERE category = ? ORDER BY page",
        (cat,),
    ).fetchall()
    items = "".join(
        f'<li><a href="/greyhawk/page/{html.escape(r["page"])}">'
        f'{html.escape(r["page"])}</a></li>'
        for r in pages
    )
    body = (
        f'<p><a href="/greyhawk">&larr; Greyhawk</a></p>'
        f'<h1>{html.escape(cat)}</h1>'
        f'<p class="muted">{len(pages)} pages</p>'
        f'<ul style="columns:2;max-width:800px">{items}</ul>'
    )
    return render(f"Greyhawk · {cat}", body)


@app.route("/greyhawk/page/<path:title>")
def greyhawk_page(title):
    """Render a single Greyhawk wiki page with categories + typed fields."""
    conn, err = _greyhawk_db_or_error()
    if err:
        return render("Greyhawk", err)

    key = title.strip()
    row = conn.execute(
        "SELECT title FROM pages WHERE title = ? COLLATE NOCASE LIMIT 1",
        (key,),
    ).fetchone()
    if row is None:
        like = conn.execute(
            "SELECT title FROM pages WHERE title LIKE ? COLLATE NOCASE "
            "AND is_redirect = 0 ORDER BY length(title) LIMIT 20",
            (f"%{key}%",),
        ).fetchall()
        cands = "".join(
            f'<li><a href="/greyhawk/page/{html.escape(r["title"])}">'
            f'{html.escape(r["title"])}</a></li>'
            for r in like
        )
        body = (
            f'<p><a href="/greyhawk">&larr; Greyhawk</a></p>'
            f'<h1>No page &ldquo;{html.escape(key)}&rdquo;</h1>'
            + (f'<p>Did you mean:</p><ul>{cands}</ul>'
               if cands else '<p class="muted">No similar pages.</p>')
        )
        return render("Greyhawk · page", body)

    original = row["title"]
    canonical = _greyhawk._resolve_redirect(conn, original)
    page_row = conn.execute(
        "SELECT title, type, raw_text FROM pages WHERE title = ?",
        (canonical,),
    ).fetchone()
    if page_row is None:
        return render("Greyhawk · page",
                      f'<p style="color:#c66">Redirect target '
                      f'<code>{html.escape(canonical)}</code> not found.</p>')

    cats = conn.execute(
        "SELECT category FROM categories WHERE page = ? ORDER BY category",
        (canonical,),
    ).fetchall()

    # Pick the page's primary typed record (first ENTRY_TABLE hit). Pages
    # are usually classified by exactly one type — characters OR realms OR …
    # — so taking the first match is sufficient in practice.
    typed_table = None
    typed_row = None
    for t in _greyhawk.ENTRY_TABLES:
        tr = conn.execute(f"SELECT * FROM {t} WHERE page = ?",
                          (canonical,)).fetchone()
        if tr:
            typed_table, typed_row = t, tr
            break

    raw_text = page_row["raw_text"] or ""
    leading = _greyhawk_extract_leading_infobox(raw_text)
    template_name = leading[0] if leading else None
    template_fields = leading[1] if leading else {}
    infobox_html = _greyhawk_render_infobox(
        template_name, template_fields, typed_table, typed_row
    )

    cat_links = " · ".join(
        f'<a href="/greyhawk/category/{html.escape(r["category"])}">'
        f'{html.escape(r["category"])}</a>'
        for r in cats
    ) or '<span class="muted">none</span>'

    redirect_note = ""
    if canonical != original:
        redirect_note = (f'<p class="muted">Redirected from '
                         f'<i>{html.escape(original)}</i></p>')

    wiki_html = _greyhawk_render_wiki(raw_text)

    # `display:flow-root` contains the floated infobox so subsequent
    # content (footer/nav) doesn't slide up alongside it.
    body = (
        f'<p><a href="/greyhawk">&larr; Greyhawk</a></p>'
        f'<h1>{html.escape(page_row["title"])}</h1>'
        + redirect_note
        + f'<p class="muted">Categories: {cat_links}</p>'
        + '<div style="display:flow-root">'
        + infobox_html
        + f'<div class="greyhawk-body">{wiki_html}</div>'
        + '</div>'
    )
    return render(f"Greyhawk · {page_row['title']}", body)


# ---------------------------------------------------------------------------
# World maps
# ---------------------------------------------------------------------------
from tools import world_map as _world_map


@app.route("/atlas")
def atlas_index():
    """List all world maps in the active campaign + their views."""
    try:
        d = _world_map._maps_dir()
    except Exception as e:
        return render("Atlas", f'<h1>Atlas</h1><p style="color:#c66">{html.escape(str(e))}</p>')

    items, fragments = [], []
    for p in sorted(d.glob("*.map")):
        slug = p.stem
        if not _world_map._is_workspace_file(p):
            fragments.append(slug)
            continue
        try:
            ws = _world_map.parse_file(p)
            view_links = " · ".join(
                f'<a href="/atlas/{html.escape(slug)}/{html.escape(v.name)}">{html.escape(v.name)}</a>'
                for v in ws.views
            ) or '<span style="color:#8a7a60">no views</span>'
            n_features = len(ws.model)
            items.append(
                f'<li style="margin:6px 0">'
                f'<b style="color:#c8a96e">{html.escape(slug)}</b> '
                f'<span style="color:#8a7a60;font-size:.9em">({n_features} top-level features)</span><br>'
                f'<span style="margin-left:1em">Views: {view_links}</span>'
                f'</li>'
            )
        except Exception as e:
            items.append(
                f'<li><b>{html.escape(slug)}</b> '
                f'<span style="color:#c66">parse error: {html.escape(str(e))}</span></li>'
            )

    if not items and not fragments:
        body = (
            '<h1>Atlas</h1>'
            '<p style="color:#8a7a60">No maps yet. Create one with the MCP tool '
            '<code>create_world_map(slug)</code>, then populate it via '
            '<code>update_world_map</code> or <code>add_world_map_feature</code>.</p>'
        )
    else:
        body = '<h1>Atlas</h1>'
        if items:
            body += '<ul style="list-style:none;padding:0">' + ''.join(items) + '</ul>'
        if fragments:
            frag_html = ', '.join(f'<code>{html.escape(s)}</code>' for s in fragments)
            body += (
                '<h3 style="margin-top:24px">Include fragments</h3>'
                '<p style="color:#8a7a60;font-size:.9em">Files without a top-level <code>workspace</code> block. '
                'These are inlined by other maps via <code>!include</code>: '
                + frag_html + '</p>'
            )
    return render("Atlas", body)


def _map_icon_overrides() -> dict:
    """Scan static/map-icons/ for per-kind icon files. Returns ``{kind: url}``.
    Files matching ``<kind>.{svg,png,jpg,jpeg,webp}`` provide overrides for
    the inline SVG icons in static/world-map.js. SVG wins ties."""
    icons_dir = Path(__file__).parent / "static" / "map-icons"
    if not icons_dir.is_dir():
        return {}
    out: dict[str, str] = {}
    rank = {".svg": 0, ".png": 1, ".webp": 2, ".jpg": 3, ".jpeg": 3}
    for p in sorted(icons_dir.iterdir(), key=lambda x: rank.get(x.suffix.lower(), 9)):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in rank:
            continue
        kind = p.stem.lower()
        out.setdefault(kind, f"/static/map-icons/{p.name}")
    return out


@app.route("/atlas/<slug>/<view>")
def world_map_view(slug, view):
    """Render a Leaflet view of a world map."""
    path = _world_map._maps_dir() / f"{_world_map._safe_slug(slug)}.map"
    if not path.exists():
        abort(404)
    try:
        _world_map.parse_file(path)  # validate before serving
    except SyntaxError as e:
        body = (
            f'<h1>{html.escape(slug)} — {html.escape(view)}</h1>'
            f'<p style="color:#c66">DSL error: {html.escape(str(e))}</p>'
            f'<p><a href="/atlas">← back to atlas</a></p>'
        )
        return render(f"Atlas: {slug}", body)

    data_url  = f"/api/atlas/{slug}/{view}.geojson"
    party_url = f"/api/atlas/{slug}/party.geojson"
    icon_overrides = _map_icon_overrides()
    body = (
        '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" '
        'integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">'
        '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" '
        'integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>'
        '<style>'
        '  .map-layout { display:flex; gap:12px; align-items:stretch; }'
        '  #worldmap { flex:1; height:75vh; min-width:0; '
        '              background:#1a1510; border:1px solid #4a3828; border-radius:6px; }'
        '  #worldmap-sidebar { width:260px; max-height:75vh; overflow-y:auto; '
        '                      padding:12px; background:#2a2018; border:1px solid #4a3828; border-radius:6px; }'
        '  .leaflet-popup-content { color:#1a1510; font-family:Georgia,serif; }'
        '  .leaflet-control-layers { background:#2a2018; color:#d4c5a9; border:1px solid #4a3828; }'
        '  .leaflet-control-layers-overlays label { color:#d4c5a9; }'
        # Hover-brighten on every interactive feature (SVG paths from L.geoJSON).
        '  .leaflet-interactive { transition: filter 0.15s ease; }'
        '  .leaflet-interactive:hover { filter: brightness(1.2) saturate(1.1); cursor: pointer; }'
        # Per-kind POI icons (inline SVG via L.divIcon, or stock images via L.icon).
        '  .map-icon { display: flex; align-items: center; justify-content: center; '
        '              filter: drop-shadow(0 1px 2px rgba(0,0,0,0.55)); transition: transform 0.15s ease; }'
        '  .map-icon svg { width: 100%; height: 100%; display: block; }'
        '  .map-icon:hover { transform: scale(1.18); cursor: pointer; z-index: 1000 !important; }'
        '  .map-icon-img { filter: drop-shadow(0 1px 2px rgba(0,0,0,0.6)); transition: transform 0.15s ease; }'
        '  .map-icon-img:hover { transform: scale(1.18); cursor: pointer; z-index: 1000 !important; }'
        # Tooltip readability — text shadow against busy backgrounds.
        '  .leaflet-tooltip { text-shadow: 0 0 3px #000, 0 0 1px #000; '
        '                     background: rgba(20,16,12,0.78); color: #f1e6cc; '
        '                     border: 1px solid #6a5333; padding: 2px 6px; }'
        '  .leaflet-tooltip:before { display: none; }'
        # Party marker — gold dot with pulsing ring + permanent label.
        '  .party-marker { position: relative; width: 16px; height: 16px; }'
        '  .party-marker .party-dot {'
        '    position: absolute; left: 0; top: 0; width: 16px; height: 16px;'
        '    border-radius: 50%;'
        '    background: radial-gradient(circle at 35% 30%, #fff4c2, #d4af37 60%, #6a5018);'
        '    box-shadow: 0 0 0 1px #2a1f0f, 0 0 8px rgba(212,175,55,0.7);'
        '    z-index: 2;'
        '  }'
        '  .party-marker .party-pulse {'
        '    position: absolute; left: -4px; top: -4px; width: 24px; height: 24px;'
        '    border-radius: 50%;'
        '    background: rgba(212,175,55,0.55);'
        '    z-index: 1;'
        '    animation: party-pulse 1.6s ease-out infinite;'
        '  }'
        '  @keyframes party-pulse {'
        '    0%   { transform: scale(0.5); opacity: 0.85; }'
        '    100% { transform: scale(2.2); opacity: 0; }'
        '  }'
        '  .party-marker .party-label {'
        '    position: absolute; left: 22px; top: 1px;'
        '    font-family: Cinzel, Georgia, serif; font-size: 0.78em;'
        '    text-transform: uppercase; letter-spacing: 0.12em;'
        '    color: #f5e6b6;'
        '    text-shadow: 0 0 3px #000, 0 0 6px #000;'
        '    white-space: nowrap;'
        '  }'
        '</style>'
        f'<h1>{html.escape(slug)} <span style="color:#8a7a60;font-size:.7em">— {html.escape(view)}</span></h1>'
        '<p style="color:#8a7a60;font-size:.9em"><a href="/atlas">← back to atlas</a></p>'
        '<div class="map-layout">'
        '  <div id="worldmap"></div>'
        '  <div id="worldmap-sidebar">Loading…</div>'
        '</div>'
        f'<script>'
        f'window.WORLD_MAP_SLUG  = {json.dumps(slug)};'
        f'window.WORLD_MAP_VIEW  = {json.dumps(view)};'
        f'window.WORLD_MAP_DATA  = {json.dumps(data_url)};'
        f'window.WORLD_MAP_PARTY = {json.dumps(party_url)};'
        f'window.MAP_ICON_OVERRIDES = {json.dumps(icon_overrides)};'
        f'</script>'
        '<script src="/static/world-map.js"></script>'
    )
    return render(f"Atlas: {slug}/{view}", body)


@app.route("/api/atlas/<slug>/<view>.geojson")
def atlas_geojson(slug, view):
    """Compile-on-demand GeoJSON for a world-map view."""
    path = _world_map._maps_dir() / f"{_world_map._safe_slug(slug)}.map"
    if not path.exists():
        return jsonify({"error": f"map '{slug}' not found"}), 404
    try:
        ws = _world_map.parse_file(path)
        fc = _world_map.compile_view(ws, view)
    except (SyntaxError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(fc)


@app.route("/api/atlas/<slug>/party.geojson")
def atlas_party_geojson(slug):
    """Serve the party-position overlay if it exists (404 otherwise)."""
    overlay = _world_map._maps_dir() / f"{_world_map._safe_slug(slug)}.party.geojson"
    if not overlay.exists():
        return jsonify({"error": "no party overlay"}), 404
    return Response(overlay.read_text(encoding="utf-8"), mimetype="application/json")


# ---------------------------------------------------------------------------
# Sub-module route registration
# ---------------------------------------------------------------------------
import dashboard_items as _dashboard_items
import dashboard_monsters as _dashboard_monsters

_dashboard_items.init(
    app,
    render=render,
    db_get=_db,
    _2E_DB=_2E_DB,
    markdown_to_html=_markdown_to_html,
    INPUT_STYLE=INPUT_STYLE,
    SELECT_STYLE=SELECT_STYLE,
)

_dashboard_monsters.init(
    app,
    get_cfg=lambda: cfg,
    render=render,
    db_get=_db,
    _MONSTERS_DB=_MONSTERS_DB,
    _MONSTERS_DIR=_MONSTERS_DIR,
    monster_portraits_dict=_monster_portraits_dict,
    monster_portrait_path=_monster_portrait_path,
)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Campaign dashboard web app."
    )
    parser.add_argument("--campaign", help="Campaign name (default: active campaign)")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--ssl-cert", metavar="FILE", help="TLS certificate file (enables HTTPS)")
    parser.add_argument("--ssl-key", metavar="FILE", help="TLS private key file")
    args = parser.parse_args()

    try:
        cfg = _c.load_campaign(args.campaign or None)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"Error loading campaign: {exc}", file=sys.stderr)
        sys.exit(1)

    ssl_context = None
    if args.ssl_cert and args.ssl_key:
        ssl_context = (args.ssl_cert, args.ssl_key)
    elif args.ssl_cert or args.ssl_key:
        print("Error: --ssl-cert and --ssl-key must both be provided.", file=sys.stderr)
        sys.exit(1)

    scheme = "https" if ssl_context else "http"
    print(f"Campaign: {cfg.get('name', cfg['_name'])}")
    print(f"Data dir: {cfg['_data_dir']}")
    print(f"Starting dashboard at {scheme}://{args.host}:{args.port}")
    app.run(debug=True, host=args.host, port=args.port, ssl_context=ssl_context,
            use_reloader=not ssl_context)
