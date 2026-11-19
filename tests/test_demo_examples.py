"""The offline examples are readable without adding any business results."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from backend import demo_examples, risk_scenarios
from backend.common import write_json
from backend.database import Database
from backend.operations_api import get
from scripts.assemble_release import copy_demo_resources


class OfflineDemoTests(unittest.TestCase):
    def test_installed_assets_use_read_only_endpoints_and_keep_business_lists_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            demo = root / demo_examples.DIRECTORY
            roads = root / 'data/derived/spatial/承灾体'
            forecast = {'id': 'FORECAST-example', 'name': '十天演示', 'hours': 240,
                        'issueTime': '2026-10-21T00:00:00', 'times': ['2026-10-21T01:00:00']}
            scenario = {'id': 'DEMO-RISK-CURRENT', 'name': '现状溃口', 'resultId': 'DEMO-RISK-RUN-CURRENT',
                        'peakFrame': 0, 'exposureIds': ['DEMO-EXPOSURE-ROAD'],
                        'outlet': {'crestM': 4543.96}, 'demOffsetM': 2.41}
            exposure = {'id': 'DEMO-EXPOSURE-ROAD', 'source': '发行道路', 'bundleFile': 'road.geojson'}
            history = {'rows': [{'date': '2026-10-20', 'level': 4544.1}]}
            write_json(demo / 'demo.json', {'forecast': forecast, 'scenarios': [scenario],
                                           'history': history, 'exposures': [exposure]})
            write_json(roads / 'road.geojson', {'type': 'FeatureCollection', 'features': [{'id': 'road-1'}]})
            result_id = scenario['resultId']
            folder = demo / 'results' / result_id
            write_json(folder / '结果.json', {'id': result_id, 'scenario': scenario,
                       'path': f'results/{result_id}/inundation.npz',
                       'rows': [{'time': forecast['times'][0], 'level': 4544.2}],
                       'terrain': {'width': 1, 'height': 1, 'bounds': [89., 31., 89.01, 31.01]}})
            np.savez_compressed(folder / 'inundation.npz', frameRows=[0], depths=[[[.25]]])
            server = SimpleNamespace(db=Database(root / 'workspace-business'))
            with patch.object(demo_examples, 'DEMO_ROOT', demo), patch.object(risk_scenarios, 'EXPOSURE_BUNDLE', roads):
                self.assertEqual(get(server, 'risk-scenarios/demo', {})[1]['scenarios'][0]['resultId'], result_id)
                self.assertEqual(get(server, 'demo/forecasts/' + forecast['id'], {})[1], forecast)
                self.assertEqual(get(server, 'demo/forecasts/' + forecast['id'] + '/history', {})[1], history)
                self.assertEqual(get(server, 'forecasts', {})[1]['items'], [])
                with self.assertRaises(FileNotFoundError):
                    get(server, 'forecasts/' + forecast['id'], {})
                catalog = get(server, 'risk-scenarios', {})[1]
                self.assertEqual((catalog['scenarios'], catalog['results']), ([], []))
                path = 'risk-results/' + result_id
                self.assertEqual(get(server, path, {})[1]['id'], result_id)
                self.assertEqual(get(server, path + '/exposures', {})[1]['items'][0]['features'], [{'id': 'road-1'}])
                frame = get(server, path + '/frame', {'depthOnly': '1'})[1]
                self.assertEqual((frame['time'], frame['depths']), (forecast['times'][0], [[.25]]))

    def test_release_cannot_silently_omit_offline_examples(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, '缺少离线演示资料'):
                copy_demo_resources(root / 'missing', root / 'release')
