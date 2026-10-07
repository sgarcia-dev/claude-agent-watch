import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "probe"))

import processes  # noqa: E402

NOW = 1_000_000_000


class AgentWordsTests(unittest.TestCase):
    def test_tasks_count_and_servers_do_not(self):
        self.assertEqual(processes.agent_words(["codex", "exec", "-m", "x"])[0], "codex")
        self.assertEqual(processes.agent_words(["/bin/bash", "/tmp/shim/codex", "exec", "-"])[1][:2],
                         ["/tmp/shim/codex", "exec"])
        self.assertEqual(processes.agent_words(["/usr/bin/python3", "/x/bin/hermes", "chat", "-q", "hi"])[0], "hermes")
        self.assertEqual(processes.agent_words(["node", "/x/codex.js", "e", "task"])[0], "codex")
        self.assertEqual(processes.agent_words(["opencode", "run", "--model", "m"])[0], "opencode")
        self.assertEqual(processes.agent_words(["agy", "--model", "g", "-p", "hi"])[0], "agy")
        self.assertIsNone(processes.agent_words(["codex", "app-server", "--listen", "stdio://"]))
        self.assertIsNone(processes.agent_words(["opencode", "serve"]))
        self.assertIsNone(processes.agent_words(["codex", "exec", "--help"]))
        self.assertIsNone(processes.agent_words(["bash", "-c", "codex exec x"]))

    def test_elapsed_time(self):
        self.assertEqual(processes._etime_ms("01:05"), 65_000)
        self.assertEqual(processes._etime_ms("2-03:04:05"), ((2 * 24 + 3) * 3600 + 4 * 60 + 5) * 1000)


class ScanTests(unittest.TestCase):
    """pid 100 is this session's Claude, 200 another session's Claude."""

    def scan(self, table, windows=None):
        base = {
            1: (0, NOW - 10**8, ["launchd"]),
            100: (1, NOW - 10**7, ["/Users/me/.local/bin/claude", "--session-id", "a"]),
            150: (100, NOW - 1000, ["/bin/zsh", "-c", "python3 probe"]),
            200: (1, NOW - 10**7, ["claude"]),
        }
        base.update(table)
        with patch.object(processes, "_table", return_value=base), \
                patch.object(processes.os, "getpid", return_value=150), \
                patch.object(processes, "_cwds", return_value={}):
            return sorted(p["pid"] for p in processes.scan(windows or [], []))

    def test_descendants_of_this_claude_only(self):
        found = self.scan({
            300: (100, NOW - 5000, ["/bin/zsh", "-c", "./run.sh"]),
            301: (300, NOW - 4000, ["codex", "exec", "-"]),
            302: (301, NOW - 3000, ["codex", "exec", "nested"]),
            400: (200, NOW - 4000, ["opencode", "run", "x"]),
        })
        self.assertEqual(found, [301])

    def test_orphans_need_a_bash_window(self):
        table = {500: (1, NOW - 4000, ["agy", "-p", "hi"])}
        self.assertEqual(self.scan(table), [])
        self.assertEqual(self.scan(table, [[NOW - 4500, NOW - 4400]]), [500])
        self.assertEqual(self.scan(table, [[NOW - 60_000, NOW - 50_000]]), [])


if __name__ == "__main__":
    unittest.main()
