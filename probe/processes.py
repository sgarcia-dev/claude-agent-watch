"""Finds the agent CLI processes this Claude Code session started.

Command parsing misses launches hidden in scripts, wrappers or heredocs, so
the mod also asks the process table. A process is this session's when:

- the Claude Code process that ran this probe is one of its ancestors, or
- it was orphaned (its launching shell exited, as with `cmd &`), no other
  Claude Code process is its ancestor, and it started inside a time window
  when this session was running a Bash command.

Nested agent processes (a wrapper script's real binary, an agent's own
children) count once, as their topmost agent process.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

INTERPRETERS = re.compile(r"^(bash|sh|zsh|dash|node|bun|deno|perl|ruby|python[0-9.]*)$")
SCRIPT_EXT = re.compile(r"\.(js|mjs|cjs|py|sh)$")
WINDOW_BEFORE_MS = 2_000
WINDOW_AFTER_MS = 3_000


def _base(word: str) -> str:
    return SCRIPT_EXT.sub("", word.rsplit("/", 1)[-1])


def agent_words(words: list[str]) -> tuple[str, list[str]] | None:
    """(tool, words from the tool name on) when this argv runs an agent task."""
    i = 0
    if words and INTERPRETERS.match(_base(words[0])):
        i = 1
        while i < len(words) and words[i].startswith("-"):
            # `perl -e <script>` and `node --flag` take no agent word here.
            i += 2 if words[i] in ("-e", "-c") else 1
    rest = words[i:]
    if not rest:
        return None
    name, args = _base(rest[0]), rest[1:]
    if any(a in ("--help", "-h", "--version", "-V") for a in args):
        return None
    first = args[0] if args else ""
    if name == "codex" and first in ("exec", "e"):
        return "codex", rest
    if name == "opencode" and first == "run":
        return "opencode", rest
    if name == "agy" and any(a in ("-p", "--print") or a.startswith("--prompt") for a in args):
        return "agy", rest
    if name == "hermes" and (first == "-z" or (first == "chat" and any(
            a in ("-q", "--oneshot") or a.startswith("--query") for a in args))):
        return "hermes", rest
    return None


def _etime_ms(text: str) -> int | None:
    """ps elapsed time, [[dd-]hh:]mm:ss, in milliseconds."""
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", text)
    if not m:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in m.groups())
    return (((days * 24 + hours) * 60 + minutes) * 60 + seconds) * 1000


def _table() -> dict[int, tuple[int, int | None, list[str]]]:
    """pid -> (ppid, start ms, argv words)."""
    out = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,etime=,command="],
                         capture_output=True, text=True, timeout=5, check=True).stdout
    now = int(time.time() * 1000)
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        age = _etime_ms(parts[2])
        table[int(parts[0])] = (int(parts[1]), None if age is None else now - age, parts[3].split())
    return table


def _is_claude(words: list[str]) -> bool:
    return bool(words) and _base(words[0]) == "claude"


def _ancestors(pid: int, table) -> list[int]:
    chain, seen = [], set()
    while pid in table and pid not in seen and pid > 1:
        seen.add(pid)
        pid = table[pid][0]
        chain.append(pid)
    return chain


def _claude_pid(table) -> int:
    """The nearest Claude Code process above this probe."""
    for pid in _ancestors(os.getpid(), table):
        if pid in table and _is_claude(table[pid][2]):
            return pid
    return os.getppid()


def _cwds(pids: list[int]) -> dict[int, str]:
    if not pids:
        return {}
    try:
        out = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    cwds, pid = {}, None
    for line in out.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            cwds[pid] = line[1:]
    return cwds


def _in_window(started: int | None, windows) -> bool:
    if started is None:
        return False
    for window in windows or []:
        begin = window[0]
        end = window[1] if len(window) > 1 and window[1] is not None else int(time.time() * 1000)
        if begin - WINDOW_BEFORE_MS <= started <= end + WINDOW_AFTER_MS:
            return True
    return False


def scan(windows, known) -> list[dict]:
    """This session's agent processes. `known` pids skip the cwd lookup."""
    table = _table()
    mine = _claude_pid(table)
    agents = {}
    for pid, (_, started, words) in table.items():
        found = agent_words(words)
        if found:
            agents[pid] = (found, started)
    result = []
    for pid, ((tool, words), started) in agents.items():
        chain = _ancestors(pid, table)
        if any(a in agents for a in chain):
            continue
        if mine not in chain:
            if any(a in table and _is_claude(table[a][2]) for a in chain):
                continue
            if not _in_window(started, windows):
                continue
        result.append({"pid": pid, "tool": tool, "words": words[:200], "startedAt": started, "cwd": None})
    known = set(known or [])
    cwds = _cwds([p["pid"] for p in result if p["pid"] not in known])
    for p in result:
        p["cwd"] = cwds.get(p["pid"])
    return result
