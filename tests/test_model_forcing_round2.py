"""Current forcing, atomic selection, and lake-only storage regressions.

Fixtures use isolated retained temporary workspaces; no test deletes files.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend import model_forcing, forcing
from backend.common import ROOT, canonical, digest, now, read_json, sha_file, write_json
from backend.database import Database


class CurrentForcingTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(tempfile.mkdtemp(prefix='selinco-model-forcing-'))
        self.spatial = {'version': 'GRID-test', 'grids': [{'id': 'a'}, {'id': 'b'}]}
        self.spatial['forcingGridVersion'] = digest(self.spatial['grids'])
        self.correction = read_json(ROOT / 'config/operational/continuous-builtin-v1.json')['inputRequirement']['lakePrecipitationCorrection']
        self.spatial_patch = patch('backend.model_forcing.grid_spatial.load', return_value=self.spatial)
        self.spatial_patch.start()
        self.addCleanup(self.spatial_patch.stop)

    def product(self, identifier, *, missing_value=False, hour_gap=False):
        folder = self.db.workspace / 'inputs/products' / identifier
        folder.mkdir(parents=True)
        times = pd.date_range('2024-01-01', periods=49, freq='h')
        if hour_gap:
            values = times.asi8.copy()
            values[20:] += 3600 * 10**9
        else:
            values = times.asi8
        rain = np.ones((49, 2))
        if missing_value:
            rain[7, 1] = np.nan
        np.savez_compressed(folder / 'forcing.npz', time=values, precipitation=rain,
                            temperature=np.ones((49, 2)), evaporation=np.ones((49, 2)))
        m = {'id': identifier, 'projectId': 'selinco', 'spatialVersion': 'GRID-test',
             'conversionVersion': model_forcing.CONVERSION, 'timeZone': 'Asia/Shanghai',
             'timeMeaning': 'end of hourly accumulation interval', 'units': model_forcing.UNITS,
             'gridCount': 2, 'hours': 49, 'start': pd.Timestamp(values[0]).isoformat(),
             'end': pd.Timestamp(values[-1]).isoformat(), 'data': 'forcing.npz',
             'dataSHA256': sha_file(folder / 'forcing.npz'),
             'lakePrecipitationCorrection': self.correction, 'transformationSHA256': digest(self.correction)}
        write_json(folder / 'manifest.json', m)
        with self.db.connect() as connection:
            connection.execute('INSERT INTO input_products VALUES(?,?,?,?)', (identifier, identifier, now(), canonical(m)))
        return {'id': identifier, 'manifest': m}

    def test_current_product_is_not_a_newer_unfinished_product(self):
        old = self.product('INPUT-complete')
        self.product('INPUT-unfinished', missing_value=True)
        self.db.set_setting(model_forcing.CURRENT_SETTING, {'inputId': old['id'], 'taskId': 'complete'})
        product, coverage = model_forcing.resolve(self.db, '2024-01-01', '2024-01-03', self.spatial)
        self.assertEqual(product['id'], old['id'])
        self.assertEqual(coverage['firstCompleteDay'], '2024-01-01')
        self.assertEqual(coverage['lastCompleteDay'], '2024-01-02')
        with self.assertRaisesRegex(ValueError, '完整日截至'):
            model_forcing.resolve(self.db, end='2024-01-04')

    def test_hour_gaps_and_missing_values_fail_verification(self):
        for identifier, options, message in [('INPUT-gap', {'hour_gap': True}, '缺口'),
                                             ('INPUT-nan', {'missing_value': True}, '缺测')]:
            with self.subTest(identifier=identifier), self.assertRaisesRegex(ValueError, message):
                model_forcing.verify_product(self.db, self.product(identifier, **options), self.spatial)

    def test_units_and_grid_must_match(self):
        product = self.product('INPUT-valid')
        wrong = deepcopy(product)
        wrong['manifest']['units']['precipitation'] = 'm/h'
        with self.assertRaisesRegex(ValueError, '单位'):
            model_forcing.verify_product(self.db, wrong, self.spatial)
        wrong_spatial = {**self.spatial, 'forcingGridVersion': 'different'}
        with self.assertRaisesRegex(ValueError, '格网'):
            model_forcing.verify_product(self.db, product, wrong_spatial)


class SharedLakeForcingTests(unittest.TestCase):
    def test_derived_manifest_shares_identical_bytes_and_cannot_apply_correction_twice(self):
        db = Database(tempfile.mkdtemp(prefix='selinco-lake-shared-'))
        spatial = read_json(ROOT / 'data/derived/spatial/manifest.json')
        folder = db.workspace / 'inputs/products/INPUT-base'
        folder.mkdir(parents=True)
        np.savez_compressed(folder / 'forcing.npz', time=np.array([0, 3600 * 10**9]),
                            precipitation=np.array([[2.], [3.]]), temperature=np.zeros((2, 1)),
                            evaporation=np.ones((2, 1)))
        base = {'id': 'INPUT-base', 'projectId': 'selinco', 'spatialVersion': spatial['version'],
                'data': 'forcing.npz', 'dataSHA256': sha_file(folder / 'forcing.npz'), 'assumptions': []}
        write_json(folder / 'manifest.json', base)
        with db.connect() as connection:
            connection.execute('INSERT INTO input_products VALUES(?,?,?,?)', ('INPUT-base', 'base', now(), canonical(base)))
        correction = {'method': 'station-point-annual-ratio-v1', 'scope': 'lake-only', 'factor': 0.5}
        derived = forcing.register_lake_correction(db, 'INPUT-base', correction)
        path = db.workspace / 'inputs/products' / derived['id'] / 'forcing.npz'
        self.assertTrue(path.samefile(folder / 'forcing.npz'))
        self.assertEqual(derived['dataSHA256'], base['dataSHA256'])
        self.assertEqual(forcing.register_lake_correction(db, 'INPUT-base', correction)['id'], derived['id'])
        _, arrays = forcing.load_input(db, derived['id'])
        np.testing.assert_array_equal(arrays['precipitation'], [[2.], [3.]])
        self.assertEqual(forcing.lake_precipitation_factor(derived), 0.5)
        with self.assertRaisesRegex(ValueError, '不得对已经订正'):
            forcing.register_lake_correction(db, derived['id'], correction)


class ContinuationJobTests(unittest.TestCase):
    def test_job_resolves_current_input_and_freezes_parent_before_queue(self):
        from backend.jobs import Jobs
        from backend.model_core import parameters
        db = Database(tempfile.mkdtemp(prefix='selinco-continuation-job-'))
        manifest = {'id': 'current', 'spatialVersion': 'space', 'sources': []}
        checkpoint = {'schemaVersion': 3, 'inputId': 'old', 'identity': {'spatialVersion': 'space',
                      'modelImplementation': 'current-model', 'routingMode': 'source-separated'},
                      'parameters': parameters({}), 'partitionParameters': {}, 'validAt': '2024-12-31T23:00:00'}
        with db.connect() as connection:
            connection.execute('INSERT INTO input_products VALUES(?,?,?,?)', ('current', 'current', now(), canonical(manifest)))
            connection.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result) VALUES(?,?,?,?,?,?,?)',
                               ('parent', 'simulation', 'succeeded', now(), now(), canonical({'_observations': {}}),
                                canonical({'checkpointSHA256': digest(checkpoint)})))
            connection.execute('INSERT INTO states VALUES(?,?,?,?,?,?,?)', ('state', 'parent', now(), checkpoint['validAt'],
                               canonical(checkpoint), '', 0))
        captured = []
        def runner(db, request, folder, progress, cancelled):
            captured.append(request)
            return {'identity': {}, 'daily': []}
        jobs = Jobs(db)
        try:
            with patch('backend.grid_simulation.model_identity', return_value='current-model'), \
                 patch('backend.grid_simulation.observations', return_value={'profile':'new','level':{},'flow':{}}), \
                 patch('backend.grid_spatial.load', return_value={'version':'space'}), \
                 patch('backend.model_forcing.resolve', return_value=({'id':'current','manifest':manifest},{})), \
                 patch('backend.grid_simulation.prepare', return_value={'fixture':True}), \
                 patch('backend.grid_simulation.verify_parent_forcing') as verify, \
                 patch('backend.grid_simulation.run', side_effect=runner):
                request = {'stateId':'state','start':'2025-01-01T00:00:00','end':'2025-02-01T00:00:00'}
                submitted = jobs.submit('continuation', request)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    result = db.task(submitted['id'])
                    if result['status'] not in ('queued','running'):
                        break
                    time.sleep(.02)
                self.assertEqual(result['status'], 'succeeded', result['message'])
                self.assertEqual(captured[0]['inputId'], 'current')
                self.assertEqual(captured[0]['_observations']['profile'], 'new')
                self.assertEqual(captured[0]['_parentState'], checkpoint)
                self.assertEqual(captured[0]['parameters'], checkpoint['parameters'])
                verify.assert_called_once()
                with self.assertRaisesRegex(ValueError, '续算参数与父状态不一致'):
                    jobs.submit('continuation', {**request, 'parameters': {'KC': 0.5}})
                with patch('backend.grid_simulation.verify_parent_forcing', side_effect=ValueError('历史气象资料已修订')):
                    with self.assertRaisesRegex(ValueError, '历史气象资料已修订'):
                        jobs.submit('continuation', request)
        finally:
            jobs.close()


if __name__ == '__main__':
    unittest.main()
