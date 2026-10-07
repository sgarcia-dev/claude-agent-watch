"""Entry point the mod runs. Reads runs on stdin, writes statuses on stdout.

See CONTRACT.md. One reader module per tool; a missing or failing reader
answers that run with an error instead of failing the whole call.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402

TOOLS = ("codex", "opencode", "agy", "hermes", "claude")


def probe_one(run: dict, now_ms: int) -> dict:
    tool = run.get("tool")
    if tool not in TOOLS:
        out = common.empty_result(run)
        out["error"] = f"unknown tool {tool!r}"
        return out
    try:
        reader = importlib.import_module(tool)
        out = reader.probe(run, now_ms)
    except Exception as exc:  # a reader bug must not hide the other runs
        out = common.empty_result(run)
        out["error"] = common.one_line(f"{type(exc).__name__}: {exc}")
    for name in common.FIELDS:
        out.setdefault(name, None)
    out["id"] = run.get("id")
    return out


def main() -> int:
    request = json.load(sys.stdin)
    now_ms = int(request.get("now") or time.time() * 1000)
    # Link in start order, and let each new link claim its session, so runs
    # launched seconds apart never share one session.
    runs = list(request.get("runs", []))
    order = sorted(range(len(runs)), key=lambda i: (runs[i].get("sessionId") is None, runs[i].get("startedAt", 0)))
    taken: dict[str, set] = {}
    for run in runs:
        if run.get("sessionId"):
            taken.setdefault(run.get("tool"), set()).add(run["sessionId"])
    answers: list[dict | None] = [None] * len(runs)
    for i in order:
        run = dict(runs[i])
        mine = taken.setdefault(run.get("tool"), set())
        run["claimed"] = sorted((set(run.get("claimed") or []) | mine) - {run.get("sessionId")})
        out = probe_one(run, now_ms)
        if out.get("found") and out.get("sessionId"):
            mine.add(out["sessionId"])
        answers[i] = out
    reply = {"runs": answers}
    scan = request.get("scan")
    if isinstance(scan, dict):
        # The process table: which agent CLIs this session has running now.
        try:
            import processes
            reply["processes"] = processes.scan(scan.get("windows"), scan.get("known"))
        except Exception as exc:
            reply["processes"] = None
            reply["scanError"] = common.one_line(f"{type(exc).__name__}: {exc}")
    json.dump(reply, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
