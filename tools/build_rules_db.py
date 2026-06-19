#!/usr/bin/env python3
"""Build a SQLite FTS5 index over the AD&D 2e reference text files.

Reads global/reference/{phb,dmg,monstermanual}.txt, splits by '## ' headings
into sections, stores them in global/rules.db with FTS5 for fast prefix /
phrase search via the rules_lookup MCP tool.

Run after editing reference texts: python3 tools/build_rules_db.py
"""
import re
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
REF_DIR  = BASE_DIR / "global" / "reference"
DB_PATH  = BASE_DIR / "global" / "rules.db"

SOURCES = [
    ("phb",  "Player's Handbook",     REF_DIR / "phb.txt"),
    ("dmg",  "Dungeon Master Guide",  REF_DIR / "dmg.txt"),
    ("mm",   "Monstrous Manual",      REF_DIR / "monstermanual.txt"),
    ("ct",   "Player's Option: Combat & Tactics", REF_DIR / "combat_and_tactics.txt"),
]

# Cap per chunk so a single result excerpt is digestible
MAX_CHUNK_CHARS = 2400


def _chunk(text: str) -> list[tuple[str, str, str]]:
    """Split into (chapter, section, body) tuples by '## ' headings.

    Lines beginning with '## Chapter N:' set chapter context; subsequent
    '## ' headings become sections within that chapter. Long sections are
    split into MAX_CHUNK_CHARS slices on paragraph boundaries.
    """
    lines = text.splitlines(keepends=True)
    chapter = ""
    section = ""
    buf: list[str] = []
    out: list[tuple[str, str, str]] = []

    def _flush():
        if not buf:
            return
        body = "".join(buf).strip()
        if not body:
            return
        # Sub-chunk on paragraph boundaries when too long
        if len(body) <= MAX_CHUNK_CHARS:
            out.append((chapter, section, body))
            return
        paras = re.split(r'\n\s*\n', body)
        cur = ""
        for p in paras:
            if cur and len(cur) + len(p) + 2 > MAX_CHUNK_CHARS:
                out.append((chapter, section, cur.strip()))
                cur = p
            else:
                cur = (cur + "\n\n" + p) if cur else p
        if cur.strip():
            out.append((chapter, section, cur.strip()))

    for ln in lines:
        m = re.match(r'^##\s+(.+?)\s*$', ln)
        if m:
            _flush()
            buf = []
            heading = m.group(1).strip()
            if re.match(r'^Chapter\s+\d+', heading, re.IGNORECASE):
                chapter = heading
                section = heading
            else:
                section = heading
            continue
        buf.append(ln)
    _flush()
    return out


def main():
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
        CREATE TABLE rules (
            id        INTEGER PRIMARY KEY,
            source    TEXT NOT NULL,
            source_label TEXT NOT NULL,
            chapter   TEXT,
            section   TEXT,
            body      TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE rules_fts USING fts5(
            source, chapter, section, body,
            content='rules', content_rowid='id',
            tokenize='porter unicode61'
        );
    """)

    total = 0
    for source, label, path in SOURCES:
        if not path.exists():
            print(f"  skip {source}: {path} not found", file=sys.stderr)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        chunks = _chunk(text)
        for chapter, section, body in chunks:
            cur = conn.execute(
                "INSERT INTO rules(source, source_label, chapter, section, body) VALUES (?,?,?,?,?)",
                (source, label, chapter, section, body),
            )
            rowid = cur.lastrowid
            conn.execute(
                "INSERT INTO rules_fts(rowid, source, chapter, section, body) VALUES (?,?,?,?,?)",
                (rowid, source, chapter, section, body),
            )
        print(f"  {source}: {len(chunks)} sections")
        total += len(chunks)

    conn.commit()
    conn.close()
    print(f"\nWrote {total} sections → {DB_PATH}")


if __name__ == "__main__":
    main()
