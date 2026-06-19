#!/usr/bin/env python3
"""Import magic items from the AD&D 2e Fandom wiki into ``global/2e.db``.

Iterates every page in https://adnd2e.fandom.com/wiki/Category:Magic_Items via
the MediaWiki API, parses each page's ``{{Item}}`` infobox + description prose,
and inserts any item NOT already recorded in the ``items`` table (dedup is by
case-insensitive name).

Why these choices:
  * MediaWiki API (not HTML scraping) — stable, paginated, batchable.
  * Dedup by name so re-runs are idempotent and existing rows are never
    duplicated or overwritten.
  * Scraped rows get IDs from a dedicated high range (>= _ID_BASE) and a
    distinctive ``source`` so they're easy to spot, delete, or re-import.

CAVEAT: ``tools/build_2e_db.py`` does ``DROP TABLE IF EXISTS items`` on a full
rebuild, which would remove these scraped rows. Re-run this script after any
such rebuild to restore them (it'll skip whatever the rebuild already covers).

Usage:
    python3 tools/import_wiki_magic_items.py [--dry-run] [--limit N]
                                             [--db PATH] [--verbose]

    --dry-run   parse and report, write nothing
    --limit N   only process the first N category members (smoke test)
    --verbose   print every item as it's inserted/skipped
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://adnd2e.fandom.com/api.php"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO_ROOT / "global" / "2e.db"
_USER_AGENT = "ttrpg2-magic-item-importer/1.0 (campaign toolkit; local use)"

# Scraped rows are assigned ids from this floor upward, kept well clear of the
# 2ERPGDB source ids (~1000–2200) so the two never collide.
_ID_BASE = 900_000
_SOURCE_TAG = "adnd2e.fandom.com"

# Rarity by item kind (parsed from the title's "(Magic X)" suffix / infobox
# type). Mirrors the conventions in build_2e_db.py; anything unrecognised is
# treated as legendary, which suits the Encyclopedia-Magica-heavy wiki set.
_RARITY_BY_KIND = {
    "potion": 15, "scroll": 10, "ring": 45, "wand": 30,
    "rod": 55, "staff": 60,
}
_DEFAULT_RARITY = 65


# --------------------------------------------------------------------------- #
# MediaWiki API helpers
# --------------------------------------------------------------------------- #
def _api_get(params: dict, retries: int = 4) -> dict:
    """GET the API with JSON output, basic retry/backoff, and a User-Agent."""
    params = {**params, "format": "json", "maxlag": "5"}
    url = API + "?" + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 — network is best-effort
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"API request failed after {retries} tries: {last_err}")


def iter_category_members(category: str, sleep: float = 0.2):
    """Yield (pageid, title) for every page in a category, following cmcontinue."""
    cont = None
    while True:
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": f"Category:{category}",
            "cmlimit": "500",
            "cmtype": "page",  # excludes sub-categories and files
        }
        if cont:
            params["cmcontinue"] = cont
        data = _api_get(params)
        for m in data.get("query", {}).get("categorymembers", []):
            yield m["pageid"], m["title"]
        cont = data.get("continue", {}).get("cmcontinue")
        if not cont:
            break
        time.sleep(sleep)


def fetch_wikitext_batch(titles: list[str], sleep: float = 0.2) -> dict[str, str]:
    """Return {title: wikitext} for up to ~50 titles in one request.

    MediaWiki may normalise titles (e.g. spacing); we map normalisations back
    so callers can look up by the title they passed in.
    """
    out: dict[str, str] = {}
    for i in range(0, len(titles), 40):
        chunk = titles[i:i + 40]
        data = _api_get({
            "action": "query",
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "titles": "|".join(chunk),
        })
        query = data.get("query", {})
        # Map normalised titles back to the originals we requested.
        norm = {n["to"]: n["from"] for n in query.get("normalized", [])}
        for page in query.get("pages", {}).values():
            title = page.get("title", "")
            orig = norm.get(title, title)
            revs = page.get("revisions")
            if not revs:
                continue
            text = revs[0].get("slots", {}).get("main", {}).get("*", "")
            out[orig] = text
        time.sleep(sleep)
    return out


# --------------------------------------------------------------------------- #
# Wikitext parsing
# --------------------------------------------------------------------------- #
def _split_top_level_pipes(s: str) -> list[str]:
    """Split on ``|`` that are NOT inside [[...]] or {{...}} or [...]."""
    parts, depth_sq, depth_br, buf = [], 0, 0, []
    i = 0
    while i < len(s):
        two = s[i:i + 2]
        if two == "[[":
            depth_sq += 1; buf.append(two); i += 2; continue
        if two == "]]":
            depth_sq = max(0, depth_sq - 1); buf.append(two); i += 2; continue
        if two == "{{":
            depth_br += 1; buf.append(two); i += 2; continue
        if two == "}}":
            depth_br = max(0, depth_br - 1); buf.append(two); i += 2; continue
        c = s[i]
        if c == "|" and depth_sq == 0 and depth_br == 0:
            parts.append("".join(buf)); buf = []
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _find_item_template(wikitext: str) -> tuple[str | None, int]:
    """Locate the ``{{Item ...}}`` block by brace-matching.

    Returns (inner_text_without_braces, end_index_after_closing_braces) or
    (None, 0) if there's no Item infobox.
    """
    m = re.search(r"\{\{\s*Item\b", wikitext, re.IGNORECASE)
    if not m:
        return None, 0
    start = m.start()
    depth = 0
    i = start
    while i < len(wikitext):
        two = wikitext[i:i + 2]
        if two == "{{":
            depth += 1; i += 2; continue
        if two == "}}":
            depth -= 1; i += 2
            if depth == 0:
                inner = wikitext[start + 2:i - 2]
                return inner, i
        else:
            i += 1
    return None, 0  # unbalanced; treat as no infobox


def parse_infobox(inner: str) -> dict[str, str]:
    """Parse ``{{Item | k = v | ...}}`` inner text into a {key: value} dict."""
    fields: dict[str, str] = {}
    # Drop the leading template name (everything up to the first top-level pipe).
    segs = _split_top_level_pipes(inner)
    for seg in segs[1:]:
        if "=" not in seg:
            continue
        k, _, v = seg.partition("=")
        fields[k.strip().lower()] = v.strip()
    return fields


def clean_markup(text: str) -> str:
    """Reduce wikitext to readable plain text."""
    if not text:
        return ""
    t = text
    t = re.sub(r"<!--.*?-->", "", t, flags=re.DOTALL)          # comments
    t = re.sub(r"<ref[^>]*>.*?</ref>", "", t, flags=re.DOTALL)  # refs
    t = re.sub(r"<ref[^>]*/>", "", t)
    t = re.sub(r"\{\{\s*br\s*\}\}", "\n", t, flags=re.IGNORECASE)  # line breaks
    # [[Page|Label]] -> Label ; [[Page]] -> Page
    t = re.sub(r"\[\[(?:[^\[\]|]*\|)?([^\[\]|]+)\]\]", r"\1", t)
    t = re.sub(r"\{\{[^{}]*\}\}", "", t)        # drop remaining simple templates
    t = re.sub(r"\{\{[^{}]*\}\}", "", t)        # second pass for nested leftovers
    t = re.sub(r"'''([^']+)'''", r"\1", t)      # bold
    t = re.sub(r"''([^']+)''", r"\1", t)        # italic
    t = re.sub(r"^=+\s*(.*?)\s*=+\s*$", r"\1", t, flags=re.MULTILINE)  # headings
    t = re.sub(r"<[^>]+>", "", t)               # stray html
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def kind_from_type(type_text: str, title: str) -> str:
    """Lowercase kind keyword (sword, ring, robe, …) from the infobox type or
    the title's '(Magic X)' suffix — used for rarity + item_type slug."""
    src = (type_text or "").lower()
    if not src:
        m = re.search(r"\(magic\s+([a-z /]+)\)", title.lower())
        src = m.group(1) if m else ""
    src = src.replace("magic", "").strip()
    # First whole word is the kind ("sword", "robe", "instrument", …).
    m = re.search(r"[a-z]+", src)
    return m.group(0) if m else "item"


def _clean_name(title: str) -> str:
    """Title minus a trailing ' (Magic X)' disambiguator."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", title).strip() or title


def _parse_int(s: str) -> int | None:
    if not s:
        return None
    m = re.search(r"[\d,]+", s)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def build_row(title: str, wikitext: str) -> dict | None:
    """Map one wiki page to an items-table row dict, or None to skip (redirect
    / no usable content)."""
    if re.match(r"\s*#redirect", wikitext, re.IGNORECASE):
        return None

    inner, end = _find_item_template(wikitext)
    fields = parse_infobox(inner) if inner else {}

    name = clean_markup(fields.get("name", "")) or _clean_name(title)
    kind = kind_from_type(fields.get("type", ""), title)
    item_type = f"misc_magic_{kind}" if kind else "misc_magic"

    # Description = prose after the infobox (or whole page if none), cleaned and
    # stripped of trailing category/file links.
    body = wikitext[end:] if end else wikitext
    body = re.sub(r"\[\[(?:Category|File|Image):[^\]]*\]\]", "", body,
                  flags=re.IGNORECASE)
    description = clean_markup(body)
    if not description:
        return None  # nothing worth recording

    source = clean_markup(fields.get("source", "")) or _SOURCE_TAG
    source = f"{source} ({_SOURCE_TAG})" if _SOURCE_TAG not in source else source

    return {
        "name": name,
        "item_type": item_type,
        "cost": clean_markup(fields.get("value", "")) or None,
        "weight": clean_markup(fields.get("weight", "")) or None,
        "description": description,
        "source": source,
        "rarity": _RARITY_BY_KIND.get(kind, _DEFAULT_RARITY),
        "xp_value": _parse_int(fields.get("xp", "")),
    }


# --------------------------------------------------------------------------- #
# DB
# --------------------------------------------------------------------------- #
def existing_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0].strip().lower()
            for r in conn.execute("SELECT name FROM items WHERE name IS NOT NULL")}


def next_id(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
    return max((cur or 0) + 1, _ID_BASE)


def insert_row(conn: sqlite3.Connection, item_id: int, row: dict) -> None:
    conn.execute(
        "INSERT INTO items "
        "(id, name, item_type, cost, weight, description, source, rarity, "
        " ac, size, weapon_type, speed, damage_sm, damage_l, rof, "
        " range_s, range_m, range_l, xp_value) "
        "VALUES (?,?,?,?,?,?,?,?, NULL,NULL,NULL,NULL,NULL,NULL,NULL, "
        " NULL,NULL,NULL, ?)",
        (item_id, row["name"], row["item_type"], row["cost"], row["weight"],
         row["description"], row["source"], row["rarity"], row["xp_value"]),
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    ap.add_argument("--limit", type=int, default=0, help="process first N members")
    ap.add_argument("--db", default=str(_DEFAULT_DB), help="path to 2e.db")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"error: db not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        have = existing_names(conn)
        nid = next_id(conn)
        print(f"DB: {db_path} — {len(have)} items already recorded; "
              f"new ids from {nid}.")

        print("Listing category members…")
        members = list(iter_category_members("Magic_Items"))
        if args.limit:
            members = members[:args.limit]
        print(f"{len(members)} member pages to examine.")

        titles = [t for _, t in members]
        seen_run: set[str] = set()
        inserted = skipped_existing = skipped_nodata = 0

        # Fetch wikitext in batches, then process in original order.
        wikitexts: dict[str, str] = {}
        for i in range(0, len(titles), 40):
            batch = titles[i:i + 40]
            wikitexts.update(fetch_wikitext_batch(batch))
            print(f"  fetched {min(i + 40, len(titles))}/{len(titles)} pages…",
                  end="\r", flush=True)
        print()

        for title in titles:
            wt = wikitexts.get(title)
            if wt is None:
                skipped_nodata += 1
                continue
            row = build_row(title, wt)
            if row is None:
                skipped_nodata += 1
                if args.verbose:
                    print(f"  · skip (no data): {title}")
                continue
            key = row["name"].strip().lower()
            if key in have or key in seen_run:
                skipped_existing += 1
                if args.verbose:
                    print(f"  · skip (exists):  {row['name']}")
                continue
            seen_run.add(key)
            if args.dry_run:
                inserted += 1
                if args.verbose:
                    print(f"  + would add [{row['rarity']}] {row['name']} "
                          f"<{row['item_type']}>")
                continue
            insert_row(conn, nid, row)
            nid += 1
            inserted += 1
            if args.verbose:
                print(f"  + added [{row['rarity']}] {row['name']} "
                      f"<{row['item_type']}>")

        if not args.dry_run:
            conn.commit()

        verb = "would insert" if args.dry_run else "inserted"
        print(f"\nDone. {verb} {inserted}; "
              f"skipped {skipped_existing} already-recorded, "
              f"{skipped_nodata} redirects/no-data.")
        if args.dry_run:
            print("(dry run — no changes written)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
