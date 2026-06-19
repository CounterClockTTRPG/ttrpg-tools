"""Unit tests for tools.dm_session.

Run: python3 -m unittest tests.test_dm_session
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import dm_session


class DmSessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._session_path = Path(self._tmp.name) / ".dm_session"
        # The parent directory must already exist for _save_session_id to
        # write — it refuses to materialise a campaign-shaped directory
        # that doesn't already exist on disk.
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        self._orig = dm_session._active_session_file
        dm_session._active_session_file = lambda: self._session_path

    def tearDown(self):
        dm_session._active_session_file = self._orig

    def test_session_id_initially_none(self):
        self.assertIsNone(dm_session.session_id())

    def test_init_event_persists_session_id(self):
        evt = dm_session._process_event_line(json.dumps({
            "type": "system",
            "subtype": "init",
            "session_id": "abc-123",
            "tools": [],
        }))
        self.assertEqual(evt["type"], "system")
        self.assertEqual(dm_session.session_id(), "abc-123")

    def test_resume_round_trip(self):
        # First turn: no prior session → no --resume.
        args = dm_session._build_args("hello", dm_session.session_id())
        self.assertNotIn("--resume", args)

        # Simulate the CLI's init event landing.
        dm_session._process_event_line(json.dumps({
            "type": "system", "subtype": "init", "session_id": "sess-xyz",
        }))

        # Second turn: persisted id flows back into args.
        args2 = dm_session._build_args("again", dm_session.session_id())
        self.assertIn("--resume", args2)
        self.assertEqual(args2[args2.index("--resume") + 1], "sess-xyz")

    def test_reset_clears(self):
        dm_session._save_session_id("to-be-removed")
        self.assertEqual(dm_session.session_id(), "to-be-removed")
        dm_session.reset_session()
        self.assertIsNone(dm_session.session_id())
        # Idempotent — second call must not raise.
        dm_session.reset_session()

    def test_blank_line_returns_none(self):
        self.assertIsNone(dm_session._process_event_line(""))
        self.assertIsNone(dm_session._process_event_line("   \n"))

    def test_malformed_json_yields_raw(self):
        evt = dm_session._process_event_line("not json {")
        self.assertEqual(evt["type"], "raw")
        self.assertIn("not json", evt["line"])

    def test_system_event_without_session_id_is_passed_through(self):
        evt = dm_session._process_event_line(json.dumps({
            "type": "system", "subtype": "shutdown",
        }))
        self.assertEqual(evt["subtype"], "shutdown")
        self.assertIsNone(dm_session.session_id())

    def test_build_args_carries_message_and_flags(self):
        args = dm_session._build_args("the party rests", None)
        self.assertEqual(args[0], dm_session._CLAUDE_BIN)
        self.assertIn("-p", args)
        self.assertEqual(args[args.index("-p") + 1], "the party rests")
        self.assertIn("--output-format", args)
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", args)
        self.assertIn("--allowedTools", args)


class LastDmTextTests(unittest.TestCase):
    """Covers the JSONL transcript scanner used by /api/last_narrative."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)

        # Override session-file location so we don't touch the repo's
        # per-campaign .dm_session.
        self._session_path = self.tmpdir / ".dm_session"
        self._orig_active = dm_session._active_session_file
        dm_session._active_session_file = lambda: self._session_path

        # Override the JSONL project-dir lookup to point inside the tmp dir.
        self._orig_project_dir = dm_session._project_jsonl_dir
        proj_dir = self.tmpdir / "transcripts"
        proj_dir.mkdir()
        dm_session._project_jsonl_dir = lambda: proj_dir

    def tearDown(self):
        dm_session._active_session_file = self._orig_active
        dm_session._project_jsonl_dir = self._orig_project_dir

    def test_returns_none_when_no_session(self):
        self.assertIsNone(dm_session.last_dm_text())

    def test_returns_none_when_session_file_missing(self):
        dm_session._save_session_id("ghost-session")
        self.assertIsNone(dm_session.last_dm_text())

    def _write_jsonl(self, sid: str, records: list[dict]):
        p = dm_session._project_jsonl_dir() / f"{sid}.jsonl"
        with p.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        return p

    def test_returns_latest_assistant_text(self):
        sid = "abc-123"
        dm_session._save_session_id(sid)
        self._write_jsonl(sid, [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "First reply."},
            ]}},
            {"type": "user", "message": {"content": "next"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "x"},
                {"type": "text", "text": "Second reply, the latest."},
            ]}},
        ])
        self.assertEqual(dm_session.last_dm_text(), "Second reply, the latest.")

    def test_skips_tool_use_only_assistant_records(self):
        sid = "abc-456"
        dm_session._save_session_id(sid)
        self._write_jsonl(sid, [
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Real prose."},
            ]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "lookup"},
            ]}},
        ])
        self.assertEqual(dm_session.last_dm_text(), "Real prose.")

    def test_returns_none_when_no_assistant_text(self):
        sid = "tools-only"
        dm_session._save_session_id(sid)
        self._write_jsonl(sid, [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "anything"},
            ]}},
        ])
        self.assertIsNone(dm_session.last_dm_text())

    def test_tolerates_malformed_lines(self):
        sid = "bad-jsonl"
        dm_session._save_session_id(sid)
        p = dm_session._project_jsonl_dir() / f"{sid}.jsonl"
        with p.open("w", encoding="utf-8") as f:
            f.write("not json\n")
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Survived."}
            ]}}) + "\n")
            f.write("\n")
        self.assertEqual(dm_session.last_dm_text(), "Survived.")


class PerCampaignSessionTests(unittest.TestCase):
    """Covers the per-campaign session-file behavior — switching the
    active campaign should swap which session resumes on the next turn,
    not wipe the old one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # Lay out a fake repo: .active + campaigns/<slug>/ for two campaigns.
        (self.root / "campaigns" / "alpha").mkdir(parents=True)
        (self.root / "campaigns" / "beta").mkdir(parents=True)
        self._orig_root = dm_session._REPO_ROOT
        dm_session._REPO_ROOT = self.root

    def tearDown(self):
        dm_session._REPO_ROOT = self._orig_root

    def _set_active(self, slug: str) -> None:
        (self.root / ".active").write_text(slug, encoding="utf-8")

    def test_session_id_follows_active_campaign(self):
        self._set_active("alpha")
        dm_session._save_session_id("alpha-sess")
        self._set_active("beta")
        dm_session._save_session_id("beta-sess")

        self._set_active("alpha")
        self.assertEqual(dm_session.session_id(), "alpha-sess")
        self._set_active("beta")
        self.assertEqual(dm_session.session_id(), "beta-sess")

    def test_reset_only_clears_active_campaign(self):
        self._set_active("alpha")
        dm_session._save_session_id("alpha-sess")
        self._set_active("beta")
        dm_session._save_session_id("beta-sess")

        # Reset while beta is active — alpha must survive.
        dm_session.reset_session()
        self.assertIsNone(dm_session.session_id())

        self._set_active("alpha")
        self.assertEqual(dm_session.session_id(), "alpha-sess")

    def test_save_refuses_missing_campaign_dir(self):
        self._set_active("ghost-not-on-disk")
        dm_session._save_session_id("phantom")
        # Should NOT have created campaigns/ghost-not-on-disk/.dm_session
        self.assertFalse((self.root / "campaigns" / "ghost-not-on-disk").exists())

    def test_session_id_none_when_no_active_campaign(self):
        # No .active file exists — session_id should be None, not raise.
        self.assertIsNone(dm_session.session_id())

    def test_migrate_legacy_folds_into_active_campaign(self):
        self._set_active("alpha")
        legacy = self.root / ".dm_session"
        legacy.write_text("legacy-sess", encoding="utf-8")
        # Re-point the module constant at our test root's legacy file so
        # the migration touches the fake repo, not the real one.
        orig_legacy = dm_session._LEGACY_SESSION_FILE
        dm_session._LEGACY_SESSION_FILE = legacy
        try:
            dm_session._migrate_legacy_session()
        finally:
            dm_session._LEGACY_SESSION_FILE = orig_legacy

        self.assertFalse(legacy.exists())
        self.assertEqual(dm_session.session_id(), "legacy-sess")

    def test_migrate_legacy_preserves_existing_per_campaign_file(self):
        self._set_active("alpha")
        dm_session._save_session_id("already-saved")
        legacy = self.root / ".dm_session"
        legacy.write_text("legacy-sess", encoding="utf-8")
        orig_legacy = dm_session._LEGACY_SESSION_FILE
        dm_session._LEGACY_SESSION_FILE = legacy
        try:
            dm_session._migrate_legacy_session()
        finally:
            dm_session._LEGACY_SESSION_FILE = orig_legacy

        # Existing per-campaign value wins; legacy is still cleaned up.
        self.assertFalse(legacy.exists())
        self.assertEqual(dm_session.session_id(), "already-saved")


if __name__ == "__main__":
    unittest.main()
