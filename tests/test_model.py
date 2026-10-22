import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from backend.model_core import *
from backend.simulation import lake_balance
from backend.forcing import deaccumulate
from backend.observations import expand_level
from backend.vendor.runoff_platform.core.types import LandParams, LandState, GlacierParams, GlacierState
from backend.vendor.runoff_platform.models.xaj import simulate_land
from backend.vendor.runoff_platform.models.glacier import simulate_glacier


class NumericalTests(unittest.TestCase):
    def test_scalar_reference_and_continuation(self):
        rng = np.random.default_rng(3)
        n = 960
        rain = rng.gamma(.3, 6, n)
        temp = np.sin(np.arange(n) / 70) * 13
        evap = rng.random(n) * .4
        meteo = pd.DataFrame({'precipitation': rain, 'temperature': temp, 'pet': evap}, index=pd.date_range('2024-01-01', periods=n, freq='h'))
        p = parameters()
        vector = np.array([p[k] for k in PARAMETER_NAMES])
        for glacier in (False, True):
            units = [{'kind': 'glacier' if glacier else 'land'}]
            state = initial_states(units, p)
            initial = state.copy()
            d, history, final = run_kernel(rain[:, None], temp[:, None], evap[:, None], np.array([glacier]), state, vector)
            if glacier:
                gp = GlacierParams()
                reference = simulate_glacier(meteo, gp, GlacierState(swe=0, snow_liquid=0, glacier_storage=0))
                names = ['swe', 'snow_liquid', 'glacier_storage']
                actual = history[:, 0, [0, 1, 7]]
            else:
                lp = LandParams(KC=p['KC'], WM=p['WM'], WUM=20, WLM=80, B=p['B'], C=p['C'], SM=p['SM'], EX=p['EX'], KI=p['KI'], KG=p['KG'], IM=p['IM'])
                reference = simulate_land(meteo, lp, LandState(swe=0, snow_liquid=0, WU=10, WL=40, WD=25, S=0, FR=0))
                names = ['swe', 'snow_liquid', 'WU', 'WL', 'WD', 'S', 'FR']
                actual = history[:, 0, :7]
            np.testing.assert_allclose(actual, reference[names].values, atol=1e-9, rtol=1e-9)
            np.testing.assert_allclose(d[:, 0, :7], reference[['RS', 'RI', 'RG', 'glacier_runoff', 'snowmelt', 'icemelt', 'et_actual']].values, atol=1e-9)
            self.assertLess(np.abs(d[:, :, 7]).max(), 1e-9)
            a, ah, s = run_kernel(rain[:411, None], temp[:411, None], evap[:411, None], np.array([glacier]), initial.copy(), vector)
            b, bh, s = run_kernel(rain[411:, None], temp[411:, None], evap[411:, None], np.array([glacier]), s, vector)
            np.testing.assert_array_equal(d, np.concatenate([a, b]))
            np.testing.assert_array_equal(history, np.concatenate([ah, bh]))

    def test_snow_exhaustion_and_ice_partial_hour(self):
        p = np.array([DEFAULT_PARAMETERS[k] for k in PARAMETER_NAMES])
        state = np.zeros(8)
        state[0] = .5
        d = unit_step(0, 6., 0, state, True, p)
        self.assertAlmostEqual(d[4], .5)
        self.assertAlmostEqual(d[5], .875)
        self.assertLess(abs(d[7]), 1e-12)

    def test_glacier_sublimation_controls_are_explicit_and_hourly_bounded(self):
        p = parameters({'iceSublimationFraction': 1.0, 'iceSublimationMaxMmHour': .01})
        vector = np.array([p[k] for k in PARAMETER_NAMES])
        diagnostic = unit_step(0., -5., 1., np.zeros(8), True, vector)
        self.assertAlmostEqual(diagnostic[8], .01)
        self.assertLess(abs(diagnostic[7]), 1e-12)
        p = parameters({'iceSublimationFraction': 0.0})
        vector = np.array([p[k] for k in PARAMETER_NAMES])
        self.assertEqual(unit_step(0., -5., 1., np.zeros(8), True, vector)[8], 0.)

    def test_routing_conservation_and_resume(self):
        inflow = np.random.default_rng(9).random(200) * 1e6
        q, h, storage, queue = route_series(inflow, 17., np.zeros(6), 28., 4.3)
        self.assertLess(abs(inflow.sum() + 17 - q.sum() - h[-1]), 1e-6)
        a, _, storage, queue = route_series(inflow[:83], 17., np.zeros(6), 28., 4.3)
        b, _, storage, queue = route_series(inflow[83:], storage, queue, 28., 4.3)
        np.testing.assert_array_equal(q, np.r_[a, b])

    def test_lake_balance_and_resume(self):
        times = pd.date_range('2024-06-01', periods=72, freq='h')
        w = [{'id': 'x', 'start': times[0], 'end': times[-1], 'level': 4543.2}]
        q = np.full(72, 10.)
        rain, evap = np.full(72, 1.), np.full(72, .2)
        h, v, flux, state, _ = lake_balance(times, q, rain, evap, 2e9, .8, w)
        self.assertAlmostEqual(v[-1], flux[1:].sum())
        self.assertAlmostEqual(h[-1], 4543.2 + v[-1] / 2e9)
        ha, va, _, s, _ = lake_balance(times[:31], q[:31], rain[:31], evap[:31], 2e9, .8, w)
        hb, vb, _, s, _ = lake_balance(times[31:], q[31:], rain[31:], evap[31:], 2e9, .8, w, s)
        np.testing.assert_array_equal(h, np.r_[ha, hb])
        np.testing.assert_array_equal(v, np.r_[va, vb])

    def test_era5_reset_utc_and_negative_evaporation(self):
        times = pd.date_range('2023-12-31T23:00', periods=5, freq='h')
        a = np.array([23, 24, 1, 2, 3.])[:, None]
        result = deaccumulate(a, times)
        self.assertTrue(np.isnan(result[0, 0]))
        np.testing.assert_array_equal(result[1:, 0], [1, 1, 1, 1])
        np.testing.assert_array_equal(deaccumulate(-a, times)[1:, 0], [-1, -1, -1, -1])
        self.assertEqual((times[2] + pd.Timedelta(hours=8)).hour, 9)

    def test_minute_level_anchor_uses_partial_hour_flux(self):
        times = pd.date_range('2024-06-01T09:00', periods=2, freq='h')
        window = [{'id': 'minute', 'start': times[0], 'end': times[-1], 'level': 4543., 'firstHourFraction': .5}]
        h, v, flux, state, _ = lake_balance(times, np.full(2, 10.), np.zeros(2), np.zeros(2), 2e9, .8, window)
        self.assertAlmostEqual(v[0], 18000)
        self.assertAlmostEqual(v[1], 54000)
        self.assertAlmostEqual(h[-1], 4543 + 54000 / 2e9)

    def test_level_abbreviations_and_flags(self):
        for raw in ('61', '61.0', 61.0):
            self.assertEqual(expand_level(raw, 4543.63)[0], 4543.61)
        self.assertEqual(expand_level('L', 4543)[2], 'ice_or_stopped')
        self.assertEqual(expand_level('(4543.62)', None)[2], 'qualified')
        self.assertIsNone(expand_level('61', None)[0])


if __name__ == '__main__':
    unittest.main()
