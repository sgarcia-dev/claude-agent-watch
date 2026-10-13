"""Read bounded Codex rollout metadata and tails without changing the store."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from . import common
except ImportError:
    import common

TAIL_BYTES = 2_000_000
HEAD_BYTES = 262_144
# With no limit of its own, Codex compacts at 90% of the model's full window.
# Rollouts report 95% of that window as model_context_window. Seen in real
# rollouts: GPT-6 Sol (272K) compacts near 235K.
AUTO_COMPACT_SHARE = 0.9
REPORTED_SHARE = 0.95


def _compact_at(root, model, reported):
    """Codex's auto-compact point: the config's override, else the model
    catalog's limit, else 90% of the full window behind the session's own
    reported one. The catalog's window changes over time; the session's
    report is what that session got."""
    try:
        for line in (root / "config.toml").read_text(encoding="utf-8").splitlines():
            if line.startswith("["):
                break  # Only the top-level key applies.
            match = re.match(r"\s*model_auto_compact_token_limit\s*=\s*(\d+)", line)
            if match:
                return int(match.group(1))
    except OSError:
        pass
    share = REPORTED_SHARE
    try:
        catalog = json.loads((root / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        catalog = None
    models = catalog.get("models") if isinstance(catalog, dict) else catalog
    for entry in models if isinstance(models, list) else []:
        if isinstance(entry, dict) and entry.get("slug") == model:
            limit = _integer(entry.get("auto_compact_token_limit"))
            if limit:
                return limit
            percent = _integer(entry.get("effective_context_window_percent"))
            share = percent / 100 if percent else share
    if not reported:
        return None
    return int(round(reported / share) * AUTO_COMPACT_SHARE)


def _ms(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return int(stamp.timestamp() * 1000)
    except ValueError:
        return None


def _record(line):
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _folders(root, run):
    start = run["startedAt"]
    lower = datetime.fromtimestamp((start - common.LINK_BEFORE_MS) / 1000, timezone.utc)
    upper = datetime.fromtimestamp((start + common.LINK_AFTER_MS) / 1000, timezone.utc)
    dates = set()
    # Current Codex uses local dates. Also cover UTC layouts and UTC boundaries.
    for local in (False, True):
        first = (lower.astimezone() if local else lower).date()
        last = (upper.astimezone() if local else upper).date()
        while first <= last:
            dates.add(first)
            first += timedelta(days=1)
    return [root / "sessions" / day.strftime("%Y/%m/%d") for day in sorted(dates)]


def _meta(path):
    with path.open("rb") as fh:
        head = fh.read(HEAD_BYTES)
    for line in head.splitlines()[:8]:
        rec = _record(line)
        if rec.get("type") == "session_meta":
            payload = rec.get("payload")
            return payload if isinstance(payload, dict) else None
    return None


def _first_turn(path):
    """Codex writes turn_context once per turn; a long session's only one can
    sit before the tail the reader keeps, so look for it at the start."""
    with path.open("rb") as fh:
        head = fh.read(TAIL_BYTES)
    for line in head.splitlines():
        if b'"turn_context"' in line:
            rec = _record(line)
            if rec.get("type") == "turn_context" and isinstance(rec.get("payload"), dict):
                return rec["payload"]
    return {}


def _parent(meta):
    parent = meta.get("parent_thread_id")
    source = meta.get("source")
    if not parent and isinstance(source, dict):
        subagent = source.get("subagent")
        if isinstance(subagent, dict):
            spawn = subagent.get("thread_spawn")
            if isinstance(spawn, dict):
                parent = spawn.get("parent_thread_id")
    return parent


def _message(rec, payload, agent_path):
    typ = payload.get("type")
    if rec.get("type") == "event_msg" and typ == "agent_message":
        return payload.get("message")
    if rec.get("type") != "response_item":
        return None
    if typ == "message" and payload.get("role") != "assistant":
        return None
    if typ == "agent_message" and payload.get("author") != agent_path:
        return None
    if typ not in ("message", "agent_message"):
        return None
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c["text"] for c in content
                         if isinstance(c, dict) and isinstance(c.get("text"), str))
    return None


_CODE_CALL = re.compile(r"tools\.([A-Za-z_]+)\(\{(.*?)\}\)", re.S)
_CODE_ARG = re.compile(r'\b(cmd|command|path|file_path|pattern)\s*:\s*"((?:[^"\\]|\\.)*)"')


def _code_mode(source):
    """Code-mode calls are JavaScript: tools.exec_command({cmd:"..."})."""
    calls = [c for c in _CODE_CALL.findall(source) if c[0] != "write_stdin"]
    if not calls:
        return None
    name, body = calls[-1]
    args = {key: value for key, value in _CODE_ARG.findall(body)}
    return common.tool_summary(name, args)


def _activity(payload):
    name = payload.get("name")
    if not isinstance(name, str):
        return None
    code = payload.get("input")
    if isinstance(code, str) and "tools." in code:
        summary = _code_mode(code)
        if summary:
            return summary
    leaf = name.rsplit(".", 1)[-1]
    if leaf not in ("exec", "exec_command", "shell", "shell_command"):
        return common.one_line(name)
    args = payload.get("arguments", payload.get("input"))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = None
    if isinstance(args, dict):
        command = args.get("cmd", args.get("command"))
        if isinstance(command, list) and all(isinstance(c, str) for c in command):
            command = " ".join(command)
        if isinstance(command, str):
            return common.tool_summary(name, {"command": command})
    return common.one_line(name)


def _integer(value):
    return value if type(value) is int and value >= 0 else None


def _read(path, meta, out):
    complete_tail = path.stat().st_size <= TAIL_BYTES
    calls = 0
    pending = None
    out["state"] = "running"
    for line in common.tail_lines(str(path), max_bytes=TAIL_BYTES):
        rec = _record(line)
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        typ = payload.get("type")
        stamp = _ms(rec.get("timestamp"))
        if rec.get("type") == "turn_context":
            out["model"] = payload.get("model")
            out["effort"] = payload.get("effort")
        if rec.get("type") == "event_msg":
            if typ == "token_count" and isinstance(payload.get("info"), dict):
                info = payload["info"]
                last = info.get("last_token_usage") or {}
                total = info.get("total_token_usage") or {}
                out["contextUsed"] = _integer(last.get("input_tokens"))
                out["contextWindow"] = _integer(info.get("model_context_window"))
                out["inputTokens"] = _integer(total.get("input_tokens"))
                out["outputTokens"] = _integer(total.get("output_tokens"))
            elif typ in ("task_started", "task_complete", "turn_aborted"):
                pending = None
                out["state"] = {"task_started": "running", "task_complete": "done",
                                "turn_aborted": "failed"}[typ]
                out["endedAt"] = (None if typ == "task_started" else
                                  _ms(payload.get("completed_at")) or stamp)
        if rec.get("type") == "response_item":
            if typ in ("function_call", "custom_tool_call"):
                calls += 1
                name = payload.get("name", "")
                if name.rsplit(".", 1)[-1] in ("request_user_input", "request_user_input_async"):
                    pending = payload.get("call_id")
                    out["state"] = "waiting"
                elif out["state"] == "waiting":
                    pending = None
                    out["state"] = "running"
                out["lastActivity"] = _activity(payload)
                out["lastActivityAt"] = stamp
                # Some models print PROGRESS from a shell command instead.
                args = payload.get("arguments", payload.get("input"))
                progress = common.find_progress(args if isinstance(args, str) else None)
                if progress is not None:
                    out["progress"] = progress
            elif typ in ("function_call_output", "custom_tool_call_output"):
                if pending is not None and payload.get("call_id") == pending:
                    pending = None
                    if out["state"] == "waiting":
                        out["state"] = "running"
            elif typ == "message" and payload.get("role") == "user" and pending is not None:
                pending = None
                out["state"] = "running"
        text = _message(rec, payload, meta.get("agent_path") or "/root")
        if isinstance(text, str) and text.strip():
            progress = common.find_progress(text)
            if progress is not None:
                out["progress"] = progress
            message = common.message_line(text)
            if message:
                out["lastMessage"] = message
                out["lastMessageAt"] = stamp
    out["steps"] = calls if complete_tail else None


def probe(run: dict, now_ms: int) -> dict:
    out = common.empty_result(run)
    try:
        root = Path(os.environ.get("AGENT_WATCH_CODEX_HOME", "~/.codex")).expanduser()
        candidates = []
        for folder in _folders(root, run):
            for path in folder.glob("rollout-*.jsonl"):
                try:
                    meta = _meta(path)
                except FileNotFoundError:
                    continue
                if not meta or not isinstance(meta.get("id"), str):
                    continue
                start = _ms(meta.get("timestamp"))
                if start is not None:
                    candidates.append({"id": meta["id"], "startedAt": start,
                                       "cwd": meta.get("cwd"), "path": path, "meta": meta})
        claimed = set(run.get("claimed") or [])
        if run.get("sessionId"):
            chosen = next((c for c in candidates if c["id"] == run["sessionId"]
                           and c["id"] not in claimed), None)
        else:
            # A launched run is a root thread; never link it to a spawned child.
            chosen = common.pick_session([c for c in candidates if _parent(c["meta"]) is None], run)
        if chosen is None:
            return out
        out.update(found=True, sessionId=chosen["id"])
        _read(chosen["path"], chosen["meta"], out)
        if out["model"] is None:
            turn = _first_turn(chosen["path"])
            out["model"] = turn.get("model") if isinstance(turn.get("model"), str) else None
            out["effort"] = out["effort"] or (turn.get("effort") if isinstance(turn.get("effort"), str) else None)
        out["compactAt"] = _compact_at(root, out["model"], out["contextWindow"])
        parents = [_parent(c["meta"]) for c in candidates if c["id"] != chosen["id"]]
        # Absence of a parent field does not establish native subagent support.
        if ("parent_thread_id" in chosen["meta"] or _parent(chosen["meta"]) is not None
                or any(p is not None for p in parents)):
            out["children"] = len({c["id"] for c in candidates
                                   if _parent(c["meta"]) == chosen["id"]})
    except Exception as exc:
        # Do not leak store paths or session content through exception messages.
        out["error"] = common.one_line(f"Codex reader: {type(exc).__name__}")
    return out
