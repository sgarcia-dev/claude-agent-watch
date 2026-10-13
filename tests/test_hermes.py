"""Hermes reader tests using only synthetic stores."""

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from probe import common, hermes


SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT, cwd TEXT, started_at REAL,
    ended_at REAL, last_activity_at REAL, end_reason TEXT, model TEXT,
    model_config TEXT, billing_base_url TEXT, parent_session_id TEXT,
    message_count INTEGER, tool_call_count INTEGER, api_call_count INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
    reasoning_tokens INTEGER, last_activity_description TEXT
);
CREATE INDEX sessions_started ON sessions(started_at);
CREATE INDEX sessions_parent ON sessions(parent_session_id);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
    timestamp REAL, active INTEGER DEFAULT 1
);
CREATE INDEX messages_session ON messages(session_id, timestamp);
CREATE TABLE async_delegations (
    delegation_id TEXT PRIMARY KEY, origin_session TEXT,
    parent_session_id TEXT, state TEXT
);
"""


class HermesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        env = patch.dict(os.environ, {"AGENT_WATCH_HERMES_HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.database = self.create_store(self.root)
        self.run = {"id": "run-test", "tool": "hermes", "startedAt": 1_000_000,
                    "cwd": str(self.cwd), "sessionId": None, "claimed": []}

    def create_store(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        database = folder / "state.db"
        with sqlite3.connect(database) as conn:
            conn.executescript(SCHEMA)
        return database

    def session(self, session_id="session-test", database=None, **values):
        data = {"id": session_id, "source": "oneshot", "cwd": str(self.cwd),
                "started_at": 1000.0, "ended_at": None, "end_reason": None,
                "model": "synthetic-model", "billing_base_url": "https://example.invalid/v1",
                "model_config": json.dumps({"reasoning_config": {"effort": "high"},
                                            "_usage_anchor": {"prompt_tokens": 321}}),
                "parent_session_id": None, "api_call_count": 7,
                "input_tokens": 900, "output_tokens": 80,
                "cache_read_tokens": 200, "reasoning_tokens": 30,
                "last_activity_at": 1001.125,
                "last_activity_description": "exec: synthetic check\ncomplete"}
        data.update(values)
        with sqlite3.connect(database or self.database) as conn:
            conn.execute("INSERT INTO sessions (" + ",".join(data) + ") VALUES (" +
                         ",".join("?" for _ in data) + ")", list(data.values()))

    def message(self, text, timestamp, role="assistant", active=1):
        with sqlite3.connect(self.database) as conn:
            conn.execute("INSERT INTO messages (session_id,role,content,timestamp,active) "
                         "VALUES (?,?,?,?,?)", ("session-test", role, text, timestamp, active))

    def probe(self, **changes):
        return hermes.probe({**self.run, **changes}, 1_100_000)

    def test_running_fields_and_total_tokens(self):
        self.session()
        result = self.probe()
        self.assertEqual(set(result), {"id", *common.FIELDS})
        self.assertTrue(result["found"])
        self.assertEqual(result["sessionId"], "session-test")
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["model"], "synthetic-model")
        self.assertEqual(result["effort"], "high")
        self.assertEqual(result["contextUsed"], 321)
        self.assertEqual(result["inputTokens"], 900)
        self.assertEqual(result["outputTokens"], 80)
        self.assertEqual(result["steps"], 7)
        self.assertEqual(result["lastActivityAt"], 1_001_125)
        self.assertEqual(result["lastActivity"], "exec: synthetic check complete")
        self.assertEqual(result["children"], 0)
        for key in ("progress", "contextWindow", "endedAt", "error"):
            self.assertIsNone(result[key])

    def test_window_boundaries_and_earliest(self):
        self.session("too-early", started_at=994.999)
        self.session("lower-bound", started_at=995.0)
        self.session("later", started_at=1001.0)
        self.session("upper-bound", started_at=1120.0)
        self.assertEqual(self.probe()["sessionId"], "lower-bound")
        self.assertEqual(self.probe(claimed=["lower-bound"])["sessionId"], "later")
        result = self.probe(claimed=["lower-bound", "later"])
        self.assertFalse(result["found"])

    def test_cwd_realpath_and_mismatch(self):
        other = self.root / "other"
        other.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(self.cwd, target_is_directory=True)
        self.session("wrong", started_at=995.0, cwd=str(other))
        self.session("right", started_at=1000.0, cwd=str(alias))
        self.assertEqual(self.probe()["sessionId"], "right")
        # With the matching folder claimed, the time window decides.
        self.assertEqual(self.probe(claimed=["right"])["sessionId"], "wrong")

    def test_matching_cwd_beats_an_earlier_session_without_one(self):
        self.session("no-cwd", started_at=995.0, cwd=None)
        self.session("with-cwd", started_at=1000.0)
        self.assertEqual(self.probe()["sessionId"], "with-cwd")

    def test_missing_run_cwd_falls_back_to_time_window(self):
        self.session()
        self.assertTrue(self.probe(cwd=None)["found"])

    def test_session_after_the_run_ended_is_not_linked(self):
        self.session(started_at=1060.0)
        self.assertFalse(self.probe(endedBy=1_002_000)["found"])
        self.assertTrue(self.probe(endedBy=1_059_000)["found"])

    def test_link_is_retained_outside_window(self):
        self.session(started_at=100.0)
        self.assertEqual(self.probe(sessionId="session-test", cwd=None)["sessionId"], "session-test")
        self.assertTrue(self.probe(sessionId="session-test")["found"])
        self.assertFalse(self.probe(sessionId="session-test", claimed=["session-test"])["found"])

    def test_sources_and_children_are_filtered(self):
        for i, source in enumerate(("discord", "telegram", "gateway", "cron", "subagent", "desktop")):
            self.session("excluded-" + source, source=source, started_at=995 + i / 10)
        self.session("child", parent_session_id="parent", started_at=996)
        self.assertFalse(self.probe()["found"])
        self.assertFalse(self.probe(sessionId="child")["found"])
        self.assertFalse(self.probe(sessionId="excluded-discord")["found"])
        for source in hermes.CLI_SOURCES:
            with self.subTest(source=source):
                self.session("allowed-" + source, source=source)
                self.assertTrue(self.probe(sessionId="allowed-" + source)["found"])

    def test_finished_and_aborted(self):
        for reason, state in (("cli_close", "done"), ("agent_close", "done"),
                              ("startup_orphan_reap", "failed"), ("new-format-reason", "unknown"),
                              (None, "unknown")):
            with self.subTest(reason=reason):
                identifier = "ended-" + str(reason)
                self.session(identifier, ended_at=1005.25, end_reason=reason)
                result = self.probe(sessionId=identifier)
                self.assertEqual(result["state"], state)
                self.assertEqual(result["endedAt"], 1_005_250)

    def test_progress_uses_latest_active_assistant_message(self):
        self.session()
        self.message("PROGRESS 1/7", 1001)
        self.message("PROGRESS 2/7\nPROGRESS 3/7", 1002)
        self.message("PROGRESS 6/7", 1003, role="user")
        self.message("PROGRESS 7/7", 1004, role="tool")
        self.message("PROGRESS 5/7", 1005, active=0)
        self.message("No progress here", 1006)
        self.assertEqual(self.probe()["progress"], {"done": 3, "total": 7})

    def test_progress_printed_from_a_shell_command(self):
        self.session()
        with sqlite3.connect(self.database) as conn:
            conn.execute("ALTER TABLE messages ADD COLUMN tool_calls TEXT")
            calls = json.dumps([{"function": {"name": "terminal", "arguments": json.dumps(
                {"command": "sleep 15; printf 'PROGRESS 4/6\\n'"})}}])
            conn.execute("INSERT INTO messages (session_id,role,content,timestamp,tool_calls) "
                         "VALUES (?,?,?,?,?)", ("session-test", "assistant", "", 1001, calls))
        out = self.probe()
        self.assertEqual(out["progress"], {"done": 4, "total": 6})
        self.assertTrue(out["lastActivity"].startswith("terminal: sleep 15"))

    def test_progress_invalid_and_tail_limit(self):
        self.session()
        self.message("PROGRESS 2/0\nPROGRESS 8/7", 1001)
        self.assertIsNone(self.probe()["progress"])
        self.message("x" * 30000 + "\nPROGRESS 4/7", 1002)
        self.assertEqual(self.probe()["progress"], {"done": 4, "total": 7})

    def test_context_cache_exact_model_and_url(self):
        self.session()
        cache = self.root / "context_length_cache.yaml"
        cache.write_text("context_lengths:\n"
                         "  synthetic-model@https://other.invalid/v1: 9999\n"
                         "  'synthetic-model@https://example.invalid/v1': 65536 # local\n"
                         "unrelated:\n  synthetic-model@https://example.invalid/v1: 1\n")
        self.assertEqual(self.probe()["contextWindow"], 65536)
        cache.write_text("context_lengths:\n  other-model@https://example.invalid/v1: 65536\n")
        self.assertIsNone(self.probe()["contextWindow"])

    def test_compression_point(self):
        self.session()
        (self.root / "context_length_cache.yaml").write_text(
            "context_lengths:\n  synthetic-model@https://example.invalid/v1: 1000000\n")
        self.assertEqual(self.probe()["compactAt"], 500000)
        config = self.root / "config.yaml"
        config.write_text("compression:\n  enabled: true\n  threshold: 0.6\n  threshold_tokens:\n"
                          "  nested:\n    threshold: 0.9\nother:\n  threshold: 0.1\n")
        self.assertEqual(self.probe()["compactAt"], 600000)
        config.write_text("compression:\n  threshold: 0.6\n  threshold_tokens: 300000\n")
        self.assertEqual(self.probe()["compactAt"], 300000)
        # Windows under 512K trigger at 75% or more.
        self.assertEqual(hermes._trigger(272000, 0.5), 204000)
        self.assertEqual(hermes._trigger(64000, 0.5), 54400)
        config.write_text("compression:\n  enabled: false\n  threshold: 0.5\n")
        self.assertIsNone(self.probe()["compactAt"])

    def test_profile_linking_competes_with_root(self):
        profile = self.create_store(self.root / "profiles" / "synthetic-profile")
        self.session("root-session", started_at=1001)
        self.session("profile-session", database=profile, started_at=995)
        (profile.parent / "context_length_cache.yaml").write_text(
            'context_lengths:\n  "synthetic-model@https://example.invalid/v1": 32768\n')
        result = self.probe()
        self.assertEqual(result["sessionId"], "profile-session")
        self.assertEqual(result["contextWindow"], 32768)
        self.assertEqual(self.probe(claimed=["profile-session"])["sessionId"], "root-session")
        self.assertTrue(self.probe(sessionId="profile-session")["found"])

    def test_children_do_not_double_count_async_delegations(self):
        self.session()
        self.session("child-one", parent_session_id="session-test", source="subagent")
        self.session("child-two", parent_session_id="session-test", source="subagent")
        self.session("unrelated", parent_session_id="elsewhere", source="subagent")
        with sqlite3.connect(self.database) as conn:
            conn.execute("INSERT INTO async_delegations VALUES (?,?,?,?)",
                         ("delegation-test", "session-test", "session-test", "running"))
        self.assertEqual(self.probe()["children"], 2)

    def test_unknown_optional_values_remain_null(self):
        self.session(model=None, model_config="malformed", billing_base_url=None,
                     input_tokens=None, output_tokens=None, api_call_count=None,
                     last_activity_at=None, last_activity_description=None)
        result = self.probe()
        self.assertTrue(result["found"])
        for field in ("model", "effort", "contextUsed", "contextWindow", "inputTokens",
                      "outputTokens", "steps", "lastActivity", "lastActivityAt", "error"):
            self.assertIsNone(result[field])

    def test_config_is_not_an_object(self):
        self.session(model_config='["synthetic"]')
        self.assertIsNone(self.probe()["contextUsed"])
        self.assertIsNone(self.probe()["effort"])

    def test_missing_store_is_not_created(self):
        self.database.unlink()
        result = self.probe()
        self.assertFalse(result["found"])
        self.assertIsNone(result["error"])
        self.assertFalse(self.database.exists())

    def test_corrupt_store_returns_error(self):
        self.database.write_bytes(b"synthetic invalid database")
        result = self.probe()
        self.assertFalse(result["found"])
        self.assertIn("DatabaseError", result["error"])

    def test_reader_preserves_database_bytes(self):
        self.session()
        before = self.database.read_bytes()
        self.probe()
        self.assertEqual(self.database.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
