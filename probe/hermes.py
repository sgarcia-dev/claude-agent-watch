"""Read Hermes session metadata without modifying its store."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time

try:
    from . import common
except ImportError:  # agent_probe.py imports readers as top-level modules.
    import common


# "tool" is what `hermes chat --source tool` writes (the task-hermes recipe).
CLI_SOURCES = ("cli", "oneshot", "tui", "api_server", "tool")
NORMAL_ENDS = {"agent_close", "cli_close", "session_reset", "session_switch"}
FAILED_ENDS = {"startup_orphan_reap"}
SESSION_FIELDS = (
    "id, source, parent_session_id, model, model_config, billing_base_url, "
    "ended_at, end_reason, input_tokens, output_tokens, api_call_count, "
    "last_activity_at, last_activity_description"
)


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _milliseconds(value):
    return int(value * 1000) if isinstance(value, (int, float)) else None


def _context_window(root: Path, store: Path, model, base_url):
    if not isinstance(model, str) or not isinstance(base_url, str):
        return None
    key = f"{model}@{base_url}"
    paths = dict.fromkeys((store.parent / "context_length_cache.yaml",
                           root / "context_length_cache.yaml"))
    for path in paths:
        if not path.is_file():
            continue
        # This cache uses a flat mapping, not general YAML.
        with path.open(encoding="utf-8") as handle:
            lines = handle.read(256_000).splitlines()
        in_mapping = False
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not line[0].isspace():
                in_mapping = stripped == "context_lengths:"
                continue
            if not in_mapping:
                continue
            entry, separator, number = stripped.rpartition(":")
            if not separator:
                continue
            entry = entry.strip()
            if len(entry) >= 2 and entry[0] == entry[-1] and entry[0] in "\"'":
                entry = entry[1:-1]
            number = number.split("#", 1)[0].strip()
            if entry == key and number.isdigit() and int(number) > 0:
                return int(number)
    return None


def _fill(out: dict, row: sqlite3.Row, conn, root: Path, store: Path):
    out.update(found=True, sessionId=row["id"], model=row["model"],
               inputTokens=_integer(row["input_tokens"]),
               outputTokens=_integer(row["output_tokens"]),
               steps=_integer(row["api_call_count"]),
               lastActivity=common.one_line(row["last_activity_description"]),
               lastActivityAt=_milliseconds(row["last_activity_at"]),
               endedAt=_milliseconds(row["ended_at"]))
    if row["ended_at"] is None:
        out["state"] = "running"
    elif row["end_reason"] in NORMAL_ENDS:
        out["state"] = "done"
    elif row["end_reason"] in FAILED_ENDS:
        out["state"] = "failed"
    else:
        out["state"] = "unknown"

    try:
        config = json.loads(row["model_config"] or "{}")
    except (ValueError, TypeError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    anchor = config.get("_usage_anchor")
    if isinstance(anchor, dict):
        out["contextUsed"] = _integer(anchor.get("prompt_tokens"))
    reasoning = config.get("reasoning_config")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        out["effort"] = reasoning["effort"] or None
    out["contextWindow"] = _context_window(root, store, row["model"], row["billing_base_url"])
    out["children"] = conn.execute(
        "SELECT count(*) FROM sessions WHERE parent_session_id = ?", (row["id"],)
    ).fetchone()[0]

    # Bound both row count and content size. Ignore user/tool progress packets.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    active = " AND active = 1" if "active" in columns else ""
    messages = conn.execute(
        "SELECT substr(content, -20000) AS content FROM messages "
        "WHERE session_id = ? AND role = 'assistant'" + active +
        " ORDER BY timestamp DESC, id DESC LIMIT 100", (row["id"],)
    )
    for message in messages:
        progress = common.find_progress(message["content"])
        if progress is not None:
            out["progress"] = progress
            break


def probe(run: dict, now_ms: int) -> dict:
    out = common.empty_result(run)
    connections = []
    errors = []
    deadline = time.monotonic() + 1.7
    try:
        root = Path(os.environ.get("AGENT_WATCH_HERMES_HOME", "~/.hermes")).expanduser()
        paths = [root / "state.db", *sorted(root.glob("profiles/*/state.db"))]
        candidates = []
        linked = run.get("sessionId")
        claimed = set(run.get("claimed") or [])
        if linked in claimed:
            return out
        for path in paths:
            if time.monotonic() >= deadline:
                errors.append("Hermes read deadline exceeded")
                break
            if not path.is_file():
                continue
            try:
                conn = common.connect_ro(str(path))
                connections.append(conn)
                conn.execute("PRAGMA busy_timeout = 50")
                conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                if linked:
                    row = conn.execute(
                        f"SELECT {SESSION_FIELDS} FROM sessions WHERE id = ?", (linked,)
                    ).fetchone()
                    if row and row["source"] in CLI_SOURCES and row["parent_session_id"] is None:
                        _fill(out, row, conn, root, path)
                        return out
                    continue
                rows = conn.execute(
                    "SELECT id, started_at, cwd FROM sessions "
                    "WHERE started_at >= ? AND started_at < ? "
                    "AND parent_session_id IS NULL "
                    "AND source IN ('cli', 'oneshot', 'tui', 'api_server', 'tool') "
                    "ORDER BY started_at ASC",
                    ((run["startedAt"] - common.LINK_BEFORE_MS) / 1000,
                     (run["startedAt"] + common.LINK_AFTER_MS) / 1000),
                )
                for row in rows:
                    candidates.append({"id": row["id"], "startedAt": row["started_at"] * 1000,
                                       "cwd": row["cwd"], "connection": conn, "store": path})
            except Exception as exc:
                errors.append(f"Hermes store read failed: {type(exc).__name__}")
        chosen = common.pick_session(candidates, run)
        if chosen:
            conn = chosen["connection"]
            row = conn.execute(f"SELECT {SESSION_FIELDS} FROM sessions WHERE id = ?",
                               (chosen["id"],)).fetchone()
            if row:
                _fill(out, row, conn, root, chosen["store"])
    except Exception as exc:
        errors.append(f"Hermes read failed: {type(exc).__name__}")
    finally:
        for conn in connections:
            conn.close()
        if errors:
            out["error"] = common.one_line("; ".join(dict.fromkeys(errors)))
    return out
