import unittest
import numpy as np
import pandas as pd
from backend.model_core import parameters, PARAMETER_NAMES, run_kernel
from backend.cryosphere_audit import cryosphere_report


class CryosphereAuditTests(unittest.TestCase):
    def run_case(self):
        p = parameters()
        # Mixed precipitation, rain on snow, freeze, snow exhaustion and bare ice.
        rain = np.array([[12., 12.], [3., 3.], [1., 1.], [0., 0.], [2., 2.], [0., 0.]])
        temp = np.array([[-5., -5.], [1., 1.], [-3., -3.], [40., 40.], [60., 60.], [80., 80.]])
        pet = np.full_like(rain, .3)
        units = [{'kind': 'land', 'areaKm2': 2}, {'kind': 'glacier', 'areaKm2': .2}]
        start = np.zeros((2, 8))
        vector = np.array([p[k] for k in PARAMETER_NAMES])
        d, h, _ = run_kernel(rain, temp, pet, np.array([False, True]), start.copy(), vector)
        times = pd.date_range('2024-12-31T22:00', periods=6, freq='h')
        return p, rain, temp, pet, units, start, d, h, times

    def test_phase_snow_ice_ledgers_use_saved_states_and_end_hour_years(self):
        p, rain, temp, pet, units, start, d, h, times = self.run_case()
        r = cryosphere_report(times, rain, temp, pet, d, h, start, units, p)
        for k in ('snowSolidMaxErrorMm', 'snowLiquidMaxErrorMm', 'snowTransferMaxErrorMm'):
            self.assertLess(r[k], 1e-10)
        self.assertFalse(r['finiteIceVolumeModelled'])
        land = next(x for x in r['annual'] if x['year'] == 2024 and x['kind'] == 'land')
        self.assertEqual(land['hours'], 3)  # 00:00 interval belongs to Dec 31.
        self.assertAlmostEqual(land['precipitationMm'], 16.)
        self.assertAlmostEqual(land['snowfallMm']+land['rainfallMm'], 16.)
        self.assertGreater(land['refreezeMm'], 0.)
        glacier = next(x for x in r['annual'] if x['year'] == 2025 and x['kind'] == 'glacier')
        self.assertGreater(glacier['icemeltMm'], 0.)
        self.assertAlmostEqual(glacier['externalIceLossMm'], glacier['icemeltMm']+glacier['iceSublimationMm'])
        self.assertAlmostEqual(glacier['modelledSolidMassChangeMm'], glacier['solidSnowStorageChangeMm']-glacier['externalIceLossMm'])

    def test_split_audit_does_not_double_count_initial_snow(self):
        p, rain, temp, pet, units, start, d, h, times = self.run_case()
        whole = cryosphere_report(times, rain, temp, pet, d, h, start, units, p)
        split = 4
        a = cryosphere_report(times[:split], rain[:split], temp[:split], pet[:split], d[:split], h[:split], start, units, p)
        b = cryosphere_report(times[split:], rain[split:], temp[split:], pet[split:], d[split:], h[split:], h[split-1], units, p)
        for kind in ('land', 'glacier'):
            for term in ('snowfallM3', 'snowmeltM3', 'refreezeM3', 'snowSublimationM3', 'snowDrainageM3', 'snowStorageChangeM3', 'icemeltM3'):
                expected = sum(x[term] for x in whole['annual'] if x['kind'] == kind)
                actual = sum(x[term] for r in (a, b) for x in r['annual'] if x['kind'] == kind)
                self.assertAlmostEqual(actual, expected, places=8)

    def test_corrupted_snow_state_produces_nonzero_closure_error(self):
        p, rain, temp, pet, units, start, d, h, times = self.run_case()
        h = h.copy()
        h[2, 0, 0] += 1.
        r = cryosphere_report(times, rain, temp, pet, d, h, start, units, p)
        self.assertGreater(r['snowSolidMaxErrorMm'], .9)
