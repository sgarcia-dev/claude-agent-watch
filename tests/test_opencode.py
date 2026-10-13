"""Synthetic OpenCode fixtures. No real session data is used."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from probe import common, opencode


class OpenCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            'AGENT_WATCH_OPENCODE_DATA': str(self.root),
            'AGENT_WATCH_OPENCODE_CACHE': str(self.root),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db = sqlite3.connect(self.root / 'opencode.db')
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE session_v2 (
                id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, model TEXT,
                tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
                tokens_cache_read INTEGER, tokens_cache_write INTEGER,
                time_created INTEGER, time_updated INTEGER, time_idle INTEGER,
                idle_outcome TEXT, time_suspended INTEGER);
            CREATE TABLE session_message (
                session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, data TEXT);
            CREATE INDEX message_lookup ON session_message(session_id,type,seq);
            CREATE TABLE session_pending (session_id TEXT, type TEXT, data TEXT);
        ''')
        self.cwd = str(self.root / 'project')
        self.run = dict(id='run', tool='opencode', startedAt=100000,
                        cwd=self.cwd, sessionId=None, claimed=[])
        (self.root / 'models.json').write_text(json.dumps({
            'fixture': {'models': {'test-model': {'limit': {'context': 64000}}}}}))

    def session(self, sid='main', start=100000, cwd=None, parent=None,
                idle=None, outcome=None, suspended=None):
        self.db.execute('INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            sid, parent, self.cwd if cwd is None else cwd,
            json.dumps(dict(id='test-model', providerID='fixture', variant='high')),
            100, 20, 5, 50, 0, start, start, idle, outcome, suspended))
        self.db.commit()

    def message(self, seq, content, tokens=None, kind='assistant', sid='main'):
        data = dict(content=content, time={'created': 100000 + seq})
        if tokens is not None:
            data['tokens'] = tokens
        self.db.execute('INSERT INTO session_message VALUES (?,?,?,?,?)',
                        (sid, kind, seq, 100000 + seq, json.dumps(data)))
        self.db.commit()

    def probe(self, **changes):
        return opencode.probe(dict(self.run, **changes), 200000)

    def test_link_window_claimed_cwd_and_top_level(self):
        self.session('too-early', 94999)
        self.session('wrong-dir', 95000, str(self.root / 'elsewhere'))
        self.session('claimed', 95001)
        self.session('child', 95002, parent='claimed')
        self.session('chosen', 95003)
        self.session('later', 100001)
        self.session('upper-bound', 220000)
        self.assertEqual(self.probe(claimed=['claimed'])['sessionId'], 'chosen')
        self.assertFalse(self.probe(startedAt=225001)['found'])

    def test_window_boundaries(self):
        self.session('lower', 95000)
        self.assertTrue(self.probe()['found'])
        self.assertFalse(self.probe(startedAt=-25000)['found'])
        self.assertFalse(self.probe(startedAt=100001)['found'])

    def test_cwd_realpath_and_missing_directory(self):
        project = self.root / 'project'
        project.mkdir()
        (self.root / 'alias').symlink_to(project, target_is_directory=True)
        self.session()
        self.assertTrue(self.probe(cwd=str(self.root / 'alias'))['found'])
        # No folder match falls back to the time window (CONTRACT.md).
        self.assertTrue(self.probe(cwd=str(self.root / 'other'))['found'])

    def test_held_link_and_claimed_link(self):
        self.session(start=1)
        self.assertTrue(self.probe(sessionId='main', cwd='unrelated')['found'])
        self.assertFalse(self.probe(sessionId='main', claimed=['main'])['found'])
        self.session('child', parent='main')
        self.assertFalse(self.probe(sessionId='child')['found'])

    def test_running_usage_activity_and_progress(self):
        self.session()
        self.session('child', parent='main')
        self.message(1, [{'type': 'text', 'text': 'PROGRESS 1/4\nPROGRESS 2/4'}],
                     dict(input=10, output=2, reasoning=3, cache={'read': 4}))
        self.message(2, [{'type': 'text', 'text': 'A later message'},
                         {'type': 'tool', 'name': 'shell',
                          'state': {'input': {'command': 'secret fixture argument'}},
                          'time': {'created': 100002, 'completed': 100010}}],
                     dict(input=30, output=5, reasoning=7, cache={'read': 8}))
        out = self.probe()
        self.assertEqual(out['state'], 'running')
        self.assertEqual(out['contextUsed'], 38)
        self.assertEqual(out['inputTokens'], 52)
        self.assertEqual(out['outputTokens'], 17)
        self.assertEqual(out['progress'], dict(done=2, total=4))
        self.assertEqual(out['steps'], 2)
        self.assertEqual(out['lastActivity'], 'shell: secret fixture argument')
        self.assertEqual(out['lastActivityAt'], 100010)
        self.assertEqual(out['children'], 1)
        self.assertEqual(out['model'], 'fixture/test-model')
        self.assertEqual(out['effort'], 'high')
        self.assertEqual(out['contextWindow'], 64000)
        self.assertIsNone(out['endedAt'])
        self.assertEqual(set(out), set(common.FIELDS) | {'id'})

    def test_finished_uses_session_counters_and_latest_progress(self):
        self.session(idle=110000, outcome='succeeded')
        self.message(1, [{'type': 'text', 'text': 'PROGRESS 1/2'}])
        self.message(2, [{'type': 'text', 'text': 'Finished\nPROGRESS 2/2'}],
                     dict(input=3, output=1, reasoning=0, cache={'read': 2}))
        self.message(3, [{'type': 'text', 'text': 'PROGRESS 0/2'}], kind='user')
        out = self.probe()
        self.assertEqual(out['state'], 'done')
        self.assertEqual(out['endedAt'], 110000)
        self.assertEqual(out['inputTokens'], 150)
        self.assertEqual(out['outputTokens'], 25)
        self.assertEqual(out['contextUsed'], 5)
        self.assertEqual(out['lastMessage'], 'Finished')
        self.assertEqual(out['progress'], dict(done=2, total=2))

    def test_failed_aborted_and_unknown_outcomes(self):
        for outcome, state in [('failed', 'failed'), ('interrupted', 'failed'),
                               ('future-outcome', 'unknown')]:
            with self.subTest(outcome=outcome):
                self.session(outcome, idle=110000, outcome=outcome)
                out = self.probe(sessionId=outcome)
                self.assertEqual(out['state'], state)
                self.assertEqual(out['endedAt'], 110000 if state == 'failed' else None)

    def test_waiting_and_suspended(self):
        # Headless runs carry time_suspended from the start: still running.
        self.session(suspended=105000)
        self.assertEqual(self.probe()['state'], 'running')
        for kind in ('question', 'permission'):
            self.db.execute('DELETE FROM session_pending')
            self.db.execute('INSERT INTO session_pending VALUES (?,?,?)', ('main', kind, '{}'))
            self.db.commit()
            out = self.probe()
            self.assertEqual(out['state'], 'waiting')
            self.assertIsNone(out['endedAt'])

    def test_database_catalog_and_chatgpt_limit(self):
        self.session()
        self.db.executescript('''
            CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE credential (integration_id TEXT, value TEXT, active INTEGER, time_updated INTEGER);
        ''')
        body = {'fixture': {'models': {'test-model': {'limit': {'context': 90000, 'input': 80000}}}},
                'openai': {'models': {'gpt-6-sol': {'limit': {'context': 1050000, 'input': 922000}}}}}
        self.db.execute("INSERT INTO kv VALUES ('models-dev:catalog', ?)", (json.dumps({'body': json.dumps(body)}),))
        self.db.commit()
        self.assertEqual(self.probe()['contextWindow'], 80000)
        self.db.execute("UPDATE session_v2 SET model=?", (json.dumps(dict(id='gpt-6-sol', providerID='openai')),))
        self.db.execute("INSERT INTO credential VALUES ('openai', ?, 1, 1)", (json.dumps({'type': 'api'}),))
        self.db.commit()
        self.assertEqual(self.probe()['contextWindow'], 922000)
        self.db.execute("UPDATE credential SET value=?", (json.dumps({'type': 'oauth', 'access': 'synthetic'}),))
        self.db.commit()
        self.assertEqual(self.probe()['contextWindow'], 272000)

    def test_unknown_usage_and_catalog(self):
        self.session()
        self.message(1, [{'type': 'reasoning', 'text': 'PROGRESS 1/1'},
                         {'type': 'tool', 'name': 'read'}])
        (self.root / 'models.json').write_text('{broken')
        out = self.probe()
        for field in ('contextUsed', 'contextWindow', 'inputTokens', 'outputTokens', 'progress'):
            self.assertIsNone(out[field])
        self.assertIsNone(out['error'])

    def test_truncated_history_does_not_guess_totals(self):
        self.session()
        tokens = dict(input=1, output=1, reasoning=0, cache={'read': 0})
        for seq in range(opencode.MAX_MESSAGES + 1):
            self.message(seq, [], tokens)
        out = self.probe()
        self.assertEqual(out['steps'], opencode.MAX_MESSAGES + 1)
        self.assertEqual(out['contextUsed'], 1)
        self.assertIsNone(out['inputTokens'])
        self.assertIsNone(out['outputTokens'])

    def test_missing_store(self):
        with patch.dict(os.environ, {'AGENT_WATCH_OPENCODE_DATA': str(self.root / 'absent')}):
            out = self.probe()
        self.assertFalse(out['found'])
        self.assertIsNone(out['error'])
        self.assertFalse((self.root / 'absent').exists())

    def test_bad_json_is_contained(self):
        self.session()
        self.db.execute("UPDATE session_v2 SET model='{bad'")
        self.db.commit()
        out = self.probe()
        self.assertIn('JSONDecodeError', out['error'])


if __name__ == '__main__':
    unittest.main()
