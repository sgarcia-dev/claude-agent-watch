"""Read OpenCode's local SQLite session store without modifying it."""
from __future__ import annotations

import json
import os
import time

try:
    from . import common
except ImportError:
    import common

MAX_MESSAGES = 512


def _object(value):
    if value is None:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _number(value):
    return value if type(value) is int and value >= 0 else None


def _sum_pair(a, b):
    a, b = _number(a), _number(b)
    return a + b if a is not None and b is not None else None


def _context_window(model):
    root = os.environ.get('AGENT_WATCH_OPENCODE_CACHE', os.path.expanduser('~/.cache/opencode'))
    try:
        with open(os.path.join(root, 'models.json'), encoding='utf-8') as fh:
            catalog = json.load(fh)
        return _number(catalog[model['providerID']]['models'][model['id']]['limit']['context'])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _activity(data, created):
    parts = data.get('content') or []
    # Tool arguments and results can contain credentials. Emit only the name.
    for part in reversed(parts):
        if part.get('type') == 'tool' and part.get('name'):
            stamp = part.get('time') or {}
            return common.one_line(part['name']), next(
                (stamp[k] for k in ('completed', 'ran', 'created')
                 if _number(stamp.get(k)) is not None), created)
    for part in reversed(parts):
        if part.get('type') == 'text' and part.get('text'):
            text = part['text'].splitlines()[0]
            stamp = data.get('time') or {}
            return common.one_line(text), next(
                (stamp[k] for k in ('completed', 'streamed', 'created')
                 if _number(stamp.get(k)) is not None), created)
    return None, None


def probe(run: dict, now_ms: int) -> dict:
    out = common.empty_result(run)
    conn = None
    try:
        root = os.environ.get('AGENT_WATCH_OPENCODE_DATA', os.path.expanduser('~/.local/share/opencode'))
        path = os.path.join(root, 'opencode.db')
        if not os.path.isfile(path):
            return out
        deadline = time.monotonic() + 1.5
        conn = common.connect_ro(path)
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        claimed = set(run.get('claimed') or [])
        sid = run.get('sessionId')
        if sid in claimed:
            return out
        if not sid:
            start = run['startedAt']
            rows = conn.execute(
                'SELECT id, time_created, directory FROM session_v2 '
                'WHERE parent_id IS NULL AND time_created >= ? AND time_created < ?',
                (start - common.LINK_BEFORE_MS, start + common.LINK_AFTER_MS))
            chosen = common.pick_session(
                ({'id': r['id'], 'startedAt': r['time_created'], 'cwd': r['directory']} for r in rows), run)
            if chosen is None:
                return out
            sid = chosen['id']
        session = conn.execute('SELECT * FROM session_v2 WHERE id=? AND parent_id IS NULL', (sid,)).fetchone()
        if session is None:
            return out
        out.update(found=True, sessionId=sid)
        idle = session['time_idle']
        if conn.execute("SELECT 1 FROM session_pending WHERE session_id=? AND type IN ('question','permission') LIMIT 1", (sid,)).fetchone():
            out['state'] = 'waiting'
        elif idle is not None:
            # Idle with no outcome yet is a pause between turns, not the end.
            outcome = session['idle_outcome']
            out['state'] = 'running' if outcome is None else {
                'succeeded': 'done', 'failed': 'failed', 'interrupted': 'failed'}.get(outcome, 'unknown')
            if out['state'] in ('done', 'failed'):
                out['endedAt'] = idle
        elif session['time_suspended'] is None:
            out['state'] = 'running'
        model = _object(session['model'])
        if model.get('providerID') and model.get('id'):
            out['model'] = model['providerID'] + '/' + model['id']
            out['contextWindow'] = _context_window(model)
        out['effort'] = model.get('variant')
        out['children'] = conn.execute('SELECT count(*) FROM session_v2 WHERE parent_id=?', (sid,)).fetchone()[0]
        count = conn.execute("SELECT count(*) FROM session_message WHERE session_id=? AND type='assistant'", (sid,)).fetchone()[0]
        out['steps'] = count
        messages = conn.execute(
            "SELECT data,time_created FROM session_message WHERE session_id=? AND type='assistant' ORDER BY seq DESC LIMIT ?",
            (sid, MAX_MESSAGES))
        input_total = output_total = 0
        input_known = output_known = True
        for index, row in enumerate(messages):
            data = _object(row['data'])
            tokens = data.get('tokens') or {}
            cached = tokens.get('cache') or {}
            used = _sum_pair(tokens.get('input'), cached.get('read'))
            output = _sum_pair(tokens.get('output'), tokens.get('reasoning'))
            if index == 0:
                out['contextUsed'] = used
            if tokens:
                input_known &= used is not None
                output_known &= output is not None
                input_total += used or 0
                output_total += output or 0
            elif not data.get('error'):
                # A request still streaming has no final usage yet.
                input_known = output_known = False
            if out['lastActivity'] is None:
                out['lastActivity'], out['lastActivityAt'] = _activity(data, row['time_created'])
            if out['progress'] is None:
                text = '\n'.join(p.get('text', '') for p in data.get('content', []) if p.get('type') == 'text')
                out['progress'] = common.find_progress(text)
        if idle is not None:
            out['inputTokens'] = _sum_pair(session['tokens_input'], session['tokens_cache_read'])
            out['outputTokens'] = _sum_pair(session['tokens_output'], session['tokens_reasoning'])
        elif count <= MAX_MESSAGES:
            out['inputTokens'] = input_total if input_known else None
            out['outputTokens'] = output_total if output_known else None
        return out
    except Exception as exc:
        # Do not include store paths or session content in errors.
        out['error'] = common.one_line('OpenCode reader: ' + type(exc).__name__)
        return out
    finally:
        if conn is not None:
            conn.close()
