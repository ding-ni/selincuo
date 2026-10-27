"""Regression checks for scheme/parameter lineage and safe result reuse."""
from copy import deepcopy
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
        self.settings = {}

    def items(self, table):
        return deepcopy(self.tables[table])

    def save_item(self, table, identifier, name, payload):
        self.tables[table].insert(0, {'id': identifier, 'name': name, 'payload': deepcopy(payload),
                                      'created_at': str(len(self.tables[table]))})

    def task(self, identifier):
        if identifier not in self.tasks:
            raise FileNotFoundError(identifier)
        return deepcopy(self.tasks[identifier])

    def setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

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
                'manifest': {'spatialVersion': 'SOURCE', 'start': '2022-01-01T00:00:00', 'end': end,
                             'transformationSHA256': 'corrected'}})
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

    def test_updated_input_is_used_and_user_display_dates_do_not_move_warmup(self):
        row = schemes.save(self.db, self.config)
        request = schemes.prepare_request(self.db, {'researchSchemeId': row['id'],
            'start': '2023-07-01T00:00:00', 'end': '2025-06-01T00:00:00'})
        self.assertEqual(request['inputId'], 'new')
        self.assertEqual(request['start'], '2022-01-01T00:00:00')
        self.assertEqual(request['evaluationStart'], '2023-07-01T00:00:00')
        self.assertEqual(row['payload']['inputId'], 'old')
        with self.assertRaisesRegex(ValueError, '未覆盖'):
            schemes.prepare_request(self.db, {'researchSchemeId': row['id'], 'inputId': 'old',
                'start': '2023-07-01T00:00:00', 'end': '2025-06-01T00:00:00'})
        with self.assertRaisesRegex(ValueError, '开始和结束'):
            schemes.prepare_request(self.db, {'researchSchemeId': row['id'],
                'start': '2021-07-01T00:00:00', 'end': '2024-06-01T00:00:00'})

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

    def test_lake_anchor_defines_first_complete_day(self):
        self.config['initialization'] = {'initialLakeTime': '2022-04-20T12:00:00',
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

    def test_reuse_requires_complete_input_parameter_anchor_and_observation_identity(self):
        row = schemes.save(self.db, self.config)
        query = {'researchSchemeId': row['id'], 'start': '2023-01-01T00:00:00', 'end': '2024-01-01T00:00:00'}
        request = schemes.prepare_request(self.db, query)
        obs = {'profile': 'test', 'level': {}, 'flow': {}, 'sourceDigests': []}
        identity = {'modelImplementation': 'code', 'spatialVersion': 'AGG-1',
                    'parameterSHA256': digest(request['parameters']),
                    'partitionParameterSHA256': digest(request['partitionParameters']),
                    'initializationSHA256': digest(None), 'observationSnapshotSHA256': digest(obs),
                    'routingMode': 'source-separated'}
        self.db.tasks['complete'] = {'id': 'complete', 'status': 'succeeded', 'trashed': 0,
            'result': {'inputId': request['inputId'], 'identity': identity, 'start': request['start'], 'end': request['end']}, 'request': {}}
        with patch('backend.grid_simulation.model_identity', return_value='code'), \
             patch('backend.grid_simulation.observations', return_value={'profile': 'test', 'level': {}, 'flow': {}}), \
             patch('backend.grid_simulation.anchor', return_value=None), \
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


if __name__ == '__main__':
    unittest.main()
