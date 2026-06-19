#!/usr/bin/env python3
"""Import creatures from the AD&D 2e Fandom wiki into global/monsters.db.

Scans https://adnd2e.fandom.com/wiki/Category:Creatures, parses each page's
``{{Creature}}`` infobox + description, and inserts any creature NOT already in
the monsters table (dedup by case-insensitive name). For each newly imported
creature it also downloads the wiki infobox image (if present) into
``global/monsters/<slug>.<ext>`` — the filename convention the dashboard's
``/monsters/portraits/<filename>`` route already discovers.

monsters.db has no builder script (it's hand-maintained), so direct inserts are
safe. Scraped rows get ids from a dedicated high range (>= _ID_BASE) and a
source footer in the description, so they're easy to spot or re-import.

Usage:
    python3 tools/import_wiki_monsters.py [--dry-run] [--limit N]
        [--no-images] [--db PATH] [--img-dir PATH] [--verbose]
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
_DEFAULT_DB = _REPO_ROOT / "global" / "monsters.db"
_DEFAULT_IMG_DIR = _REPO_ROOT / "global" / "monsters"
_USER_AGENT = "ttrpg2-monster-importer/1.0 (campaign toolkit; local use)"
_SOURCE_TAG = "adnd2e.fandom.com"
_ID_BASE = 900_000
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# {{Creature}} infobox field -> monsters.db column.
_FIELD_MAP = {
    "frequency": "frequency",
    "numberappearing": "no_appearing",
    "armorclass": "armor_class",
    "movement": "move",
    "hitdice": "hit_dice",
    "thac0": "thac0",
    "treasure": "treasure_type",
    "noofattacks": "no_of_attacks",
    "damageattack": "damage_attack",
    "specialattack": "special_attacks",
    "specialdefenses": "special_defenses",
    "magicalresistance": "magic_resistance",
    "intelligence": "intelligence",
    "alignment": "alignment",
    "size": "size",
    "moral": "morale",
    "xp": "xp_value",
    "terrain": "climate_terrain",
}
# DB columns the wiki infobox doesn't provide.
_NULL_COLUMNS = ("pct_in_lair", "psionic_ability", "attack_defense_modes")
_ALL_COLUMNS = (
    "id", "name", "frequency", "no_appearing", "armor_class", "move",
    "hit_dice", "thac0", "pct_in_lair", "treasure_type", "no_of_attacks",
    "damage_attack", "special_attacks", "special_defenses", "magic_resistance",
    "intelligence", "alignment", "size", "psionic_ability",
    "attack_defense_modes", "description", "climate_terrain", "morale",
    "xp_value", "categories", "source",
)


# --------------------------------------------------------------------------- #
# MediaWiki API
# --------------------------------------------------------------------------- #
def _api_get(params: dict, retries: int = 4) -> dict:
    params = {**params, "format": "json", "maxlag": "5"}
    url = API + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"API failed after {retries} tries: {last}")


def iter_category_members(category: str, sleep: float = 0.2):
    cont = None
    while True:
        params = {
            "action": "query", "list": "categorymembers",
            "cmtitle": f"Category:{category}", "cmlimit": "500", "cmtype": "page",
        }
        if cont:
            params["cmcontinue"] = cont
        data = _api_get(params)
        for m in data.get("query", {}).get("categorymembers", []):
            yield m["title"]
        cont = data.get("continue", {}).get("cmcontinue")
        if not cont:
            break
        time.sleep(sleep)


def fetch_wikitext_batch(titles: list[str], sleep: float = 0.2) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(titles), 40):
        data = _api_get({
            "action": "query", "prop": "revisions", "rvprop": "content",
            "rvslots": "main", "titles": "|".join(titles[i:i + 40]),
        })
        query = data.get("query", {})
        norm = {n["to"]: n["from"] for n in query.get("normalized", [])}
        for page in query.get("pages", {}).values():
            title = page.get("title", "")
            revs = page.get("revisions")
            if revs:
                out[norm.get(title, title)] = \
                    revs[0].get("slots", {}).get("main", {}).get("*", "")
        time.sleep(sleep)
    return out


def fetch_image_urls(filenames: list[str], sleep: float = 0.2) -> dict[str, str]:
    """Resolve {wiki image filename -> direct download URL} via imageinfo."""
    out: dict[str, str] = {}
    uniq = sorted({f for f in filenames if f})
    for i in range(0, len(uniq), 40):
        chunk = uniq[i:i + 40]
        data = _api_get({
            "action": "query", "prop": "imageinfo", "iiprop": "url",
            "titles": "|".join(f"File:{f}" for f in chunk),
        })
        query = data.get("query", {})
        norm = {n["to"]: n["from"] for n in query.get("normalized", [])}
        for page in query.get("pages", {}).values():
            title = page.get("title", "")           # e.g. "File:Aboleth 2e.png"
            ii = page.get("imageinfo")
            if not ii:
                continue
            orig = norm.get(title, title)
            fname = orig.split(":", 1)[1] if ":" in orig else orig
            out[fname] = ii[0]["url"]
        time.sleep(sleep)
    return out


def download_image(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        if not data:
            return False
        dest.write_bytes(data)
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Wikitext parsing (shared style with import_wiki_magic_items.py)
# --------------------------------------------------------------------------- #
def _split_top_level_pipes(s: str) -> list[str]:
    parts, dsq, dbr, buf, i = [], 0, 0, [], 0
    while i < len(s):
        two = s[i:i + 2]
        if two == "[[":
            dsq += 1; buf.append(two); i += 2; continue
        if two == "]]":
            dsq = max(0, dsq - 1); buf.append(two); i += 2; continue
        if two == "{{":
            dbr += 1; buf.append(two); i += 2; continue
        if two == "}}":
            dbr = max(0, dbr - 1); buf.append(two); i += 2; continue
        c = s[i]
        if c == "|" and dsq == 0 and dbr == 0:
            parts.append("".join(buf)); buf = []
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _find_template(wikitext: str, name: str) -> tuple[str | None, int]:
    m = re.search(r"\{\{\s*" + name + r"\b", wikitext, re.IGNORECASE)
    if not m:
        return None, 0
    start, depth, i = m.start(), 0, m.start()
    while i < len(wikitext):
        two = wikitext[i:i + 2]
        if two == "{{":
            depth += 1; i += 2
        elif two == "}}":
            depth -= 1; i += 2
            if depth == 0:
                return wikitext[start + 2:i - 2], i
        else:
            i += 1
    return None, 0


def parse_infobox(inner: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for seg in _split_top_level_pipes(inner)[1:]:
        if "=" in seg:
            k, _, v = seg.partition("=")
            fields[k.strip().lower()] = v.strip()
    return fields


def clean_markup(text: str) -> str:
    if not text:
        return ""
    t = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    t = re.sub(r"<ref[^>]*>.*?</ref>", "", t, flags=re.DOTALL)
    t = re.sub(r"<ref[^>]*/>", "", t)
    t = re.sub(r"\{\{\s*br\s*\}\}", "\n", t, flags=re.IGNORECASE)
    t = re.sub(r"\[\[(?:[^\[\]|]*\|)?([^\[\]|]+)\]\]", r"\1", t)
    t = re.sub(r"\{\{[^{}]*\}\}", "", t)
    t = re.sub(r"\{\{[^{}]*\}\}", "", t)
    t = re.sub(r"'''([^']+)'''", r"\1", t)
    t = re.sub(r"''([^']+)''", r"\1", t)
    t = re.sub(r"^=+\s*(.*?)\s*=+\s*$", r"\1", t, flags=re.MULTILINE)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def monster_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def split_metadata(description: str) -> tuple[str, list[str], str]:
    """Split a wiki-imported description into (body, categories, source).

    Imported descriptions carry a trailing metadata block: zero or more
    ``Category:<name>`` lines followed by a (possibly multi-line) ``Source: ...``
    footer ending in the wiki domain tag. This peels that block off the end:

    - ``source``     — text after ``Source:`` with newlines collapsed to spaces
                       ('' if no footer).
    - ``categories`` — de-duplicated category names with the ``Category:`` prefix
                       stripped, in document order ([] if none).
    - ``body``       — the description with the metadata block removed.

    Only the *trailing* contiguous run of category lines is treated as metadata,
    so stray ``Category:`` lines embedded mid-body are left untouched.
    """
    if not description:
        return (description or "", [], "")
    lines = description.split("\n")

    # 1. Pull off the Source: footer — the last 'Source:' line to end-of-text
    #    (the source string may wrap across several following lines).
    source = ""
    src_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("Source:"):
            src_idx = i
    if src_idx is not None:
        parts = [lines[src_idx][len("Source:"):].strip()]
        parts += [l.strip() for l in lines[src_idx + 1:]]
        source = " ".join(p for p in parts if p).strip()
        lines = lines[:src_idx]

    # 2. Strip the trailing contiguous run of Category:/blank lines.
    cats_rev: list[str] = []
    while lines:
        s = lines[-1].strip()
        if not s:
            lines.pop()
            continue
        if s.startswith("Category:"):
            cats_rev.append(s[len("Category:"):].strip())
            lines.pop()
            continue
        break

    seen: set[str] = set()
    categories: list[str] = []
    for c in reversed(cats_rev):
        if c and c not in seen:
            seen.add(c)
            categories.append(c)

    body = "\n".join(lines).rstrip()
    return body, categories, source


def _parse_thac0(v: str):
    m = re.search(r"\d+", v or "")
    return int(m.group(0)) if m else None


def build_row(title: str, wikitext: str) -> dict | None:
    """Map a wiki page to a monsters-row dict (+ '_image' filename), or None to
    skip (redirect / not a creature infobox / no content)."""
    if re.match(r"\s*#redirect", wikitext, re.IGNORECASE):
        return None
    inner, end = _find_template(wikitext, "Creature")
    if inner is None:
        return None
    fields = parse_infobox(inner)

    name = (clean_markup(fields.get("name", "")) or title).strip().lower()
    if not name:
        return None
    # Skip overview/index pages ("X, general information") — not real stat blocks.
    if ("general information" in name or name.endswith(", general")
            or name.endswith(" (general)") or "overview" in name):
        return None

    row = {c: None for c in _ALL_COLUMNS}
    for wiki_key, col in _FIELD_MAP.items():
        # Multi-variant infoboxes (Badger Common/Giant, Aartuk Warrior/Elder, …)
        # suffix their stat fields with a variant number — ``hitdice1``,
        # ``thac01``, ``xp1`` — and leave the unsuffixed key empty. Fall back to
        # variant 1 as the canonical row when the base key is missing/blank.
        raw = fields.get(wiki_key)
        if raw is None or raw.strip() == "":
            raw = fields.get(wiki_key + "1")
        if raw is not None and raw.strip() != "":
            val = clean_markup(raw)
            row[col] = _parse_thac0(val) if col == "thac0" else (val or None)

    raw_body = clean_markup(wikitext[end:] if end else wikitext)
    body, categories, _ = split_metadata(raw_body)
    src = clean_markup(fields.get("source", ""))
    row["description"] = body or None
    row["categories"] = json.dumps(categories) if categories else None
    row["source"] = f"{src} ({_SOURCE_TAG})" if src else _SOURCE_TAG
    row["name"] = name

    img = fields.get("image", "").strip()
    img = re.sub(r"\[\[(?:File|Image):", "", img, flags=re.IGNORECASE).strip("[]| ")
    img = img.split("|")[0].strip() if img else ""
    row["_image"] = img or None
    return row


# --------------------------------------------------------------------------- #
# DB
# --------------------------------------------------------------------------- #
def existing_names(conn) -> set[str]:
    return {r[0].strip().lower()
            for r in conn.execute("SELECT name FROM monsters WHERE name IS NOT NULL")}


def next_id(conn) -> int:
    cur = conn.execute("SELECT MAX(id) FROM monsters").fetchone()[0]
    return max((cur or 0) + 1, _ID_BASE)


def insert_row(conn, item_id: int, row: dict) -> None:
    cols = ", ".join(_ALL_COLUMNS)
    qs = ", ".join("?" for _ in _ALL_COLUMNS)
    vals = [item_id if c == "id" else row.get(c) for c in _ALL_COLUMNS]
    conn.execute(f"INSERT INTO monsters ({cols}) VALUES ({qs})", vals)


# --------------------------------------------------------------------------- #
# Backfill: re-parse the wiki and fill EMPTY stat columns on existing rows.
# --------------------------------------------------------------------------- #
# Columns the backfill is allowed to fill: exactly the ones the {{Creature}}
# infobox provides. Never touches id/name/description/categories/source/image,
# nor _NULL_COLUMNS (which the wiki never supplies, so counting them as "gaps"
# would mark every row gappy).
_BACKFILL_COLUMNS = tuple(_FIELD_MAP.values())


def _is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def backfill(conn, args) -> int:
    """Re-list Category:Creatures, re-parse each page with the (now
    variant-aware) parser, and UPDATE only the empty stat columns of the
    matching existing row. Matched by the same name-derivation the import uses,
    so no title-casing guesswork. Never clobbers a non-empty value."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM monsters").fetchall()
    # rows that have at least one empty stat column worth filling
    gappy = {
        r["name"].strip().lower(): r
        for r in rows
        if any(_is_empty(r[c]) for c in _BACKFILL_COLUMNS if c in r.keys())
    }
    print(f"{len(gappy)} existing rows have ≥1 empty stat column.")

    print("Listing Category:Creatures members…")
    titles = list(iter_category_members("Creatures"))
    if args.limit:
        titles = titles[:args.limit]
    print(f"{len(titles)} member pages to re-parse.")

    wikitexts: dict[str, str] = {}
    for i in range(0, len(titles), 40):
        wikitexts.update(fetch_wikitext_batch(titles[i:i + 40]))
        print(f"  fetched {min(i + 40, len(titles))}/{len(titles)} pages…",
              end="\r", flush=True)
    print()

    # Build a parsed row per wiki page, keyed by canonical name.
    parsed: dict[str, dict] = {}
    for title in titles:
        wt = wikitexts.get(title)
        row = build_row(title, wt) if wt else None
        if row is not None:
            parsed.setdefault(row["name"], row)

    matched = updated = fields_filled = 0
    unmatched: list[str] = []
    fill_tally: dict[str, int] = {}
    for name, dbrow in gappy.items():
        src = parsed.get(name)
        if src is None:
            unmatched.append(name)
            continue
        matched += 1
        sets: dict[str, object] = {}
        for col in _BACKFILL_COLUMNS:
            if col not in dbrow.keys():
                continue
            if _is_empty(dbrow[col]) and not _is_empty(src.get(col)):
                sets[col] = src[col]
        if sets:
            conn.execute(
                f"UPDATE monsters SET {', '.join(c + '=?' for c in sets)} "
                "WHERE id=?",
                list(sets.values()) + [dbrow["id"]],
            )
            updated += 1
            fields_filled += len(sets)
            for c in sets:
                fill_tally[c] = fill_tally.get(c, 0) + 1
            if args.verbose:
                print(f"  ~ {name}: filled {', '.join(sorted(sets))}")

    if not args.dry_run:
        conn.commit()

    verb = "would fill" if args.dry_run else "filled"
    print(f"\nMatched {matched}/{len(gappy)} gappy rows to a wiki page; "
          f"{verb} {fields_filled} fields across {updated} rows.")
    print("Fields filled by column:")
    for c, n in sorted(fill_tally.items(), key=lambda x: -x[1]):
        print(f"  {n:4}  {c}")
    if unmatched:
        print(f"\n{len(unmatched)} gappy rows had no matching Creatures page "
              f"(umbrella/index entries with no single stat block), e.g.:")
        for n in unmatched[:20]:
            print(f"  - {n}")
    if args.dry_run:
        print("(dry run — nothing written)")
    return 0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backfill", action="store_true",
                    help="Fill empty stat columns on existing rows instead of "
                         "inserting new creatures (variant-aware re-parse).")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--db", default=str(_DEFAULT_DB))
    ap.add_argument("--img-dir", default=str(_DEFAULT_IMG_DIR))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    db_path, img_dir = Path(args.db), Path(args.img_dir)
    if not db_path.exists():
        print(f"error: monsters db not found: {db_path}", file=sys.stderr)
        return 2
    if not args.dry_run and not args.no_images:
        img_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        if args.backfill:
            return backfill(conn, args)

        have = existing_names(conn)
        nid = next_id(conn)
        print(f"DB: {db_path} — {len(have)} creatures recorded; new ids from {nid}.")

        print("Listing Category:Creatures members…")
        titles = list(iter_category_members("Creatures"))
        if args.limit:
            titles = titles[:args.limit]
        print(f"{len(titles)} member pages to examine.")

        wikitexts: dict[str, str] = {}
        for i in range(0, len(titles), 40):
            wikitexts.update(fetch_wikitext_batch(titles[i:i + 40]))
            print(f"  fetched {min(i + 40, len(titles))}/{len(titles)} pages…",
                  end="\r", flush=True)
        print()

        # Build rows for creatures not already recorded.
        new_rows: list[dict] = []
        seen: set[str] = set()
        skipped_existing = skipped_nodata = 0
        for title in titles:
            wt = wikitexts.get(title)
            row = build_row(title, wt) if wt else None
            if row is None:
                skipped_nodata += 1
                continue
            key = row["name"]
            if key in have or key in seen:
                skipped_existing += 1
                continue
            seen.add(key)
            new_rows.append(row)

        print(f"{len(new_rows)} new creatures (skipped {skipped_existing} "
              f"already-recorded, {skipped_nodata} redirects/non-creature).")

        # Resolve image URLs in bulk.
        url_map: dict[str, str] = {}
        if not args.no_images:
            wanted = [r["_image"] for r in new_rows if r.get("_image")]
            print(f"Resolving {len(set(filter(None, wanted)))} image URLs…")
            if not args.dry_run:
                url_map = fetch_image_urls(wanted)

        inserted = images = 0
        for row in new_rows:
            if args.dry_run:
                inserted += 1
                if args.verbose:
                    print(f"  + {row['name']}  (HD {row['hit_dice']}, "
                          f"img={'yes' if row.get('_image') else 'no'})")
                continue
            insert_row(conn, nid, row)
            nid += 1
            inserted += 1
            # Download image into <slug>.<ext>.
            img = row.get("_image")
            if img and not args.no_images:
                ext = Path(img).suffix.lower()
                if ext in _IMG_EXTS and img in url_map:
                    dest = img_dir / f"{monster_slug(row['name'])}{ext}"
                    if not dest.exists() and download_image(url_map[img], dest):
                        images += 1
            if args.verbose and inserted % 50 == 0:
                print(f"  …{inserted} inserted", end="\r", flush=True)

        if not args.dry_run:
            conn.commit()

        verb = "would insert" if args.dry_run else "inserted"
        print(f"\nDone. {verb} {inserted} creatures; downloaded {images} images.")
        if args.dry_run:
            print("(dry run — nothing written)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
