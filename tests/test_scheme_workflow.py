"""Regression checks for scheme/parameter lineage and safe result reuse."""
from copy import deepcopy
from contextlib import contextmanager
import json
import sqlite3
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.common import digest
from backend.model_core import DEFAULT_PARAMETERS, PARAMETER_DEFINITIONS
from backend import business_context, research_schemes as schemes
from backend.forcing_latest import acquisition_window


class MemoryDB:
    def __init__(self):
        self.tables = {'schemes': [], 'parameter_versions': [], 'input_products': []}
        self.tasks = {}
        self.connection = sqlite3.connect(':memory:')
        self.connection.executescript('''
            CREATE TABLE parameter_versions(id TEXT PRIMARY KEY);
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE audit(id INTEGER PRIMARY KEY, at TEXT, action TEXT, payload TEXT);
        ''')

    @contextmanager
    def connect(self):
        with self.connection:
            yield self.connection

    def items(self, table):
        return deepcopy(self.tables[table])

    def save_item(self, table, identifier, name, payload):
        self.tables[table].insert(0, {'id': identifier, 'name': name, 'payload': deepcopy(payload),
                                      'created_at': str(len(self.tables[table]))})
        if table == 'parameter_versions':
            with self.connect() as connection:
                connection.execute('INSERT INTO parameter_versions VALUES(?)', (identifier,))

    def task(self, identifier):
        if identifier not in self.tasks:
            raise FileNotFoundError(identifier)
        return deepcopy(self.tasks[identifier])

    def setting(self, key, default=None):
        value = self.connection.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(value[0]) if value else default

    def set_setting(self, key, value):
        with self.connect() as connection:
            connection.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))

    def rows(self, sql, args=()):
        if "SELECT id,request FROM tasks" in sql:
            import json
            return [{'id': t['id'], 'request': json.dumps(t['request'])} for t in self.tasks.values()
                    if t['status'] in ('queued', 'running', 'stopping')]
        if "SELECT id FROM tasks" in sql:
            return [{'id': t['id']} for t in self.tasks.values() if t['status'] == 'succeeded' and not t.get('trashed')]
        return []

    def log(self, *args):
        pass


class SchemeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB()
        self.spatial = {'version': 'AGG-1', 'unitStrategy': 'partition-elevation-surface-area-weighted-v1',
                        'units': [{'id': 'u', 'partition': 'duja'}], 'grids': [{'id': 'g'}]}
        self.spatial['forcingGridVersion'] = digest(self.spatial['grids'])
        for identifier, end in [('old', '2025-01-01T00:00:00'), ('new', '2026-01-01T00:00:00')]:
            self.db.tables['input_products'].append({'id': identifier, 'name': identifier, 'created_at': end,
                'manifest': {'spatialVersion': 'SOURCE', 'start': '2020-01-01T00:00:00', 'end': end,
                             'transformationSHA256': 'corrected'}})
        self.db.set_setting('current-model-forcing', {'inputId': 'new'})
        def verified(db, product, spatial=None):
            if spatial is not None and spatial['forcingGridVersion'] != digest(self.spatial['grids']):
                raise ValueError('格网不兼容')
            m = product['manifest']
            return {'start': m['start'], 'end': m['end'], 'firstCompleteDay': m['start'][:10],
                    'lastCompleteDay': '2025-12-31'}
        self.forcing_patch = patch('backend.model_forcing.verify_product', side_effect=verified)
        self.forcing_patch.start()
        self.addCleanup(self.forcing_patch.stop)
        self.load_patch = patch.object(schemes.grid_spatial, 'load', return_value=self.spatial)
        self.builtin_patch = patch.object(schemes, 'builtin', return_value=None)
        self.result_patch = patch.object(business_context, 'result_task', side_effect=lambda db, identifier: db.task(identifier))
        self.load_patch.start()
        self.builtin_patch.start()
        self.result_patch.start()
        self.addCleanup(self.load_patch.stop)
        self.addCleanup(self.builtin_patch.stop)
        self.addCleanup(self.result_patch.stop)
        self.config = {'schemaVersion': 2, 'name': '方案', 'modelFamily': 'grid-continuous-v1',
            'spatialVersion': 'AGG-1', 'inputId': 'old', 'parameters': deepcopy(DEFAULT_PARAMETERS),
            'partitionParameters': {'duja': {'WM': 200}}, 'routingMode': 'source-separated', 'target': 'duja',
            'inputRequirement': {'transformationSHA256': 'corrected'},
            'periods': {'warmupStart': '2022-01-01T00:00:00', 'trainingStart': '2023-01-01T00:00:00',
                        'trainingEnd': '2024-01-01T00:00:00', 'evaluationStart': '2024-01-01T00:00:00',
                        'evaluationEnd': '2025-01-01T00:00:00'},
            'search': {'scope': 'duja', 'keys': ['WM'],
                       'bounds': {d['key']: [d['min'], d['max']] for d in PARAMETER_DEFINITIONS},
                       'budgets': {'runoff': 1, 'routing': 1, 'lake': 1}, 'seed': 42,
                       'objectiveProfile': 'balanced'}}

    def completed(self, identifier='job', status='succeeded'):
        p = deepcopy(DEFAULT_PARAMETERS)
        p['WM'] = 210
        self.db.tasks[identifier] = {'id': identifier, 'kind': 'trial', 'status': status, 'trashed': 0,
            'request': {'researchSchemeId': 'original'},
            'result': {'modelFamily': 'grid-continuous-v1', 'spatialVersion': 'AGG-1',
                       'routingMode': 'source-separated', 'parameters': p,
                       'partitionParameters': {'duja': {'WM': 250}}, 'identity': {'test': True}}}

    def test_adopt_actual_parameters_preserves_calibration_initial_and_prior_results(self):
        original = schemes.save(self.db, self.config)
        self.completed()
        self.assertRaises(FileNotFoundError, schemes.save_parameters_from_task, self.db, 'missing', '参数')
        self.completed('failed', 'failed')
        with self.assertRaisesRegex(ValueError, '成功成果'):
            schemes.save_parameters_from_task(self.db, 'failed', '参数')
        group = schemes.save_parameters_from_task(self.db, 'job', '参数')
        self.assertEqual(group['payload']['parameters']['WM'], 210)
        self.assertEqual(group['payload']['partitionParameters']['duja']['WM'], 250)
        adopted = schemes.adopt_parameters(self.db, original['id'], group['id'])
        self.assertEqual(adopted['payload']['parameters']['WM'], 210)
        self.assertEqual(adopted['payload']['calibrationInitialParameters'], original['payload']['parameters'])
        self.assertEqual(adopted['payload']['calibrationInitialPartitionParameters']['duja']['WM'], 200)
        run = schemes.prepare_request(self.db, {'researchSchemeId': adopted['id']})
        calibration = schemes.prepare_request(self.db, {'researchSchemeId': adopted['id']}, kind='calibration')
        self.assertEqual(run['parameters']['WM'], 210)
        self.assertEqual(calibration['partitionParameters']['duja']['WM'], 200)
        self.assertEqual(self.db.task('job')['result']['partitionParameters']['duja']['WM'], 250)
        self.db.tasks['job']['trashed'] = 1
        self.assertEqual(schemes.prepare_request(self.db, {'researchSchemeId': adopted['id'],
            'candidateParameterId': group['id']})['parameters']['WM'], 210)
        self.db.tables['parameter_versions'][0]['payload']['parameters']['WM'] = 211
        with self.assertRaisesRegex(ValueError, '不一致'):
            schemes.prepare_request(self.db, {'researchSchemeId': adopted['id'], 'candidateParameterId': group['id']})

    def test_parameter_recycle_restores_selection_without_changing_history(self):
        original = schemes.save(self.db, self.config)
        self.completed()
        group = schemes.save_parameters_from_task(self.db, 'job', '参数')
        adopted = schemes.adopt_parameters(self.db, original['id'], group['id'])
        saved_bytes = deepcopy(self.db.items('parameter_versions'))
        saved_result = deepcopy(self.db.task('job'))
        # Recycling a source task does not hide an independently saved parameter.
        self.db.tasks['job']['trashed'] = 1
        self.assertEqual([row['id'] for row in schemes.list_parameters(self.db)], [group['id']])
        schemes.parameter_metadata(self.db, group['id'], True)
        self.assertEqual(schemes.list_parameters(self.db), [])
        self.assertTrue(schemes.list_parameters(self.db, include_trashed=True)[0]['trashed'])
        self.assertEqual(self.db.items('parameter_versions'), saved_bytes)
        self.assertEqual(self.db.task('job')['result'], saved_result['result'])
        self.assertEqual(schemes.prepare_request(self.db, {'researchSchemeId': adopted['id']})['parameters']['WM'], 210)
        resaved = schemes.save(self.db, adopted['payload'], adopted['id'])
        self.assertEqual(resaved['payload']['adoptedParameterId'], group['id'])
        with self.assertRaisesRegex(ValueError, '请先恢复'):
            schemes.prepare_request(self.db, {'researchSchemeId': resaved['id'], 'candidateParameterId': group['id']})
        with self.assertRaisesRegex(ValueError, '请先恢复'):
            schemes.adopt_parameters(self.db, resaved['id'], group['id'])
        with self.assertRaisesRegex(ValueError, '参数组不存在'):
            schemes.parameter_metadata(self.db, 'missing', True)
        schemes.parameter_metadata(self.db, group['id'], False)
        self.assertEqual([row['id'] for row in schemes.list_parameters(self.db)], [group['id']])
        self.assertEqual(schemes.prepare_request(self.db, {'researchSchemeId': resaved['id'],
            'candidateParameterId': group['id']})['parameters']['WM'], 210)
        audit = self.db.connection.execute("SELECT action,payload FROM audit ORDER BY id").fetchall()
        self.assertEqual([json.loads(payload)['trashed'] for action, payload in audit], [True, False])
        self.assertTrue(all(action == 'parameters.visibility' for action, payload in audit))

    def test_parameter_recycle_rolls_back_if_audit_write_fails(self):
        self.completed()
        group = schemes.save_parameters_from_task(self.db, 'job', '参数')
        with self.db.connect() as connection:
            connection.execute("CREATE TRIGGER audit_failure BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'audit unavailable'):
            schemes.parameter_metadata(self.db, group['id'], True)
        self.assertFalse(self.db.setting('parameter-hidden:' + group['id'], False))
        self.assertEqual([row['id'] for row in schemes.list_parameters(self.db)], [group['id']])

    def test_parameter_recycle_routes_list_and_restore(self):
        from backend.server import Handler
        self.completed()
        group = schemes.save_parameters_from_task(self.db, 'job', '参数')
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(db=self.db, jobs=SimpleNamespace(lock=RLock()))
        handler.send_json = lambda value, status=200: value
        handler.path = '/api/model/parameters/' + group['id'] + '/metadata'
        handler.document = lambda: {'trashed': True}
        self.assertTrue(handler.post()['trashed'])
        handler.path = '/api/model/parameters'
        self.assertEqual(handler.get()['parameters'], [])
        handler.path += '?includeTrashed=true'
        self.assertTrue(handler.get()['parameters'][0]['trashed'])
        handler.path = '/api/model/parameters/' + group['id'] + '/metadata'
        handler.document = lambda: {'trashed': 'false'}
        with self.assertRaisesRegex(ValueError, '指定是否移入回收站'):
            handler.post()
        handler.document = lambda: {'trashed': False}
        self.assertFalse(handler.post()['trashed'])
        handler.path = '/api/model/parameters'
        self.assertEqual(handler.get()['parameters'][0]['id'], group['id'])

    def test_updated_input_is_used_and_user_display_dates_do_not_move_warmup(self):
        row = schemes.save(self.db, self.config)
        request = schemes.prepare_request(self.db, {'researchSchemeId': row['id'],
            'start': '2023-07-01T00:00:00', 'end': '2025-06-01T00:00:00'})
        self.assertEqual(request['inputId'], 'new')
        self.assertEqual(request['start'], '2022-01-01T00:00:00')
        self.assertEqual(request['evaluationStart'], '2023-07-01T00:00:00')
        self.assertNotIn('inputId', row['payload'])
        with self.assertRaisesRegex(ValueError, '气象资料已更新'):
            schemes.prepare_request(self.db, {'researchSchemeId': row['id'], 'inputId': 'old',
                'start': '2023-07-01T00:00:00', 'end': '2025-06-01T00:00:00'})
        with self.assertRaisesRegex(ValueError, '预热起点'):
            schemes.prepare_request(self.db, {'researchSchemeId': row['id'],
                'start': '2020-07-01T00:00:00', 'end': '2024-06-01T00:00:00'})

    def test_revisions_hidden_as_one_recoverable_scheme_and_active_protected(self):
        first = schemes.save(self.db, self.config)
        second = schemes.save(self.db, self.config, first['id'])
        self.assertEqual([row['id'] for row in schemes.list_schemes(self.db)], [second['id']])
        with self.assertRaisesRegex(ValueError, '已有更新'):
            schemes.save(self.db, self.config, first['id'])
        self.db.tasks['active'] = {'id': 'active', 'status': 'running', 'request': {'researchSchemeId': first['id']}}
        with self.assertRaisesRegex(ValueError, '正在计算'):
            schemes.scheme_metadata(self.db, second['id'], True)
        self.db.tasks['active']['status'] = 'succeeded'
        schemes.scheme_metadata(self.db, second['id'], True)
        self.assertEqual(schemes.list_schemes(self.db), [])
        self.assertTrue(schemes.list_schemes(self.db, include_trashed=True)[0]['trashed'])
        schemes.scheme_metadata(self.db, second['id'], False)
        self.assertEqual(len(schemes.list_schemes(self.db)), 1)
        self.assertEqual(len(self.db.tables['schemes']), 2)

    def test_builtin_trash_restores_same_identity_without_changing_configuration(self):
        self.builtin_patch.stop()
        config = {'id': 'builtin-selinco', **deepcopy(self.config), 'resultId': 'saved-result'}
        with patch.object(schemes, 'read_json', return_value=config):
            standard = schemes.builtin(self.db)
            identity = standard['payload']['configurationSHA256']
            for status in ('queued', 'running', 'stopping'):
                self.db.tasks['active'] = {'id': 'active', 'status': status,
                                           'request': {'researchSchemeId': standard['id']}}
                with self.assertRaisesRegex(ValueError, '正在计算'):
                    schemes.scheme_metadata(self.db, standard['id'], True)
            self.db.tasks['active']['status'] = 'succeeded'
            schemes.scheme_metadata(self.db, standard['id'], True)
            self.assertEqual(schemes.list_schemes(self.db), [])
            hidden = schemes.list_schemes(self.db, include_trashed=True)[0]
            self.assertTrue(hidden['trashed'])
            self.assertEqual(hidden['payload']['configurationSHA256'], identity)
            self.assertEqual(hidden['payload']['resultId'], 'saved-result')
            self.assertEqual(schemes.template(self.db)['name'], '我的色林错方案')
            with self.assertRaisesRegex(ValueError, '请先恢复'):
                schemes.prepare_request(self.db, {'researchSchemeId': standard['id']})
            with self.assertRaisesRegex(ValueError, '请先恢复'):
                schemes.save(self.db, self.config, standard['id'])
            schemes.scheme_metadata(self.db, standard['id'], False)
            restored = schemes.list_schemes(self.db)[0]
            self.assertEqual(restored['id'], standard['id'])
            self.assertEqual(restored['payload']['configurationSHA256'], identity)
            self.assertFalse(restored['trashed'])

    def test_lake_anchor_defines_first_complete_day(self):
        self.config['initialization'] = {'mode': 'manual', 'initialLakeTime': '2022-04-20T12:00:00',
                                         'initialLakeLevel': 4544.03, 'initialLakeSource': '日值代表锚点'}
        row = schemes.save(self.db, self.config)
        with self.assertRaisesRegex(ValueError, '完整湖位从2022-04-21起'):
            schemes.prepare_request(self.db, {'researchSchemeId': row['id'],
                'start': '2022-01-01T00:00:00', 'end': '2022-02-01T00:00:00'})
        with self.assertRaisesRegex(ValueError, '完整湖位从2022-04-21起'):
            schemes.prepare_request(self.db, {'researchSchemeId': row['id'],
                'start': '2022-04-20T00:00:00', 'end': '2022-04-21T00:00:00'})
        request = schemes.prepare_request(self.db, {'researchSchemeId': row['id'],
            'start': '2022-01-01T00:00:00', 'end': '2022-06-01T00:00:00'})
        self.assertEqual(request['earliestCompleteLakeDay'], '2022-04-21')

    def test_calibration_target_must_change_the_parameters_it_scores(self):
        bad = deepcopy(self.config)
        bad['search']['scope'] = 'background'
        with self.assertRaisesRegex(ValueError, '杜加控制区'):
            schemes.save(self.db, bad)
        bad = deepcopy(self.config)
        bad['target'] = 'lake'
        bad['search']['keys'] = ['lakeCoefficient']
        with self.assertRaisesRegex(ValueError, '全湖参数'):
            schemes.save(self.db, bad)

    def test_automatic_earlier_request_and_no_evaluation_period_anchor_leak(self):
        self.config['initialization'] = {'mode': 'automatic', 'initialLakeTime': '2022-04-20T12:00:00',
                                         'initialLakeLevel': 4544.03, 'initialLakeSource': '旧锚点'}
        row = schemes.save(self.db, self.config)
        early = schemes.prepare_request(self.db, {'researchSchemeId': row['id'],
            'start': '2021-06-01T00:00:00', 'end': '2021-07-01T00:00:00',
            'initialLakeTime': '2023-01-01T12:00:00', 'initialLakeLevel': 4550, 'initialLakeSource': 'old'})
        self.assertEqual(early['start'], '2020-06-01T00:00:00')
        self.assertNotIn('initialLakeTime', early)
        obs = {'profile': 'test', 'level': {'2021-04-23T00:00:00': 4543.66}, 'flow': {}}
        initial, first = schemes.resolved_initialization(early, obs)
        self.assertEqual((initial['time'], initial['level'], first),
                         ('2021-04-23T12:00:00', 4543.66, '2021-04-24'))
        calibration = schemes.prepare_request(self.db, {'researchSchemeId': row['id']}, kind='calibration')
        later = {'profile': 'test', 'level': {'2024-01-01T00:00:00': 4544.0}, 'flow': {}}
        with self.assertRaisesRegex(ValueError, '没有可用的主站湖位'):
            schemes.resolved_initialization(calibration, later, 'calibration')

    def test_plan_token_changes_with_input_or_observation(self):
        row = schemes.save(self.db, self.config)
        request = schemes.prepare_request(self.db, {'researchSchemeId': row['id']})
        observed = {'profile': 'test', 'level': {'2022-04-20T00:00:00': 4544.03}, 'flow': {}}
        initial, _ = schemes.resolved_initialization(request, observed)
        token = schemes.plan_token(request, initial, observed)
        self.assertNotEqual(token, schemes.plan_token({**request, 'inputId': 'other'}, initial, observed))
        self.assertNotEqual(token, schemes.plan_token(request, initial, {**observed, 'flow': {'2023-01-01T00:00:00': 1}}))

    def test_reuse_requires_complete_input_parameter_anchor_and_observation_identity(self):
        row = schemes.save(self.db, self.config)
        query = {'researchSchemeId': row['id'], 'start': '2023-01-01T00:00:00', 'end': '2024-01-01T00:00:00'}
        request = schemes.prepare_request(self.db, query)
        obs = {'profile': 'test', 'level': {'2022-04-20T00:00:00': 4544.03}, 'flow': {}, 'sourceDigests': []}
        initial = {'time': '2022-04-20T12:00:00', 'level': 4544.03, 'method': 'first-quality-daily-level-at-noon',
                   'source': 'test', 'reference': 'SLC-working-datum'}
        identity = {'modelImplementation': 'code', 'spatialVersion': 'AGG-1',
                    'parameterSHA256': digest(request['parameters']),
                    'partitionParameterSHA256': digest(request['partitionParameters']),
                    'initializationSHA256': digest(initial), 'observationSnapshotSHA256': digest(obs),
                    'routingMode': 'source-separated'}
        self.db.tasks['complete'] = {'id': 'complete', 'status': 'succeeded', 'trashed': 0,
            'result': {'inputId': request['inputId'], 'identity': identity, 'start': request['start'], 'end': request['end']}, 'request': {}}
        with patch('backend.grid_simulation.model_identity', return_value='code'), \
             patch('backend.grid_simulation.observations', return_value={k:v for k,v in obs.items() if k!='sourceDigests'}), \
             patch('backend.result_query.metadata', return_value={}):
            self.assertEqual(business_context.simulation_plan(self.db, query)['action'], 'reuse')
            self.db.tasks['complete']['result']['identity']['parameterSHA256'] = 'changed'
            self.assertEqual(business_context.simulation_plan(self.db, query)['action'], 'rerun')


class ForcingWindowTests(unittest.TestCase):
    def test_beijing_inclusive_dates_include_utc_boundary_and_refuse_gap(self):
        product = {'start': '2023-01-01T00:00:00', 'end': '2026-10-21T07:00:00'}
        catalog = {'availableStartUTC': '1981-01-01T00:00:00Z',
                   'availableEndUTC': '2026-10-31T23:00:00Z'}
        start, end = acquisition_window(product, catalog, '2021-01-01', '2022-12-31')
        self.assertEqual(start.isoformat(), '2020-12-30T00:00:00')
        self.assertEqual(end.isoformat(), '2022-12-31T23:00:00')
        with self.assertRaisesRegex(ValueError, '缺口'):
            acquisition_window(product, catalog, '2021-01-01', '2022-06-01')
        with self.assertRaisesRegex(ValueError, '缺口'):
            acquisition_window(product, catalog, '2026-10-23', '2026-10-24')
        with self.assertRaisesRegex(ValueError, '超过官方'):
            acquisition_window(product, catalog, '2026-10-21', '2026-11-01')


class PersistentSchemeTrashTests(unittest.TestCase):
    def test_builtin_and_revision_family_survive_database_reopen_and_restore(self):
        import json
        import tempfile
        from backend.database import Database
        from backend.common import now

        # This isolated database is deliberately left intact for inspection.
        workspace = tempfile.mkdtemp(prefix='selinco-scheme-check-')
        db = Database(workspace)
        standard = schemes.builtin(db)
        before = deepcopy(standard['payload'])
        family = 'FAMILY-test'
        for revision in (1, 2):
            db.save_item('schemes', f'SCHEME-test-{revision}', '测试方案',
                         {'schemaVersion': 2, 'name': '测试方案', 'familyId': family, 'revision': revision})
        stamp = now()
        with db.connect() as connection:
            connection.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request) VALUES(?,?,?,?,?,?)',
                               ('JOB-test', 'simulation', 'running', stamp, stamp,
                                json.dumps({'researchSchemeId': standard['id']})))
        with self.assertRaisesRegex(ValueError, '正在计算'):
            schemes.scheme_metadata(db, standard['id'], True)
        with db.connect() as connection:
            connection.execute("UPDATE tasks SET status='succeeded' WHERE id='JOB-test'")
        schemes.scheme_metadata(db, standard['id'], True)
        schemes.scheme_metadata(db, 'SCHEME-test-2', True)
        reopened = Database(workspace)
        self.assertEqual(schemes.list_schemes(reopened), [])
        self.assertEqual({row['id'] for row in schemes.list_schemes(reopened, True)},
                         {standard['id'], 'SCHEME-test-2'})
        self.assertEqual(schemes.builtin(reopened)['payload'], before)
        self.assertEqual(len(reopened.items('schemes')), 2)
        self.assertEqual(schemes.template(reopened)['name'], '我的色林错方案')
        schemes.scheme_metadata(reopened, standard['id'], False)
        schemes.scheme_metadata(reopened, 'SCHEME-test-2', False)
        reopened_again = Database(workspace)
        self.assertEqual({row['id'] for row in schemes.list_schemes(reopened_again)},
                         {standard['id'], 'SCHEME-test-2'})
        self.assertEqual(schemes.builtin(reopened_again)['payload'], before)


if __name__ == '__main__':
    unittest.main()
