"""Relocate ``<sid>.md`` session exports under the correct
``campaigns/<slug>/_session-logs/<sid>.md`` based on the per-campaign
session manifests.

Handles two scenarios in one pass:
1. The legacy shared dir ``campaigns/_session-logs/`` — drains into
   the right campaign, then removes the empty dir.
2. Reshuffle: a markdown already in ``campaigns/<X>/_session-logs/``
   that the manifest says belongs to campaign Y gets moved to Y.

Idempotent. Files already in the correct dir are skipped."""

from __future__ import annotations

import shutil
from pathlib import Path

from tools import dm_session as _dm
from tools import session_manifest as _smf

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CAMPAIGNS = _REPO_ROOT / "campaigns"
_LEGACY_DIR = _CAMPAIGNS / "_session-logs"


def _slug_for_sid(sid: str) -> str:
    for slug in _smf.list_campaign_slugs():
        if sid in _dm.manifest_sids(slug):
            return slug
    if sid in _dm.manifest_sids(_smf._UNSORTED_SLUG):
        return _smf._UNSORTED_SLUG
    return _smf._UNSORTED_SLUG


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    if _LEGACY_DIR.is_dir():
        dirs.append(_LEGACY_DIR)
    for slug in _smf.list_campaign_slugs() + [_smf._UNSORTED_SLUG]:
        d = _CAMPAIGNS / slug / "_session-logs"
        if d.is_dir():
            dirs.append(d)
    return dirs


def main() -> int:
    moved = 0
    skipped = 0
    for src_dir in _candidate_dirs():
        for md in sorted(src_dir.glob("*.md")):
            sid = md.stem
            slug = _slug_for_sid(sid)
            dst_dir = _CAMPAIGNS / slug / "_session-logs"
            if src_dir == dst_dir:
                continue
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / md.name
            if dst.exists():
                md.unlink()
                skipped += 1
                continue
            shutil.move(str(md), str(dst))
            moved += 1
            print(f"  {sid[:8]}  {src_dir.relative_to(_CAMPAIGNS)}  →  {slug}/_session-logs/")

    if _LEGACY_DIR.is_dir():
        try:
            _LEGACY_DIR.rmdir()
            print("legacy campaigns/_session-logs/ removed (empty)")
        except OSError:
            pass

    print(f"migrated {moved}, skipped {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
