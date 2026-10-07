"""Read Antigravity conversation databases without a protobuf dependency."""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import re
import time
from urllib.parse import unquote, urlsplit

try:
    from . import common
except ImportError:  # The isolated entry point imports readers as top-level modules.
    import common

MAX_FILES = 128
MAX_STEPS = 128
MAX_GENERATIONS = 512
MAX_BLOB = 2_000_000
QUIET_MS = 30_000
ACTIVE = {1, 2, 8, 11}  # pending, running, generating, queued
FAILED = {6, 7, 12}  # canceled, error, interrupted
REQUESTED_MODEL_RE = re.compile(rb"gemini-[0-9][0-9.]*-[a-z]+(?:-[a-z]+)*?-(minimal|low|medium|high)\b")


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("truncated protobuf varint")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ValueError("protobuf varint overflow")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("protobuf varint overflow")


def _decode(data: bytes | None) -> list[tuple[int, int, int | bytes]]:
    """Decode one wire message. Nested messages are visited by known field paths."""
    if data is None:
        return []
    if not isinstance(data, bytes) or len(data) > MAX_BLOB:
        raise ValueError("unsupported protobuf blob")
    fields = []
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not 0 < number < 1 << 29:
            raise ValueError("invalid protobuf field")
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = _varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            end = offset + size
            if end > len(data):
                raise ValueError("truncated protobuf field")
            value = data[offset:end]
            if wire != 2:
                value = int.from_bytes(value, "little")
            offset = end
        else:
            raise ValueError("unsupported protobuf wire type")
        fields.append((number, wire, value))
    return fields


def _at(data: bytes | None, *path: int, wire: int = 2):
    for index, number in enumerate(path):
        expected = wire if index == len(path) - 1 else 2
        values = [v for f, w, v in _decode(data) if f == number and w == expected]
        if not values:
            return None
        data = values[-1]
    return data


def _text(data: bytes | None) -> str | None:
    if data is None:
        return None
    return data.decode("utf-8") or None


def _timestamp(data: bytes | None) -> int | None:
    seconds = _at(data, 1, wire=0)
    nanos = _at(data, 2, wire=0) or 0
    if seconds is None or not 0 <= nanos < 1_000_000_000:
        return None
    if seconds >= 1 << 63:  # Signed int64 Timestamp seconds.
        seconds -= 1 << 64
    return seconds * 1000 + nanos // 1_000_000


def _cwd(data: bytes | None) -> str | None:
    # CortexWorkspaceMetadata.workspace_folder_absolute_uri, never its git root.
    uris = []
    for number, wire, workspace in _decode(data):
        if number == 1 and wire == 2:
            uri = _text(_at(workspace, 1))
            if uri:
                parsed = urlsplit(uri)
                if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
                    uris.append(unquote(parsed.path))
    paths = {common.real(p) for p in uris if os.path.isabs(p)}
    if len(paths) > 1:
        raise ValueError("multiple conversation workspaces")
    return next(iter(paths), None)


def _open(path: Path, deadline: float):
    conn = common.connect_ro(str(path))
    conn.execute("PRAGMA busy_timeout=20")
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    return conn


def _candidate(conn, path: Path) -> dict:
    first = conn.execute("SELECT metadata FROM steps ORDER BY idx LIMIT 1").fetchone()
    row = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
    trajectory = row[0] if row else None
    start = _timestamp(_at(first[0], 1)) if first else None
    if start is None:
        start = _timestamp(_at(trajectory, 2))
    if start is None:
        birth = getattr(path.stat(), "st_birthtime", None)
        start = int(birth * 1000) if birth is not None else None
    return {"id": path.stem, "startedAt": start, "cwd": _cwd(trajectory)}


def _activity_time(metadata: bytes | None) -> int | None:
    times = [_timestamp(_at(metadata, f)) for f in (1, 6, 7, 8, 22, 32)]
    return max((t for t in times if t is not None), default=None)


def _read(conn, path: Path, out: dict, now_ms: int) -> None:
    out["steps"] = conn.execute("SELECT count(*) FROM steps").fetchone()[0]
    rows = conn.execute(
        "SELECT idx,step_type,status,metadata,step_payload FROM steps "
        "ORDER BY idx DESC LIMIT ?", (MAX_STEPS,)
    ).fetchall()
    if rows:
        latest = rows[0]
        status = latest["status"]
        if status in ACTIVE:
            out["state"] = "running"
        elif status == 9:
            out["state"] = "waiting"
        elif status in FAILED:
            out["state"] = "failed"
        elif status == 3:
            # Include SQLite sidecars because a WAL writer need not touch the DB.
            changed = path.stat().st_mtime_ns // 1_000_000
            for suffix in ("-wal", "-journal"):
                sidecar = Path(str(path) + suffix)
                try:
                    changed = max(changed, sidecar.stat().st_mtime_ns // 1_000_000)
                except FileNotFoundError:
                    pass
            if now_ms - changed >= QUIET_MS:
                out["state"] = "done"
        name = _text(_at(latest["metadata"], 4, 2))
        if name and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:/-]{0,99}", name):
            out["lastActivity"] = common.one_line(name)
        elif latest["step_type"] == 15:
            out["lastActivity"] = common.one_line("assistant response")
        out["lastActivityAt"] = _activity_time(latest["metadata"])
    for row in rows:
        if row["step_type"] != 15:
            continue
        response = _at(row["step_payload"], 20)
        text = _text(_at(response, 8)) or _text(_at(response, 1))
        progress = common.find_progress(text)
        if progress is not None:
            out["progress"] = progress
            break
    generations = conn.execute(
        "SELECT data FROM gen_metadata ORDER BY idx DESC LIMIT ?",
        (MAX_GENERATIONS + 1,)
    ).fetchall()
    if generations:
        model = _text(_at(generations[0][0], 1, 19))
        if model and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}", model):
            out["model"] = model
        # Field 19 holds an internal ID (gemini-3.8-flash-n). The blob also
        # carries the requested name with its effort suffix; prefer that one,
        # since it matches the --model flag people type.
        named = REQUESTED_MODEL_RE.search(bytes(generations[0][0] or b""))
        if named:
            out["model"] = named.group(0).decode("ascii")
            out["effort"] = named.group(1).decode("ascii")
        # Decode only the verified usage message, never prompts or retry copies.
        usages = [_at(row[0], 1, 4) for row in generations]
        outputs = [_at(usage, 3, wire=0) if usage is not None else None
                   for usage in usages]
        if len(generations) <= MAX_GENERATIONS and all(v is not None for v in outputs):
            out["outputTokens"] = sum(outputs)
        # Fields 2 and 5 are input_tokens and cache_read_tokens. Their relation
        # to full occupancy was not verified, so input/context totals stay null.


def probe(run: dict, now_ms: int) -> dict:
    out = common.empty_result(run)
    deadline = time.monotonic() + 1.5
    try:
        root = Path(os.environ.get("AGENT_WATCH_AGY_HOME", "~/.gemini/antigravity-cli")).expanduser()
        folder = root / "conversations"
        if not folder.is_dir():
            return out
        session_id = run.get("sessionId")
        claimed = set(run.get("claimed") or [])
        if session_id:
            if session_id in claimed:
                return out
            if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
                raise ValueError("invalid conversation id")
            path = folder / (session_id + ".db")
            if not path.is_file():
                return out
        else:
            paths = sorted(folder.glob("*.db"), key=lambda p: p.stat().st_mtime_ns,
                           reverse=True)[:MAX_FILES]
            candidates = []
            problem = None
            for path in paths:
                if path.stem in claimed:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("conversation scan exceeded budget")
                try:
                    with closing(_open(path, deadline)) as conn:
                        candidate = _candidate(conn, path)
                    # common.pick_session leaves unmatched known cwd out when
                    # a cwd was supplied. Apply strict matching even if absent.
                    if candidate["cwd"] and not common.same_dir(candidate["cwd"], run.get("cwd")):
                        continue
                    if candidate["startedAt"] is not None:
                        candidates.append(candidate)
                except Exception as exc:
                    problem = type(exc).__name__
            picked = common.pick_session(candidates, run)
            if picked is None:
                if problem:
                    out["error"] = common.one_line("conversation read failed: " + problem)
                return out
            path = folder / (picked["id"] + ".db")
        with closing(_open(path, deadline)) as conn:
            out.update(found=True, sessionId=path.stem)
            _read(conn, path, out, now_ms)
    except Exception as exc:
        # Exception text can contain real paths or session text. Keep only type.
        out["error"] = common.one_line("Antigravity reader: " + type(exc).__name__)
    return out
