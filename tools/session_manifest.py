"""Per-campaign session manifests + a one-shot backfill for the historical
pool of Claude Code JSONLs.

Claude Code writes every session transcript into
``~/.claude/projects/<encoded-cwd>/<sid>.jsonl`` — a flat pool with no
campaign attribution. To present a clean per-campaign session history in
the dashboard we maintain ``campaigns/<slug>/sessions.json`` (forward
tracking lives in ``dm_session._save_session_id``) and, for sessions
already in the pool, this module attributes them by scanning each JSONL
for ``campaigns/<slug>/`` path fragments and crediting the slug with the
highest mention count.

Run as a script for a one-shot backfill::

    python3 -m tools.session_manifest --backfill
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from tools import dm_session as _dm
from tools import export_session as _export

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CAMPAIGNS_DIR = _REPO_ROOT / "campaigns"
_UNSORTED_SLUG = "_unsorted"


def list_campaign_slugs() -> list[str]:
    """Real campaign slugs (subdirs under campaigns/, excluding _-prefixed)."""
    if not _CAMPAIGNS_DIR.is_dir():
        return []
    return sorted(
        d.name for d in _CAMPAIGNS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    )


def _attribute_jsonl(path: Path, slugs: list[str],
                     name_to_slug: dict[str, str]) -> str | None:
    """Scan one JSONL and return the most-mentioned campaign slug.

    Two signals are counted into the same tally:
    - ``campaigns/<slug>/`` path fragments in any tool result or argument.
    - ``mcp__ttrpg__switch_campaign`` calls whose ``input.name`` matches a
      known slug or its display name.

    Returns ``None`` if no slug matches."""
    if not slugs:
        return None
    path_re = re.compile(r"campaigns/(" + "|".join(re.escape(s) for s in slugs) + r")/")
    switch_re = re.compile(
        r'"name"\s*:\s*"mcp__ttrpg__switch_campaign"\s*,\s*"input"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]+)"'
    )
    counts: Counter[str] = Counter()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                for m in path_re.finditer(line):
                    counts[m.group(1)] += 1
                for m in switch_re.finditer(line):
                    target = m.group(1)
                    slug = name_to_slug.get(target.lower())
                    if slug:
                        # Weight switch_campaign heavily — it's an explicit
                        # selection rather than an incidental path mention.
                        counts[slug] += 5
    except OSError:
        return None
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _campaign_name_index(slugs: list[str]) -> dict[str, str]:
    """Lowercase display-name → slug, plus lowercase slug → slug."""
    idx: dict[str, str] = {}
    for slug in slugs:
        idx[slug.lower()] = slug
        camp_json = _CAMPAIGNS_DIR / slug / "campaign.json"
        try:
            data = json.loads(camp_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = data.get("name")
        if isinstance(name, str) and name:
            idx[name.lower()] = slug
    return idx


def _jsonl_created_at(path: Path) -> str:
    """ISO timestamp of the JSONL's mtime — used when we have no better
    creation signal during backfill."""
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def known_sids() -> set[str]:
    """Sids already attributed to any campaign manifest (or unsorted)."""
    seen: set[str] = set()
    for slug in list_campaign_slugs() + [_UNSORTED_SLUG]:
        for sid in _dm.manifest_sids(slug):
            seen.add(sid)
    return seen


def _active_sid_overrides() -> dict[str, str]:
    """Map sid → slug for every campaign's current ``.dm_session`` file.

    This is the strongest possible attribution signal: a sid that a
    campaign is actively resuming definitionally belongs to that
    campaign, even if its transcript text references other slugs."""
    out: dict[str, str] = {}
    for slug in list_campaign_slugs():
        p = _CAMPAIGNS_DIR / slug / ".dm_session"
        try:
            sid = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if sid:
            out[sid] = slug
    return out


def backfill(*, verbose: bool = False) -> dict:
    """Attribute every JSONL in the pool to a campaign manifest.

    Sessions already present in any manifest are skipped. Unattributable
    sessions land in ``campaigns/_unsorted/sessions.json``. Returns a
    summary dict suitable for printing or JSON-encoding."""
    pool = _dm._project_jsonl_dir()
    if not pool.is_dir():
        return {"scanned": 0, "attributed": {}, "unsorted": 0, "skipped": 0}

    slugs = list_campaign_slugs()
    name_index = _campaign_name_index(slugs)
    (_CAMPAIGNS_DIR / _UNSORTED_SLUG).mkdir(parents=True, exist_ok=True)
    already = known_sids()
    active_overrides = _active_sid_overrides()

    attributed: Counter[str] = Counter()
    unsorted = 0
    skipped = 0
    scanned = 0
    for path in sorted(pool.glob("*.jsonl")):
        sid = path.stem
        scanned += 1
        if sid in already:
            skipped += 1
            continue
        if sid in active_overrides:
            slug = active_overrides[sid]
        else:
            slug = _attribute_jsonl(path, slugs, name_index) or _UNSORTED_SLUG
        _dm.record_session_in_manifest(slug, sid, created_at=_jsonl_created_at(path))
        if slug == _UNSORTED_SLUG:
            unsorted += 1
        else:
            attributed[slug] += 1
        if verbose:
            print(f"{sid[:8]}  →  {slug}")
    return {
        "scanned": scanned,
        "attributed": dict(attributed),
        "unsorted": unsorted,
        "skipped": skipped,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backfill", action="store_true",
                    help="Scan the pool and attribute unknown sessions to manifests.")
    ap.add_argument("--reset", action="store_true",
                    help="Wipe every campaigns/<slug>/sessions.json before backfilling.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.reset:
        for slug in list_campaign_slugs() + [_UNSORTED_SLUG]:
            p = _CAMPAIGNS_DIR / slug / "sessions.json"
            if p.is_file():
                p.unlink()
    if args.backfill:
        summary = backfill(verbose=args.verbose)
        print(json.dumps(summary, indent=2))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
