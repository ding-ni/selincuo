"""Retained workspaces; fake bin moves use rename and never erase fixtures."""
import json
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen

from backend.common import ROOT, canonical, now
from backend.database import Database, SCHEMA
from backend import windows_recycle as recycle
from backend import scheme_management


class WindowsRecycleTests(unittest.TestCase):
    def setUp(self):
        self.db = Database.__new__(Database)
        self.db.workspace = ROOT / 'build/系统回收站业务隔离验收' / uuid.uuid4().hex
        self.db.path = self.db.workspace / 'database/hydrology.sqlite3'
        self.db.path.parent.mkdir(parents=True)
        with self.db.connect() as conn:
            conn.executescript(SCHEMA)
        self.addCleanup(patch.stopall)
        patch('backend.scheme_management.builtin', return_value=None).start()
        patch('backend.permanent_deletion._current_config', return_value={}).start()

    def objects(self, suffix='one'):
        scheme, task, parameter = 'SCHEME-' + suffix, 'JOB-' + suffix, 'PARAM-' + suffix
        self.db.save_item('schemes', scheme, '隔离方案' + suffix,
                          {'schemaVersion': 2, 'familyId': scheme, 'revision': 1, 'adoptedParameterId': parameter})
        self.db.set_setting('scheme-hidden:' + scheme, True)
        self.db.save_item('parameter_versions', parameter, '隔离参数' + suffix, {'sourceTaskId': task})
        with self.db.connect() as conn:
            conn.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result,trashed,name) VALUES(?,?,?,?,?,?,?,?,?)',
                         (task, 'simulation', 'succeeded', now(), now(), canonical({'researchSchemeId': scheme}), '{}', 1, '隔离结果'))
        folder = self.db.workspace / 'tasks' / task
        folder.mkdir(parents=True)
        (folder / '记录.txt').write_text('仅隔离核验字节', encoding='utf-8')
        return [{'kind': 'scheme', 'id': scheme}], task, parameter

    def fake_bin(self, path):
        folder = self.db.workspace / '模拟系统回收站'
        folder.mkdir(exist_ok=True)
        destination = folder / path.name
        path.rename(destination)
        metadata = folder / (path.name + '.json')
        metadata.write_text(str(path), encoding='utf-8')
        return {'metadata': str(metadata), 'recoverable': str(destination), 'original': str(path)}

    def test_scheme_results_and_exclusive_parameters_are_previewed_and_recoverable(self):
        objects, task, parameter = self.objects()
        shared = self.db.workspace / 'inputs/original.txt'
        shared.parent.mkdir();shared.write_bytes(b'preserved')
        plan = recycle.preview(self.db, objects)
        self.assertEqual(plan['deletableCount'], 3)
        self.assertEqual(plan['automaticParameterCount'], 1)
        self.assertFalse(plan['irreversible'])
        self.assertEqual(plan['fileCount'], 1)
        with patch('backend.maintenance.recycle_native', side_effect=self.fake_bin):
            result = recycle.execute(self.db, objects, plan['token'])
        self.assertEqual(result['status'], 'recycled')
        self.assertTrue(shared.is_file())
        self.assertEqual(self.db.task(task)['result'], {})
        self.assertTrue(self.db.setting('parameter-hidden:' + parameter))
        with self.assertRaisesRegex(ValueError, '先从Windows'):
            recycle.guard_available(self.db, 'task', task)
        with self.assertRaisesRegex(ValueError, '先从Windows'):
            recycle.planner.execute(self.db, objects, plan['token'])
        self.assertEqual(recycle.execute(self.db, objects, plan['token'])['status'], 'recycled')
        (self.db.workspace / '模拟系统回收站' / task).rename(self.db.workspace / 'tasks' / task)
        recycle.guard_available(self.db, 'task', task)
        self.assertEqual(recycle.states(self.db), {})

    def test_changed_file_invalidates_preview_without_moving(self):
        objects, task, _ = self.objects()
        plan = recycle.preview(self.db, objects)
        (self.db.workspace / 'tasks' / task / '新文件.txt').write_text('new')
        with patch('backend.maintenance.recycle_native') as native:
            with self.assertRaisesRegex(ValueError, '变化'):
                recycle.execute(self.db, objects, plan['token'])
            native.assert_not_called()

    def test_native_failure_keeps_records_and_supports_same_operation_retry(self):
        first, a, _ = self.objects('a')
        second, b, _ = self.objects('b')
        objects = first + second
        plan = recycle.preview(self.db, objects)
        def fail_second(path):
            if path.name == b:
                raise RuntimeError('模拟Windows拒绝')
            return self.fake_bin(path)
        with patch('backend.maintenance.recycle_native', side_effect=fail_second):
            partial = recycle.execute(self.db, objects, plan['token'])
        self.assertEqual(partial['status'], 'pending')
        self.assertEqual(partial['recycledFolderCount'], 1)
        self.assertEqual(recycle.preview(self.db, objects)['token'], plan['token'])
        with self.assertRaisesRegex(ValueError, '尚未完成'):
            recycle.guard_available(self.db, 'task', a)
        with patch('backend.maintenance.recycle_native', side_effect=self.fake_bin) as native:
            result = recycle.execute(self.db, objects, plan['token'])
        self.assertEqual(result['status'], 'recycled')
        self.assertEqual(native.call_count, 1)
        self.assertEqual(len(self.db.rows('SELECT id FROM tasks')), 2)

    def test_current_adoption_and_shared_parameter_remain_protected(self):
        objects, task, parameter = self.objects()
        self.db.set_setting('business-adopted-scheme-v1', {'resultId': task, 'parameterId': parameter})
        plan = recycle.preview(self.db, objects)
        self.assertEqual(plan['deletableCount'], 1)  # metadata-only scheme
        self.assertEqual(plan['fileCount'], 0)

    def test_browser_cannot_supply_filesystem_paths(self):
        with self.assertRaises(ValueError):
            recycle.preview(self.db, [{'kind': 'task', 'id': '../outside'}])
        with self.assertRaises(ValueError):
            recycle.preview(self.db, [{'kind': 'task', 'id': 'JOB-a', 'path': 'C:/'}])

    def test_http_preview_and_execution_use_windows_endpoint(self):
        from backend.server import Handler
        objects, task, _ = self.objects()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.db = self.db
        server.jobs = SimpleNamespace(lock=threading.RLock(), restore_pending=False, restart_required=False)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        def post(action, value):
            request = Request('http://127.0.0.1:' + str(server.server_port) + '/api/model/windows-recycle/' + action,
                              data=canonical(value).encode(), headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        try:
            plan = post('preview', {'objects': objects})
            with patch('backend.maintenance.recycle_native', side_effect=self.fake_bin):
                result = post('execute', {'objects': objects, 'token': plan['token']})
            self.assertEqual(result['status'], 'recycled')
            self.assertTrue(self.db.task(task)['trashed'])
        finally:
            server.shutdown();server.server_close()


if __name__ == '__main__':
    unittest.main()
