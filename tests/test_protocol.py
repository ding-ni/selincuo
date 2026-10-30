import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend.calibration import calibrate
from backend.common import ROOT, read_json
from backend.model_core import parameters


class ProtocolTests(unittest.TestCase):
    def test_generation_routing_searches_are_disjoint_and_ice_sublimation_is_versioned(self):
        from backend.calibration_config import RUNOFF_KEYS, ROUTING_KEYS, LAND_KEYS, SNOW_KEYS
        from backend.model_core import PARAMETER_DEFINITIONS
        self.assertFalse(set(RUNOFF_KEYS) & set(ROUTING_KEYS))
        self.assertEqual(set(ROUTING_KEYS), {'tauHours', 'lagHours', 'interflowTauHours', 'baseflowTauHours'})
        p = parameters()
        self.assertEqual(p['iceSublimationFraction'], .25)
        self.assertEqual(p['iceSublimationMaxMmHour'], .08)
        roles={item['key']:item['calibrationRole'] for item in PARAMETER_DEFINITIONS}
        self.assertEqual({k for k,v in roles.items() if v=='land-runoff'},set(LAND_KEYS))
        self.assertEqual({k for k,v in roles.items() if v=='snow-ice'},set(SNOW_KEYS))
        self.assertEqual({k for k,v in roles.items() if v=='routing'},set(ROUTING_KEYS))
        self.assertEqual(roles['iceDdf'],'snow-ice')
        selected={d['key'] for d in PARAMETER_DEFINITIONS if d['flowCalibration']['defaultSelected']}
        self.assertEqual(selected, set(LAND_KEYS + SNOW_KEYS + ROUTING_KEYS))
        self.assertEqual({k for k,v in roles.items() if v=='fixed-prior'},
                         {'iceSublimationFraction', 'iceSublimationMaxMmHour'})

    def test_validation_values_do_not_change_parameter_search(self):
        prepared = {'times': pd.DatetimeIndex(['2023-01-01', '2025-12-31'])}
        def run(future_error):
            def simulation(db, request, folder=None, **kwargs):
                p = {**request['parameters'], **request.get('partitionParameters', {}).get('duja', {})}
                # Controlled objective surface, with deliberately unrelated
                # validation performance. Test orchestration, not model skill.
                loss = ((p['WM'] - 130) / 100) ** 2 + ((p['KC'] - 1.1) / 2) ** 2
                return {'parameters': p, 'metrics': {'2024': {'flow': {'count': 100, 'nse': 1 - loss, 'logNse': 1-loss, 'pbias': 0}, 'lake': {'count': 80, 'rmse': abs(p['lakeCoefficient'] - .6)}},
                                                    '2025': {'flow': {'rmse': future_error}}}}
            with tempfile.TemporaryDirectory() as temp, patch('backend.calibration.prepare', return_value=prepared), patch('backend.calibration.run_simulation', side_effect=simulation):
                return calibrate(None, {'inputId': 'test-only', 'start': '2023-01-01', 'maxEvaluations': 30, 'seed': 7}, Path(temp))
        a, b = run(0), run(1e8)
        self.assertEqual(a['parameters'], b['parameters'])
        self.assertEqual(a['calibration']['trace'], b['calibration']['trace'])
        self.assertGreater(len({r['parameters']['KC'] for r in a['calibration']['trace']}), 1)
        self.assertEqual(a['calibration']['evaluations'], 30)
        self.assertEqual(a['calibration']['termination'], 'budget-exhausted')
        for key in ('snowDdf', 'iceDdf', 'lapseRate', 'lakeCoefficient'):
            self.assertEqual(a['parameters'][key], parameters()[key])

    def test_lake_stage_only_changes_evaporation(self):
        prepared = {'times': pd.DatetimeIndex(['2023-01-01', '2025-12-31'])}
        def simulation(db, request, folder=None, **kwargs):
            return {'parameters': request['parameters'], 'metrics': {'2024': {'lake': {'count': 40, 'rmse': abs(request['parameters']['lakeCoefficient'] - .55)}}}}
        base = parameters({'WM': 185, 'tauHours': 65})
        with tempfile.TemporaryDirectory() as temp, patch('backend.calibration.prepare', return_value=prepared), patch('backend.calibration.run_simulation', side_effect=simulation):
            result = calibrate(None, {'stage': 'lake', 'inputId': 'test-only', 'parameters': base, 'maxEvaluations': 15}, Path(temp))
        self.assertEqual({k: v for k, v in base.items() if k != 'lakeCoefficient'},
                         {k: v for k, v in result['parameters'].items() if k != 'lakeCoefficient'})
        with self.assertRaises(ValueError):
            calibrate(None, {'calibrationYear': 2025}, Path(temp))

    def test_map_labels_are_within_their_actual_basins_and_rivers_keep_source_ids(self):
        from shapely.geometry import shape, Point
        context = read_json(ROOT / 'data/derived/spatial/display-context.json')
        collection = read_json(ROOT / 'data/derived/spatial/map.geojson')
        for label in context['labels']:
            feature = next(f for f in collection['features'] if f['properties']['kind'] == label['kind'] and f['properties'].get('id') == label.get('id'))
            self.assertTrue(shape(feature['geometry']).covers(Point(label['anchor'])), label['name'])
        rivers = read_json(ROOT / 'data/derived/spatial/rivers-display.geojson')['features']
        self.assertEqual({f['properties']['basinId'] for f in rivers}, {'zajia','zagen','boqu','ali'})
        expected = {b['id']: b['name'] for b in read_json(ROOT / 'data/derived/spatial/manifest.json')['basins']}
        self.assertTrue(all(f['properties']['name'] == expected[f['properties']['basinId']] for f in rivers))

    def test_real_spatial_closure_and_small_glaciers(self):
        m = read_json(ROOT / 'data/derived/spatial/manifest.json')
        if not m:
            self.skipTest('尚未建立真实空间产品')
        weights = np.array(m['weights'])
        self.assertTrue(np.isfinite(weights).all())
        self.assertGreaterEqual(weights.min(), 0)
        np.testing.assert_allclose(weights.sum(axis=1), 1, atol=1e-10)
        self.assertAlmostEqual(sum(u['areaKm2'] for u in m['units']) + m['lakeAreaKm2'], m['domainAreaKm2'], places=4)
        self.assertAlmostEqual(sum(u['areaKm2'] for u in m['units'] if u['kind'] == 'glacier'), m['glacierAreaKm2'], places=4)
        self.assertTrue(any(u['areaKm2'] < 1 for u in m['units'] if u['kind'] == 'glacier'))
        self.assertLess(m['duja']['areaKm2'], next(b['areaKm2'] for b in m['basins'] if b['id'] == 'zajia'))


if __name__ == '__main__':
    unittest.main()
