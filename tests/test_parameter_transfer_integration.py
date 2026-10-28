"""Numerical integration of transfer policies with isolated synthetic forcing.

Tests use the real model kernel and in-memory inputs, never production data or
result folders. Run with SELINCO_WORKSPACE set to an isolated location if Numba
compilation caching is enabled.
"""
from copy import deepcopy
import math
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend import grid_calibration, grid_simulation
from backend.calibration import calibrate
from backend.calibration_config import flow_objective
from backend.model_core import DEFAULT_PARAMETERS, PARAMETER_NAMES
from backend.simulation import metrics


class ParameterTransferNumericalTests(unittest.TestCase):
    def setUp(self):
        self.times = pd.date_range('2024-01-01', periods=169, freq='h')
        specs = [('duja-land', 'duja', 'land', 75.), ('duja-glacier', 'duja', 'glacier', 25.),
                 ('zajia', 'zajia_rest', 'land', 25.), ('zagen', 'zagen', 'land', 400.),
                 ('boqu', 'boqu', 'land', 9.), ('ali', 'ali', 'land', 225.),
                 ('fringe', 'fringe', 'land', 50.), ('unresolved', 'unresolved', 'land', 11.),
                 ('external', 'external', 'land', 22.)]
        units = [{'id': name, 'partition': partition, 'kind': kind, 'areaKm2': area,
                  'gridIndex': 0, 'elevation': 5000.}
                 for name, partition, kind, area in specs]
        hours = np.arange(len(self.times))
        rainfall = .1 + (hours % 24 < 9) * 1.2
        self.source = {'times': self.times,
                       'spatial': {'version': 'synthetic-transfer-space', 'units': units,
                                   'grids': [{'referenceElevation': 5000.}],
                                   'forcingGridVersion': 'synthetic-transfer-grid',
                                   'lakeWeights': [1.], 'lakeAreaKm2': 200.,
                                   'drainage': {'inflowComplete': False, 'unresolvedAreaKm2': 11.}},
                       'manifest': {'id': 'synthetic-transfer-input',
                                    'dataSHA256': 'synthetic-transfer-data',
                                    'transformationSHA256': 'synthetic-transfer-rule'},
                       'arrays': {'precipitation': rainfall[:, None].astype(float),
                                  'temperature': np.full((len(hours), 1), 4.),
                                  'evaporation': np.full((len(hours), 1), .08)}}
        self.request = {'inputId': 'synthetic-transfer-input', 'spatialVersion': 'synthetic-transfer-space',
                        'start': self.times[0].isoformat(), 'end': self.times[-1].isoformat(),
                        'parameters': {**DEFAULT_PARAMETERS, 'WM': 120., 'tauHours': 24., 'lagHours': 2.},
                        'partitionParameters': {}, 'routingMode': 'source-separated',
                        '_modelIdentity': 'synthetic-transfer-kernel',
                        '_observations': {'profile': 'synthetic', 'level': {}, 'flow': {}},
                        '_fixedInitialization': {'time': self.times[0].isoformat(), 'level': 4544.,
                                                 'method': 'synthetic', 'source': 'synthetic',
                                                 'reference': 'synthetic'}}

    def run_mode(self, mode, **request_changes):
        request = {**self.request, 'parameterTransfer': {'mode': mode}, **request_changes}
        return grid_simulation.run(None, request, prepared=self.source)

    def assert_checkpoints_equal(self, full, resumed):
        a, b = full['checkpoint'], resumed['checkpoint']
        np.testing.assert_allclose(a['unitStates'], b['unitStates'], rtol=1e-12, atol=1e-12)
        self.assertEqual(a['unitIds'], b['unitIds'])
        self.assertEqual(a['identity'], b['identity'])
        self.assertAlmostEqual(a['lake']['level'], b['lake']['level'], places=10)
        np.testing.assert_allclose(a['lake']['volumeChange'], b['lake']['volumeChange'], rtol=1e-12, atol=1e-6)
        np.testing.assert_allclose(a['continuousLakeVolume'], b['continuousLakeVolume'], rtol=1e-12, atol=1e-6)
        for partition in grid_simulation.PARTITIONS:
            left, right = a['routing'][partition], b['routing'][partition]
            np.testing.assert_allclose(left['channel']['storage'], right['channel']['storage'], rtol=1e-12, atol=1e-6)
            np.testing.assert_allclose(left['channel']['queue'], right['channel']['queue'], rtol=1e-12, atol=1e-6)
            for source in ('interflow', 'baseflow'):
                np.testing.assert_allclose(left['sources'][source], right['sources'][source], rtol=1e-12, atol=1e-6)

    def assert_water_balance(self, result):
        diagnostics = result['diagnostics']
        self.assertLessEqual(diagnostics['landGlacierMaxBalanceErrorMm'], 1e-6)
        self.assertLessEqual(diagnostics['routingMaxBalanceErrorM3'], 1e-4)
        for partition, amounts in diagnostics['partitions'].items():
            storage = result['checkpoint']['routing'][partition]
            stored = (storage['channel']['storage'] + sum(storage['channel']['queue']) +
                      sum(storage['sources'].values()))
            self.assertTrue(math.isclose(amounts['generatedM3'], amounts['routedM3'] + stored,
                                         rel_tol=1e-10, abs_tol=1e-4), partition)
        for row in result['daily']:
            if row['forcingHours'] != 24:
                continue
            self.assertTrue(math.isclose(row['lakeDeltaVolumeM3'],
                                         row['lakeInflowM3'] + row['lakePrecipitationM3'] - row['lakeEvaporationM3'],
                                         rel_tol=1e-9, abs_tol=1e-4))
            self.assertTrue(math.isclose(row['lakeInflowM3'], row['total'] * 86400., rel_tol=1e-9, abs_tol=1e-4))

    def test_a1_and_a2_advance_one_continuous_state_and_conserve_water(self):
        for mode in ('duja-shared', 'duja-area-routing'):
            with self.subTest(mode=mode):
                full = self.run_mode(mode)
                first = self.run_mode(mode, end=self.times[72].isoformat())
                resumed = self.run_mode(mode, start=self.times[73].isoformat(), _parentState=first['checkpoint'])
                self.assert_checkpoints_equal(full, resumed)
                self.assert_water_balance(full)

    def test_kernel_and_routing_receive_the_values_recorded_per_unit_and_partition(self):
        original_kernel = grid_simulation.run_region_kernel
        original_routing = grid_simulation.route_partition
        for mode in ('duja-shared', 'duja-area-routing'):
            kernel_calls, routing_calls = [], []

            def observe_kernel(rain, temperature, evaporation, glaciers, state, units, base, regions):
                kernel_calls.append([dict(regions.get(unit['partition'], base)) for unit in units])
                return original_kernel(rain, temperature, evaporation, glaciers, state, units, base, regions)

            def observe_routing(sources, old, params, routing_mode):
                partition = grid_simulation.PARTITIONS[len(routing_calls) % len(grid_simulation.PARTITIONS)]
                routing_calls.append((partition, dict(params)))
                return original_routing(sources, old, params, routing_mode)

            with self.subTest(mode=mode), patch.object(grid_simulation, 'run_region_kernel', side_effect=observe_kernel), \
                    patch.object(grid_simulation, 'route_partition', side_effect=observe_routing):
                result = self.run_mode(mode)
            audit = result['parameterTransferAudit']
            self.assertGreater(len(kernel_calls), 1)
            for call in kernel_calls:
                for unit, actual in zip(self.source['spatial']['units'], call):
                    self.assertEqual(set(actual), set(PARAMETER_NAMES))
                    self.assertEqual(actual, audit['effective']['regions'][unit['partition']]['parameters'])
            for partition, actual in routing_calls:
                expected = audit['effective']['regions'].get(partition, {}).get('parameters', audit['effective']['base'])
                self.assertEqual(actual, expected)
            for unit in audit['units']:
                row = audit['effective']['regions'][unit['partition']]
                self.assertEqual(unit['parameterGroup'], row['parameterGroup'])
                self.assertEqual(unit['id'], self.source['spatial']['units'][unit['index']]['id'])

    def test_a1_wm_change_reaches_all_land_units_but_not_glacier_process(self):
        first = self.run_mode('duja-shared')
        altered = self.run_mode('duja-shared', parameters={**self.request['parameters'], 'WM': 250.})
        for index, unit in enumerate(self.source['spatial']['units']):
            a, b = np.array(first['checkpoint']['unitStates'][index]), np.array(altered['checkpoint']['unitStates'][index])
            if unit['kind'] == 'land':
                self.assertGreater(float(np.max(np.abs(a - b))), 1e-6, unit['id'])
                self.assertNotEqual(first['diagnostics']['partitions'][unit['partition']]['generatedM3'],
                                    altered['diagnostics']['partitions'][unit['partition']]['generatedM3'])
            else:
                np.testing.assert_allclose(a, b, rtol=0, atol=1e-12)

    def test_a2_changes_only_selected_channel_routing_and_not_duja_flow(self):
        shared = self.run_mode('duja-shared')
        scaled = self.run_mode('duja-area-routing')
        np.testing.assert_allclose(shared['checkpoint']['unitStates'], scaled['checkpoint']['unitStates'],
                                   rtol=0, atol=1e-12)
        for partition in grid_simulation.PARTITIONS:
            self.assertEqual(shared['diagnostics']['partitions'][partition]['generatedM3'],
                             scaled['diagnostics']['partitions'][partition]['generatedM3'])
            self.assertEqual(shared['checkpoint']['routing'][partition]['sources'],
                             scaled['checkpoint']['routing'][partition]['sources'])
        shared_regions = shared['parameterTransferAudit']['effective']['regions']
        scaled_regions = scaled['parameterTransferAudit']['effective']['regions']
        for partition in shared_regions:
            for key in set(PARAMETER_NAMES) - {'tauHours', 'lagHours'}:
                self.assertEqual(shared_regions[partition]['parameters'][key],
                                 scaled_regions[partition]['parameters'][key], (partition, key))
        for partition in ('duja', 'fringe', 'external', 'unresolved'):
            self.assertEqual(shared['diagnostics']['partitions'][partition]['routedM3'],
                             scaled['diagnostics']['partitions'][partition]['routedM3'])
        for partition in ('zajia_rest', 'zagen', 'boqu', 'ali'):
            self.assertNotEqual(shared['diagnostics']['partitions'][partition]['routedM3'],
                                scaled['diagnostics']['partitions'][partition]['routedM3'])
        for a, b in zip(shared['daily'], scaled['daily']):
            if a['forcingHours'] == 24:
                self.assertEqual(a['duja'], b['duja'])
                self.assertEqual(a['fringe'], b['fringe'])

    def test_duja_enters_lake_once_and_unknown_outside_are_excluded(self):
        self.source['spatial']['units'] = [u for u in self.source['spatial']['units']
                                           if u['partition'] in ('duja', 'unresolved', 'external')]
        for mode in ('duja-shared', 'duja-area-routing'):
            result = self.run_mode(mode)
            self.assert_water_balance(result)
            for row in result['daily']:
                if row['forcingHours'] != 24:
                    continue
                self.assertGreater(row['duja'], 0.)
                self.assertGreater(row['excludedRunoff'], 0.)
                self.assertEqual(row['duja'], row['zajia'])
                self.assertEqual(row['duja'], row['total'])
                self.assertTrue(math.isclose(row['excludedRunoff'],
                                             row['externalRunoffM3'] + row['unresolvedRunoffM3'],
                                             rel_tol=1e-9, abs_tol=1e-6))
                self.assertTrue(math.isclose(row['lakeInflowM3'], row['duja'] * 86400.,
                                             rel_tol=1e-9, abs_tol=1e-4))

    def test_changed_transfer_policy_cannot_reuse_parent_state(self):
        first = self.run_mode('duja-shared', end=self.times[72].isoformat())
        with self.assertRaisesRegex(ValueError, '身份不一致'):
            self.run_mode('duja-area-routing', start=self.times[73].isoformat(),
                          _parentState=first['checkpoint'])

    def test_request_overrides_are_checked_and_recorded_before_normalization(self):
        for mode in ('duja-shared', 'duja-area-routing'):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, '先迁移到共享基准并清除覆盖'):
                self.run_mode(mode, partitionParameters={'duja': {}})
        result = self.run_mode('legacy', partitionParameters={'duja': {}})
        self.assertEqual(result['partitionParameters'], {})
        self.assertEqual(result['parameterTransferAudit']['raw']['partitionParameters'], {'duja': {}})

    def test_duja_override_masks_global_search_in_real_kernel_and_guards_reject_it(self):
        # Synthetic input only: reproduce the old scope error through the same
        # numerical kernel, without bypassing the new calibration guards.
        days = pd.date_range('2024-01-01', periods=7, freq='D')
        observation = {'profile': 'synthetic-scope-regression', 'level': {},
                       'flow': {day.isoformat(): value for day, value in
                                zip(days, (2., 2.5, 3., 2.2, 3.4, 1.5, 3.1))}}
        duja = {key: value for key, value in self.request['parameters'].items()
                if key != 'lakeCoefficient'}
        duja['WM'] = 150.
        candidates = (40., 65., 90., 120., 150., 190., 240., 300.)
        samples = []
        for wm in candidates:
            # Old global path: alter only the global candidate while keeping
            # the complete Duja override fixed. Control: alter Duja itself.
            masked = self.run_mode('legacy',
                parameters={**self.request['parameters'], 'WM': wm},
                partitionParameters={'duja': duja}, _observations=observation)
            control = self.run_mode('legacy',
                partitionParameters={'duja': {**duja, 'WM': wm}}, _observations=observation)

            def extract(result):
                frame = pd.DataFrame(result['daily']).set_index('date')
                frame = frame.loc[frame['forcingHours'].eq(24)]
                self.assertEqual(len(frame), 7)
                target = metrics(frame['observedFlow'], frame['duja'])
                objective, _ = flow_objective(target, 'balanced')
                return {'dujaFlow': frame['duja'].tolist(),
                        'zagenFlow': frame['zagen'].tolist(),
                        'objective': float(objective)}

            self.assertEqual(masked['parameterTransferAudit']['effective']['regions']['duja']['parameters']['WM'], 150.)
            self.assertEqual(masked['parameterTransferAudit']['effective']['regions']['zagen']['parameters']['WM'], wm)
            self.assertEqual(control['parameterTransferAudit']['effective']['regions']['duja']['parameters']['WM'], wm)
            samples.append({'WM': wm, 'globalWithDujaOverride': extract(masked),
                            'dujaControl': extract(control)})

        initial = samples[0]['globalWithDujaOverride']
        for item in samples[1:]:
            self.assertEqual(item['globalWithDujaOverride']['dujaFlow'], initial['dujaFlow'])
            self.assertEqual(item['globalWithDujaOverride']['objective'], initial['objective'])
        global_zagen_difference = max(abs(a - b) for item in samples[1:]
                                      for a, b in zip(item['globalWithDujaOverride']['zagenFlow'], initial['zagenFlow']))
        duja_flow_difference = max(abs(a - b) for item in samples[1:]
                                   for a, b in zip(item['dujaControl']['dujaFlow'], samples[0]['dujaControl']['dujaFlow']))
        duja_objectives = [item['dujaControl']['objective'] for item in samples]
        self.assertGreater(global_zagen_difference, 1e-6)
        self.assertGreater(duja_flow_difference, 1e-6)
        self.assertGreater(max(duja_objectives) - min(duja_objectives), 1e-6)

        old_request = {'stage': 'runoff', 'calibrationPartition': 'uniform',
                       'parameters': self.request['parameters'], 'partitionParameters': {'duja': duja}}
        with self.assertRaisesRegex(ValueError, '被杜加分区覆盖'):
            calibrate(None, old_request, Path('unused'))
        new_request = {'target': 'duja', 'periods': {}, 'search': {'scope': 'global', 'keys': ['WM']}}
        with self.assertRaisesRegex(ValueError, '杜加控制区'):
            grid_calibration.run(None, new_request, Path('unused'), lambda *_: None, lambda: False)
        self.scope_mask_summary = {'candidates': samples,
            'maskedDujaMaxDifferenceM3s': 0., 'maskedObjectiveRange': 0.,
            'globalZagenMaxDifferenceM3s': global_zagen_difference,
            'controlDujaMaxDifferenceM3s': duja_flow_difference,
            'controlObjectiveRange': max(duja_objectives) - min(duja_objectives),
            'legacyGuardRejected': True, 'newGuardRejected': True}


if __name__ == '__main__':
    unittest.main()
