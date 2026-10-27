import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from backend.parameter_regions import resolve_regions, region_initial_states, run_region_kernel
from backend.model_core import parameters, initial_states, run_kernel, PARAMETER_NAMES
from backend.cryosphere_audit import cryosphere_report
from backend.calibration import calibrate
from backend.calibration_config import ROUTING_KEYS


class ParameterRegionTests(unittest.TestCase):
    def test_region_changes_only_its_units_and_preserves_snow_and_resume(self):
        units = [{'partition': 'duja', 'kind': 'land', 'areaKm2': 2},
                 {'partition': 'ali', 'kind': 'land', 'areaKm2': 3},
                 {'partition': 'duja', 'kind': 'glacier', 'areaKm2': .1}]
        p = parameters()
        normalized, regions = resolve_regions(units, p, {'duja': {'KC': .5, 'WM': 240, 'snowDdf': 6}})
        self.assertEqual(normalized['duja']['WM'], 240)
        start = region_initial_states(units, p, regions)
        self.assertAlmostEqual(start[0, 2:5].sum(), 120)
        self.assertAlmostEqual(start[1, 2:5].sum(), p['WM']/2)
        rng = np.random.default_rng(81)
        rain = rng.gamma(.4, 3, (120, 3))
        temp = np.tile(np.sin(np.arange(120)/10)[:, None]*10, (1, 3))
        pet = np.full((120, 3), .2)
        ice = np.array([False, False, True])
        full, h, final = run_region_kernel(rain, temp, pet, ice, start.copy(), units, p, regions)
        for i, u in enumerate(units):
            local = regions.get(u['partition'], p)
            expected, eh, _ = run_kernel(rain[:, i:i+1].copy(), temp[:, i:i+1].copy(), pet[:, i:i+1].copy(),
                                         ice[i:i+1], start[i:i+1].copy(), np.array([local[k] for k in PARAMETER_NAMES]))
            np.testing.assert_array_equal(full[:, i:i+1], expected)
            np.testing.assert_array_equal(h[:, i:i+1], eh)
        a, ah, state = run_region_kernel(rain[:43], temp[:43], pet[:43], ice, start.copy(), units, p, regions)
        b, bh, state = run_region_kernel(rain[43:], temp[43:], pet[43:], ice, state, units, p, regions)
        np.testing.assert_array_equal(full, np.concatenate([a, b]))
        np.testing.assert_array_equal(h, np.concatenate([ah, bh]))
        np.testing.assert_array_equal(final, state)
        report = cryosphere_report(pd.date_range('2024-01-01T01:00', periods=120, freq='h'),
                                   rain, temp, pet, full, h, start, units, p, regions)
        self.assertLess(report['snowTransferMaxErrorMm'], 1e-10)

    def test_invalid_scope_and_lake_override_rejected(self):
        units = [{'partition': 'duja', 'kind': 'land'}]
        for values in ([], {'foreign': {'KC': .5}}, {'duja': {'WM': -1}},
                       {'duja': {'lakeCoefficient': .8}}, {'duja': 3}):
            with self.assertRaises(ValueError):
                resolve_regions(units, parameters(), values)

    def test_empty_override_preserves_uniform_initial_states(self):
        units = [{'partition': 'duja', 'kind': 'land'}, {'partition': 'ali', 'kind': 'glacier'}]
        normalized, regions = resolve_regions(units, parameters(), {'duja': {}})
        self.assertEqual(normalized, {})
        self.assertEqual(regions, {})
        np.testing.assert_array_equal(region_initial_states(units, parameters(), regions), initial_states(units, parameters()))

    def test_calibration_freezes_other_regions_and_lake_stage_freezes_all_land(self):
        base = parameters({'KC': .73, 'WM': 220})
        overrides = {'ali': {'KC': .65}, 'duja': {'KC': .6, 'WM': 240}}
        source = {'times': pd.DatetimeIndex(['2023-01-01', '2026-01-01'])}
        for stage in ('routing', 'lake'):
            calls = []
            def simulation(db, request, **kwargs):
                calls.append(request)
                if request.get('_skipDiagnostics'):
                    self.assertEqual(request['end'], '2025-01-01T00:00:00')
                if stage == 'routing':
                    self.assertEqual(request['parameters'], base)
                    self.assertEqual(request['partitionParameters']['ali'], overrides['ali'])
                    local = parameters({**base, **overrides['duja']})
                    for key, value in local.items():
                        if key not in ROUTING_KEYS and key != 'lakeCoefficient':
                            self.assertEqual(request['partitionParameters']['duja'][key], value)
                else:
                    self.assertEqual(request['partitionParameters'], overrides)
                    self.assertEqual({k: v for k, v in request['parameters'].items() if k != 'lakeCoefficient'},
                                     {k: v for k, v in base.items() if k != 'lakeCoefficient'})
                metric = {'count': 100, 'nse': .7, 'logNse': .5, 'pbias': 2, 'rmse': .2}
                return {'parameters': request['parameters'], 'metrics': {'2024': {'flow': metric, 'lake': metric}, '2025': {'flow': metric, 'lake': metric}}}
            with tempfile.TemporaryDirectory() as tmp, patch('backend.calibration.prepare', return_value=source), patch('backend.calibration.run_simulation', side_effect=simulation):
                result = calibrate(None, {'stage': stage, 'inputId': 'test', 'parameters': base,
                                         'partitionParameters': overrides, 'maxEvaluations': 10,
                                         'end': '2026-01-01'}, Path(tmp))
            self.assertEqual(calls[-1]['end'], '2026-01-01')
            self.assertEqual(result['calibration']['calibrationPartition'], 'duja' if stage == 'routing' else None)

    def test_first_flow_calibration_also_defaults_to_duja_only(self):
        source = {'times': pd.DatetimeIndex(['2023-01-01', '2026-01-01'])}
        base = parameters()
        def simulation(db, request, **kwargs):
            self.assertEqual(request['parameters'], base)
            self.assertEqual(set(request['partitionParameters']), {'duja'})
            return {'metrics': {'2024': {'flow': {'count': 100, 'nse': .7, 'logNse': .5, 'pbias': 2}}}}
        with tempfile.TemporaryDirectory() as tmp, patch('backend.calibration.prepare', return_value=source), patch('backend.calibration.run_simulation', side_effect=simulation):
            calibrate(None, {'inputId': 'test', 'maxEvaluations': 10}, Path(tmp))

    def test_sensitivity_changes_duja_instead_of_an_overridden_background_value(self):
        from backend.sensitivity import analyze
        base = parameters()
        regions = {'duja': {'KC': .63}, 'ali': {'KC': .7}}
        perturbed = []
        def simulation(db, request, **kwargs):
            if request.get('_skipDiagnostics'):
                self.assertEqual(request['partitionParameters']['ali'], regions['ali'])
                self.assertEqual(request['parameters']['KC'], base['KC'])
                if request['partitionParameters']['duja']['KC'] != .63:
                    perturbed.append(request['partitionParameters']['duja']['KC'])
            metric = {'count': 100, 'nse': .7, 'logNse': .5, 'pbias': 2, 'rmse': .2}
            return {'parameters': request['parameters'], 'metrics': {'2024': {'flow': metric, 'lake': metric}}}
        with tempfile.TemporaryDirectory() as tmp, patch('backend.sensitivity.prepare', return_value={}), patch('backend.sensitivity.run_simulation', side_effect=simulation):
            result = analyze(None, {'inputId': 'test', 'parameters': base, 'partitionParameters': regions}, Path(tmp))
        self.assertEqual(len(perturbed), 2)
        self.assertTrue(all(r['parameterScope'] == ('basin' if r['parameter'] == 'lakeCoefficient' else 'duja') for r in result['sensitivity']['rows']))
