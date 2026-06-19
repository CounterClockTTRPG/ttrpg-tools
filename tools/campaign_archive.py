"""Export and import a campaign as a single tarball.

Bundles ``campaigns/<slug>/`` and nothing else — no ``global/`` overrides,
no repo-level pointers like ``.active`` or ``.dm_session``. Round-trips
cleanly between machines as long as both repos share the same ``global/``
data (any homebrew classes or item-rarity tweaks live in the destination's
global DB and are NOT carried by this archive).

CLI::

    python3 -m tools.campaign_archive export <slug> [-o out.tgz]
    python3 -m tools.campaign_archive import <archive.tgz> [--rename NEW] [--force]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CAMPAIGNS = _REPO_ROOT / "campaigns"

# Files we never want inside an export — backup files and Python build
# artefacts have no place in a portable archive.
_SKIP_NAMES = {"__pycache__"}
_SKIP_SUFFIX_PATTERNS = (
    re.compile(r"\.bak(\.\d+)?$"),
    re.compile(r"\.bak\.\d{8}_\d{6}$"),
)
_VALID_SLUG = re.compile(r"^[a-z0-9][a-z0-9\-_]*$")


def _should_skip(path: Path) -> bool:
    name = path.name
    if name in _SKIP_NAMES:
        return True
    for pat in _SKIP_SUFFIX_PATTERNS:
        if pat.search(name):
            return True
    return False


def export_campaign(slug: str, out_path: Path | None = None) -> Path:
    """Tar-gzip ``campaigns/<slug>/`` into ``out_path`` (default
    ``./<slug>.tgz`` in the caller's cwd). Returns the resulting archive
    path. Raises ``FileNotFoundError`` if the slug isn't a directory."""
    src = _CAMPAIGNS / slug
    if not src.is_dir():
        raise FileNotFoundError(f"campaign not found: {src}")
    if out_path is None:
        out_path = Path.cwd() / f"{slug}.tgz"
    out_path = out_path.resolve()

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        rel = Path(info.name)
        for part in rel.parts:
            if part in _SKIP_NAMES:
                return None
        if _should_skip(Path(info.name)):
            return None
        return info

    with tarfile.open(out_path, "w:gz") as tar:
        tar.add(src, arcname=slug, filter=_filter)
    return out_path


def _archive_root_slug(tar: tarfile.TarFile) -> str:
    """Return the single top-level directory name in the archive, or
    raise ``ValueError`` if the layout isn't ``<slug>/...``."""
    roots: set[str] = set()
    for m in tar.getmembers():
        # Defence against absolute paths or parent traversal — also caught
        # by the safer extract path below, but reject up-front so we never
        # accept a malformed archive even by accident.
        if m.name.startswith("/") or ".." in Path(m.name).parts:
            raise ValueError(f"unsafe path in archive: {m.name}")
        first = Path(m.name).parts[0]
        roots.add(first)
    if len(roots) != 1:
        raise ValueError(
            f"archive must have a single top-level directory, found {sorted(roots)}"
        )
    return roots.pop()


def import_campaign(
    archive_path: Path,
    rename: str | None = None,
    force: bool = False,
) -> str:
    """Extract ``archive_path`` into ``campaigns/``. Returns the slug the
    campaign was imported as.

    Raises ``FileExistsError`` if the destination slug exists and ``force``
    is False. Raises ``ValueError`` for malformed archives or invalid
    rename slugs."""
    archive_path = Path(archive_path).resolve()
    with tarfile.open(archive_path, "r:gz") as tar:
        archive_slug = _archive_root_slug(tar)
        target_slug = rename or archive_slug
        if not _VALID_SLUG.match(target_slug):
            raise ValueError(
                f"invalid slug {target_slug!r} — use lowercase letters, "
                f"digits, '-', '_' only"
            )
        dest = _CAMPAIGNS / target_slug
        if dest.exists() and not force:
            raise FileExistsError(f"campaign already exists: {dest}")

        with tempfile.TemporaryDirectory(dir=_CAMPAIGNS, prefix=".import-") as tmp:
            tmp_path = Path(tmp)
            # ``filter='data'`` (Python 3.12+) blocks absolute paths, parent
            # traversal, device files, symlinks pointing outside, and
            # setuid/setgid bits — safer than the legacy default.
            try:
                tar.extractall(tmp_path, filter="data")
            except TypeError:
                tar.extractall(tmp_path)
            extracted = tmp_path / archive_slug
            if not extracted.is_dir():
                raise ValueError(
                    f"archive root {archive_slug!r} did not extract to a directory"
                )
            campaign_json = extracted / "campaign.json"
            if not campaign_json.is_file():
                raise ValueError("archive does not contain campaign.json at its root")

            if rename and rename != archive_slug:
                _rewrite_campaign_name(campaign_json, rename)

            if dest.exists() and force:
                import shutil
                shutil.rmtree(dest)
            extracted.rename(dest)
    return target_slug


def _rewrite_campaign_name(campaign_json: Path, new_name: str) -> None:
    """Patch the ``name`` field inside ``campaign.json`` to match the new
    slug after a rename. Leaves the file untouched if it can't be parsed —
    a corrupt name field is better than a corrupt JSON file."""
    try:
        data = json.loads(campaign_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(data, dict):
        data["name"] = new_name
        campaign_json.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def list_summaries() -> list[dict]:
    """Return one summary dict per campaign on disk, suitable for the
    dashboard's /campaigns card grid. Sorted by ``last_played`` descending
    so the most recently touched campaign sits first."""
    if not _CAMPAIGNS.exists():
        return []
    active = _active_slug()
    out: list[dict] = []
    for camp_json in sorted(_CAMPAIGNS.glob("*/campaign.json")):
        slug = camp_json.parent.name
        try:
            data = json.loads(camp_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        camp_dir = camp_json.parent
        try:
            created = data.get("created_at") or _iso(camp_json.stat().st_ctime)
        except OSError:
            created = ""
        state_path = camp_dir / "state.json"
        try:
            last = _iso(state_path.stat().st_mtime) if state_path.exists() \
                else _iso(camp_json.stat().st_mtime)
        except OSError:
            last = ""
        banner_field = (data.get("banner") or "").strip()
        banner_url = f"/campaigns/{slug}/banner" if banner_field else ""
        out.append({
            "slug": slug,
            "name": data.get("name") or slug,
            "system": data.get("system", ""),
            "world": data.get("world", ""),
            "tone": data.get("tone", ""),
            "status": data.get("status", "active"),
            "closed_at": data.get("closed_at", ""),
            "closed_reason": data.get("closed_reason", ""),
            "created_at": created,
            "last_played": last,
            "banner_url": banner_url,
            "active": slug == active,
            "size_bytes": _dir_size(camp_dir),
        })
    out.sort(key=lambda c: c["last_played"], reverse=True)
    return out


def banner_path(slug: str) -> Path | None:
    """Return the absolute path to the campaign's banner image, or
    ``None`` if the campaign or its banner field isn't set up. Used by
    the dashboard route that serves the image."""
    camp_json = _CAMPAIGNS / slug / "campaign.json"
    if not camp_json.is_file():
        return None
    try:
        data = json.loads(camp_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    rel = (data.get("banner") or "").strip()
    if not rel:
        return None
    # Banner path is stored repo-relative under the campaign directory;
    # resolve and verify it stays inside that directory (defence against a
    # tampered campaign.json pointing outside the campaign).
    camp_dir = (_CAMPAIGNS / slug).resolve()
    candidate = (camp_dir / rel).resolve()
    try:
        candidate.relative_to(camp_dir)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def set_banner(slug: str, image_bytes: bytes, suffix: str) -> str:
    """Write ``image_bytes`` to ``campaigns/<slug>/images/banner<suffix>``
    and update ``campaign.json.banner`` to that relative path. ``suffix``
    must start with ``.`` and be a common image extension. Returns the
    relative path stored in campaign.json."""
    suffix = suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        raise ValueError(f"unsupported image extension: {suffix}")
    camp_dir = _CAMPAIGNS / slug
    camp_json = camp_dir / "campaign.json"
    if not camp_json.is_file():
        raise FileNotFoundError(f"campaign not found: {slug}")
    images_dir = camp_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    # Wipe any previous banner files so we don't accumulate stale variants
    # under different extensions.
    for old in images_dir.glob("banner.*"):
        try:
            old.unlink()
        except OSError:
            pass
    rel = f"images/banner{suffix}"
    (camp_dir / rel).write_bytes(image_bytes)
    data = json.loads(camp_json.read_text(encoding="utf-8"))
    data["banner"] = rel
    camp_json.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return rel


def delete_campaign(slug: str) -> None:
    """Remove ``campaigns/<slug>/`` entirely. Caller must guard against
    deleting the active campaign."""
    if not _VALID_SLUG.match(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    camp_dir = _CAMPAIGNS / slug
    if not camp_dir.is_dir():
        raise FileNotFoundError(f"campaign not found: {slug}")
    import shutil
    shutil.rmtree(camp_dir)


def _active_slug() -> str:
    f = _REPO_ROOT / ".active"
    try:
        return f.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _dir_size(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="campaign_archive")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export", help="bundle a campaign into a .tgz")
    p_exp.add_argument("slug")
    p_exp.add_argument("-o", "--output", type=Path, default=None,
                       help="output path (default: ./<slug>.tgz)")

    p_imp = sub.add_parser("import", help="extract a .tgz into campaigns/")
    p_imp.add_argument("archive", type=Path)
    p_imp.add_argument("--rename", default=None,
                       help="import under a different slug")
    p_imp.add_argument("--force", action="store_true",
                       help="overwrite an existing campaign with the same slug")

    args = parser.parse_args(argv)
    if args.cmd == "export":
        out = export_campaign(args.slug, args.output)
        print(out)
        return 0
    if args.cmd == "import":
        slug = import_campaign(args.archive, rename=args.rename, force=args.force)
        print(slug)
        return 0
    return 2


if __name__ == "__main__":
    try:
        sys.exit(_cli(sys.argv[1:]))
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
