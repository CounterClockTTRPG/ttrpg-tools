"""Greyhawk setting lookup — wraps settings/greyhawk/greyhawk.db.

The DB is a scraped/structured snapshot of the Greyhawk wiki. It holds a
free-text `pages` table (FTS5-indexed as `pages_fts`), a `categories` tag
table, a `redirects` table, and typed entry tables for the major entity
kinds: characters, settlements, deities, creatures, realms, archfiends,
locations, organizations, creators, items, planes, holidays, buildings.

Three MCP tools are exposed:

  greyhawk_metadata()                 — what's in the DB (categories, fields)
  greyhawk_category(name, limit?)     — list entries of one kind
  greyhawk_lookup(page)               — full page text + typed fields
  greyhawk_search(query, limit?)      — FTS over page text

Read-only. The DB is shipped with the repo and not mutated at runtime.
"""
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
GREYHAWK_DB = BASE_DIR / "settings" / "greyhawk" / "greyhawk.db"

# Typed entry tables in the order we want to surface them. The `pages`,
# `categories`, `links`, `redirects` and FTS shadow tables are infrastructure
# and are excluded from the "entry categories" listing.
ENTRY_TABLES = [
    "characters", "settlements", "deities", "creatures", "realms",
    "archfiends", "locations", "organizations", "creators", "items",
    "planes", "holidays", "buildings",
]


def _escape_fts(query: str) -> str:
    """Defang quotes so user input cannot break out of the MATCH expression."""
    return query.replace('"', '""')


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(GREYHAWK_DB))
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_redirect(conn: sqlite3.Connection, title: str) -> str:
    """Follow a redirect chain (max 5 hops) and return the canonical title.
    If no redirect exists, returns the input unchanged."""
    seen = {title}
    cur = title
    for _ in range(5):
        row = conn.execute(
            "SELECT redirect_target FROM pages WHERE title = ? AND is_redirect = 1",
            (cur,),
        ).fetchone()
        if not row or not row["redirect_target"]:
            return cur
        cur = row["redirect_target"]
        if cur in seen:
            return cur
        seen.add(cur)
    return cur


def register(mcp):

    @mcp.tool()
    def greyhawk_metadata() -> dict:
        """Describe what's in the Greyhawk DB.

        Returns:
          - entry_categories: list of typed tables (deities, characters, …)
            with row counts and column names. These are the structured
            entity tables — use greyhawk_category(name) to list their rows.
          - wiki_categories: top wiki tag-categories by member count
            (e.g. 'Human characters', 'Settlements', 'Wizards'). These are
            free-form tags assigned to pages and are not the same as the
            typed tables. Use greyhawk_category(name) for those too.
          - page_count: total pages (excluding redirects).
        """
        if not GREYHAWK_DB.exists():
            return {"error": f"greyhawk.db not found at {GREYHAWK_DB}"}

        conn = _connect()
        try:
            entry_categories = []
            for t in ENTRY_TABLES:
                try:
                    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({t})")]
                    n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    entry_categories.append({"name": t, "count": n, "fields": cols})
                except sqlite3.OperationalError:
                    continue

            wiki = conn.execute(
                "SELECT category, COUNT(*) AS n FROM categories "
                "GROUP BY category ORDER BY n DESC LIMIT 40"
            ).fetchall()
            wiki_categories = [{"name": r["category"], "count": r["n"]} for r in wiki]
            wiki_total = conn.execute(
                "SELECT COUNT(DISTINCT category) FROM categories"
            ).fetchone()[0]

            page_count = conn.execute(
                "SELECT COUNT(*) FROM pages WHERE is_redirect = 0"
            ).fetchone()[0]

            return {
                "page_count": page_count,
                "entry_categories": entry_categories,
                "wiki_categories": wiki_categories,
                "wiki_category_count": wiki_total,
            }
        finally:
            conn.close()

    @mcp.tool()
    def greyhawk_category(name: str, limit: int = 50) -> dict:
        """List entries in a Greyhawk category.

        `name` matches either a typed entry table (e.g. 'deities', 'realms')
        or a wiki tag-category (e.g. 'Human characters', 'Wizards' —
        case-insensitive substring match). Typed tables return their full
        structured rows; wiki categories return page titles.

        limit: max rows (default 50, capped at 500).
        """
        if not GREYHAWK_DB.exists():
            return {"error": f"greyhawk.db not found at {GREYHAWK_DB}"}

        limit = max(1, min(int(limit or 50), 500))
        key = (name or "").strip()
        if not key:
            return {"error": "Empty category name."}

        conn = _connect()
        try:
            if key.lower() in ENTRY_TABLES:
                table = key.lower()
                rows = conn.execute(
                    f"SELECT * FROM {table} ORDER BY page LIMIT ?",
                    (limit,),
                ).fetchall()
                total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                return {
                    "kind": "entry_table",
                    "name": table,
                    "total": total,
                    "returned": len(rows),
                    "entries": [dict(r) for r in rows],
                }

            cat_row = conn.execute(
                "SELECT category FROM categories WHERE category = ? COLLATE NOCASE LIMIT 1",
                (key,),
            ).fetchone()
            if cat_row is None:
                like = conn.execute(
                    "SELECT category, COUNT(*) AS n FROM categories "
                    "WHERE category LIKE ? COLLATE NOCASE "
                    "GROUP BY category ORDER BY n DESC LIMIT 10",
                    (f"%{key}%",),
                ).fetchall()
                if not like:
                    return {"error": f"No category matching '{name}'.",
                            "hint": "Call greyhawk_metadata() for valid names."}
                return {"error": f"No exact match for '{name}'.",
                        "candidates": [{"name": r["category"], "count": r["n"]} for r in like]}

            cat = cat_row["category"]
            rows = conn.execute(
                "SELECT page FROM categories WHERE category = ? ORDER BY page LIMIT ?",
                (cat, limit),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM categories WHERE category = ?", (cat,)
            ).fetchone()[0]
            return {
                "kind": "wiki_category",
                "name": cat,
                "total": total,
                "returned": len(rows),
                "pages": [r["page"] for r in rows],
            }
        finally:
            conn.close()

    @mcp.tool()
    def greyhawk_lookup(page: str) -> dict:
        """Fetch a single Greyhawk page by title (case-insensitive).

        Resolves redirects automatically. Returns:
          - title, type, raw_text   (page body — wiki markup)
          - categories              (wiki tags)
          - typed                   (mapping of entry-table-name → row, if
                                     the page also has a structured record)
          - redirected_from         (if input differed from canonical title)

        If no exact title match, returns up to 10 fuzzy candidates.
        """
        if not GREYHAWK_DB.exists():
            return {"error": f"greyhawk.db not found at {GREYHAWK_DB}"}

        key = (page or "").strip()
        if not key:
            return {"error": "Empty page title."}

        conn = _connect()
        try:
            row = conn.execute(
                "SELECT title FROM pages WHERE title = ? COLLATE NOCASE LIMIT 1",
                (key,),
            ).fetchone()
            if row is None:
                like = conn.execute(
                    "SELECT title FROM pages WHERE title LIKE ? COLLATE NOCASE "
                    "AND is_redirect = 0 ORDER BY length(title) LIMIT 10",
                    (f"%{key}%",),
                ).fetchall()
                return {"error": f"No page matching '{page}'.",
                        "candidates": [r["title"] for r in like]}

            original = row["title"]
            canonical = _resolve_redirect(conn, original)
            page_row = conn.execute(
                "SELECT title, type, raw_text FROM pages WHERE title = ?",
                (canonical,),
            ).fetchone()
            if page_row is None:
                return {"error": f"Redirect target '{canonical}' not found."}

            cats = [
                r["category"]
                for r in conn.execute(
                    "SELECT category FROM categories WHERE page = ? ORDER BY category",
                    (canonical,),
                )
            ]

            typed = {}
            for t in ENTRY_TABLES:
                tr = conn.execute(
                    f"SELECT * FROM {t} WHERE page = ?", (canonical,)
                ).fetchone()
                if tr:
                    typed[t] = dict(tr)

            result = {
                "title":     page_row["title"],
                "type":      page_row["type"],
                "raw_text":  page_row["raw_text"],
                "categories": cats,
                "typed":     typed,
            }
            if canonical != original:
                result["redirected_from"] = original
            return result
        finally:
            conn.close()

    @mcp.tool()
    def greyhawk_search(query: str, limit: int = 10) -> dict:
        """Full-text search over Greyhawk pages.

        query: FTS5 query. Plain words = AND. Quote a phrase:
               '"Circle of Eight"'. Wildcards: 'Morden*'. Boolean: 'a OR b'.
        limit: max results (default 10, capped at 50).

        Returns ranked {title, type, excerpt} hits. Use greyhawk_lookup() to
        fetch a full page after picking from the list.
        """
        if not GREYHAWK_DB.exists():
            return {"error": f"greyhawk.db not found at {GREYHAWK_DB}"}

        limit = max(1, min(int(limit or 10), 50))
        q = _escape_fts((query or "").strip())
        if not q:
            return {"error": "Empty query."}

        conn = _connect()
        try:
            sql = (
                "SELECT p.title, p.type, "
                "snippet(pages_fts, 1, '<<', '>>', ' … ', 16) AS excerpt "
                "FROM pages_fts "
                "JOIN pages p ON p.title = pages_fts.title "
                "WHERE pages_fts MATCH ? AND p.is_redirect = 0 "
                "ORDER BY rank LIMIT ?"
            )
            try:
                rows = conn.execute(sql, (q, limit)).fetchall()
            except sqlite3.OperationalError as exc:
                return {"error": f"FTS query failed: {exc}. Check syntax."}

            return {
                "query": query,
                "count": len(rows),
                "results": [
                    {"title": r["title"], "type": r["type"] or "", "excerpt": r["excerpt"]}
                    for r in rows
                ],
            }
        finally:
            conn.close()
