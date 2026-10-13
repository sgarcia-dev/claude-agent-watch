import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from probe import codex, common


START = 1_800_000_000_000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def event(kind, offset=1000, **fields):
    return {"timestamp": iso(START + offset), "type": "event_msg",
            "payload": {"type": kind, **fields}}


def item(kind, offset=2000, **fields):
    return {"timestamp": iso(START + offset), "type": "response_item",
            "payload": {"type": kind, **fields}}


def message(text, offset=3000, role="assistant"):
    return item("message", offset, role=role,
                content=[{"type": "output_text", "text": text}])


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = str(self.root / "project")
        Path(self.cwd).mkdir()
        env = patch.dict(os.environ, {"AGENT_WATCH_CODEX_HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.run = {"id": "run-test", "tool": "codex", "startedAt": START,
                    "cwd": self.cwd, "claimed": [], "sessionId": None}

    def rollout(self, sid="session-test", start=START, cwd=None, records=(), **meta):
        date = datetime.fromtimestamp(start / 1000, timezone.utc)
        folder = self.root / "sessions" / date.strftime("%Y/%m/%d")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-{date.strftime('%Y-%m-%dT%H-%M-%S')}-{sid}.jsonl"
        header = {"timestamp": iso(start), "type": "session_meta",
                  "payload": {"id": sid, "timestamp": iso(start),
                              "cwd": cwd if cwd is not None else self.cwd, **meta}}
        path.write_text("\n".join(json.dumps(r) for r in (header, *records)) + "\n")
        return path

    def probe(self, **overrides):
        return codex.probe({**self.run, **overrides}, START + 60_000)

    def test_link_window_earliest_and_claimed(self):
        self.rollout("too-early", START - 5001)
        self.rollout("claimed", START - 5000)
        self.rollout("earliest", START - 4999)
        self.rollout("later", START + 50_000)
        self.rollout("too-late", START + 120_000)
        out = self.probe(claimed=["claimed"])
        self.assertEqual(out["sessionId"], "earliest")
        self.assertTrue(out["found"])
        self.assertEqual(set(out), {"id", *common.FIELDS})

    def test_exclusive_upper_boundary(self):
        self.rollout(start=START + 120_000)
        self.assertFalse(self.probe()["found"])

    def test_cwd_and_realpath(self):
        self.rollout("wrong", START - 1000, cwd=str(self.root / "elsewhere"))
        alias = self.root / "alias"
        alias.symlink_to(self.cwd, target_is_directory=True)
        self.rollout("right", START, cwd=str(alias))
        self.assertEqual(self.probe()["sessionId"], "right")

    def test_cwd_mismatch_falls_back_to_time_window(self):
        self.rollout(cwd=str(self.root / "elsewhere"))
        self.assertTrue(self.probe()["found"])

    def test_missing_cwd_can_link(self):
        path = self.rollout()
        record = json.loads(path.read_text())
        del record["payload"]["cwd"]
        path.write_text(json.dumps(record) + "\n")
        self.assertTrue(self.probe()["found"])

    def test_pinned_session_holds_and_claimed_is_excluded(self):
        self.rollout("earlier", START - 4000)
        self.rollout("pinned", START + 130_000, cwd=str(self.root / "other"))
        self.assertEqual(self.probe(sessionId="pinned")["sessionId"], "pinned")
        self.assertFalse(self.probe(sessionId="pinned", claimed=["pinned"])["found"])

    def test_running_usage_model_steps_activity_and_progress(self):
        self.rollout(records=[
            event("task_started"),
            {"type": "turn_context", "payload": {"model": "synthetic-model", "effort": "high"}},
            item("function_call", name="exec_command", call_id="call-a",
                 arguments=json.dumps({"cmd": "python3 -m unittest"})),
            message("Working\nPROGRESS 1/7\nPROGRESS 3/7"),
            event("token_count", 4000, info={
                "last_token_usage": {"input_tokens": 123},
                "total_token_usage": {"input_tokens": 700, "cached_input_tokens": 300,
                                      "output_tokens": 100, "reasoning_output_tokens": 50},
                "model_context_window": 1000}),
            item("custom_tool_call", 5000, name="apply_patch", call_id="call-b", input="synthetic"),
        ])
        out = self.probe()
        expected = {"state": "running", "model": "synthetic-model", "effort": "high",
                    "contextUsed": 123, "contextWindow": 1000,
                    "inputTokens": 700, "outputTokens": 100, "steps": 2,
                    "progress": {"done": 3, "total": 7}, "lastActivity": "apply_patch",
                    "lastActivityAt": START + 5000, "endedAt": None, "error": None}
        for key, value in expected.items():
            self.assertEqual(out[key], value, key)

    def test_compact_point(self):
        self.rollout(records=[{"type": "turn_context", "payload": {"model": "synthetic-model"}},
                              event("token_count", 4000, info={"model_context_window": 950})])
        self.assertEqual(self.probe()["compactAt"], 900)
        catalog = self.root / "models_cache.json"
        catalog.write_text(json.dumps({"models": [
            {"slug": "synthetic-model", "effective_context_window_percent": 50}]}))
        self.assertEqual(self.probe()["compactAt"], 1710)
        catalog.write_text(json.dumps({"models": [
            {"slug": "synthetic-model", "auto_compact_token_limit": 700}]}))
        self.assertEqual(self.probe()["compactAt"], 700)
        (self.root / "config.toml").write_text(
            'model = "synthetic-model"\nmodel_auto_compact_token_limit = 600\n[profiles.x]\n')
        self.assertEqual(self.probe()["compactAt"], 600)

    def test_model_from_first_turn_when_tail_lacks_it(self):
        filler = [item("reasoning", 2000 + i, summary=[], content="x" * 1000) for i in range(30)]
        self.rollout(records=[{"type": "turn_context", "payload": {"model": "synthetic-model", "effort": "low"}}, *filler])
        with patch.object(codex, "TAIL_BYTES", 5000):
            out = self.probe()
        self.assertEqual((out["model"], out["effort"]), ("synthetic-model", "low"))

    def test_done_aborted_and_restart(self):
        for terminal, state in (("task_complete", "done"), ("turn_aborted", "failed")):
            with self.subTest(terminal=terminal):
                self.rollout(records=[event("task_started"), event(terminal, 5000)])
                out = self.probe()
                self.assertEqual(out["state"], state)
                self.assertEqual(out["endedAt"], START + 5000)
                self.rollout(records=[event(terminal, 5000), event("task_started", 6000)])
                out = self.probe()
                self.assertEqual(out["state"], "running")
                self.assertIsNone(out["endedAt"])

    def test_completion_time(self):
        self.rollout(records=[event("task_complete", 5000, completed_at=iso(START + 4500))])
        self.assertEqual(self.probe()["endedAt"], START + 4500)

    def test_waiting_then_answered(self):
        call = item("function_call", name="functions.request_user_input", call_id="question",
                    arguments='{}')
        self.rollout(records=[event("task_started"), call])
        self.assertEqual(self.probe()["state"], "waiting")
        unrelated = item("function_call_output", call_id="other", output="synthetic")
        self.rollout(records=[call, unrelated])
        self.assertEqual(self.probe()["state"], "waiting")
        for answer in (item("function_call_output", call_id="question", output="synthetic"),
                       message("Answer", role="user"),
                       item("custom_tool_call", name="apply_patch", call_id="next")):
            self.rollout(records=[call, answer])
            self.assertEqual(self.probe()["state"], "running")

    def test_progress_only_own_messages(self):
        self.rollout(records=[message("PROGRESS 2/7"), message("PROGRESS 7/7", role="user"),
                              item("function_call_output", output="PROGRESS 6/7"),
                              item("agent_message", author="/root/other",
                                   content=[{"text": "PROGRESS 5/7"}]),
                              item("agent_message", author="/root",
                                   content=[{"text": "Latest\nPROGRESS 4/7"}])])
        out = self.probe()
        self.assertEqual(out["progress"], {"done": 4, "total": 7})
        self.assertEqual(out["lastMessage"], "Latest")

    def test_children_direct_and_nested_parent(self):
        self.rollout("parent", parent_thread_id=None)
        self.rollout("child-a", START + 10_000, parent_thread_id="parent")
        self.rollout("child-b", START + 20_000,
                     source={"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}})
        self.rollout("unrelated", START + 30_000, parent_thread_id="other")
        self.assertEqual(self.probe()["children"], 2)

    def test_children_unknown_without_parent_schema(self):
        self.rollout()
        self.assertIsNone(self.probe()["children"])

    def test_unknown_fields_null(self):
        self.rollout()
        out = self.probe()
        for field in ("model", "effort", "contextUsed", "contextWindow", "inputTokens",
                      "outputTokens", "progress", "lastActivity", "lastActivityAt", "endedAt"):
            self.assertIsNone(out[field], field)

    def test_large_file_steps_null_tail_still_used(self):
        path = self.rollout(records=[item("function_call", name="earlier")])
        with path.open("a") as fh:
            fh.write(json.dumps({"type": "padding", "payload": "x" * (codex.TAIL_BYTES + 100)}) + "\n")
            fh.write(json.dumps(message("PROGRESS 5/7")) + "\n")
            fh.write(json.dumps(event("task_complete", 6000)) + "\n")
        out = self.probe()
        self.assertIsNone(out["steps"])
        self.assertEqual(out["progress"], {"done": 5, "total": 7})
        self.assertEqual(out["state"], "done")

    def test_partial_json_line_is_ignored(self):
        path = self.rollout(records=[event("task_complete")])
        with path.open("a") as fh:
            fh.write('{"unfinished":')
        self.assertEqual(self.probe()["state"], "done")

    def test_safe_command_activity_and_length(self):
        self.rollout(records=[item("function_call", name="exec_command",
                                   arguments=json.dumps({"cmd": "echo " + "word " * 100}))])
        line = self.probe()["lastActivity"]
        self.assertTrue(line.startswith("exec_command: echo word word"))
        self.assertEqual(len(line), 300)
        self.rollout(records=[item("function_call", name="exec_command",
                                   arguments=json.dumps({"cmd": "echo --token synthetic-secret"}))])
        self.assertEqual(self.probe()["lastActivity"], "exec_command: echo [hidden]")

    def test_missing_store(self):
        out = self.probe()
        self.assertFalse(out["found"])
        self.assertIsNone(out["error"])

    def test_errors_do_not_raise_or_expose_paths(self):
        self.rollout()
        with patch.object(codex, "_read", side_effect=OSError("sensitive path")):
            self.assertEqual(self.probe()["error"], "Codex reader: OSError")

    def test_midnight_link_window(self):
        start = int(datetime(2027, 1, 16, tzinfo=timezone.utc).timestamp() * 1000)
        self.rollout("previous-day", start - 4000)
        self.assertEqual(self.probe(startedAt=start)["sessionId"], "previous-day")


if __name__ == "__main__":
    unittest.main()
