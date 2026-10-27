"""Checks for area aggregation and the continuous numerical contract."""
import unittest
from copy import deepcopy

import numpy as np
import pandas as pd

from backend.grid_spatial import aggregate_manifest, _AGGREGATE_SOURCE_FILES
from backend.grid_simulation import run, unit_forcing
from backend.model_core import DEFAULT_PARAMETERS, parameters


class AggregateContinuousTests(unittest.TestCase):
    def setUp(self):
        units = [
            {'id': 'D1', 'partition': 'duja', 'kind': 'land', 'elevationBandMin': 5000,
             'areaKm2': 2., 'elevation': 5250., 'gridIndex': 0},
            {'id': 'D2', 'partition': 'duja', 'kind': 'land', 'elevationBandMin': 5000,
             'areaKm2': 1., 'elevation': 5250., 'gridIndex': 1},
            {'id': 'U1', 'partition': 'unresolved', 'kind': 'land', 'elevationBandMin': 5000,
             'areaKm2': 4., 'elevation': 5250., 'gridIndex': 1},
        ]
        self.parent = {'version': 'GRID-test', 'unitStrategy': 'grid-partition-elevation-surface-v2',
                       'projectId': 'selinco', 'units': units,
                       'grids': [{'referenceElevation': 5000.}, {'referenceElevation': 5200.}],
                       'forcingGridVersion': 'grid-test', 'lakeAreaKm2': 20., 'lakeWeights': [.5, .5],
                       'drainage': {'inflowComplete': False}}
        self.spatial = aggregate_manifest(self.parent, {n: n for n in _AGGREGATE_SOURCE_FILES})
        times = pd.date_range('2024-12-29', periods=169, freq='h')
        self.source = {'times': times, 'spatial': self.spatial,
                       'manifest': {'id': 'INPUT-test', 'dataSHA256': 'data', 'transformationSHA256': 'transform'},
                       'arrays': {'precipitation': np.tile([.3, .6], (len(times), 1)),
                                  'temperature': np.tile([5., 6.], (len(times), 1)),
                                  'evaporation': np.tile([.05, .08], (len(times), 1))}}
        self.observation = {'profile': 'test-quality', 'level': {'2024-12-31T00:00:00': 4544.1,
                           '2025-01-01T00:00:00': 4544.2}, 'flow': {'2025-01-01T00:00:00': 1.}}
        self.request = {'inputId': 'INPUT-test', 'spatialVersion': self.spatial['version'],
                        'start': times[0].isoformat(), 'end': times[-1].isoformat(),
                        'parameters': deepcopy(DEFAULT_PARAMETERS), '_observations': self.observation,
                        '_modelIdentity': 'numeric-test', 'initialLakeTime': '2024-12-30T12:00:00',
                        'initialLakeLevel': 4544., 'initialLakeSource': 'independent test anchor'}

    def test_members_and_corrected_forcing_are_area_weighted(self):
        groups = self.spatial['aggregation']
        self.assertEqual(len(groups), 2)
        self.assertEqual(sorted(m['unitId'] for g in groups for m in g['members']), ['D1', 'D2', 'U1'])
        self.assertEqual(self.spatial['units'][0]['areaKm2'], 3.)
        np.testing.assert_allclose(self.spatial['weights'][0], [2/3, 1/3], atol=1e-15)
        p = parameters(DEFAULT_PARAMETERS)
        rain, temp, evap = unit_forcing(self.spatial, self.source['arrays'], [0], p, {})
        self.assertAlmostEqual(rain[0, 0], .4)
        self.assertAlmostEqual(evap[0, 0], .06)
        self.assertAlmostEqual(temp[0, 0], (2*(5 + 250*p['lapseRate']) + (6 + 50*p['lapseRate']))/3)

    def test_evaluation_observation_gaps_do_not_reset_the_process(self):
        full = run(None, self.request, prepared=self.source)
        sparse = run(None, {**self.request, '_observations': {'profile': 'test-quality', 'level': {}, 'flow': {}}},
                     prepared=self.source)
        self.assertEqual(full['initialization']['time'], '2024-12-30T12:00:00')
        self.assertEqual(full['checkpoint']['lake'], sparse['checkpoint']['lake'])
        self.assertEqual(full['checkpoint']['routing'], sparse['checkpoint']['routing'])
        np.testing.assert_array_equal(full['checkpoint']['unitStates'], sparse['checkpoint']['unitStates'])
        for a, b in zip(full['daily'], sparse['daily']):
            self.assertEqual(a['lakeLevel'], b['lakeLevel'])
            self.assertEqual(a['total'], b['total'])
        self.assertTrue(all(r['lakeLevel'] is not None for r in full['daily'] if r['date'] >= '2024-12-31'))
        self.assertTrue(any(r['excludedRunoff'] > 0 for r in full['daily'] if r['excludedRunoff'] is not None))
        self.assertLess(full['diagnostics']['landGlacierMaxBalanceErrorMm'], 1e-6)
        self.assertLess(max(abs(r['lakeBalanceErrorM3']) for r in full['daily'] if r['lakeBalanceErrorM3'] is not None), 1e-6)

    def test_resume_matches_full_and_rejects_changed_identity(self):
        full = run(None, self.request, prepared=self.source)
        first = run(None, {**self.request, 'end': self.source['times'][72].isoformat()}, prepared=self.source)
        second_request = {**self.request, 'start': self.source['times'][73].isoformat(), '_parentState': first['checkpoint']}
        second = run(None, second_request, prepared=self.source)
        np.testing.assert_allclose(full['checkpoint']['unitStates'], second['checkpoint']['unitStates'], rtol=0, atol=1e-12)
        self.assertEqual(full['checkpoint']['lake'], second['checkpoint']['lake'])
        self.assertEqual(full['checkpoint']['routing'], second['checkpoint']['routing'])
        for cause in ('parameters', 'forcing', 'unit_order'):
            bad = deepcopy(second_request)
            source = deepcopy(self.source)
            if cause == 'parameters': bad['parameters']['KC'] += .01
            if cause == 'forcing':
                source['arrays']['precipitation'][0, 0] += 1
                source['manifest']['dataSHA256'] = 'changed'
            if cause == 'unit_order': bad['_parentState']['unitIds'].reverse()
            with self.subTest(cause=cause), self.assertRaises(ValueError):
                run(None, bad, prepared=source)

    def test_resume_uses_new_evaluation_observations_without_resetting_state(self):
        full = run(None, self.request, prepared=self.source)
        first = run(None, {**self.request, 'end': self.source['times'][72].isoformat()}, prepared=self.source)
        updated = deepcopy(self.observation)
        updated['profile'] = 'updated-quality'
        updated['level']['2025-01-02T00:00:00'] = 4544.5
        updated['flow']['2025-01-02T00:00:00'] = 2.0
        second = run(None, {**self.request, 'start': self.source['times'][73].isoformat(),
                            '_parentState': first['checkpoint'], '_observations': updated}, prepared=self.source)
        self.assertNotEqual(first['identity']['observationSnapshotSHA256'], second['identity']['observationSnapshotSHA256'])
        self.assertEqual(second['initialization'], first['initialization'])
        self.assertEqual(second['checkpoint']['lake'], full['checkpoint']['lake'])
        self.assertEqual(second['checkpoint']['routing'], full['checkpoint']['routing'])
        np.testing.assert_allclose(second['checkpoint']['unitStates'], full['checkpoint']['unitStates'], rtol=0, atol=1e-12)
        day = next(row for row in second['daily'] if row['date'].startswith('2025-01-02'))
        self.assertEqual(day['observedLevel'], 4544.5)
        self.assertEqual(day['observedFlow'], 2.0)


if __name__ == '__main__':
    unittest.main()
