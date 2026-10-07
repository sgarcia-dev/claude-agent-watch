"""Read Claude Code subagent transcripts without modifying the store."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

try:
    from . import common
except ImportError:
    import common

TAIL_BYTES = 2_000_000
MAX_SESSIONS = 128
MAX_CANDIDATES = 256


def _milliseconds(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                return int(stamp.timestamp() * 1000)
        except ValueError:
            pass
    return None


def _record(line):
    try:
        record = json.loads(line)
        return record if isinstance(record, dict) else None
    except ValueError:
        return None  # A live writer can leave its final line incomplete.


def _paths(root, cwd, agent_id, deadline):
    if agent_id:
        # A known agent ID names its file; the session's project folder may
        # not match the cwd the mod saw, so look it up directly.
        found = sorted((root / "projects").glob(f"*/*/subagents/agent-{agent_id}.jsonl"))
        if found:
            return found[:1]
    if not cwd:
        return []
    directories = set()
    for spelling in (cwd, common.real(cwd)):
        path = Path(spelling)
        for parent in (path, *path.parents):
            for slug in (str(parent).replace("/", "-"),
                         re.sub(r"[^a-zA-Z0-9]", "-", str(parent))):
                directories.add(root / "projects" / slug)
    files = []
    for project in sorted(directories):
        if time.monotonic() >= deadline:
            break
        if not project.is_dir():
            continue
        sessions = sorted((p for p in project.iterdir() if p.is_dir()),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        for session in sessions[:MAX_SESSIONS]:
            if time.monotonic() >= deadline:
                break
            directory = session / "subagents"
            if not directory.is_dir():
                continue
            if agent_id:
                path = directory / f"agent-{agent_id}.jsonl"
                if path.is_file():
                    files.append(path)
            else:
                files.extend(directory.glob("agent-*.jsonl"))
    return sorted(set(files), key=lambda p: p.stat().st_mtime,
                  reverse=True)[:MAX_CANDIDATES]


def _candidate(path, run):
    with path.open("rb") as stream:
        first = _record(stream.readline(262_144).decode("utf-8", "replace"))
    if not first:
        return None
    agent_id = first.get("agentId") or path.stem.removeprefix("agent-")
    if agent_id in (run.get("claimed") or []):
        return None
    if run.get("sessionId"):
        return path if agent_id == run["sessionId"] else None
    cwd = first.get("cwd")
    if cwd and not common.same_dir(cwd, run.get("cwd")):
        return None
    started = _milliseconds(first.get("timestamp"))
    if started is None or not common.in_link_window(started, run):
        return None
    agent_type = first.get("agentType")
    meta = path.with_suffix(".meta.json")
    if meta.is_file():
        with meta.open("rb") as stream:
            metadata = _record(stream.read(65_536).decode("utf-8", "replace"))
        if metadata:
            agent_type = metadata.get("agentType", agent_type)
    if agent_type and agent_type != run.get("command"):
        return None
    return (started, path)


def _input(usage):
    values = [usage.get(key) for key in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    if all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in values):
        return sum(values)
    return None


def _read(path, out):
    complete = path.stat().st_size <= TAIL_BYTES
    records = [r for line in common.tail_lines(str(path), TAIL_BYTES)
               if (r := _record(line)) is not None]
    out.update(found=True, sessionId=path.stem.removeprefix("agent-"))
    responses = {}
    assistants = []
    for index, record in enumerate(records):
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        assistants.append((index, record, message))
        response_id = message.get("id")
        if response_id:
            # Each split block repeats usage. The last block has final usage.
            previous = responses.get(response_id, {})
            responses[response_id] = {**previous, **message}
    if complete and len(responses) and all(m.get("id") for _, _, m in assistants):
        out["steps"] = len(responses)
        inputs = [_input(m.get("usage") or {}) for m in responses.values()]
        outputs = [(m.get("usage") or {}).get("output_tokens")
                   for m in responses.values()]
        if all(n is not None for n in inputs):
            out["inputTokens"] = sum(inputs)
        if all(isinstance(n, int) and not isinstance(n, bool) and n >= 0
               for n in outputs):
            out["outputTokens"] = sum(outputs)
    elif complete and not assistants:
        out["steps"] = 0
    if not assistants:
        return
    index, record, message = assistants[-1]
    model = message.get("model")
    out["model"] = model if isinstance(model, str) else None
    effort = record.get("effort") or record.get("perTurnEffort")
    out["effort"] = effort if isinstance(effort, str) else None
    out["contextUsed"] = _input(message.get("usage") or {})
    if out["model"]:
        # Explicit task rule. This default is not read from a model catalog.
        out["contextWindow"] = 1_000_000 if "[1m]" in model else 200_000
    explicit_window = next((r.get("contextWindow") for r in reversed(records)
                            if r.get("contextWindow") == 1_000_000), None)
    if explicit_window:
        out["contextWindow"] = explicit_window
    later_user = any(r.get("type") == "user" for r in records[index + 1:])
    out["state"] = "done" if message.get("stop_reason") == "end_turn" and not later_user else "running"
    if out["state"] == "done":
        out["endedAt"] = _milliseconds(record.get("timestamp"))
    for _, activity_record, activity_message in reversed(assistants):
        content = activity_message.get("content") or []
        if not isinstance(content, list):
            continue
        texts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(t for t in texts if isinstance(t, str))
        if out["progress"] is None:
            out["progress"] = common.find_progress(text)
        if out["lastActivity"] is None:
            tools = [b for b in content if isinstance(b, dict)
                     and b.get("type") == "tool_use"]
            activity = tools[-1].get("name") if tools else next(
                (t.splitlines()[0] for t in reversed(texts) if t), None)
            # Tool arguments can contain credentials. Report only its name.
            out["lastActivity"] = common.one_line(activity)
            if out["lastActivity"]:
                out["lastActivityAt"] = _milliseconds(activity_record.get("timestamp"))
        if out["progress"] is not None and out["lastActivity"] is not None:
            break


def probe(run: dict, now_ms: int) -> dict:
    out = common.empty_result(run)
    try:
        root = Path(os.environ.get("AGENT_WATCH_CLAUDE_HOME", "~/.claude")).expanduser()
        if not root.is_dir():
            return out
        agent_id = run.get("sessionId")
        if agent_id and (not isinstance(agent_id, str) or
                         re.fullmatch(r"[A-Za-z0-9_-]+", agent_id) is None):
            out["error"] = "Invalid subagent ID"
            return out
        deadline = time.monotonic() + 1.5
        candidates = []
        for path in _paths(root, run.get("cwd"), agent_id, deadline):
            if time.monotonic() >= deadline:
                break
            candidate = _candidate(path, run)
            if candidate is not None:
                if agent_id:
                    _read(candidate, out)
                    return out
                candidates.append(candidate)
        if candidates:
            _read(max(candidates, key=lambda c: c[0])[1], out)
    except Exception as exc:
        # Do not expose store paths or transcript content in errors.
        out["error"] = common.one_line(f"{type(exc).__name__}: Claude transcript read failed")
    return out
