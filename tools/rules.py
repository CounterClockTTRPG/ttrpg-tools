"""Rules text lookup — FTS5 search over PHB / DMG / Monstrous Manual.

Build / rebuild the index with: python3 tools/build_rules_db.py
"""
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
RULES_DB = BASE_DIR / "global" / "rules.db"

SOURCE_LABELS = {
    "phb": "Player's Handbook",
    "dmg": "Dungeon Master Guide",
    "mm": "Monstrous Manual",
    "ct": "Player's Option: Combat & Tactics",
}


def _escape_fts(query: str) -> str:
    """Defang quotes so user input cannot break out of the MATCH expression.
    FTS5 uses double-quotes for phrase queries; doubling them escapes."""
    return query.replace('"', '""')


def register(mcp):

    @mcp.tool()
    def rules_lookup(query: str, source: str = "", limit: int = 5) -> dict:
        """Search the AD&D 2e rules text for an exact phrase or keywords.
        Use this BEFORE narrating a rule the player might dispute, instead of
        recalling from training data — it returns the actual book text.

        query:  FTS5 query. Plain words = AND. Quote a phrase: '"saving throw vs spell"'.
                Wildcards: 'turn*'. Boolean: 'morale OR rally'.
        source: filter — 'phb', 'dmg', 'mm', 'ct' (Combat & Tactics optional rules);
                blank = all sources.
        limit:  max results (default 5, capped at 20).

        Returns a list of {source, chapter, section, excerpt} ranked by relevance."""
        if not RULES_DB.exists():
            return {"error": "rules.db not found. Run: python3 tools/build_rules_db.py"}

        limit = max(1, min(int(limit or 5), 20))
        q = _escape_fts(query.strip())
        if not q:
            return {"error": "Empty query."}

        conn = sqlite3.connect(str(RULES_DB))
        conn.row_factory = sqlite3.Row
        try:
            sql = (
                "SELECT source, chapter, section, "
                "snippet(rules_fts, 3, '<<', '>>', ' … ', 24) AS excerpt "
                "FROM rules_fts WHERE rules_fts MATCH ?"
            )
            params: list = [q]
            if source:
                sql += " AND source = ?"
                params.append(source.lower())
            sql += " ORDER BY rank LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            results = [
                {
                    "source":  r["source"],
                    "label":   SOURCE_LABELS.get(r["source"], r["source"]),
                    "chapter": r["chapter"] or "",
                    "section": r["section"] or "",
                    "excerpt": r["excerpt"],
                }
                for r in rows
            ]
            return {"query": query, "count": len(results), "results": results}
        except sqlite3.OperationalError as exc:
            return {"error": f"FTS query failed: {exc}. Check syntax."}
        finally:
            conn.close()

    @mcp.tool()
    def rules_section(source: str, section: str) -> dict:
        """Return the full text of one rules section (no FTS — exact section title match).
        Use after rules_lookup when you need more than the snippet excerpt.

        source:  'phb', 'dmg', 'mm', or 'ct' (Combat & Tactics)
        section: section heading text (case-insensitive substring match)"""
        if not RULES_DB.exists():
            return {"error": "rules.db not found. Run: python3 tools/build_rules_db.py"}

        conn = sqlite3.connect(str(RULES_DB))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT chapter, section, body FROM rules "
                "WHERE source = ? AND section LIKE ? COLLATE NOCASE LIMIT 1",
                (source.lower(), f"%{section}%"),
            ).fetchone()
            if row is None:
                return {"error": f"No section matching '{section}' in {source}."}
            return {
                "source":  source,
                "chapter": row["chapter"] or "",
                "section": row["section"] or "",
                "body":    row["body"],
            }
        finally:
            conn.close()
