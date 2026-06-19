"""SessionEnd hook entry point.

Claude Code invokes this with a JSON payload on stdin
(``{"session_id": "..."}``). We resolve the active campaign from
``.active``, export the session transcript to markdown in that
campaign's ``_session-logs/`` directory, and best-effort ensure the
session id is recorded in that campaign's manifest.

If no campaign is active we fall back to the legacy shared dir
``campaigns/_session-logs/`` so transcripts are never silently dropped."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _active_campaign_slug() -> str | None:
    try:
        slug = (_REPO_ROOT / ".active").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return slug or None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    sid = (payload or {}).get("session_id")
    if not sid:
        return 0

    slug = _active_campaign_slug()
    if slug and (_REPO_ROOT / "campaigns" / slug).is_dir():
        out_dir = _REPO_ROOT / "campaigns" / slug / "_session-logs"
    else:
        out_dir = _REPO_ROOT / "campaigns" / "_session-logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{sid}.md"

    try:
        subprocess.run(
            [sys.executable, "tools/export_session.py",
             "--session", sid, "-o", str(out_path)],
            cwd=str(_REPO_ROOT),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=25,
        )
    except (OSError, subprocess.SubprocessError):
        pass

    if slug:
        try:
            from tools import dm_session as _dm
            _dm.record_session_in_manifest(slug, sid)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
