"""Scheme/result recycling is verified in retained, isolated SQLite workspaces."""
import json
import sqlite3
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.request import Request, urlopen
from unittest.mock import patch

from backend.common import ROOT, canonical, now
from backend.database import Database, SCHEMA
from backend.business_selection import CURRENT_SETTING
from backend import scheme_management as management


class SchemeManagementTests(unittest.TestCase):
    def setUp(self):
        self.db = Database.__new__(Database)
        self.db.workspace = ROOT / 'build' / '方案管理隔离验收' / uuid.uuid4().hex
        self.db.path = self.db.workspace / 'database/hydrology.sqlite3'
        self.db.path.parent.mkdir(parents=True)
        with self.db.connect() as connection:
            connection.executescript(SCHEMA)
        self.standard = patch('backend.scheme_management.builtin', return_value=None)
        self.standard.start()
        self.addCleanup(self.standard.stop)

    def scheme(self, name, revision=1, family=None, **fields):
        identifier = 'SCHEME-' + name + '-' + str(revision)
        self.db.save_item('schemes', identifier, name, {'schemaVersion': 2,
            'familyId': family or 'FAMILY-' + name, 'revision': revision, 'purpose': 'business', **fields})
        return identifier

    def task(self, name, scheme, status='succeeded', trashed=False, **request):
        identifier = 'JOB-' + name
        with self.db.connect() as connection:
            connection.execute('''INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result,trashed,name)
                VALUES(?,?,?,?,?,?,?,?,?)''', (identifier, 'trial', status, now(), now(),
                canonical({'researchSchemeId': scheme, **request}), canonical({'untouched': name}), int(trashed), name))
        return identifier

    def recycled(self, task):
        return bool(self.db.task(task)['trashed'])

    def test_all_revisions_and_results_recycle_and_restore_original_visibility(self):
        first = self.scheme('old')
        second = self.scheme('old', 2)
        a = self.task('first', first)
        b = self.task('second', second, status='failed')
        pretrashed = self.task('already-recycled', first, trashed=True)
        result_before = self.db.task(a)['result']
        folder = self.db.workspace / 'tasks' / a
        folder.mkdir(parents=True)
        original = folder / 'result.json'
        original.write_bytes(b'original artifact bytes')
        plan = management.preview(self.db, [first, second])
        self.assertEqual((plan['schemeCount'], plan['resultCount'], plan['alreadyTrashedResultCount']), (1, 2, 1))
        batch = management.recycle(self.db, [first, second], plan['token'])
        self.assertTrue(all(self.recycled(task) for task in (a, b, pretrashed)))
        self.assertTrue(self.db.setting('scheme-hidden:FAMILY-old'))
        self.assertEqual(self.db.task(a)['result'], result_before)
        self.assertEqual(original.read_bytes(), b'original artifact bytes')
        management.restore(self.db, batch['batchId'])
        self.assertFalse(self.recycled(a))
        self.assertFalse(self.recycled(b))
        self.assertTrue(self.recycled(pretrashed))
        self.assertFalse(self.db.setting('scheme-hidden:FAMILY-old'))
        self.assertEqual(management.restore(self.db, batch['batchId'])['status'], 'restored')

    def test_current_family_blocks_entire_mixed_selection(self):
        old, current = self.scheme('old'), self.scheme('current')
        task = self.task('old', old)
        self.db.set_setting(CURRENT_SETTING, {'schemeId': current, 'familyId': 'FAMILY-current'})
        plan = management.preview(self.db, [old, current])
        self.assertTrue(plan['blocked'])
        with self.assertRaisesRegex(ValueError, '正在采用'):
            management.recycle(self.db, [old, current], plan['token'])
        self.assertFalse(self.recycled(task))
        self.assertFalse(self.db.setting('scheme-hidden:FAMILY-old', False))

    def test_current_result_protects_source_family_even_if_adopted_family_differs(self):
        source = self.scheme('source')
        task = self.task('adopted-result', source)
        self.db.set_setting(CURRENT_SETTING, {'schemeId': 'other', 'familyId': 'other', 'resultId': task})
        self.assertTrue(management.preview(self.db, [source])['blocked'])

    def test_active_task_and_published_result_are_protected(self):
        active, published = self.scheme('active'), self.scheme('published')
        self.task('active', active, status='running')
        task = self.task('published', published)
        with self.db.connect() as connection:
            connection.execute('CREATE TABLE operational_releases(result_id TEXT)')
            connection.execute('INSERT INTO operational_releases VALUES(?)', (task,))
        self.assertTrue(management.preview(self.db, [active])['blocked'])
        self.assertTrue(management.preview(self.db, [published])['blocked'])

    def test_new_result_invalidates_preview_before_any_change(self):
        scheme = self.scheme('old')
        a = self.task('a', scheme)
        plan = management.preview(self.db, [scheme])
        b = self.task('b', scheme)
        with self.assertRaisesRegex(ValueError, '已变化'):
            management.recycle(self.db, [scheme], plan['token'])
        self.assertFalse(self.recycled(a) or self.recycled(b))

    def test_copy_provenance_does_not_claim_source_results(self):
        original = self.scheme('original')
        copied = self.scheme('copied', sourceCopy={'schemeId': original}, parameterSources={'sourceTaskId': 'JOB-original'})
        self.task('original', original)
        plan = management.preview(self.db, [copied])
        self.assertEqual(plan['resultCount'], 0)
        catalog = management.catalog(self.db)
        self.assertEqual(next(row['resultCount'] for row in catalog['schemes'] if row['id'] == copied), 0)

    def test_sql_failure_rolls_back_family_and_all_results(self):
        scheme = self.scheme('old')
        a, b = self.task('a', scheme), self.task('b', scheme)
        with self.db.connect() as connection:
            connection.execute("CREATE TRIGGER fail_fixture BEFORE UPDATE ON tasks WHEN NEW.id='JOB-b' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        plan = management.preview(self.db, [scheme])
        with self.assertRaises(sqlite3.IntegrityError):
            management.recycle(self.db, [scheme], plan['token'])
        self.assertFalse(self.recycled(a) or self.recycled(b))
        self.assertFalse(self.db.setting('scheme-hidden:FAMILY-old', False))
        self.assertFalse(management.catalog(self.db)['recycleBatches'])

    def test_missing_restore_object_does_not_partially_restore(self):
        scheme = self.scheme('old')
        task = self.task('a', scheme)
        plan = management.preview(self.db, [scheme])
        result = management.recycle(self.db, [scheme], plan['token'])
        key = management.PREFIX + result['batchId']
        batch = self.db.setting(key)
        batch['resultIds'].append('JOB-missing-fixture')
        self.db.set_setting(key, batch)
        with self.assertRaisesRegex(ValueError, '部分成果'):
            management.restore(self.db, result['batchId'])
        self.assertTrue(self.recycled(task))
        self.assertTrue(self.db.setting('scheme-hidden:FAMILY-old'))

    def test_http_catalog_preview_recycle_restore(self):
        from backend.server import Handler
        scheme = self.scheme('http')
        task = self.task('http', scheme)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.db = self.db
        server.jobs = SimpleNamespace(lock=threading.RLock(), restore_pending=False, restart_required=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port) + '/api/model/'
        def request(path, payload=None):
            req = Request(base + path, data=canonical(payload).encode() if payload is not None else None,
                          headers={'Content-Type': 'application/json'})
            with urlopen(req, timeout=20) as response:
                return json.load(response)
        try:
            self.assertEqual(request('scheme-management')['schemes'][0]['resultCount'], 1)
            plan = request('schemes/recycle-preview', {'schemeIds': [scheme]})
            result = request('schemes/recycle', {'schemeIds': [scheme], 'token': plan['token']})
            self.assertTrue(self.recycled(task))
            request('schemes/restore-batch', {'batchId': result['batchId']})
            self.assertFalse(self.recycled(task))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
