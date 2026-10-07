"""Shared helpers for the per-tool readers. See CONTRACT.md."""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Iterable

PROGRESS_RE = re.compile(r"PROGRESS\s+(\d+)\s*/\s*(\d+)")
LINK_BEFORE_MS = 5_000
LINK_AFTER_MS = 120_000
LINK_END_SLACK_MS = 2_000

FIELDS = (
    "found", "sessionId", "state", "model", "effort", "contextUsed",
    "contextWindow", "inputTokens", "outputTokens", "progress", "steps",
    "lastActivity", "lastActivityAt", "endedAt", "children", "error",
)


def empty_result(run: dict) -> dict:
    out = {name: None for name in FIELDS}
    out["id"] = run.get("id")
    out["found"] = False
    out["sessionId"] = run.get("sessionId")
    out["state"] = "unknown"
    return out


def connect_ro(path: str) -> sqlite3.Connection:
    """Open a SQLite store read-only, safe while its tool is writing."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    return conn


def real(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return os.path.realpath(os.path.expanduser(path))
    except OSError:
        return path


def same_dir(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and real(a) == real(b)


def in_link_window(started_ms: float, run: dict) -> bool:
    start = run["startedAt"]
    if not start - LINK_BEFORE_MS <= started_ms < start + LINK_AFTER_MS:
        return False
    # A run that already ended (a launch that failed at once) cannot own a
    # session that started after it; that session belongs to a later run.
    ended = run.get("endedBy")
    return ended is None or started_ms <= ended + LINK_END_SLACK_MS


def pick_session(candidates: Iterable[dict], run: dict) -> dict | None:
    """Apply the contract's linking rule.

    Each candidate is {"id", "startedAt" (ms), "cwd" (or None)}.
    """
    claimed = set(run.get("claimed") or [])
    pool = [c for c in candidates
            if c["id"] not in claimed and in_link_window(c["startedAt"], run)]
    # Prefer a session in the run's folder. The mod's idea of that folder can
    # be wrong (the Bash tool's cwd moves), so fall back to the time window.
    if run.get("cwd"):
        matched = [c for c in pool if same_dir(c.get("cwd"), run["cwd"])]
        if matched:
            pool = matched
    pool.sort(key=lambda c: c["startedAt"])
    return pool[0] if pool else None


def find_progress(text: str | None) -> dict | None:
    if not text:
        return None
    hits = PROGRESS_RE.findall(text)
    if not hits:
        return None
    done, total = (int(n) for n in hits[-1])
    if total <= 0 or done > total:
        return None
    return {"done": done, "total": total}


def one_line(text: str | None, limit: int = 120) -> str | None:
    if not text:
        return None
    # PROGRESS markers already show as the progress bar; keep the real text.
    stripped = PROGRESS_RE.sub("", str(text))
    line = " ".join(stripped.split()) or " ".join(str(text).split())
    if not line:
        return None
    return line if len(line) <= limit else line[: limit - 1] + "…"


def tail_lines(path: str, max_bytes: int = 2_000_000) -> list[str]:
    """Last lines of a file, reading at most max_bytes from its end."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    return lines


SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|passw(or)?d|bearer|authorization|sk-[a-z0-9]{8,}|ghp_[a-z0-9]{8,})")
PATH_KEYS = ("file_path", "filePath", "path", "absolute_path", "notebook_path")
COMMAND_KEYS = ("command", "cmd", "script")
PATTERN_KEYS = ("pattern", "query", "url")


def tool_summary(name: str | None, args: object = None) -> str | None:
    """`tool: short argument` for the activity line.

    Shows a command's first 60 characters, a file's name (not its folder) or
    a search pattern. Anything that looks like a credential shows the tool
    name alone.
    """
    if not name:
        return None
    detail = None
    if isinstance(args, str):
        try:
            import json
            parsed = json.loads(args)
            args = parsed if isinstance(parsed, dict) else {"command": args}
        except ValueError:
            args = {"command": args}
    if isinstance(args, dict):
        for key in COMMAND_KEYS:
            value = args.get(key)
            if isinstance(value, list):
                value = " ".join(str(v) for v in value)
            if isinstance(value, str) and value.strip():
                detail = " ".join(value.split())[:60]
                break
        if detail is None:
            for key in PATH_KEYS:
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    detail = os.path.basename(value.rstrip("/")) or value
                    break
        if detail is None:
            for key in PATTERN_KEYS:
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    detail = value[:60]
                    break
    if detail and not SECRET_RE.search(detail):
        return one_line(f"{name}: {detail}")
    return one_line(name)
