"""Export a Claude Code session transcript as a clean markdown narrative log.

Pulls the JSONL transcript Claude Code writes under
``~/.claude/projects/<encoded-cwd>/`` and emits a readable log of the session:
DM (assistant) prose, player (user) turns, and — optionally — tool-call markers.

Usage:
    python3 tools/export_session.py                       # latest session, current project
    python3 tools/export_session.py --list                # show available sessions
    python3 tools/export_session.py --session <id>        # pick a specific session id
    python3 tools/export_session.py --all                 # concatenate every session for this project
    python3 tools/export_session.py -o session.md         # write to file
    python3 tools/export_session.py --include-tools       # show elided tool calls
    python3 tools/export_session.py --dm-only             # narration only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Strip <system-reminder>...</system-reminder>, <command-name>...</command-name>,
# <command-message>...</command-message>, and <local-command-stdout>...</local-command-stdout>
# blocks from user content — they are harness scaffolding, not player input.
_NOISE_TAGS = ("system-reminder", "command-name", "command-message", "command-args",
               "local-command-stdout", "local-command-stderr")
_NOISE_RE = re.compile(
    r"<(" + "|".join(_NOISE_TAGS) + r")>.*?</\1>",
    re.DOTALL,
)


@dataclass
class Turn:
    role: str        # "player" | "dm" | "tool"
    text: str
    timestamp: str | None = None


def encode_cwd(cwd: Path) -> str:
    """Match Claude Code's project-dir encoding: /home/raf/x → -home-raf-x."""
    return str(cwd.resolve()).replace("/", "-")


def find_project_dir(cwd: Path) -> Path:
    encoded = encode_cwd(cwd)
    candidate = PROJECTS_DIR / encoded
    if candidate.is_dir():
        return candidate
    raise SystemExit(f"No transcript directory found for {cwd} (looked for {candidate})")


def list_sessions(project_dir: Path) -> list[Path]:
    return sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def clean_user_text(text: str) -> str:
    cleaned = _NOISE_RE.sub("", text).strip()
    # Drop pure slash-command echoes like "/foo bar" with no other content.
    if cleaned.startswith("/") and "\n" not in cleaned and len(cleaned) < 120:
        return ""
    return cleaned


def summarize_tool_call(block: dict) -> str:
    name = block.get("name", "?")
    inp = block.get("input", {}) or {}
    # Pick the most informative arg for one-line display.
    for key in ("command", "file_path", "query", "skill", "prompt", "description"):
        if key in inp and isinstance(inp[key], str):
            val = inp[key].replace("\n", " ").strip()
            if len(val) > 80:
                val = val[:77] + "..."
            return f"{name}({key}={val!r})"
    if inp:
        first_key = next(iter(inp))
        return f"{name}({first_key}=...)"
    return f"{name}()"


def parse_session(path: Path) -> list[Turn]:
    turns: list[Turn] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = rec.get("type")
            ts = rec.get("timestamp")
            if t == "user":
                content = rec.get("message", {}).get("content", "")
                if isinstance(content, str):
                    cleaned = clean_user_text(content)
                    if cleaned:
                        turns.append(Turn("player", cleaned, ts))
                # tool_result blocks: skip — they are tool output, not player speech.
            elif t == "assistant":
                content = rec.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    btype = block.get("type")
                    if btype == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            turns.append(Turn("dm", text, ts))
                    elif btype == "tool_use":
                        turns.append(Turn("tool", summarize_tool_call(block), ts))
                    # thinking blocks: never export — private chain-of-thought.
    return turns


def render(turns: list[Turn], *, include_tools: bool, dm_only: bool,
           player_only: bool, show_timestamps: bool) -> str:
    out: list[str] = []
    for turn in turns:
        if turn.role == "tool" and not include_tools:
            continue
        if dm_only and turn.role != "dm":
            continue
        if player_only and turn.role != "player":
            continue

        prefix = ""
        if show_timestamps and turn.timestamp:
            try:
                dt = datetime.fromisoformat(turn.timestamp.replace("Z", "+00:00"))
                prefix = f"_[{dt.strftime('%H:%M')}]_ "
            except ValueError:
                pass

        if turn.role == "player":
            out.append(f"### Player\n\n{prefix}{turn.text}\n")
        elif turn.role == "dm":
            heading = "### DM" if not dm_only else ""
            body = f"{prefix}{turn.text}".strip()
            out.append(f"{heading}\n\n{body}\n" if heading else f"{body}\n")
        elif turn.role == "tool":
            out.append(f"_[tool: {turn.text}]_\n")
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=Path, default=Path.cwd(),
                    help="Project working directory (default: cwd)")
    ap.add_argument("--session", help="Specific session id (filename stem); default = latest")
    ap.add_argument("--all", action="store_true", help="Concatenate all sessions for this project, oldest first")
    ap.add_argument("--list", action="store_true", help="List available sessions and exit")
    ap.add_argument("-o", "--output", type=Path, help="Write to file instead of stdout")
    ap.add_argument("--include-tools", action="store_true", help="Include elided tool-call markers")
    ap.add_argument("--dm-only", action="store_true", help="Narration only")
    ap.add_argument("--player-only", action="store_true", help="Player turns only")
    ap.add_argument("--no-timestamps", action="store_true", help="Suppress per-turn timestamps")
    args = ap.parse_args()

    if args.dm_only and args.player_only:
        ap.error("--dm-only and --player-only are mutually exclusive")

    project_dir = find_project_dir(args.project)
    sessions = list_sessions(project_dir)
    if not sessions:
        raise SystemExit(f"No sessions found in {project_dir}")

    if args.list:
        for p in sessions:
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            size_kb = p.stat().st_size / 1024
            print(f"{p.stem}  {mtime}  {size_kb:>7.1f} KB")
        return 0

    if args.all:
        targets = list(reversed(sessions))  # oldest first for chronological log
    elif args.session:
        match = next((p for p in sessions if p.stem == args.session), None)
        if not match:
            raise SystemExit(f"Session {args.session} not found in {project_dir}")
        targets = [match]
    else:
        targets = [sessions[0]]

    chunks: list[str] = []
    for path in targets:
        turns = parse_session(path)
        if not turns:
            continue
        if args.all or len(targets) > 1:
            mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            chunks.append(f"# Session {path.stem[:8]} — {mtime}\n")
        chunks.append(render(
            turns,
            include_tools=args.include_tools,
            dm_only=args.dm_only,
            player_only=args.player_only,
            show_timestamps=not args.no_timestamps,
        ))

    output = "\n".join(chunks)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        print(f"Wrote {len(output):,} chars to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
