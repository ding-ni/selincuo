"""Permanent deletion is exercised only against newly created isolated objects.

The test workspaces are deliberately retained for inspection; no fixture
teardown clears a directory or touches the user's platform recycle bin.
"""
import json
import os
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

from backend.common import ROOT, canonical, now
from backend.database import Database, SCHEMA
from backend import permanent_deletion as deletion


class PermanentDeletionTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'build' / '第三轮永久删除隔离测试' / uuid.uuid4().hex
        (self.root / 'database').mkdir(parents=True)
        self.db = Database.__new__(Database)
        self.db.workspace = self.root
        self.db.path = self.root / 'database' / 'hydrology.sqlite3'
        with self.db.connect() as conn:
            conn.executescript(SCHEMA)
        self.ids = {}

    def task(self, suffix, *, trashed=True, favorite=False, kind='simulation', request=None, result=None):
        identifier = 'JOB-test-' + suffix
        at = now()
        with self.db.connect() as conn:
            conn.execute('''INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result,trashed,favorite,name)
                            VALUES(?,?,?,?,?,?,?,?,?,?)''',
                         (identifier, kind, 'succeeded', at, at, canonical(request or {}),
                          canonical(result or {}), int(trashed), int(favorite), '测试成果' + suffix))
        self.ids[suffix] = identifier
        return identifier

    def state(self, task, *, name='', favorite=False):
        identifier = 'STATE-test-' + task.removeprefix('JOB-test-')
        with self.db.connect() as conn:
            conn.execute('INSERT INTO states VALUES(?,?,?,?,?,?,?)',
                         (identifier, task, now(), '2024-01-01', '{}', name, int(favorite)))
        return identifier

    def parameter(self, suffix, source, trashed=True):
        identifier = 'PARAM-test-' + suffix
        self.db.save_item('parameter_versions', identifier, '测试参数' + suffix, {'sourceTaskId': source})
        self.db.set_setting('parameter-hidden:' + identifier, trashed)
        return identifier

    def scheme(self, suffix, parameter=None, trashed=True, revisions=1):
        family = 'FAMILY-test-' + suffix
        for revision in range(1, revisions + 1):
            identifier = f'SCHEME-test-{suffix}-{revision}'
            self.db.save_item('schemes', identifier, '测试方案' + suffix,
                              {'schemaVersion': 2, 'familyId': family, 'revision': revision,
                               'adoptedParameterId': parameter})
        self.db.set_setting('scheme-hidden:' + family, trashed)
        return identifier

    def write(self, identifier, name, content=b'test'):
        path = self.root / 'tasks' / identifier / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    @staticmethod
    def objects(**groups):
        return [{'kind': kind, 'id': identifier} for kind, ids in groups.items() for identifier in ids]

    def test_deletes_only_owned_files_and_preserves_shared_hardlink(self):
        task = self.task('owned')
        self.state(task)
        self.write(task, 'hourly.zarr/chunk', b'owned-data')
        shared = self.root / 'inputs' / 'shared.bin'
        shared.parent.mkdir(parents=True)
        shared.write_bytes(b'shared-input')
        link = self.root / 'tasks' / task / 'shared.bin'
        os.link(shared, link)
        objects = self.objects(task=[task])
        plan = deletion.preview(self.db, objects)
        self.assertEqual((plan['deletableCount'], plan['fileCount']), (1, 2))
        self.assertEqual(plan['estimatedReclaimBytes'], len(b'owned-data'))
        result = deletion.execute(self.db, objects, plan['token'])
        self.assertEqual(result['status'], 'deleted')
        self.assertEqual(shared.read_bytes(), b'shared-input')
        self.assertFalse((self.root / 'tasks' / task).exists())
        self.assertFalse(self.db.rows('SELECT id FROM tasks WHERE id=?', (task,)))
        self.assertFalse(self.db.rows('SELECT id FROM states WHERE task_id=?', (task,)))
        self.assertEqual(deletion.execute(self.db, objects, plan['token']), result)
        journal = self.db.rows('SELECT plan FROM permanent_deletion_operations WHERE token=?', (plan['token'],))[0]
        self.assertNotIn('files', json.loads(journal['plan']))

    def test_dependency_closure_allows_selecting_related_trashed_objects_together(self):
        task = self.task('source')
        state = self.state(task)
        continuation = self.task('continuation', request={'stateId': state})
        parameter = self.parameter('saved', task)
        scheme = self.scheme('family', parameter, revisions=2)
        with self.db.connect() as conn:
            conn.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                         (now(), 'historical.weak', canonical({'task': task, 'scheme': scheme})))
        first = deletion.preview(self.db, self.objects(task=[task]))
        self.assertEqual(first['deletableCount'], 0)
        related = [reason['related'] for reason in first['items'][0]['blocked'] if reason.get('related')]
        self.assertEqual({(item['kind'], item['id']) for item in related},
                         {('parameter', parameter), ('task', continuation)})
        self.assertTrue(all(item['trashed'] for item in related))
        objects = self.objects(task=[task, continuation], parameter=[parameter], scheme=[scheme])
        plan = deletion.preview(self.db, objects)
        self.assertEqual((plan['deletableCount'], plan['blockedCount']), (4, 0))
        self.assertEqual(deletion.execute(self.db, objects, plan['token'])['status'], 'deleted')
        self.assertFalse(self.db.rows('SELECT id FROM schemes'))
        self.assertFalse(self.db.rows('SELECT id FROM parameter_versions'))
        self.assertTrue(self.db.rows("SELECT id FROM audit WHERE action='historical.weak'"))

    def test_nonselected_adopted_parameter_blocks_its_source_even_when_selected_parameter_is_blocked(self):
        task = self.task('closure')
        parameter = self.parameter('closure', task)
        self.scheme('active', parameter, trashed=False)
        plan = deletion.preview(self.db, self.objects(task=[task], parameter=[parameter]))
        self.assertEqual(plan['deletableCount'], 0)
        self.assertTrue(all(item['blocked'] for item in plan['items']))

    def test_changed_reference_or_file_rejects_old_preview_before_any_deletion(self):
        task = self.task('stale')
        original = self.write(task, 'result.json', b'original')
        objects = self.objects(task=[task])
        before = deletion.preview(self.db, objects)
        parameter = self.parameter('new-reference', task, trashed=False)
        with self.assertRaisesRegex(ValueError, '已变化'):
            deletion.execute(self.db, objects, before['token'])
        self.assertEqual(original.read_bytes(), b'original')
        self.assertTrue(self.db.rows('SELECT id FROM parameter_versions WHERE id=?', (parameter,)))
        self.db.set_setting('parameter-hidden:' + parameter, True)
        plan = deletion.preview(self.db, self.objects(task=[task], parameter=[parameter]))
        self.write(task, 'new.bin', b'new')
        with self.assertRaisesRegex(ValueError, '已变化'):
            deletion.execute(self.db, self.objects(task=[task], parameter=[parameter]), plan['token'])
        self.assertTrue(original.exists())

    def test_partial_failure_is_visible_locked_and_resumable_after_reopen(self):
        task = self.task('partial')
        state = self.state(task)
        self.write(task, 'a.bin', b'a')
        self.write(task, 'b.bin', b'b')
        objects = self.objects(task=[task])
        plan = deletion.preview(self.db, objects)
        real_remove = deletion._remove_file
        count = [0]
        def fail_second(path):
            count[0] += 1
            if count[0] == 2:
                raise OSError('模拟文件被占用')
            real_remove(path)
        with patch.object(deletion, '_remove_file', side_effect=fail_second):
            partial = deletion.execute(self.db, objects, plan['token'])
        self.assertEqual((partial['status'], partial['deletedFileCount'], partial['remainingFileCount']), ('partial', 1, 1))
        self.assertTrue(self.db.rows('SELECT id FROM tasks WHERE id=?', (task,)))
        self.assertTrue(self.db.rows('SELECT id FROM states WHERE id=?', (state,)))
        with self.assertRaisesRegex(ValueError, '尚未完成'):
            deletion.guard_pending(self.db, 'task', task)
        with self.assertRaisesRegex(ValueError, '尚未完成'):
            deletion.guard_state(self.db, state)
        with self.assertRaisesRegex(ValueError, '未完成'):
            deletion.guard_no_pending(self.db)
        again = deletion.preview(self.db, objects)
        self.assertTrue(again['resume'])
        self.assertEqual(again['token'], plan['token'])
        unrelated = self.task('partial-unrelated-update', kind='update-forcing', trashed=False)
        with self.db.connect() as conn:
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (unrelated,))
        self.assertEqual(deletion.execute(self.db, again['objects'], again['token'])['status'], 'deleted')

    def test_database_finalize_failure_preserves_all_indices_and_retry_finishes(self):
        task = self.task('rollback')
        state = self.state(task)
        self.write(task, 'a.bin', b'a')
        self.db.set_setting('test-finalize-failure', True)
        with self.db.connect() as conn:
            conn.execute('''CREATE TRIGGER test_block_task_delete BEFORE DELETE ON tasks
                            WHEN (SELECT value FROM settings WHERE key='test-finalize-failure')='true'
                            BEGIN SELECT RAISE(ABORT, 'test failure'); END''')
        objects = self.objects(task=[task])
        plan = deletion.preview(self.db, objects)
        result = deletion.execute(self.db, objects, plan['token'])
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(self.db.rows('SELECT id FROM tasks WHERE id=?', (task,)))
        self.assertTrue(self.db.rows('SELECT id FROM states WHERE id=?', (state,)))
        self.db.set_setting('test-finalize-failure', False)
        self.assertEqual(deletion.execute(self.db, objects, plan['token'])['status'], 'deleted')

    def test_owned_export_is_included_and_favorite_export_is_protected(self):
        task = self.task('export-source')
        self.write(task, 'daily.csv', b'data')
        export = self.task('export', trashed=False, kind='export-result', request={'taskId': task},
                           result={'sourceTaskId': task, 'artifacts': [{'storage': 'task', 'ownerTaskId': task}]})
        self.write(export, 'hourly.npz', b'cache')
        objects = self.objects(task=[task])
        plan = deletion.preview(self.db, objects)
        self.assertEqual((plan['ownedExportCount'], plan['fileCount']), (1, 2))
        self.assertEqual(deletion.execute(self.db, objects, plan['token'])['status'], 'deleted')
        self.assertFalse(self.db.rows('SELECT id FROM tasks WHERE id=?', (export,)))
        second = self.task('favorite-source')
        self.task('favorite-export', favorite=True, kind='export-result', request={'taskId': second})
        self.assertEqual(deletion.preview(self.db, self.objects(task=[second]))['deletableCount'], 0)

    def test_named_state_active_task_favorite_and_untrusted_paths_are_protected(self):
        task = self.task('protected', favorite=True)
        self.state(task, name='用户状态')
        self.write(task, 'data', b'x')
        plan = deletion.preview(self.db, self.objects(task=[task]))
        self.assertEqual(plan['deletableCount'], 0)
        self.assertGreaterEqual(len(plan['items'][0]['blocked']), 2)
        with self.assertRaises(ValueError):
            deletion.preview(self.db, [{'kind': 'task', 'id': '../inputs'}])
        with self.assertRaises(ValueError):
            deletion.preview(self.db, [{'kind': 'task', 'id': task, 'path': 'anything'}])
        active = self.task('active', trashed=False)
        with self.db.connect() as conn:
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (active,))
        self.assertEqual(deletion.preview(self.db, self.objects(task=[task]))['deletableCount'], 0)

    def test_only_relevant_running_tasks_block_deletion_and_failed_continuations_do_not(self):
        source = self.task('active-source')
        state = self.state(source)
        parameter = self.parameter('active-parameter', source)
        scheme = self.scheme('active-scheme', parameter)
        objects = self.objects(task=[source], parameter=[parameter], scheme=[scheme])
        unrelated = self.task('forcing-update', kind='update-forcing', trashed=False)
        with self.db.connect() as conn:
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (unrelated,))
        self.assertEqual(deletion.preview(self.db, objects)['deletableCount'], 3)

        active = self.task('active-evaluation', kind='evaluation', trashed=False,
                           request={'candidateResultId': source, 'candidateParameterId': parameter,
                                    'researchSchemeId': scheme})
        with self.db.connect() as conn:
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (active,))
        blocked = deletion.preview(self.db, objects)
        self.assertEqual(blocked['deletableCount'], 0)
        self.assertTrue(all(any('正在使用' in reason['message'] for reason in item['blocked'])
                            for item in blocked['items']))
        with self.db.connect() as conn:
            conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (active,))
        continuation = self.task('failed-continuation', request={'stateId': state}, trashed=False)
        with self.db.connect() as conn:
            conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (continuation,))
        self.assertEqual(deletion.preview(self.db, objects)['deletableCount'], 3)
        with self.db.connect() as conn:
            conn.execute("UPDATE tasks SET status='succeeded' WHERE id=?", (continuation,))
        self.assertEqual(deletion.preview(self.db, objects)['deletableCount'], 2)

        backup = self.task('active-backup', kind='backup', trashed=False)
        with self.db.connect() as conn:
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (backup,))
        self.assertEqual(deletion.preview(self.db, objects)['deletableCount'], 0)

    def test_current_and_immediate_rollback_release_are_protected(self):
        current = self.task('current')
        previous = self.task('previous')
        older = self.task('older')
        with self.db.connect() as conn:
            conn.execute('CREATE TABLE operational_releases(sequence INTEGER PRIMARY KEY,id TEXT,previous_id TEXT,result_id TEXT)')
            conn.executemany('INSERT INTO operational_releases VALUES(?,?,?,?)', [
                (1, 'release-older', None, older),
                (2, 'release-previous', 'release-older', previous),
                (3, 'release-current', 'release-previous', current),
            ])
        plan = deletion.preview(self.db, self.objects(task=[current, previous, older]))
        states = {item['id']: item['deletable'] for item in plan['items']}
        self.assertEqual(states, {current: False, previous: False, older: True})

    def test_clear_preview_accepts_more_than_200_objects(self):
        identifiers = []
        for index in range(205):
            identifiers.append(self.scheme('bulk-' + str(index)))
        plan = deletion.preview(self.db, self.objects(scheme=identifiers))
        self.assertEqual((plan['deletableCount'], plan['blockedCount']), (205, 0))

    def test_http_partial_recovery_and_guards_use_an_isolated_platform(self):
        from backend.server import Platform
        server = Platform(('127.0.0.1', 0), self.root / 'http-workspace')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}/api/model/'
        def post(path, body):
            request = urllib.request.Request(base + path, data=canonical(body).encode('utf-8'),
                                             headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        try:
            identifier = 'JOB-http-isolated'
            at = now()
            with server.db.connect() as conn:
                conn.execute('''INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result,name)
                                VALUES(?,?,?,?,?,?,?,?)''',
                             (identifier, 'simulation', 'succeeded', at, at, '{}', '{}', '隔离成果'))
            folder = server.db.workspace / 'tasks' / identifier
            folder.mkdir(parents=True)
            (folder / 'a.bin').write_bytes(b'a')
            (folder / 'b.bin').write_bytes(b'b')
            post('tasks/' + identifier + '/metadata', {'trashed': True})
            objects = self.objects(task=[identifier])
            plan = post('permanent-delete/preview', {'objects': objects})
            real_remove = deletion._remove_file
            count = [0]
            def fail_second(path):
                count[0] += 1
                if count[0] == 2:
                    raise OSError('模拟占用')
                real_remove(path)
            with patch.object(deletion, '_remove_file', side_effect=fail_second):
                partial = post('permanent-delete/execute', {'objects': objects, 'token': plan['token']})
            self.assertEqual((partial['status'], partial['deletedFileCount'], partial['remainingFileCount']), ('partial', 1, 1))
            for route, body in [('tasks/' + identifier + '/metadata', {'trashed': False}),
                                ('tasks/' + identifier + '/cleanup-preview', {}),
                                ('tasks/' + identifier + '/cleanup-execute', {'token': 'invalid'}),
                                ('backup', {}), ('operational-release', {})]:
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    post(route, body)
                self.assertEqual(failure.exception.code, 400)
            resumed = post('permanent-delete/preview', {'objects': objects})
            self.assertTrue(resumed['resume'])
            done = post('permanent-delete/execute', {'objects': resumed['objects'], 'token': resumed['token']})
            self.assertEqual(done['status'], 'deleted')
            self.assertEqual(post('permanent-delete/execute', {'objects': objects, 'token': plan['token']}), done)
            self.assertFalse(server.db.rows('SELECT id FROM tasks WHERE id=?', (identifier,)))
        finally:
            server.shutdown()
            server.jobs.close()
            server.server_close()

    def test_recycle_bin_listing_includes_entries_older_than_normal_poll_limit(self):
        from backend.server import Platform
        server = Platform(('127.0.0.1', 0), self.root / 'list-workspace')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        at = now()
        with server.db.connect() as conn:
            conn.executemany('''INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result,trashed,name)
                                VALUES(?,?,?,?,?,?,?,?,?)''', [
                (f'JOB-list-{index:04}', 'simulation', 'succeeded', at, at, '{}', '{}', 1,
                 '回收成果' + str(index)) for index in range(505)])
            conn.execute('''INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result,trashed,name)
                            VALUES(?,?,?,?,?,?,?,?,?)''',
                         ('JOB-list-export', 'export-result', 'succeeded', at, at, '{}', '{}', 1, '导出'))
        try:
            url = f'http://127.0.0.1:{server.server_port}/api/model/'
            with urllib.request.urlopen(url + 'tasks', timeout=20) as response:
                self.assertEqual(len(json.load(response)['tasks']), 500)
            with urllib.request.urlopen(url + 'trashed-results', timeout=20) as response:
                rows = json.load(response)['tasks']
            self.assertEqual(len(rows), 505)
            self.assertNotIn('JOB-list-export', {row['id'] for row in rows})
        finally:
            server.shutdown()
            server.jobs.close()
            server.server_close()

    def test_failed_cancelled_and_interrupted_tasks_can_be_recycled_and_restored(self):
        from backend.server import Platform
        server = Platform(('127.0.0.1', 0), self.root / 'failed-lifecycle-workspace')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}/api/model/'
        at = now()
        rows = [('JOB-failed-test', 'simulation', 'failed'),
                ('JOB-cancelled-test', 'trial', 'cancelled'),
                ('JOB-interrupted-test', 'calibration-workflow', 'interrupted'),
                ('JOB-running-test', 'simulation', 'running')]
        with server.db.connect() as conn:
            conn.executemany('''INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result,name)
                                VALUES(?,?,?,?,?,?,?,?)''',
                             [(identifier, kind, status, at, at, '{}', '{}', identifier)
                              for identifier, kind, status in rows])
        folder = server.db.workspace / 'tasks' / 'JOB-failed-test'
        folder.mkdir(parents=True)
        (folder / 'failure-log.txt').write_text('isolated test', encoding='utf-8')

        def post(path, payload):
            request = urllib.request.Request(base + path, data=canonical(payload).encode('utf-8'),
                                             headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)

        def trash_ids():
            with urllib.request.urlopen(base + 'trashed-results', timeout=20) as response:
                return {row['id'] for row in json.load(response)['tasks']}

        try:
            for identifier, _, _ in rows[:3]:
                post('tasks/' + identifier + '/metadata', {'trashed': True})
            self.assertEqual(trash_ids(), {row[0] for row in rows[:3]})
            with self.assertRaises(urllib.error.HTTPError) as failure:
                post('tasks/JOB-running-test/metadata', {'trashed': True})
            self.assertEqual(failure.exception.code, 400)
            # Even an inconsistent pre-existing active row must not appear as
            # a selectable recycled result.
            with server.db.connect() as conn:
                conn.execute("UPDATE tasks SET trashed=1 WHERE id='JOB-running-test'")
            self.assertNotIn('JOB-running-test', trash_ids())
            post('tasks/JOB-cancelled-test/metadata', {'trashed': False})
            self.assertNotIn('JOB-cancelled-test', trash_ids())
            objects = self.objects(task=['JOB-failed-test'])
            plan = post('permanent-delete/preview', {'objects': objects})
            self.assertEqual((plan['deletableCount'], plan['fileCount']), (1, 1))
            done = post('permanent-delete/execute', {'objects': objects, 'token': plan['token']})
            self.assertEqual(done['status'], 'deleted')
            self.assertNotIn('JOB-failed-test', trash_ids())
            self.assertIn('JOB-interrupted-test', trash_ids())
        finally:
            server.shutdown()
            with server.db.connect() as conn:
                conn.execute("UPDATE tasks SET status='interrupted' WHERE id='JOB-running-test'")
            server.jobs.close()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
