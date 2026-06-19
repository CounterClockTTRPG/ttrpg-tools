"""Fantasy-themed random word generator.

Backs the `random_words` MCP tool and a standalone CLI:

    python3 tools/wordlist.py 10              # 10 mixed words
    python3 tools/wordlist.py 5 noun          # 5 nouns
    python3 tools/wordlist.py 20 verb,adj     # 20 verbs+adjectives mixed
    python3 tools/wordlist.py --stats         # show pool sizes

Source: global/reference/fantasy_words.json (~2000 curated words across
noun / verb / adjective, screened for pre-industrial fantasy use — no
telephones, engines, computers, etc.).
"""
import json
import random
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
WORDLIST_PATH = BASE_DIR / "global" / "reference" / "fantasy_words.json"

KINDS = ("noun", "verb", "adjective")

# alias → canonical kind
_ALIASES = {
    "noun": "noun", "nouns": "noun", "n": "noun",
    "verb": "verb", "verbs": "verb", "v": "verb",
    "adj":  "adjective", "adjs": "adjective", "adjective": "adjective",
    "adjectives": "adjective", "a": "adjective",
}

_cache: dict | None = None


def _load() -> dict:
    """Lazy-load and cache the JSON wordlist."""
    global _cache
    if _cache is None:
        with WORDLIST_PATH.open() as f:
            data = json.load(f)
        _cache = {k: list(data.get(k, [])) for k in KINDS}
    return _cache


def _resolve_kinds(kind: str) -> list[str] | None:
    """Parse a `kind` selector. Empty/'any'/'all' = every category.
    Comma-separated list (e.g. 'noun,verb') = those categories. Returns
    None for an unknown token (caller turns that into an error)."""
    raw = (kind or "").strip().lower()
    if raw in ("", "any", "all", "mixed", "*"):
        return list(KINDS)
    out: list[str] = []
    seen: set[str] = set()
    for tok in (t.strip() for t in raw.split(",") if t.strip()):
        canon = _ALIASES.get(tok)
        if canon is None:
            return None
        if canon not in seen:
            seen.add(canon); out.append(canon)
    return out


def pick(count: int, kind: str = "", rng: random.Random | None = None) -> dict:
    """Return up to `count` random distinct words.

    count: target sample size; clamped to [1, pool_size].
    kind:  '' / 'any' for all categories, otherwise a comma-separated list
           of 'noun', 'verb', 'adjective' (or aliases n/v/a/adj).
    rng:   inject a Random for deterministic tests; defaults to module random.

    Returns {kind, count, words}; on bad input returns {error: ...}."""
    kinds = _resolve_kinds(kind)
    if kinds is None:
        return {
            "error": f"Unknown kind '{kind}'. Use any of: noun, verb, "
                     f"adjective (or leave blank for mixed). Comma-separate "
                     f"for multiple categories.",
        }

    try:
        count = int(count)
    except (TypeError, ValueError):
        return {"error": f"count must be an integer (got {count!r})"}
    if count < 1:
        return {"error": f"count must be >= 1 (got {count})"}

    data = _load()
    pool = [w for k in kinds for w in data[k]]
    n = min(count, len(pool))

    r = rng or random
    words = r.sample(pool, n)

    return {
        "kind":  ",".join(kinds) if kinds != list(KINDS) else "any",
        "count": n,
        "words": words,
    }


def stats() -> dict:
    """Return per-kind pool sizes for diagnostics."""
    data = _load()
    counts = {k: len(data[k]) for k in KINDS}
    counts["total"] = sum(counts.values())
    return counts


def register(mcp):

    @mcp.tool()
    def random_words(count: int = 10, kind: str = "") -> dict:
        """Draw `count` random fantasy-appropriate words from the curated
        ~2000-word dictionary at global/reference/fantasy_words.json.

        Useful for: seeding NPC nicknames, scroll text fragments, prophecy
        phrasing, codeword tokens, password puzzles, mnemonic devices,
        random magic-item adjective+noun rolls.

        count: number of distinct words to return (clamped to pool size).
        kind:  '' or 'any' = mixed across all categories. Or pass a
               comma-separated list of 'noun' / 'verb' / 'adjective'
               (aliases: n, v, a, adj, nouns, verbs, adjectives).
               Examples: 'noun', 'noun,adjective', 'verb'.

        The dictionary excludes modern/industrial vocabulary (engine,
        telephone, plastic, computer, etc.) so results stay in-genre."""
        return pick(count, kind)


# ─── CLI ──────────────────────────────────────────────────────────────────
def _main(argv: list[str]) -> int:
    import sys

    if "--stats" in argv or "-s" in argv:
        s = stats()
        print(f"noun:      {s['noun']:>4}")
        print(f"verb:      {s['verb']:>4}")
        print(f"adjective: {s['adjective']:>4}")
        print(f"total:     {s['total']:>4}")
        return 0

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    try:
        count = int(argv[0])
    except ValueError:
        print(f"error: count must be an integer (got {argv[0]!r})",
              file=sys.stderr)
        return 2

    kind = argv[1] if len(argv) > 1 else ""
    result = pick(count, kind)
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        return 2

    print(" ".join(result["words"]))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
