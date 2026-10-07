"""Synthetic Antigravity stores. No real conversation data is used."""

import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from probe import agy, common


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def integer(field, value):
    return varint(field << 3) + varint(value)


def blob(field, value):
    if isinstance(value, str):
        value = value.encode()
    return varint((field << 3) | 2) + varint(len(value)) + value


def timestamp(ms):
    return integer(1, ms // 1000) + integer(2, (ms % 1000) * 1_000_000)


def metadata(created, completed=None, tool=None):
    result = blob(1, timestamp(created))
    if completed is not None:
        result += blob(8, timestamp(completed))
    if tool:
        result += blob(4, blob(1, "synthetic-call") + blob(2, tool)
                       + blob(3, '{"secret":"never expose tool arguments"}'))
    return result


def generation(model="gemini-test-flash", input_count=100, output_count=20,
               cache=0, thinking=5):
    usage = (integer(2, input_count) + integer(3, output_count)
             + integer(5, cache) + integer(9, thinking)
             + integer(10, output_count - thinking))
    return blob(1, blob(4, usage) + blob(19, model))


class AgyTests(unittest.TestCase):
    START = 1_800_000_000_000
    NOW = START + 90_000

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "conversations").mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {"AGENT_WATCH_AGY_HOME": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_input(self, **changes):
        run = {"id": "synthetic-run", "tool": "agy", "startedAt": self.START,
               "cwd": str(self.workspace), "command": "agy -p synthetic",
               "sessionId": None, "claimed": []}
        run.update(changes)
        return run

    def session(self, name="synthetic-session", start=None, cwd=True,
                status=3, step_type=15, text="Synthetic response", tool=None,
                generations=None, modified=None, first_metadata=True,
                trajectory_time=True):
        start = self.START if start is None else start
        path = self.root / "conversations" / (name + ".db")
        if cwd is True:
            cwd = self.workspace
        trajectory = blob(2, timestamp(start)) if trajectory_time else b""
        if cwd:
            # Include a different git root to ensure it is never treated as cwd.
            trajectory += blob(1, blob(1, Path(cwd).as_uri())
                               + blob(2, self.root.as_uri()))
        payload = blob(20, blob(1, text)) if step_type == 15 else b""
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER,
                    status INTEGER, metadata BLOB, step_payload BLOB);
                CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB);
                CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY, data BLOB);
            """)
            conn.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)",
                         (trajectory,))
            conn.execute("INSERT INTO steps VALUES (0, 14, 3, ?, ?)",
                         (metadata(start) if first_metadata else b"",
                          blob(19, blob(2, "PROGRESS 99/100 user content"))))
            conn.execute("INSERT INTO steps VALUES (1, ?, ?, ?, ?)",
                         (step_type, status, metadata(start + 1000, start + 2000, tool), payload))
            for idx, data in enumerate(generations or []):
                conn.execute("INSERT INTO gen_metadata VALUES (?, ?)", (idx, data))
        changed = self.START + 2000 if modified is None else modified
        os.utime(path, ns=(changed * 1_000_000, changed * 1_000_000))
        return path

    def test_link_window_earliest_and_claimed(self):
        self.session("too-early", start=self.START - 5001)
        self.session("boundary", start=self.START - 5000)
        self.session("later", start=self.START + 3000)
        self.session("too-late", start=self.START + 120000)
        out = agy.probe(self.run_input(), self.NOW)
        self.assertEqual(out["sessionId"], "boundary")
        out = agy.probe(self.run_input(claimed=["boundary"]), self.NOW)
        self.assertEqual(out["sessionId"], "later")
        out = agy.probe(self.run_input(claimed=["boundary", "later"]), self.NOW)
        self.assertFalse(out["found"])

    def test_link_cwd_and_realpath(self):
        other = self.root / "other"
        other.mkdir()
        alias = self.root / "workspace-alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        self.session("wrong-cwd", start=self.START - 1000, cwd=other)
        self.session("right-cwd", cwd=alias)
        out = agy.probe(self.run_input(), self.NOW)
        self.assertEqual(out["sessionId"], "right-cwd")

    def test_known_cwd_must_match(self):
        self.session()
        out = agy.probe(self.run_input(cwd=str(self.root)), self.NOW)
        self.assertFalse(out["found"])
        out = agy.probe(self.run_input(cwd=None), self.NOW)
        self.assertFalse(out["found"])

    def test_missing_cwd_can_link(self):
        self.session(cwd=False)
        self.assertTrue(agy.probe(self.run_input(), self.NOW)["found"])

    def test_existing_link_holds_outside_window(self):
        self.session(start=self.START - 1_000_000)
        out = agy.probe(self.run_input(sessionId="synthetic-session", cwd=str(self.root)), self.NOW)
        self.assertTrue(out["found"])
        out = agy.probe(self.run_input(sessionId="synthetic-session",
                                       claimed=["synthetic-session"]), self.NOW)
        self.assertFalse(out["found"])

    def test_trajectory_start_fallback(self):
        self.session(first_metadata=False)
        self.assertTrue(agy.probe(self.run_input(), self.NOW)["found"])

    def test_birth_time_fallback(self):
        path = self.session(first_metadata=False, trajectory_time=False)
        candidate = {"id": path.stem, "startedAt": self.START,
                     "cwd": common.real(str(self.workspace))}
        # Verify the fallback independently of the host OS's stat fields.
        fake_stat = type("Stat", (), {"st_birthtime": self.START / 1000})()
        with common.connect_ro(str(path)) as conn:
            with patch.object(Path, "stat", return_value=fake_stat):
                self.assertEqual(agy._candidate(conn, path), candidate)

    def test_running_and_waiting_status(self):
        for status, state in ((1, "running"), (2, "running"), (8, "running"),
                              (11, "running"), (9, "waiting")):
            with self.subTest(status=status):
                name = "state-" + str(status)
                self.session(name, status=status, modified=self.NOW)
                out = agy.probe(self.run_input(sessionId=name), self.NOW)
                self.assertEqual(out["state"], state)

    def test_finished_model_counts_and_timestamps(self):
        self.session(generations=[generation(output_count=20),
                                  generation(input_count=200, cache=400, output_count=35)])
        out = agy.probe(self.run_input(), self.NOW)
        self.assertTrue(out["found"])
        self.assertEqual(out["state"], "done")
        self.assertEqual(out["model"], "gemini-test-flash")
        self.assertEqual(out["steps"], 2)
        self.assertEqual(out["outputTokens"], 55)
        self.assertEqual(out["lastActivityAt"], self.START + 2000)
        self.assertEqual(out["lastActivity"], "assistant response")
        for field in ("effort", "contextUsed", "contextWindow", "inputTokens", "endedAt", "children"):
            self.assertIsNone(out[field], field)
        self.assertIsNone(out["error"])
        self.assertEqual(set(out), {"id", *common.FIELDS})

    def test_recent_completion_and_wal_are_unknown(self):
        path = self.session(modified=self.NOW - 29999)
        self.assertEqual(agy.probe(self.run_input(), self.NOW)["state"], "unknown")
        os.utime(path, ns=(self.START * 1_000_000,) * 2)
        # A journal's mtime also prevents a premature done status.
        journal = Path(str(path) + "-journal")
        journal.touch()
        os.utime(journal, ns=(self.NOW * 1_000_000,) * 2)
        self.assertEqual(agy.probe(self.run_input(), self.NOW)["state"], "unknown")

    def test_failed_or_aborted_latest_step(self):
        for status in (6, 7, 12):
            with self.subTest(status=status):
                name = "failed-" + str(status)
                self.session(name, status=status)
                self.assertEqual(agy.probe(self.run_input(sessionId=name), self.NOW)["state"], "failed")

    def test_old_tool_failure_does_not_override_final_completion(self):
        path = self.session()
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE steps SET status=7 WHERE idx=0")
        os.utime(path, ns=(self.START * 1_000_000,) * 2)
        self.assertEqual(agy.probe(self.run_input(), self.NOW)["state"], "done")

    def test_progress_latest_assistant_only(self):
        path = self.session(text="PROGRESS 1/7\nPROGRESS 2/7")
        with sqlite3.connect(path) as conn:
            conn.execute("INSERT INTO steps VALUES (2,15,3,?,?)",
                         (metadata(self.START + 3000), blob(20, blob(1, "PROGRESS 3/7"))))
            conn.execute("INSERT INTO steps VALUES (3,132,3,?,?)",
                         (metadata(self.START + 4000, tool="exec"),
                          blob(140, blob(2, blob(1, "PROGRESS 6/7 tool output")))))
        out = agy.probe(self.run_input(), self.NOW)
        self.assertEqual(out["progress"], {"done": 3, "total": 7})
        self.assertEqual(out["lastActivity"], "exec")
        self.assertNotIn("secret", str(out))

    def test_modified_response_preferred_over_original_and_thinking(self):
        path = self.session(text="PROGRESS 1/7")
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE steps SET step_payload=? WHERE idx=1",
                         (blob(20, blob(1, "PROGRESS 1/7") + blob(3, "PROGRESS 6/7")
                               + blob(8, "PROGRESS 4/7")),))
        self.assertEqual(agy.probe(self.run_input(), self.NOW)["progress"], {"done": 4, "total": 7})

    def test_no_progress_from_user_or_thinking(self):
        path = self.session()
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE steps SET step_payload=? WHERE idx=1",
                         (blob(20, blob(1, "Synthetic response") + blob(3, "PROGRESS 6/7")),))
        self.assertIsNone(agy.probe(self.run_input(), self.NOW)["progress"])

    def test_missing_store_and_missing_link(self):
        with patch.dict(os.environ, {"AGENT_WATCH_AGY_HOME": str(self.root / "missing")}):
            out = agy.probe(self.run_input(), self.NOW)
            self.assertFalse(out["found"])
            self.assertIsNone(out["error"])
        self.assertFalse(agy.probe(self.run_input(sessionId="missing"), self.NOW)["found"])

    def test_corrupt_database_does_not_raise_or_expose_paths(self):
        (self.root / "conversations" / "corrupt.db").write_bytes(b"invalid sqlite")
        out = agy.probe(self.run_input(), self.NOW)
        self.assertFalse(out["found"])
        self.assertIsNotNone(out["error"])
        self.assertNotIn(str(self.root), out["error"])

    def test_link_cannot_escape_store(self):
        out = agy.probe(self.run_input(sessionId="../outside"), self.NOW)
        self.assertFalse(out["found"])
        self.assertIsNotNone(out["error"])

    def test_unknown_status_is_unknown(self):
        self.session(status=77)
        self.assertEqual(agy.probe(self.run_input(), self.NOW)["state"], "unknown")

    def test_long_generation_history_does_not_report_partial_total(self):
        self.session(generations=[generation()] * (agy.MAX_GENERATIONS + 1))
        out = agy.probe(self.run_input(), self.NOW)
        self.assertEqual(out["model"], "gemini-test-flash")
        self.assertIsNone(out["outputTokens"])

    def test_missing_output_count_is_unknown(self):
        self.session(generations=[generation(), blob(1, blob(19, "gemini-test-flash")
                                                         + blob(4, integer(2, 123)))])
        self.assertIsNone(agy.probe(self.run_input(), self.NOW)["outputTokens"])

    def test_wire_decoder_fixed_fields_and_nested_paths(self):
        data = (integer(1, 300) + blob(2, integer(3, 123))
                + varint((4 << 3) | 5) + struct.pack("<I", 42)
                + varint((5 << 3) | 1) + struct.pack("<Q", 99))
        self.assertEqual(agy._decode(data), [(1, 0, 300), (2, 2, integer(3, 123)),
                                           (4, 5, 42), (5, 1, 99)])
        self.assertEqual(agy._at(data, 2, 3, wire=0), 123)

    def test_malformed_wire_data_is_rejected(self):
        for data in (b"\x80", b"\x00", b"\x0a\x10short", b"\x0dshort"[:3],
                     b"\x0b", b"\x08" + b"\xff" * 10):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    agy._decode(data)
        path = self.session()
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE steps SET step_payload=? WHERE idx=1", (b"\x80",))
        out = agy.probe(self.run_input(sessionId=path.stem), self.NOW)
        self.assertIsNotNone(out["error"])


if __name__ == "__main__":
    unittest.main()
