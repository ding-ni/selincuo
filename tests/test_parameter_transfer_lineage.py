"""Isolated HTTP and lineage tests for parameter-transfer strategies.

The HTTP server uses only an in-memory SQLite database and synthetic records;
it never contacts or changes the running platform workspace.
"""
from contextlib import contextmanager
from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from backend.common import canonical, digest, now
from backend.database import Database, SCHEMA
from backend.jobs import Jobs
from backend.model_core import DEFAULT_PARAMETERS
from backend.parameter_transfer import normalize_transfer_policy
from backend import business_context, grid_simulation, research_schemes as schemes
from backend.server import Handler


class MemoryDatabase(Database):
    def __init__(self):
        self.connection = sqlite3.connect(':memory:', check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        with self.connection:
            yield self.connection

    def add_task(self, identifier, result):
        at = now()
        with self.connect() as conn:
            conn.execute('''INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result,name)
                            VALUES(?,?,?,?,?,?,?,?)''',
                         (identifier, 'simulation', 'succeeded', at, at, '{}', canonical(result), identifier))


class ParameterTransferLineageTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDatabase()
        self.spatial = {'version': 'synthetic-space', 'unitStrategy': 'grid-partition-elevation-surface-v2',
                        'units': [{'id': 'synthetic-duja', 'partition': 'duja', 'kind': 'land', 'areaKm2': 100.}],
                        'grids': [{'id': 'synthetic-grid'}]}
        self.shared = {'modelFamily': 'grid-continuous-v1', 'spatialVersion': 'synthetic-space',
                       'routingMode': 'source-separated', 'parameters': deepcopy(DEFAULT_PARAMETERS),
                       'partitionParameters': {}, 'parameterTransfer': {'mode': 'duja-shared'}, 'identity': {}}
        self.old = {**deepcopy(self.shared), 'partitionParameters': {'duja': {'WM': 250.}}}
        self.old.pop('parameterTransfer')
        self.db.add_task('JOB-synthetic-shared', self.shared)
        self.db.add_task('JOB-synthetic-old', self.old)
        self.load_patch = patch.object(schemes.grid_spatial, 'load', return_value=self.spatial)
        self.load_patch.start()
        self.addCleanup(self.load_patch.stop)
        self.addCleanup(self.db.connection.close)

    def start_http(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.db = self.db
        server.jobs = SimpleNamespace(lock=threading.RLock(), restore_pending=False, restart_required=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}/api/model/parameters'

    def post(self, url, payload):
        request = urllib.request.Request(url, data=canonical(payload).encode('utf-8'),
                                         headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_http_rejects_incorrect_or_invalid_strategy_without_writing(self):
        url = self.start_http()
        for policy in ({'mode': 'duja-area-routing'}, None, {'mode': 'wrong'}):
            before = deepcopy(self.db.items('parameter_versions'))
            body = {'name': '错误策略', 'payload': {'sourceTaskId': 'JOB-synthetic-shared',
                                                   'parameters': self.shared['parameters'],
                                                   'partitionParameters': {}, 'parameterTransfer': policy}}
            with self.subTest(policy=policy):
                status, error = self.post(url, body)
                self.assertEqual(status, 400, error)
                self.assertEqual(self.db.items('parameter_versions'), before)

    def test_http_legacy_client_omission_and_matching_policy_save_actual_strategy(self):
        url = self.start_http()
        for suffix, payload in [('old-client', {}), ('matching', {'parameterTransfer': {'mode': 'duja-shared'}})]:
            body = {'name': '一致策略-' + suffix,
                    'payload': {'sourceTaskId': 'JOB-synthetic-shared', 'parameters': self.shared['parameters'],
                                'partitionParameters': {}, **payload}}
            with self.subTest(case=suffix):
                status, result = self.post(url, body)
                self.assertEqual(status, 200, result)
                self.assertEqual(result['payload']['parameterTransfer'], normalize_transfer_policy({'mode': 'duja-shared'}))
                stored = next(row for row in self.db.items('parameter_versions') if row['id'] == result['id'])
                self.assertEqual(stored['payload']['parameterTransfer'], result['payload']['parameterTransfer'])

    def test_old_parameter_group_without_transfer_is_compatible_with_old_result_and_scheme(self):
        self.db.save_item('parameter_versions', 'PARAM-synthetic-old', '历史参数',
                          {'sourceTaskId': 'JOB-synthetic-old', 'parameters': self.old['parameters'],
                           'partitionParameters': self.old['partitionParameters'],
                           'resultIdentitySHA256': digest(self.old['identity'])})
        scheme = {'modelFamily': 'grid-continuous-v1', 'spatialVersion': 'synthetic-space',
                  'routingMode': 'source-separated'}
        actual = schemes._verified_parameter(self.db, 'PARAM-synthetic-old', scheme)
        self.assertEqual(actual['parameterTransfer'], normalize_transfer_policy(None))
        self.assertEqual(actual['partitionParameters'], self.old['partitionParameters'])
        self.db.save_item('parameter_versions', 'PARAM-synthetic-old-no-hash', '更早参数',
                          {'sourceTaskId': 'JOB-synthetic-old', 'parameters': self.old['parameters'],
                           'partitionParameters': self.old['partitionParameters']})
        self.assertEqual(schemes._verified_parameter(self.db, 'PARAM-synthetic-old-no-hash', scheme), actual)

    def test_source_identity_change_is_rejected_even_if_values_and_policy_match(self):
        original = {**deepcopy(self.shared), 'identity': {'modelImplementation': 'synthetic-original'}}
        changed = {**deepcopy(self.shared), 'identity': {'modelImplementation': 'synthetic-different'}}
        self.db.add_task('JOB-synthetic-original', original)
        self.db.add_task('JOB-synthetic-different', changed)
        group = schemes.save_parameters_from_task(self.db, 'JOB-synthetic-original', '隔离参数')
        payload = deepcopy(group['payload'])
        payload['sourceTaskId'] = 'JOB-synthetic-different'
        with self.db.connect() as conn:
            conn.execute('UPDATE parameter_versions SET payload=? WHERE id=?', (canonical(payload), group['id']))
        scheme = {'modelFamily': 'grid-continuous-v1', 'spatialVersion': 'synthetic-space',
                  'routingMode': 'source-separated', 'parameterTransfer': {'mode': 'duja-shared'}}
        with self.assertRaisesRegex(ValueError, '来源成果身份已变化'):
            schemes._verified_parameter(self.db, group['id'], scheme)

    def test_saved_numerical_parameter_hash_matches_run_identity(self):
        request = {'parameters': deepcopy(DEFAULT_PARAMETERS), 'partitionParameters': {},
                   'parameterTransfer': {'mode': 'duja-area-routing'}}
        declared, patches, _, _, audit, identity = grid_simulation.parameter_configuration(self.spatial, request)
        result = {**deepcopy(self.shared), 'parameters': declared, 'partitionParameters': patches,
                  'parameterTransfer': audit['policy'], 'parameterTransferAudit': audit, 'identity': identity}
        self.db.add_task('JOB-synthetic-identity', result)
        saved = schemes.save_parameters_from_task(self.db, 'JOB-synthetic-identity', '同口径参数')
        self.assertEqual(saved['payload']['effectiveParameterSHA256'], identity['effectiveParameterSHA256'])
        self.assertEqual(saved['payload']['effectiveAuditSHA256'], digest(audit['effective']))

    def test_preview_will_not_reuse_other_strategy_even_with_identical_numeric_values(self):
        baseline = {**deepcopy(DEFAULT_PARAMETERS), 'lagHours': 2.}
        value = {'schemaVersion': 2, 'name': '隔离方案', 'modelFamily': 'grid-continuous-v1',
                 'spatialVersion': 'synthetic-space', 'routingMode': 'source-separated',
                 'parameters': baseline, 'partitionParameters': {}, 'parameterTransfer': {'mode': 'duja-area-routing'},
                 'target': 'duja', 'initialization': {'mode': 'automatic'}, 'inputRequirement': {}, 'search': {},
                 'periods': {'warmupStart': '2022-01-01T00:00:00', 'trainingStart': '2023-01-01T00:00:00',
                             'trainingEnd': '2024-01-01T00:00:00', 'evaluationStart': '2024-01-01T00:00:00',
                             'evaluationEnd': '2025-01-01T00:00:00'}}
        row = {'id': 'SCHEME-synthetic', 'payload': value}
        query = {'researchSchemeId': row['id']}
        observation = {'profile': 'synthetic', 'level': {}, 'flow': {}}
        with patch.object(schemes, '_scheme', return_value=row), \
                patch.object(schemes, 'builtin', return_value=None), \
                patch.object(schemes, 'validate', side_effect=lambda db, v, end: v), \
                patch.object(schemes, '_input', return_value={'id': 'synthetic-input'}), \
                patch.object(schemes, 'resolved_initialization', return_value=(None, None)), \
                patch.object(grid_simulation, 'model_identity', return_value='synthetic-model'), \
                patch.object(grid_simulation, 'observations', return_value=observation), \
                patch('backend.result_query.metadata', return_value={}):
            request = schemes.prepare_request(self.db, query)
            _, _, _, _, _, expected = grid_simulation.parameter_configuration(self.spatial, request)
            shared = {**request, 'parameterTransfer': {'mode': 'duja-shared'}}
            _, _, _, _, _, wrong = grid_simulation.parameter_configuration(self.spatial, shared)
            self.assertEqual(wrong['effectiveParameterSHA256'], expected['effectiveParameterSHA256'])
            self.assertNotEqual(wrong['parameterTransferSHA256'], expected['parameterTransferSHA256'])
            identity = {'modelImplementation': 'synthetic-model', 'spatialVersion': 'synthetic-space', **wrong,
                        'initializationSHA256': digest(None),
                        'observationSnapshotSHA256': digest({**observation, 'sourceDigests': []}),
                        'routingMode': 'source-separated'}
            result = {'inputId': request['inputId'], 'start': request['start'], 'end': request['end'], 'identity': identity}
            self.db.add_task('JOB-synthetic-preview', result)
            self.assertEqual(business_context.simulation_plan(self.db, query)['action'], 'rerun')
            result['identity'].update(expected)
            with self.db.connect() as conn:
                conn.execute('UPDATE tasks SET result=? WHERE id=?', (canonical(result), 'JOB-synthetic-preview'))
            plan = business_context.simulation_plan(self.db, query)
            self.assertEqual(plan['action'], 'reuse')
            self.assertEqual(plan['resultId'], 'JOB-synthetic-preview')

    def test_candidate_parameter_cannot_bypass_strategy(self):
        old_group = schemes.save_parameters_from_task(self.db, 'JOB-synthetic-old', '历史参数')
        new_group = schemes.save_parameters_from_task(self.db, 'JOB-synthetic-shared', '共享参数')
        scheme_value = {'schemaVersion': 2, 'name': '隔离共享方案', 'modelFamily': 'grid-continuous-v1',
                        'spatialVersion': 'synthetic-space', 'routingMode': 'source-separated',
                        'parameters': deepcopy(self.shared['parameters']), 'partitionParameters': {},
                        'parameterTransfer': {'mode': 'duja-shared'}, 'target': 'duja',
                        'initialization': {'mode': 'automatic'}, 'inputRequirement': {}, 'search': {},
                        'periods': {'warmupStart': '2022-01-01T00:00:00', 'trainingStart': '2023-01-01T00:00:00',
                                    'trainingEnd': '2024-01-01T00:00:00', 'evaluationStart': '2024-01-01T00:00:00',
                                    'evaluationEnd': '2025-01-01T00:00:00'}}
        scheme = {'id': 'SCHEME-synthetic-shared', 'payload': scheme_value}
        with patch.object(schemes, '_scheme', return_value=scheme), \
                patch.object(schemes, 'validate', side_effect=lambda db, value, end: value), \
                patch.object(schemes, '_input', return_value={'id': 'synthetic-input'}):
            with self.assertRaisesRegex(ValueError, '传递策略不兼容'):
                schemes.prepare_request(self.db, {'researchSchemeId': scheme['id'], 'candidateParameterId': old_group['id']})
            request = schemes.prepare_request(self.db, {'researchSchemeId': scheme['id'], 'candidateParameterId': new_group['id']})
        self.assertEqual(request['parameterTransfer'], normalize_transfer_policy({'mode': 'duja-shared'}))
        self.assertEqual(request['partitionParameters'], {})
        self.assertEqual(request['parameters'], self.shared['parameters'])

    def test_continuation_rejects_changed_policy_before_creating_task(self):
        checkpoint = {'schemaVersion': 3, 'parameters': deepcopy(DEFAULT_PARAMETERS), 'partitionParameters': {},
                      'parameterTransfer': {'mode': 'duja-shared'}, 'validAt': '2024-01-01T00:00:00',
                      'identity': {'routingMode': 'source-separated'}}
        self.db.add_task('JOB-synthetic-parent', {'checkpointSHA256': digest(checkpoint)})
        with self.db.connect() as conn:
            conn.execute('INSERT INTO states VALUES(?,?,?,?,?,?,?)',
                         ('STATE-synthetic', 'JOB-synthetic-parent', now(), checkpoint['validAt'],
                          canonical(checkpoint), '', 0))
        jobs = Jobs.__new__(Jobs)
        jobs.db = self.db
        jobs.restore_pending = False
        jobs.restart_required = False
        before = len(self.db.rows('SELECT id FROM tasks'))
        with self.assertRaisesRegex(ValueError, '续算参数传递策略与父状态不一致'):
            jobs._submit('continuation', {'stateId': 'STATE-synthetic', 'start': '2024-01-01T01:00:00',
                                           'end': '2024-01-02T00:00:00',
                                           'parameterTransfer': {'mode': 'duja-area-routing'}})
        self.assertEqual(len(self.db.rows('SELECT id FROM tasks')), before)


if __name__ == '__main__':
    unittest.main()
