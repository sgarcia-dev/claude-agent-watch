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

# Context limits as Claude Code 2.1.296 sets them, read from its built-in
# model table and compaction code. These models get a 1M window; any other
# gets 200K unless its name ends in [1m].
NATIVE_1M = {
    "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-opus-5-5",
    "claude-sonnet-5", "claude-sonnet-5-5", "claude-haiku-5-5",
    "claude-fable-5", "claude-fable-5-1", "claude-mythos-5", "claude-mythos-5-1",
}
# Claude Code keeps room for the reply (the model's output cap, at most 20K;
# every model above allows more) plus a 13K buffer, and compacts at the rest.
REPLY_RESERVE = 20_000
COMPACT_BUFFER = 13_000
MIN_COMPACT_WINDOW = 100_000


def _flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _tokens(value):
    """A token count such as 400000, 400k or 0.4m, as Claude Code accepts it."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    scale = {"k": 1_000, "m": 1_000_000}.get(text[-1:], 1)
    try:
        number = float(text[:-1] if scale > 1 else text) * scale
    except ValueError:
        return None
    return int(number) if number > 0 else None


def _window(model):
    if _flag("CLAUDE_CODE_DISABLE_1M_CONTEXT"):
        return 200_000
    name = model.lower()
    if name.endswith("[1m]"):
        return 1_000_000
    return 1_000_000 if re.sub(r"-\d{8}$", "", name) in NATIVE_1M else 200_000


def _compact_at(window, root):
    """Where Claude Code compacts: the auto-compact window (the model's,
    unless the env or settings lower it) less the reply room and buffer.
    Claude Code can also tune the window from its servers, which no local
    file shows, so this is the default rule."""
    if _flag("DISABLE_AUTO_COMPACT") or _flag("DISABLE_COMPACT"):
        return None
    chosen = _tokens(os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"))
    if chosen is None:
        try:
            settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
            chosen = _tokens(settings.get("autoCompactWindow")) if isinstance(settings, dict) else None
        except (OSError, ValueError):
            chosen = None
    if chosen is not None:
        window = min(window, max(MIN_COMPACT_WINDOW, chosen))
    usable = window - REPLY_RESERVE
    at = usable - COMPACT_BUFFER
    try:
        share = float(os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", ""))
        if 0 < share <= 100:
            at = min(int(usable * share / 100), at)
    except ValueError:
        pass
    return at


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


def _read(path, out, root):
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
        out["contextWindow"] = _window(model)
        out["compactAt"] = _compact_at(out["contextWindow"], root)
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
            if tools:
                out["lastActivity"] = common.tool_summary(tools[-1].get("name"), tools[-1].get("input"))
                out["lastActivityAt"] = _milliseconds(activity_record.get("timestamp"))
        if out["lastMessage"] is None:
            out["lastMessage"] = common.message_line(text)
            if out["lastMessage"]:
                out["lastMessageAt"] = _milliseconds(activity_record.get("timestamp"))
        if None not in (out["progress"], out["lastActivity"], out["lastMessage"]):
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
                    _read(candidate, out, root)
                    return out
                candidates.append(candidate)
        if candidates:
            _read(max(candidates, key=lambda c: c[0])[1], out, root)
    except Exception as exc:
        # Do not expose store paths or transcript content in errors.
        out["error"] = common.one_line(f"{type(exc).__name__}: Claude transcript read failed")
    return out
