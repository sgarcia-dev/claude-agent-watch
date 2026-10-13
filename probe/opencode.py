"""Read OpenCode's local SQLite session store without modifying it."""
from __future__ import annotations

import json
import os
import re
import sqlite3
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


# OpenCode 2.0.24 caps OpenAI GPT models from 5.5 on when the user signs in
# with a ChatGPT account instead of an API key (read from its binary). Seen in
# real sessions: GPT-6 Sol compacts near 243K, not at the catalog's 1.05M.
CHATGPT_LIMIT = {'context': 400_000, 'input': 272_000}


def _catalog(conn):
    """The models.dev catalog: OpenCode 2 keeps it in its database; older
    versions in a cache file."""
    try:
        row = conn.execute("SELECT value FROM kv WHERE key='models-dev:catalog'").fetchone()
        if row:
            return json.loads(json.loads(row['value'])['body'])
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        pass
    root = os.environ.get('AGENT_WATCH_OPENCODE_CACHE', os.path.expanduser('~/.cache/opencode'))
    try:
        with open(os.path.join(root, 'models.json'), encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _chatgpt_login(conn, provider):
    # Only the credential's type is read, never its value's secrets.
    try:
        row = conn.execute(
            "SELECT json_extract(value, '$.type') AS type FROM credential "
            "WHERE integration_id=? AND active=1 ORDER BY time_updated DESC LIMIT 1", (provider,)).fetchone()
        return row is not None and row['type'] == 'oauth'
    except sqlite3.Error:
        return False


def _context_window(conn, model):
    """The most input the model gets: the input limit when the catalog has
    one, else the whole context limit."""
    try:
        limit = _catalog(conn)[model['providerID']]['models'][model['id']]['limit']
    except (KeyError, TypeError):
        limit = None
    match = re.match(r'gpt-(\d+)(?:\.(\d+))?', model['id'])
    if (model['providerID'] == 'openai' and match and
            (int(match.group(1)), int(match.group(2) or 0)) >= (5, 5) and _chatgpt_login(conn, 'openai')):
        limit = CHATGPT_LIMIT
    if not isinstance(limit, dict):
        return None
    return _number(limit.get('input')) or _number(limit.get('context'))


def _message(data, created):
    text = '\n'.join(p.get('text', '') for p in data.get('content') or [] if p.get('type') == 'text' and p.get('text'))
    stamp = data.get('time') or {}
    return common.message_line(text), next(
        (stamp[k] for k in ('completed', 'streamed', 'created') if _number(stamp.get(k)) is not None), created)


def _activity(data, created):
    parts = data.get('content') or []
    # common.tool_summary hides credential-looking values.
    for part in reversed(parts):
        if part.get('type') == 'tool' and part.get('name'):
            stamp = part.get('time') or {}
            args = (part.get('state') or {}).get('input') or part.get('input')
            return common.tool_summary(part['name'], args), next(
                (stamp[k] for k in ('completed', 'ran', 'created')
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
        else:
            # Headless `opencode run` sessions get time_suspended at start, so
            # it does not mean paused: not idle is running.
            out['state'] = 'running'
        model = _object(session['model'])
        if model.get('providerID') and model.get('id'):
            out['model'] = model['providerID'] + '/' + model['id']
            out['contextWindow'] = _context_window(conn, model)
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
            # Tokens written to the cache were read as input too.
            used = _sum_pair(_sum_pair(tokens.get('input'), cached.get('read')), cached.get('write', 0))
            output = _sum_pair(tokens.get('output'), tokens.get('reasoning'))
            # The newest message may still be streaming with no usage yet;
            # the latest one that has usage is the current context.
            if out['contextUsed'] is None and used is not None:
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
            if out['lastMessage'] is None:
                message, at = _message(data, row['time_created'])
                if message:
                    out['lastMessage'], out['lastMessageAt'] = message, at
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
