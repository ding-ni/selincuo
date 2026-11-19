import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, local
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend import state_update


class FakeClimate:
    def __init__(self, clim):
        self.clim = np.asarray(clim, dtype=float)
        self.weights = np.eye(len(clim))
        self.areas = np.ones(len(clim))

    def daily_precipitation(self, date):
        return self.clim

    def basin_mean(self, grid):
        return float(np.average(grid @ self.weights.T, weights=self.areas))


class StateUpdateTests(unittest.TestCase):
    def test_state_plan_and_forecast_share_the_first_climate_build(self):
        spatial = {'units': [{'areaKm2': 1., 'gridIndex': 0}], 'grids': [{}]}
        context = {'spatial': spatial, 'state': {'inputId': 'INPUT', 'validAt': '2026-10-21T00:00:00'}}
        callers = Barrier(2)
        checked = local()
        exists = Path.is_file

        def both_miss(path):
            found = exists(path)
            if path.parent.name == 'climatology' and not getattr(checked, 'cache', False):
                checked.cache = True
                callers.wait(timeout=10)
            return found

        def build(db, input_id, base, path):
            ones = np.ones((366, 1))
            np.savez(path, precipitation=ones, temperature=ones, evaporation=ones)

        with TemporaryDirectory() as workspace, patch.object(Path, 'is_file', both_miss), \
             patch.object(state_update._Climate, '_build', side_effect=build) as writer, \
             ThreadPoolExecutor(max_workers=2) as pool:
            db = SimpleNamespace(workspace=Path(workspace))
            pending = [pool.submit(state_update._Climate, db, context) for _ in range(2)]
            for future in pending:
                np.testing.assert_array_equal(future.result(timeout=15).daily_precipitation('2026-10-21'), [1.])
            writer.assert_called_once()

    def test_manual_areal_total_keeps_basin_mean_and_pattern(self):
        climate = FakeClimate([1., 2., 3.])
        grid, method = state_update._grid_precipitation(climate, [], pd.Timestamp('2026-10-21'), {}, 6.)
        self.assertEqual(method, 'manual')
        self.assertAlmostEqual(climate.basin_mean(grid), 6.)
        np.testing.assert_allclose(grid / grid[0], [1., 2., 3.])

    def test_station_ratio_scales_climatological_pattern(self):
        climate = FakeClimate([1., 2., 4.])
        station = {'id': 'S', 'gridIndex': 0, 'distanceKm': np.array([1., 10., 20.])}
        grid, method = state_update._grid_precipitation(climate, [station], pd.Timestamp('2026-10-21'), {'S': 3.}, None)
        self.assertEqual(method, 'station')
        np.testing.assert_allclose(grid, [3., 6., 12.])

    def test_station_forecast_uses_gauges_and_climatology_for_blank_days(self):
        from unittest.mock import patch
        from backend import forecast_inputs
        climate = FakeClimate([1., 2.])
        climate.hourly = lambda variable, time: np.array([2., 3.]) if variable == 'temperature' else np.array([.1, .1])
        station = {'id': 'S', 'name': 'S站', 'gridIndex': 0, 'distanceKm': np.array([1., 10.])}
        spatial = {'units': [{'id': 'U0', 'areaKm2': 1., 'elevation': 4600.}, {'id': 'U1', 'areaKm2': 1., 'elevation': 4700.}],
                   'grids': [{}, {}], 'lakeWeights': [.5, .5], 'version': 'S'}
        context = {'spatial': spatial, 'regions': {}, 'parameters': {}}
        def day_forcing(ctx, clim, day, times):
            grid = day['grid']
            return (np.repeat(grid[None, :] / 24., 24, axis=0), np.full((24, 2), 1.), np.full((24, 2), .1),
                    np.full(24, grid.mean() / 24.), np.full(24, .1))
        saved = {}
        def save(db, ctx, payload, times, arrays, metadata, member_ids=None):
            saved.update(payload=payload, arrays=arrays, metadata=metadata); return {'id': 'X'}
        with patch('backend.forecast_inputs._context', return_value=context), \
             patch('backend.state_update._Climate', return_value=climate), \
             patch('backend.state_update._rain_stations', return_value=[station]), \
             patch('backend.state_update._day_forcing', side_effect=day_forcing), \
             patch('backend.forecast_inputs._save', side_effect=save):
            daily = [{'stations': {'S': 3.}}] + [{} for _ in range(6)]
            forecast_inputs.create_station_forecast(None, {'sourceResultId': 'J', 'issueTime': '2026-10-21T00:00:00',
                                                           'days': 7, 'method': 'station', 'daily': daily})
            self.assertEqual(saved['metadata']['dayMethods'], {'station': 1, 'climatology': 6})
            p = saved['arrays']['precipitation'][0]
            np.testing.assert_allclose(p[:24].sum(axis=0), [3., 6.])      # gauge 3 mm scales the pattern
            np.testing.assert_allclose(p[24:48].sum(axis=0), [1., 2.])    # blank day: same-period mean
            with self.assertRaises(ValueError):
                forecast_inputs.create_station_forecast(None, {'sourceResultId': 'J', 'issueTime': '2026-10-21T00:00:00', 'days': 7,
                                                               'daily': [{'stations': {'S': -1}}] + [{}] * 6})
            with self.assertRaises(ValueError):
                forecast_inputs.create_station_forecast(None, {'sourceResultId': 'J', 'issueTime': '2026-10-21T00:00:00', 'days': 16})

    def test_no_data_day_uses_climatology(self):
        climate = FakeClimate([1., 2.])
        grid, method = state_update._grid_precipitation(climate, [], pd.Timestamp('2026-10-21'), {}, None)
        self.assertEqual(method, 'climatology')
        np.testing.assert_allclose(grid, [1., 2.])

    def test_overrides_validate_ranges(self):
        values = state_update._overrides({'daily': [{'date': '2026-10-22', 'precipitationMm': '5', 'lakeLevelM': ''}]})
        self.assertEqual(values[pd.Timestamp('2026-10-22')], {'precipitationMm': 5.})
        with self.assertRaises(ValueError):
            state_update._overrides({'daily': [{'date': '2026-10-22', 'lakeLevelM': 4600}]})
        with self.assertRaises(ValueError):
            state_update._overrides({'daily': [{'date': '2026-10-22', 'precipitationMm': -1}]})

    def test_window_wraps_year_end(self):
        doy = np.array([1, 5, 362, 366, 180])
        np.testing.assert_array_equal(state_update._window(doy, 3, 7), [True, True, True, True, False])


if __name__ == '__main__':
    unittest.main()
