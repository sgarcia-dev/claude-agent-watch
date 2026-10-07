import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "probe", "agent_probe.py")
sys.path.insert(0, os.path.join(ROOT, "probe"))

import common  # noqa: E402


class AgentProbeTests(unittest.TestCase):
    def run_probe(self, request, env=None):
        out = subprocess.run(
            [sys.executable, "-I", PROBE], input=json.dumps(request),
            capture_output=True, text=True, env={**os.environ, **(env or {})}, check=True,
        )
        return json.loads(out.stdout)["runs"]

    def test_unknown_tool_answers_with_error(self):
        runs = self.run_probe({"now": 1, "runs": [{"id": "a", "tool": "nope", "startedAt": 0}]})
        self.assertEqual(runs[0]["id"], "a")
        self.assertFalse(runs[0]["found"])
        self.assertIn("unknown tool", runs[0]["error"])
        self.assertEqual(set(common.FIELDS) | {"id"}, set(runs[0]))

    def test_activity_line_drops_progress_markers(self):
        self.assertEqual(common.one_line("PROGRESS 2/5\nReading codex.py"), "Reading codex.py")
        self.assertEqual(common.one_line("PROGRESS 2/5"), "PROGRESS 2/5")

    def test_runs_started_together_get_different_sessions(self):
        with tempfile.TemporaryDirectory() as home:
            day = os.path.join(home, "sessions", "2026", "10", "07")
            os.makedirs(day)
            for name, ts in (("one", "2026-10-07T10:00:01.000Z"), ("two", "2026-10-07T10:00:02.000Z")):
                with open(os.path.join(day, f"rollout-2026-10-07T10-00-0{name == 'two' and 2 or 1}-{name}.jsonl"), "w") as fh:
                    fh.write(json.dumps({"timestamp": ts, "type": "session_meta",
                                         "payload": {"id": name, "timestamp": ts, "cwd": home}}) + "\n")
            start = 1791367200000  # 2026-10-07T10:00:00Z
            request = {"now": start + 60000, "runs": [
                {"id": "b", "tool": "codex", "startedAt": start + 500, "cwd": home, "sessionId": None, "claimed": []},
                {"id": "a", "tool": "codex", "startedAt": start, "cwd": home, "sessionId": None, "claimed": []},
            ]}
            runs = self.run_probe(request, {"AGENT_WATCH_CODEX_HOME": home})
            linked = {r["id"]: r["sessionId"] for r in runs}
            self.assertEqual(linked, {"a": "one", "b": "two"})


if __name__ == "__main__":
    unittest.main()
