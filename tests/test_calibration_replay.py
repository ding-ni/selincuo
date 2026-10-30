"""Focused checks for historical replay without starting the real search."""
from copy import deepcopy
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import calibration_replay as replay, server
from backend.calibration_replay_worker import WorkerControl, record_worker_environment
from backend.business_selection import visible_tasks
from backend.common import read_json, write_json
from backend.database import Database
from backend.jobs import Jobs


def fresh():
    # Deliberately keep isolated test objects; do not remove user files.
    return Path(tempfile.mkdtemp(prefix='selinco-replay-check-'))


class WorkerControlTests(unittest.TestCase):
    def setUp(self):
        self.root = fresh()
        self.path = self.root/'control.json'
        self.tick = 0.
        self.guard = WorkerControl(self.path, 'worker', monotonic=lambda: self.tick,
                                   wall_time=lambda: 1000. + self.tick)

    def record(self, *, token='worker', cancel=False, heartbeat=None):
        write_json(self.path, {'token': token, 'cancel': cancel,
                               'heartbeat': 1000. + self.tick if heartbeat is None else heartbeat})

    def test_transient_and_initial_read_failures_do_not_cancel(self):
        self.assertFalse(self.guard.cancelled())  # initial control not yet readable
        self.record()
        self.assertFalse(self.guard.cancelled())
        self.tick = 2.
        with patch.object(Path, 'read_text', side_effect=PermissionError('temporarily busy')):
            self.assertFalse(self.guard.cancelled())
        self.record()
        self.assertFalse(self.guard.cancelled())
        self.assertIsNone(self.guard.reason)

    def test_persistent_unreadable_or_stale_heartbeat_records_reason(self):
        self.record()
        self.assertFalse(self.guard.cancelled())
        self.tick = 21.
        with patch.object(Path, 'read_text', side_effect=PermissionError('busy')):
            self.assertTrue(self.guard.cancelled())
        reason = read_json(self.root/'control-reason-worker.json')
        self.assertEqual(reason['reason'], 'control-unreadable')
        self.assertIn('PermissionError', reason['lastReadError'])
        root = fresh()
        clock = [0.]
        guard = WorkerControl(root/'control.json', 'other', monotonic=lambda: clock[0],
                              wall_time=lambda: 2000. + clock[0])
        write_json(root/'control.json', {'token': 'other', 'cancel': False, 'heartbeat': 2000.})
        self.assertFalse(guard.cancelled())
        clock[0] = 21.
        self.assertTrue(guard.cancelled())
        self.assertEqual(read_json(root/'control-reason-other.json')['reason'], 'heartbeat-stale')

    def test_explicit_stop_and_changed_token_are_immediate(self):
        self.record(cancel=True)
        self.assertTrue(self.guard.cancelled())
        self.assertEqual(read_json(self.root/'control-reason-worker.json')['reason'], 'explicit-cancel')
        other = WorkerControl(self.root/'control.json', 'different', monotonic=lambda: self.tick,
                              wall_time=lambda: 1000. + self.tick)
        self.assertTrue(other.cancelled())
        self.assertEqual(read_json(self.root/'control-reason-different.json')['reason'], 'token-changed')

    def test_worker_records_actual_environment_and_checks_dependencies(self):
        import importlib.metadata
        import platform
        runtime = self.root/'replay-runtime'
        runtime.mkdir()
        expected = {'python': platform.python_version(), 'pythonVersion': sys.version,
                    'executable': str(Path(sys.executable).resolve()),
                    'platform': 'Windows-10-10.0.26100-SP0',
                    'packages': {'numpy': importlib.metadata.version('numpy')}}
        write_json(runtime/'worker-expected-environment-ok.json', expected)
        with patch('backend.calibration_replay_worker.platform.platform',
                   return_value='Windows-10-10.0.26200-SP0'):
            path = record_worker_environment(self.root, 'ok')
        actual = read_json(path)
        self.assertEqual(actual['differences']['platform']['expected'], 'Windows-10-10.0.26100-SP0')
        self.assertEqual(actual['differences']['platform']['actual'], 'Windows-10-10.0.26200-SP0')
        self.assertEqual(len(actual['actual']['executableSHA256']), 64)
        self.assertIn('numpy', actual['actual']['numericPackageFiles'])
        bad = deepcopy(expected)
        bad['packages']['numpy'] = 'different'
        write_json(runtime/'worker-expected-environment-bad.json', bad)
        with patch('backend.calibration_replay_worker.platform.platform',
                   return_value='Windows-10-10.0.26200-SP0'), self.assertRaises(ValueError):
            record_worker_environment(self.root, 'bad')
        self.assertIn('packages', read_json(runtime/'worker-environment-bad.json')['differences'])


class ReplayTests(unittest.TestCase):
    def test_worker_only_plan_migration_is_audited_and_rejects_other_changes(self):
        folder = fresh()
        old_sha, new_sha = next(iter(replay.WORKER_CONTROL_MIGRATIONS))
        self.assertEqual(replay.sha_file(replay.WORKER), new_sha)
        frozen = {'available': True, 'assets': [{'name': 'replayWorker', 'sha256': old_sha, 'bytes': 4588},
                                                {'name': 'forcing', 'sha256': 'unchanged', 'bytes': 1}],
                  'configuration': {'parameters': {'KC': .8}}, 'environment': {'python': 'same'}}
        frozen['planSHA256'] = replay.digest(frozen)
        current = deepcopy(frozen)
        current['assets'][0].update(sha256=new_sha, bytes=replay.WORKER.stat().st_size)
        current['planSHA256'] = replay.digest({k: v for k, v in current.items() if k != 'planSHA256'})
        write_json(folder/'replay-snapshot/plan.json', frozen)
        request = {'replay': {'planSHA256': frozen['planSHA256']}}
        with patch.object(replay, 'replay_plan', return_value=current), patch.object(replay, '_verify_snapshot'):
            self.assertEqual(replay.verify_inputs(None, request, folder), current)
            receipt = replay._record_worker_migration(folder, frozen, current)
            self.assertEqual(receipt['previousWorker']['sha256'], old_sha)
            self.assertEqual(receipt['currentWorker']['sha256'], new_sha)
            self.assertEqual(read_json(folder/'replay-snapshot/plan.json'), frozen)
            self.assertEqual(replay._record_worker_migration(folder, frozen, current), receipt)
            changed = deepcopy(current)
            changed['assets'][1]['sha256'] = 'different-forcing'
            changed['planSHA256'] = replay.digest({k: v for k, v in changed.items() if k != 'planSHA256'})
            with patch.object(replay, 'replay_plan', return_value=changed), self.assertRaises(ValueError):
                replay.verify_inputs(None, request, folder)
            tampered = deepcopy(current)
            tampered['configuration']['parameters']['KC'] = .9
            with patch.object(replay, 'replay_plan', return_value=tampered), self.assertRaises(ValueError):
                replay.verify_inputs(None, request, folder)

    def test_only_recorded_windows_build_change_is_allowed(self):
        old_sha, new_sha = next(iter(replay.WORKER_CONTROL_MIGRATIONS))
        frozen = {'available': True, 'assets': [{'name': 'replayWorker', 'sha256': old_sha, 'bytes': 10}],
                  'environment': {'platform': 'Windows-10-10.0.26100-SP0', 'packages': {'numpy': '1.26.4'}},
                  'configuration': {'KC': .8}, 'planSHA256': 'old'}
        current = deepcopy(frozen)
        current['assets'][0].update(sha256=new_sha, bytes=20)
        current['environment']['platform'] = 'Windows-10-10.0.26200-SP0'
        current['planSHA256'] = 'new'
        migration = replay._worker_migration(frozen, current)
        self.assertEqual(migration['environmentChange']['previous'], 'Windows-10-10.0.26100-SP0')
        self.assertEqual(migration['environmentChange']['current'], 'Windows-10-10.0.26200-SP0')
        saved_environment = {**frozen['environment'], 'allDistributions': [['numpy', '1.26.4']]}
        new_environment = {**current['environment'], 'allDistributions': [['numpy', '1.26.4']]}
        self.assertIsNotNone(replay._runtime_environment_migration(saved_environment, new_environment, migration))
        changed = deepcopy(new_environment)
        changed['packages']['numpy'] = 'other'
        with self.assertRaises(ValueError):
            replay._runtime_environment_migration(saved_environment, changed, migration)
        current['environment']['platform'] = 'Windows-10-10.0.26300-SP0'
        with self.assertRaises(ValueError):
            replay._worker_migration(frozen, current)

    def test_archived_package_matches_original_and_ignores_current_sources(self):
        files, hashes = replay._archive()
        self.assertEqual(len(files), 77)
        self.assertIn('backend/two_stage_calibration.py', files)
        self.assertNotIn('backend/calibration_replay.py', files)
        self.assertEqual(replay.digest(hashes), replay.BACKEND_SHA)

    def test_prepare_requires_preview_and_retains_frozen_internal_request(self):
        folder = fresh()
        original = {'parameters': {'KC': .8}, 'search': {'keys': ['KC']},
                    'periods': {'warmupStart': '2017-01-01'}, 'twoStage': {'mode': 'both'},
                    '_observations': {'flow': {'2024-01-01': 3}, 'level': {}},
                    '_fixedInitialization': {'time': '2021-04-23', 'level': 4543.66},
                    'adoptOnSuccess': True}
        write_json(folder/'original.json', original)
        preview = {'available': True, 'planSHA256': 'abc'}
        with patch.object(replay, 'replay_plan', return_value=preview), \
                patch.object(replay, '_source_paths', return_value={'sourceRequest': folder/'original.json'}):
            with self.assertRaisesRegex(ValueError, '预览'):
                replay.prepare_request(None, {'sourceTaskId': replay.SOURCE_ID, 'planSHA256': 'wrong'})
            request, plan = replay.prepare_request(None, {'sourceTaskId': replay.SOURCE_ID,
                                                          'planSHA256': 'abc', 'clientRequestId': 'r1'})
        self.assertEqual(plan, preview)
        self.assertEqual(request['_observations'], original['_observations'])
        self.assertEqual(request['_fixedInitialization'], original['_fixedInitialization'])
        self.assertEqual(request['parameters'], original['parameters'])
        self.assertTrue(request['adoptOnSuccess'])
        self.assertFalse(request['replay']['automaticAdoption'])
        self.assertEqual(read_json(folder/'original.json'), original)

    def test_comparison_applies_distinct_flow_lake_metric_and_parameter_tolerances(self):
        workspace = fresh()
        source = workspace/replay.SOURCE_ID/'joint'
        folder = workspace/'JOB-new'
        write_json(source/'candidate-pool.json', {'candidateIds': ['a', 'b']})
        write_json(folder/'joint/candidate-pool.json', {'candidateIds': ['a', 'b']})
        selection = {'trainingCandidateId': 'a', 'finalCandidateId': 'b', 'totalRescored': 2,
                     'eligibleRescored': 2, 'selectedForBasin': 2, 'basinCandidatesEvaluated': 2,
                     'refinement': {'evaluations': 1}, 'flowMetrics': {'all': {'nse': .72}},
                     'allDataEvaluation': {'rmse': .15}}
        day = {'date': '2024-01-01', **{k: 1. for k in
               ('duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total', 'lakeLevel')}}
        old = {'parameters': {'KC': .8}, 'daily': [day], 'calibrationWorkflow': {'selection': selection}}
        self.assertTrue(replay._comparison(folder, old, deepcopy(old))['passes'])
        for change in ('lake', 'flow', 'metric', 'parameter'):
            candidate = deepcopy(old)
            if change == 'lake': candidate['daily'][0]['lakeLevel'] += 2e-8
            if change == 'flow': candidate['daily'][0]['total'] += 2e-7
            if change == 'metric': candidate['calibrationWorkflow']['selection']['allDataEvaluation']['rmse'] += 2e-8
            if change == 'parameter': candidate['parameters']['KC'] += 2e-12
            self.assertFalse(replay._comparison(folder, old, candidate)['passes'], change)
        write_json(folder/'joint/candidate-pool.json', {'candidateIds': ['b', 'a']})
        self.assertFalse(replay._comparison(folder, old, deepcopy(old))['passes'])

    def test_queue_stop_resume_does_not_adopt_or_recapture_observations(self):
        root = fresh()
        db = Database(root)
        db.set_setting('business-adopted-scheme-v1', {'resultId': 'original'})
        request = {'projectId': 'selinco', 'contextKey': 'selinco:semidistributed-cold-lake:0p1deg',
                   'parameters': {'KC': .8}, 'search': {'keys': ['KC']}, 'periods': {}, 'twoStage': {},
                   '_observations': {'flow': {'2024-01-01': 3}, 'level': {}},
                   'adoptOnSuccess': True, 'replay': {'sourceTaskId': replay.SOURCE_ID,
                                                      'planSHA256': 'abc', 'automaticAdoption': False},
                   'purpose': 'development'}
        entered = threading.Event()
        runs = []
        def runner(_db, actual, _folder, _progress, cancelled):
            runs.append(deepcopy(actual))
            if len(runs) == 1:
                entered.set()
                while not cancelled(): time.sleep(.01)
                raise InterruptedError('已停止')
            return {'identity': {}, 'daily': [], 'replay': {'comparison': {'passes': True}}}
        jobs = Jobs(db)
        try:
            with patch.object(jobs, '_check_backend_identity'), \
                 patch.object(replay, 'prepare_request', return_value=(request, {'planSHA256': 'abc'})), \
                 patch.object(replay, 'freeze'), patch.object(replay, 'verify_inputs'), \
                 patch.object(replay, 'run', side_effect=runner), \
                 patch('backend.business_selection.adopt_result', side_effect=AssertionError('must not adopt')):
                task = jobs.submit('calibration-replay', {'sourceTaskId': replay.SOURCE_ID,
                                                          'planSHA256': 'abc', 'clientRequestId': 'once'})
                self.assertEqual(jobs.submit('calibration-replay', {'sourceTaskId': replay.SOURCE_ID,
                    'planSHA256': 'abc', 'clientRequestId': 'once'})['id'], task['id'])
                self.assertTrue(entered.wait(10))
                jobs.stop(task['id'])
                deadline = time.monotonic()+10
                while time.monotonic() < deadline and db.task(task['id'])['status'] not in ('cancelled', 'failed'):
                    time.sleep(.02)
                self.assertEqual(db.task(task['id'])['status'], 'cancelled')
                jobs.resume_calibration(task['id'])
                deadline = time.monotonic()+10
                while time.monotonic() < deadline and db.task(task['id'])['status'] not in ('succeeded', 'failed'):
                    time.sleep(.02)
                saved = db.task(task['id'])
                self.assertEqual(saved['status'], 'succeeded', saved['message'])
                self.assertEqual(saved['request']['_observations'], request['_observations'])
                self.assertEqual(db.setting('business-adopted-scheme-v1')['resultId'], 'original')
                self.assertEqual(len(runs), 2)
                self.assertEqual(runs[0], runs[1])
                rows = [{'id': task['id'], 'kind': 'calibration-replay', 'request': {}}]
                self.assertEqual(visible_tasks(db, rows), [])
                for status in ('queued', 'running', 'stopping'):
                    rows[0]['status'] = status
                    self.assertEqual(visible_tasks(db, rows), rows)
                rows[0]['status'] = 'succeeded'
                rows[0]['userSaved'] = True
                self.assertEqual(visible_tasks(db, rows), rows)
        finally:
            jobs.executor.shutdown(wait=True)

    def test_worker_obeys_preexisting_stop_before_loading_any_backend(self):
        root = fresh()
        runtime = root/'replay-runtime'
        write_json(runtime/'control.json', {'token': 'test', 'heartbeat': time.time(), 'cancel': True})
        app = runtime/'app'
        app.mkdir(parents=True)
        environment = dict(__import__('os').environ, SELINCO_APP_ROOT=str(app))
        child = subprocess.run([sys.executable, '-B', str(replay.WORKER), str(root), str(root), 'test'],
                               env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(child.returncode, 130)
        self.assertFalse((root/'result.json').exists())

    def test_worker_boots_archived_implementation_without_live_database(self):
        root = fresh()
        workspace = root/'source'
        folder = workspace/'tasks/JOB-replay'
        folder.mkdir(parents=True)
        assets = []
        paths = {}
        for name, version in [('spatial', replay.SPATIAL_ID), ('parentSpatial', replay.PARENT_SPATIAL_ID),
                              ('inputSpatial', replay.INPUT_SPATIAL_ID)]:
            path = root/(name+'.json')
            write_json(path, {'version': version})
            paths[name] = path
            assets.append(replay._asset(name, path))
        with patch.object(replay, '_source_paths', return_value=paths):
            app, cache = replay._runtime(SimpleNamespace(workspace=workspace), folder, {'assets': assets})
        request = folder/'replay-snapshot/sourceRequest.json'
        write_json(request, {'fixture': True})
        token = 'isolated'
        write_json(folder/'replay-runtime/control.json', {'token': token, 'heartbeat': time.time(), 'cancel': False})
        environment = dict(__import__('os').environ, SELINCO_APP_ROOT=str(app), SELINCO_WORKSPACE=str(cache),
                           XAJ_RUNTIME_DIR=str(cache), PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
                           PYTHONIOENCODING='utf-8')
        child = subprocess.run([sys.executable, '-B', str(replay.WORKER), str(folder), str(workspace), token],
                               cwd=str(app), env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=30)
        self.assertEqual(child.returncode, 1)
        self.assertIn('历史请求摘要不一致', child.stderr.decode('utf-8'))
        self.assertFalse((workspace/'database/hydrology.sqlite3').exists())
        self.assertFalse((folder/'result.json').exists())

    def test_http_routes_replay_through_preview_and_queue(self):
        fake_db = SimpleNamespace(task=lambda identifier: {'id': identifier})
        response = {'available': True, 'planSHA256': 'abc'}
        handler = SimpleNamespace(path='/api/model/tasks/'+replay.SOURCE_ID+'/replay-plan', db=fake_db,
                                  send_json=lambda value, status=200: (value, status))
        with patch.object(replay, 'replay_plan', return_value=response):
            self.assertEqual(server.Handler.get(handler), (response, 200))
        jobs = SimpleNamespace(lock=threading.RLock(), submit=lambda kind, payload: {'kind': kind, **payload})
        handler = SimpleNamespace(path='/api/model/tasks/'+replay.SOURCE_ID+'/replay', db=fake_db,
                                  server=SimpleNamespace(jobs=jobs), document=lambda: {'planSHA256': 'abc'},
                                  send_json=lambda value, status=200: (value, status))
        with patch('backend.permanent_deletion.guard_pending'):
            value, status = server.Handler.post(handler)
        self.assertEqual(status, 202)
        self.assertEqual(value['kind'], 'calibration-replay')
        self.assertEqual(value['sourceTaskId'], replay.SOURCE_ID)
        self.assertEqual(value['planSHA256'], 'abc')


if __name__ == '__main__':
    unittest.main()
