import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch
from backend.common import ROOT, read_json
from backend.database import Database
from backend.jobs import Jobs
from backend.operational_scheme import definition, catalog, prepare_request, SCHEME_ID


class OperationalSchemeTests(unittest.TestCase):
    def product(self):
        spatial = read_json(ROOT/'data/derived/spatial/manifest.json')
        return {'id': 'real', 'name': '真实驱动', 'manifest': {
            'spatialVersion': spatial['version'], 'start': '2022-12-01T09:00:00',
            'end': '2026-01-01T07:00:00', 'hours': 27047,
            **definition().get('inputRequirement', {})}}

    def test_empty_workspace_has_builtin_configuration_without_user_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp)
            scheme = catalog(db)[0]
            self.assertEqual(scheme['name'], '色林错标准历史方案')
            self.assertFalse(scheme['available'])
            self.assertEqual(db.summary()['counts']['parameter_versions'], 0)
            self.assertEqual(scheme['partitionParameters']['duja']['WM'], 300)
            self.assertFalse(scheme['evidence']['globallyOptimalClaimed'])

    def test_fixed_bundle_is_server_owned_and_subperiod_keeps_original_warmup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp)
            with patch.object(db, 'items', return_value=[self.product()]):
                requested = {'schemeId': SCHEME_ID, 'start': '2025-04-01T00:00', 'end': '2025-11-01T00:00',
                             'parameters': {'WM': 40}, 'partitionParameters': {}, 'routingMode': 'lumped'}
                before = deepcopy(requested)
                ready = prepare_request(db, requested)
                self.assertEqual(requested, before)
                self.assertEqual(ready['start'], '2023-01-01T00:00:00')
                self.assertEqual(ready['evaluationStart'], '2025-04-01T00:00:00')
                self.assertEqual(ready['inputId'], 'real')
                self.assertEqual(ready['partitionParameters']['duja']['WM'], 300)
                self.assertEqual(ready['parameters'], definition()['parameters'])
                self.assertEqual(ready['routingMode'], 'source-separated')
                self.assertIn('configurationSHA256', ready['schemeSnapshot'])
                self.assertEqual(ready['inputId'],'real')
            original=self.product()
            original['manifest'].pop('transformationSHA256',None)
            with patch.object(db,'items',return_value=[original]), self.assertRaises(ValueError):
                prepare_request(db,{**requested,'inputId':'real'})

    def test_wrong_revision_space_uncovered_and_partial_days_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp)
            baseline = {'schemeId': SCHEME_ID, 'start': '2024-01-01', 'end': '2026-01-01'}
            with patch.object(db, 'items', return_value=[self.product()]):
                for change in ({'schemeRevision': 99}, {'start': '2023-01-01'}, {'start': '2024-01-01T01:00'},
                               {'end': '2026-02-01'}, {'inputId': 'missing'}):
                    with self.assertRaises(ValueError):
                        prepare_request(db, {**baseline, **change})
            product = self.product()
            product['manifest']['start'] = '2023-06-01T00:00:00'
            with patch.object(db, 'items', return_value=[product]), self.assertRaises(ValueError):
                prepare_request(db, {**baseline, 'inputId': 'real'})

    def test_retried_request_creates_one_task_and_keeps_frozen_snapshot_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp)
            with db.connect() as conn:
                import json
                conn.execute('INSERT INTO input_products VALUES(?,?,?,?)',
                             ('real', '真实驱动', '2026-01-01', json.dumps(self.product()['manifest'])))
            jobs = Jobs(db)
            request = {'schemeId': SCHEME_ID, 'start': '2025-01-01', 'end': '2026-01-01',
                       'clientRequestId': 'test-retry-same-operation'}
            with patch('backend.jobs.observation_snapshot', return_value={}), patch.object(jobs.executor, 'submit') as submit:
                first = jobs.submit('simulation', request)
                second = jobs.submit('simulation', request)
                self.assertEqual(first['id'], second['id'])
                self.assertEqual(submit.call_count, 1)
                self.assertEqual(first['collection'], '历史模拟')
                self.assertTrue(first['favorite'])
                self.assertEqual(first['request']['start'], '2023-01-01T00:00:00')
                with self.assertRaises(ValueError):
                    jobs.submit('simulation', {**request, 'end': '2025-06-01'})
            jobs.executor.shutdown(wait=False)
            restarted = Jobs(db)
            try:
                restored = restarted.submit('simulation', request)
                self.assertEqual(restored['id'], first['id'])
                self.assertEqual(restored['request']['schemeSnapshot'], first['request']['schemeSnapshot'])
                self.assertEqual(db.summary()['counts']['tasks'], 1)
            finally:
                restarted.close()

