"""Lore facts — the "did you know" trivia store shared by the dashboard and MCP.

A single SQLite DB (global/lore_facts.db) holds short, interesting facts shown
on the /play page during the wait for the DM's reply. This module is the one
source of truth for the schema and CRUD, used by:

  * dashboard.py  — the /api/facts REST endpoints and the /play rotation
  * the MCP tools registered below (add/list/update/delete + campaign helper)

Categories are free-form, but the curated set uses: greyhawk, monster, rules,
spell, magic, history, planes, class, and **campaign**. Campaign facts record
fun facts & achievements from ongoing campaigns; their ``campaign`` column
carries the campaign name so it can be shown alongside the fact.
"""
import sqlite3
import threading
from pathlib import Path

import _campaign as _c

BASE_DIR = Path(__file__).parent.parent
LORE_DB = BASE_DIR / "global" / "lore_facts.db"

# Serialises writes within a single process; SQLite file locking (plus the
# busy_timeout below) handles the dashboard and MCP server writing concurrently.
_lock = threading.Lock()
_ensured = False

# Base seed used ONLY when the DB is empty (fresh install). After first run the
# DB is the source of truth; additions go through the CRUD functions / MCP tools,
# not here.
SEED: list[tuple[str, str]] = [
    ("Iuz, the cambion demigod who rules a cruel empire north of the Vesve Forest, is the half-fiend son of the witch-queen Iggwilv and the demon lord Graz'zt.", "greyhawk"),
    ("Vecna rose from mortal lich to god of secrets. Of his first form only two relics remain — the Hand and the Eye of Vecna — each granting terrible power at terrible cost.", "greyhawk"),
    ("The Circle of Eight, founded by the archmage Mordenkainen, works to keep any single power from dominating the Flanaess — balancing good and evil alike.", "greyhawk"),
    ("Many famous spells are named for the archmages of Oerth: Bigby's crushing hands, Tenser's floating disc, Otiluke's resilient sphere, Mordenkainen's faithful hound.", "greyhawk"),
    ("The City of Greyhawk is called the Gem of the Flanaess, a free city of guilds, thieves, and scholars at the crossroads of the central lands.", "greyhawk"),
    ("A mind flayer (illithid) devours the brains of its prey and can stun a whole party with a cone of psionic force — its mind blast.", "monster"),
    ("A lich's life is bound to its phylactery. Destroy the body and it reforms in days — to truly end a lich you must find and destroy that hidden vessel.", "monster"),
    ("Adventurers fear the lowly rust monster more than dragons: a single touch corrodes enchanted armour and weapons into worthless flakes.", "monster"),
    ("THAC0 means 'To Hit Armor Class 0' — subtract your target's AC from it to find the roll you need. In 2e, lower Armor Class is better.", "rules"),
    ("A natural 20 always hits and a natural 1 always misses, no matter the numbers — the dice can always surprise you.", "rules"),
    ("At 0 hit points a character falls unconscious; at -10 they die. The grim few points between are where heroes are made or lost.", "rules"),
]


def _ensure(conn: sqlite3.Connection) -> None:
    """Create the table on first use, migrate in the ``campaign`` column for
    older DBs, and seed when empty. Idempotent; runs once per process."""
    global _ensured
    if _ensured:
        return
    with _lock:
        if _ensured:
            return
        conn.execute(
            "CREATE TABLE IF NOT EXISTS facts ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  text TEXT NOT NULL,"
            "  category TEXT NOT NULL DEFAULT '',"
            "  source TEXT NOT NULL DEFAULT '',"
            "  campaign TEXT NOT NULL DEFAULT '',"
            "  enabled INTEGER NOT NULL DEFAULT 1,"
            "  created_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(facts)")}
        if "campaign" not in cols:
            conn.execute("ALTER TABLE facts ADD COLUMN campaign TEXT NOT NULL DEFAULT ''")
        if conn.execute("SELECT COUNT(*) AS c FROM facts").fetchone()["c"] == 0:
            conn.executemany(
                "INSERT INTO facts (text, category, source) VALUES (?, ?, 'seed')",
                SEED,
            )
        conn.commit()
        _ensured = True


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(LORE_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 3000")
    _ensure(conn)
    return conn


def _row(r: sqlite3.Row) -> dict:
    keys = r.keys()
    return {
        "id": r["id"],
        "text": r["text"],
        "category": r["category"],
        "source": r["source"],
        "campaign": r["campaign"] if "campaign" in keys else "",
        "enabled": bool(r["enabled"]),
        "created_at": r["created_at"],
    }


def _active_campaign_name() -> str:
    """Best-effort name of the active campaign, for campaign-fact tagging."""
    try:
        cfg = _c.load_campaign()
        return (cfg.get("name") or cfg.get("_name") or "").strip()
    except Exception:
        return ""


# ── CRUD (used by both the dashboard endpoints and the MCP tools) ───────────

def list_facts(category=None, enabled=None, campaign=None, limit=None) -> list[dict]:
    conn = _connect()
    try:
        clauses, params = [], []
        if category:
            clauses.append("category = ?"); params.append(category)
        if enabled is not None:
            clauses.append("enabled = ?"); params.append(1 if enabled else 0)
        if campaign:
            clauses.append("campaign = ?"); params.append(campaign)
        q = "SELECT * FROM facts"
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY id"
        if limit:
            q += " LIMIT ?"; params.append(int(limit))
        return [_row(r) for r in conn.execute(q, params)]
    finally:
        conn.close()


def random_facts(n=1, category=None, campaign=None) -> list[dict]:
    conn = _connect()
    try:
        try:
            n = max(1, min(100, int(n)))
        except (TypeError, ValueError):
            n = 1
        clauses, params = ["enabled = 1"], []
        if category:
            clauses.append("category = ?"); params.append(category)
        if campaign:
            clauses.append("campaign = ?"); params.append(campaign)
        q = ("SELECT * FROM facts WHERE " + " AND ".join(clauses)
             + " ORDER BY RANDOM() LIMIT ?")
        params.append(n)
        return [_row(r) for r in conn.execute(q, params)]
    finally:
        conn.close()


def create_fact(text, category="", source="", campaign="") -> dict:
    text = (text or "").strip()
    if not text:
        return {"error": "text is required"}
    conn = _connect()
    try:
        with _lock:
            cur = conn.execute(
                "INSERT INTO facts (text, category, source, campaign) VALUES (?, ?, ?, ?)",
                (text, (category or "").strip(), (source or "").strip(), (campaign or "").strip()),
            )
            conn.commit()
            return {"ok": True, "id": cur.lastrowid}
    finally:
        conn.close()


def update_fact(fid, **fields) -> dict:
    sets, params = [], []
    for col in ("text", "category", "source", "campaign"):
        if col in fields and fields[col] is not None:
            val = str(fields[col]).strip()
            if col == "text" and not val:
                return {"error": "text cannot be empty"}
            sets.append(f"{col} = ?"); params.append(val)
    if fields.get("enabled") is not None:
        sets.append("enabled = ?"); params.append(1 if fields["enabled"] else 0)
    if not sets:
        return {"error": "no updatable fields provided"}
    params.append(fid)
    conn = _connect()
    try:
        with _lock:
            cur = conn.execute(f"UPDATE facts SET {', '.join(sets)} WHERE id = ?", params)
            conn.commit()
        if cur.rowcount == 0:
            return {"error": f"no fact {fid}"}
        return {"ok": True, "id": fid}
    finally:
        conn.close()


def delete_fact(fid) -> dict:
    conn = _connect()
    try:
        with _lock:
            cur = conn.execute("DELETE FROM facts WHERE id = ?", (fid,))
            conn.commit()
        if cur.rowcount == 0:
            return {"error": f"no fact {fid}"}
        return {"ok": True, "id": fid}
    finally:
        conn.close()


def counts_by_category() -> dict:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT category, COUNT(*) AS c FROM facts GROUP BY category ORDER BY c DESC"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS c FROM facts").fetchone()["c"]
        return {"total": total, "by_category": {r["category"]: r["c"] for r in rows}}
    finally:
        conn.close()


# ── MCP tools ───────────────────────────────────────────────────────────────

def register(mcp):

    @mcp.tool()
    def add_lore_fact(text: str, category: str = "", source: str = "") -> dict:
        """Add a 'did you know' lore fact to the shared trivia pool shown on the
        dashboard /play page while the DM composes a reply.

        Keep it short (one or two sentences) and genuinely interesting.
        category: free-form, but the curated set uses greyhawk, monster, rules,
                  spell, magic, history, planes, class. Use add_campaign_fact()
                  for campaign achievements instead of this.
        Returns {ok, id} or {error}."""
        return create_fact(text, category=category, source=source)

    @mcp.tool()
    def add_campaign_fact(text: str, campaign: str = "") -> dict:
        """Record a fun fact or achievement from an ongoing campaign — a great
        victory, a memorable death, a clever escape, a milestone reached. These
        join the 'did you know' rotation tagged with the campaign name, so
        players see highlights from current and past campaigns between turns.

        text:     the fact/achievement, phrased so it reads well standalone.
        campaign: campaign name to attribute it to; defaults to the active
                  campaign. Stored in the 'campaign' category.
        Returns {ok, id} or {error}."""
        name = (campaign or "").strip() or _active_campaign_name()
        return create_fact(text, category="campaign", source="campaign", campaign=name)

    @mcp.tool()
    def list_lore_facts(category: str = "", campaign: str = "", limit: int = 50) -> dict:
        """List lore facts, optionally filtered by category (e.g. 'campaign',
        'monster') and/or campaign name. Returns {facts, count}."""
        rows = list_facts(category=category or None,
                          campaign=campaign or None, limit=limit or None)
        return {"facts": rows, "count": len(rows)}

    @mcp.tool()
    def random_lore_facts(n: int = 3, category: str = "") -> dict:
        """Draw n random enabled lore facts (max 100), optionally from one
        category. Returns {facts, count}."""
        rows = random_facts(n=n, category=category or None)
        return {"facts": rows, "count": len(rows)}

    @mcp.tool()
    def update_lore_fact(fact_id: int, text: str = "", category: str = "",
                         source: str = "", campaign: str = "", enabled: int = -1) -> dict:
        """Edit an existing lore fact. Only non-empty arguments are applied;
        pass enabled=0 to hide a fact from the rotation or enabled=1 to show it.
        Returns {ok, id} or {error}."""
        fields: dict = {}
        if text:
            fields["text"] = text
        if category:
            fields["category"] = category
        if source:
            fields["source"] = source
        if campaign:
            fields["campaign"] = campaign
        if enabled in (0, 1):
            fields["enabled"] = enabled
        if not fields:
            return {"error": "nothing to update (pass at least one field)"}
        return update_fact(fact_id, **fields)

    @mcp.tool()
    def delete_lore_fact(fact_id: int) -> dict:
        """Permanently delete a lore fact by id. Returns {ok, id} or {error}."""
        return delete_fact(fact_id)
