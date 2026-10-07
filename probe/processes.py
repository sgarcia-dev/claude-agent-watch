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

import ctypes
import os
import re
import struct
import subprocess
import time

INTERPRETERS = re.compile(r"^(bash|sh|zsh|dash|node|bun|deno|perl|ruby|python[0-9.]*)$")
SCRIPT_EXT = re.compile(r"\.(js|mjs|cjs|py|sh)$")
# Codex options that take a value. Wrappers (a terminal app's shim, say) put
# global options before the subcommand: `codex --enable hooks -c k=v exec`.
CODEX_VALUE_FLAGS = {"-c", "--config", "--enable", "--disable", "-m", "--model", "-C", "--cd",
                     "-p", "--profile", "-s", "--sandbox", "-a", "--ask-for-approval", "-i",
                     "--image", "--add-dir", "--local-provider"}
NAMES_RE = re.compile(r"codex|opencode|agy|hermes")
WINDOW_BEFORE_MS = 2_000
WINDOW_AFTER_MS = 3_000


def _base(word: str) -> str:
    return SCRIPT_EXT.sub("", word.rsplit("/", 1)[-1])


def _codex_subcommand(args: list[str]) -> int | None:
    """Index of Codex's subcommand, past any global options."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in CODEX_VALUE_FLAGS else 1
    return i if i < len(args) else None


def agent_words(words: list[str]) -> tuple[str, list[str]] | None:
    """(tool, words from the tool name on) when this argv runs an agent task.

    Codex words come back with the subcommand first: `codex exec <options>`.
    """
    i = 0
    script = None
    if words and INTERPRETERS.match(_base(words[0])):
        i = 1
        while i < len(words) and words[i].startswith("-"):
            # `perl -e <script>`, `python3 -c <script>`: the next word is code.
            if words[i] in ("-e", "-c") and i + 1 < len(words):
                script = words[i + 1]
                i += 2
            else:
                i += 1
    rest = words[i:]
    if script is not None and "hermes_cli" in script:
        # Hermes's launcher: python3 -I -c '<bootstrap>' chat --query-file ...
        rest = ["hermes"] + rest
    if not rest:
        return None
    name, args = _base(rest[0]), rest[1:]
    if any(a in ("--help", "-h", "--version", "-V") for a in args):
        return None
    first = args[0] if args else ""
    if name == "codex":
        at = _codex_subcommand(args)
        if at is not None and args[at] in ("exec", "e"):
            return "codex", [rest[0], args[at]] + args[:at] + args[at + 1:]
        return None
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


_libc = None


def _argv(pid: int) -> list[str] | None:
    """A process's exact arguments (KERN_PROCARGS2), or None if hidden.

    ps joins arguments with spaces, which loses where a prompt or a -c script
    starts and ends. macOS hides other processes' environment, not argv.
    """
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL("/usr/lib/libc.dylib")
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t(0)
        if _libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if _libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buf.raw[:size.value]
        argc = struct.unpack("i", raw[:4])[0]
        rest = raw[4:]
        rest = rest[rest.index(b"\0"):].lstrip(b"\0")  # skip the executable path
        args = rest.split(b"\0")[:argc]
        return [a.decode("utf-8", errors="replace") for a in args]
    except (OSError, ValueError, AttributeError, struct.error):
        return None


def _start_ms(pid: int) -> int | None:
    """Exact start time (KERN_PROC_PID); ps gives whole seconds only."""
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL("/usr/lib/libc.dylib")
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
        buf = ctypes.create_string_buffer(1024)
        size = ctypes.c_size_t(1024)
        if _libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value < 16:
            return None
        # kinfo_proc starts with extern_proc, whose first field is the start
        # time as a timeval: 64-bit seconds, 32-bit microseconds.
        seconds, micros = struct.unpack("qi", buf.raw[:12])
        return seconds * 1000 + micros // 1000 if seconds > 0 else None
    except (OSError, ValueError, AttributeError, struct.error):
        return None


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
        if not NAMES_RE.search(" ".join(words)):
            continue
        found = agent_words(_argv(pid) or words)
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
        # The shell Claude Code started for the Bash call: its start time
        # names the call, more surely than the agent's own start time.
        shell = next((c for c, parent in zip([pid] + chain, chain) if parent == mine), None)
        result.append({"pid": pid, "tool": tool, "words": [w[:300] for w in words[:200]],
                       "startedAt": _start_ms(pid) or started,
                       "shellStartedAt": _start_ms(shell) if shell else None, "cwd": None})
    known = set(known or [])
    cwds = _cwds([p["pid"] for p in result if p["pid"] not in known])
    for p in result:
        p["cwd"] = cwds.get(p["pid"])
    return result
