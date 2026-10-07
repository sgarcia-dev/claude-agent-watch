"""Synthetic Claude subagent fixtures. No real store content is used."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
from probe import claude, common

ROOT = Path(__file__).resolve().parents[1]
START = 1_800_000_000_000


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.env = patch.dict(os.environ, {"AGENT_WATCH_CLAUDE_HOME": str(self.root / "store")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.run = dict(id="run-test", tool="claude", startedAt=START,
                        cwd=str(self.cwd), command="Explore", sessionId=None,
                        claimed=[])

    def transcript(self, agent_id="synthetic", start=START, cwd=None,
                   messages=None, agent_type="Explore", project=None):
        cwd = str(cwd or self.cwd)
        slug = str(project or self.cwd).replace("/", "-")
        directory = self.root / "store" / "projects" / slug / "parent-session" / "subagents"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"agent-{agent_id}.jsonl"
        records = [dict(type="user", agentId=agent_id, cwd=cwd,
                        timestamp=start, message={"content": "Synthetic task"})]
        records.extend(messages or [])
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        if agent_type is not None:
            path.with_suffix(".meta.json").write_text(json.dumps({"agentType": agent_type}))
        return path

    def assistant(self, ident="response-1", text="Working", stop="tool_use",
                  timestamp=START + 1000, usage=None, model="synthetic-model",
                  blocks=None):
        return dict(type="assistant", timestamp=timestamp, effort="high", message=dict(
            id=ident, model=model, stop_reason=stop,
            usage=usage or dict(input_tokens=10, cache_read_input_tokens=20,
                                cache_creation_input_tokens=30, output_tokens=4),
            content=blocks if blocks is not None else [dict(type="text", text=text)]))

    def probe(self, **changes):
        return claude.probe({**self.run, **changes}, START + 10_000)

    def test_missing_store(self):
        result = self.probe()
        self.assertFalse(result["found"])
        self.assertIsNone(result["error"])
        self.assertEqual(set(result), {"id", *common.FIELDS})

    def test_window_and_newest_candidate(self):
        self.transcript("too-early", START - 5001)
        self.transcript("lower-bound", START - 5000)
        self.transcript("newest", START + 119999)
        self.transcript("upper-bound", START + 120000)
        self.assertEqual(self.probe()["sessionId"], "newest")
        self.assertEqual(self.probe(claimed=["newest"])["sessionId"], "lower-bound")
        self.assertFalse(self.probe(claimed=["newest", "lower-bound"])["found"])

    def test_cwd_and_type(self):
        self.transcript("wrong-cwd", START + 100, cwd=self.root / "elsewhere")
        self.transcript("wrong-type", START + 200, agent_type="Plan")
        self.transcript("right", START)
        self.assertEqual(self.probe()["sessionId"], "right")
        # A known agent ID wins over the folder check (the mod's cwd can be stale).
        self.assertTrue(self.probe(sessionId="wrong-cwd")["found"])

    def test_realpath_and_ancestor_project(self):
        alias = self.root / "alias"
        alias.symlink_to(self.cwd, target_is_directory=True)
        self.transcript(cwd=alias, project=self.root)
        self.assertTrue(self.probe()["found"])

    def test_pinned_id_holds_outside_window_but_not_claimed(self):
        self.transcript(start=START - 1_000_000, agent_type="Plan")
        self.assertTrue(self.probe(sessionId="synthetic")["found"])
        self.assertFalse(self.probe(sessionId="synthetic", claimed=["synthetic"])["found"])

    def test_running_deduplicates_usage_and_progress(self):
        messages = [self.assistant(text="PROGRESS 1/7"),
                    self.assistant(text="PROGRESS 2/7\nStill working"),
                    self.assistant(ident="response-2", blocks=[dict(
                        type="tool_use", id="tool-1", name="Bash",
                        input={"command": "echo synthetic-secret"})])]
        self.transcript(messages=messages)
        out = self.probe()
        self.assertEqual(out["state"], "running")
        self.assertEqual(out["steps"], 2)
        self.assertEqual(out["contextUsed"], 60)
        self.assertEqual(out["inputTokens"], 120)
        self.assertEqual(out["outputTokens"], 8)
        self.assertEqual(out["progress"], {"done": 2, "total": 7})
        self.assertEqual(out["contextWindow"], 200000)
        self.assertEqual(out["effort"], "high")
        self.assertEqual(out["lastActivity"], "Bash: echo synthetic-secret")
        self.assertEqual(out["lastActivityAt"], START + 1000)
        self.assertIsNone(out["endedAt"])
        self.assertIsNone(out["children"])

    def test_finished_and_latest_text_first_line(self):
        self.transcript(messages=[self.assistant(stop="end_turn", text="Finished\nPROGRESS 7/7")])
        out = self.probe()
        self.assertEqual(out["state"], "done")
        self.assertEqual(out["endedAt"], START + 1000)
        self.assertEqual(out["lastMessage"], "Finished")
        self.assertEqual(out["progress"], {"done": 7, "total": 7})

    def test_later_tool_use_resumes(self):
        self.transcript(messages=[self.assistant(stop="end_turn"), self.assistant(
            ident="response-2", timestamp=START + 2000, blocks=[dict(type="tool_use", name="Read")])])
        self.assertEqual(self.probe()["state"], "running")

    def test_tool_error_does_not_imply_session_failure(self):
        self.transcript(messages=[self.assistant(), dict(type="user", timestamp=START + 2000,
            message={"content": [{"type": "tool_result", "is_error": True,
                                    "content": "Synthetic tool failure"}]})])
        self.assertEqual(self.probe()["state"], "running")

    def test_million_token_rule(self):
        self.transcript(messages=[self.assistant(model="synthetic-model[1m]")])
        self.assertEqual(self.probe()["contextWindow"], 1000000)
        self.transcript(messages=[self.assistant(), dict(type="system", contextWindow=1000000)])
        self.assertEqual(self.probe()["contextWindow"], 1000000)

    def test_missing_usage_stays_null(self):
        message = self.assistant()
        del message["message"]["usage"]["cache_read_input_tokens"]
        self.transcript(messages=[message])
        out = self.probe()
        self.assertIsNone(out["contextUsed"])
        self.assertIsNone(out["inputTokens"])

    def test_bounded_tail_does_not_report_partial_totals(self):
        path = self.transcript(messages=[self.assistant(text="old " * 300),
                                         self.assistant(ident="response-2", text="PROGRESS 3/7")])
        with patch.object(claude, "TAIL_BYTES", 800):
            out = self.probe()
        self.assertGreater(path.stat().st_size, 800)
        self.assertTrue(out["found"])
        self.assertIsNone(out["steps"])
        self.assertIsNone(out["inputTokens"])
        self.assertIsNone(out["outputTokens"])
        self.assertEqual(out["contextUsed"], 60)
        self.assertEqual(out["progress"], {"done": 3, "total": 7})

    def test_partial_final_line_and_missing_type(self):
        path = self.transcript(agent_type=None, messages=[self.assistant()])
        with path.open("a") as stream:
            stream.write('{"type":')
        self.assertTrue(self.probe()["found"])

    def test_reader_catches_oserror(self):
        self.transcript()
        with patch.object(claude.common, "tail_lines", side_effect=OSError("sensitive detail")):
            out = self.probe()
        self.assertIn("OSError", out["error"])
        self.assertNotIn("sensitive detail", out["error"])

    def test_isolated_entry_point(self):
        self.transcript(messages=[self.assistant(stop="end_turn")])
        completed = subprocess.run([sys.executable, "-I", "-B", "probe/agent_probe.py"],
            input=json.dumps({"now": START + 10000, "runs": [self.run]}),
            text=True, capture_output=True, cwd=ROOT, check=True)
        self.assertEqual(json.loads(completed.stdout)["runs"][0]["state"], "done")


if __name__ == "__main__":
    unittest.main()
