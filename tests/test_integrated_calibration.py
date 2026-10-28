"""Joint calibration uses independent observations and one frozen trajectory."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend import integrated_calibration as joint
from backend import grid_calibration
from backend.model_core import DEFAULT_PARAMETERS, PARAMETER_DEFINITIONS
from backend.research_schemes import INTEGRATED_OBJECTIVE, resolved_initialization


class JointCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.periods = {'warmupStart': '2022-01-01T00:00:00', 'trainingStart': '2023-01-01T00:00:00',
                        'trainingEnd': '2024-01-01T00:00:00', 'evaluationStart': '2024-01-01T00:00:00',
                        'evaluationEnd': '2025-01-01T00:00:00'}
        self.dates = pd.date_range('2023-01-01', periods=90)
        bounds = {d['key']: [d['min'], d['max']] for d in PARAMETER_DEFINITIONS}
        self.request = {'inputId': 'input', 'spatialVersion': 'space', 'start': '2022-01-01T00:00:00',
                        'end': '2025-01-01T00:00:00', 'target': 'integrated', 'periods': self.periods,
                        'parameters': deepcopy(DEFAULT_PARAMETERS), 'partitionParameters': {'duja': {'WM': 200.}},
                        'search': {'scope': 'duja', 'keys': ['WM', 'lakeCoefficient'], 'bounds': bounds,
                                   'budgets': {'integrated': 3}, 'seed': 4, 'joint': deepcopy(INTEGRATED_OBJECTIVE)},
                        'schemeSnapshot': {'configurationSHA256': 'test'},
                        '_observations': {'profile': 'test', 'flow': {}, 'level': {}}}

    def frame(self, flow_count=40, lake_count=80):
        flow = np.arange(90, dtype=float) + 10
        level = 4540 + np.arange(90, dtype=float) / 100
        return pd.DataFrame({'date': self.dates, 'observedFlow': np.where(np.arange(90) < flow_count, flow, np.nan),
                             'duja': flow + 2, 'observedLevel': np.where(np.arange(90) < lake_count, level, np.nan),
                             'lakeLevel': level + .2}, index=self.dates)

    def test_independent_dates_fixed_scales_equal_weight_and_missing_candidate_day(self):
        frame = self.frame()
        masks = joint.target_masks(frame, self.periods, None, INTEGRATED_OBJECTIVE)
        self.assertEqual((masks['flow']['count'], masks['lake']['count']), (40, 80))
        self.assertEqual(masks['flow']['countByYear'], {'2023': 40})
        loss, found, components = joint.score(frame, masks, INTEGRATED_OBJECTIVE)
        self.assertAlmostEqual(loss, (components['flow'] + components['lake']) / 2)
        self.assertEqual((found['flow']['count'], found['lake']['count']), (40, 80))
        self.assertAlmostEqual(components['flow'], 2 / np.std(np.arange(40)))
        changed = frame.copy()
        changed.loc[self.dates[10], 'duja'] = np.nan
        with self.assertRaisesRegex(ValueError, '缺少固定评价日期'):
            joint.score(changed, masks, INTEGRATED_OBJECTIVE)

    def test_initialization_day_excluded_and_constant_observation_not_evaluable(self):
        frame = self.frame()
        frame['observedFlow'] = 1.
        masks = joint.target_masks(frame, self.periods, {'time': '2023-01-01T12:00:00'}, INTEGRATED_OBJECTIVE)
        self.assertFalse(masks['flow']['available'])
        self.assertIn('变化太小', masks['flow']['reason'])
        self.assertEqual(masks['lake']['count'], 79)
        self.assertNotIn('2023-01-01T00:00:00', masks['lake']['dates'])

    def execute(self, flow=True, lake=True, budget=3):
        request = deepcopy(self.request)
        request['search']['budgets']['integrated'] = budget
        if flow:
            request['_observations']['flow'] = {d.isoformat(): float(10 + i) for i, d in enumerate(self.dates[:40])}
        if lake:
            request['_observations']['level'] = {d.isoformat(): float(4540 + i / 100) for i, d in enumerate(self.dates[:80])}
        else:
            # This is deliberately outside training and may never initialize it.
            request['_observations']['level'] = {'2024-02-01T00:00:00': 4550.}
        calls = []

        def simulate(db, payload, *args, **kwargs):
            calls.append(deepcopy(payload))
            frame = self.frame(40 if flow else 0, 80 if lake else 0)
            if payload['_fixedInitialization'] is None:
                frame['lakeLevel'] = np.nan
            # Use candidate values so reported parameter changes are physical.
            frame['duja'] += (payload.get('partitionParameters', {}).get('duja', {}).get('WM', 200.) - 200.) / 100
            frame['lakeLevel'] += (payload['parameters']['lakeCoefficient'] - .8) / 10
            return {'daily': frame.to_dict('records'), 'parameters': deepcopy(payload['parameters']),
                    'partitionParameters': deepcopy(payload['partitionParameters']),
                    'initialization': deepcopy(payload['_fixedInitialization'])}

        with patch.object(joint, 'prepare', return_value={}), patch.object(joint, 'simulate', side_effect=simulate), \
             patch.object(joint, 'write_json'):
            result = joint.run(None, request, Path('unused'), lambda *a: None, lambda: False)
        return request, result, calls

    def test_no_training_lake_does_not_use_validation_anchor_or_change_lake_parameter(self):
        request, result, calls = self.execute(flow=True, lake=False)
        self.assertEqual(resolved_initialization(request, request['_observations'], 'calibration'), (None, None))
        self.assertTrue(all(c['_fixedInitialization'] is None for c in calls))
        self.assertTrue(all('2024-02-01T00:00:00' not in c['_observations']['level'] for c in calls[:-1]))
        self.assertIn('2024-02-01T00:00:00', calls[-1]['_observations']['level'])
        self.assertEqual(result['parameters']['lakeCoefficient'], request['parameters']['lakeCoefficient'])
        self.assertEqual(result['calibration']['stages'][0]['fixedParameters'], ['lakeCoefficient'])
        self.assertFalse(result['calibration']['targets']['lake']['available'])

    def test_no_flow_fixes_duja_parameters_but_lake_can_be_calibrated(self):
        request, result, calls = self.execute(flow=False, lake=True)
        self.assertEqual(result['partitionParameters'], request['partitionParameters'])
        self.assertEqual(result['calibration']['stages'][0]['keys'], ['lakeCoefficient'])
        self.assertEqual(result['calibration']['stages'][0]['fixedParameters'], ['WM'])
        self.assertEqual(len({c['_fixedInitialization']['time'] for c in calls}), 1)

    def test_no_targets_still_runs_final_trajectory_with_original_parameters(self):
        request, result, calls = self.execute(flow=False, lake=False)
        self.assertEqual(result['calibration']['termination'], 'no-evaluable-target')
        self.assertEqual(result['calibration']['evaluations'], 0)
        self.assertEqual(result['parameters'], request['parameters'])
        self.assertEqual(result['partitionParameters'], request['partitionParameters'])
        self.assertEqual(len(calls), 2)

    def test_joint_result_metrics_and_parameters_come_from_same_final_run(self):
        _, result, calls = self.execute()
        self.assertEqual(result['parameters'], calls[-1]['parameters'])
        self.assertEqual(result['partitionParameters'], calls[-1]['partitionParameters'])
        assessed = result['calibration']['assessments']['training']
        self.assertEqual(assessed['flow']['count'], 40)
        self.assertEqual(assessed['lake']['count'], 79)
        self.assertIn('firstToLastErrorChangeM', assessed['lake'])
        self.assertIn('observedPeak', assessed['flow'])
        self.assertTrue(result['calibration']['validationExcludedFromSearch'])

    def test_single_target_diagnostic_without_lake_does_not_block_flow_simulation(self):
        request = deepcopy(self.request)
        request.update(target='lake', _fixedInitialization=None)
        request['search'].update(scope='background', keys=['lakeCoefficient'])
        request['search']['budgets']['lake'] = 2
        calls = []
        def simulation(db, payload, *args, **kwargs):
            calls.append(deepcopy(payload))
            frame = self.frame(lake_count=0)
            frame['lakeLevel'] = np.nan
            return {'daily': frame.to_dict('records'), 'parameters': deepcopy(payload['parameters']),
                    'partitionParameters': deepcopy(payload['partitionParameters']), 'initialization': None}
        with patch.object(grid_calibration, 'prepare', return_value={}), \
             patch.object(grid_calibration, 'simulate', side_effect=simulation), \
             patch.object(grid_calibration, 'write_json'):
            result = grid_calibration.run(None, request, Path('unused'), lambda *a: None, lambda: False)
        self.assertEqual(result['calibration']['termination'], 'no-evaluable-target')
        self.assertEqual(result['parameters'], request['parameters'])
        self.assertIsNone(calls[-1]['_fixedInitialization'])
        self.assertEqual(len(calls), 2)

    def test_lake_seed_uses_frozen_training_dates_and_true_model_difference(self):
        baseline = self.frame()
        masks = joint.target_masks(baseline, self.periods, None, INTEGRATED_OBJECTIVE)
        probe = baseline.copy()
        baseline['lakeLevel'] = baseline['observedLevel'] + .5
        probe['lakeLevel'] = probe['observedLevel'] - .2
        seed = joint.lake_seed(baseline,probe,masks['lake'],.8,1.5,(.2,1.5))
        self.assertAlmostEqual(seed,1.3)
        validation = pd.Timestamp('2024-02-01')
        baseline.loc[validation] = {'date':validation,'observedFlow':0,'duja':0,
                                    'observedLevel':1000,'lakeLevel':9000}
        probe.loc[validation] = {'date':validation,'observedFlow':0,'duja':0,
                                 'observedLevel':9000,'lakeLevel':1000}
        self.assertAlmostEqual(joint.lake_seed(baseline,probe,masks['lake'],.8,1.5,(.2,1.5)),seed)
        self.assertEqual(joint.lake_seed(baseline,probe,masks['lake'],.8,1.5,(.2,1.1)),1.1)
        probe.loc[self.dates[5],'lakeLevel'] = np.nan
        with self.assertRaisesRegex(ValueError,'缺少固定率定日期'):
            joint.lake_seed(baseline,probe,masks['lake'],.8,1.5,(.2,1.5))

    def test_lake_probe_and_seed_count_towards_budget_and_leave_other_values_fixed(self):
        request, result, calls = self.execute(budget=3)
        stage = result['calibration']['stages'][0]
        self.assertEqual(stage['lakeSeed']['status'],'evaluated')
        self.assertEqual(stage['lakeSeed']['probeEvaluation'],2)
        self.assertEqual(stage['lakeSeed']['seedEvaluation'],3)
        self.assertEqual(result['calibration']['evaluations'],3)
        self.assertEqual(len(calls),4)  # baseline, probe, seed, one final run
        self.assertEqual(calls[0]['partitionParameters'],calls[1]['partitionParameters'])
        self.assertEqual(calls[0]['partitionParameters'],calls[2]['partitionParameters'])
        self.assertTrue(all(c['_fixedInitialization']==calls[0]['_fixedInitialization'] for c in calls))
        self.assertEqual(calls[0]['parameters']['lakeCoefficient'],request['parameters']['lakeCoefficient'])
        _, short, short_calls = self.execute(budget=2)
        self.assertEqual(short['calibration']['stages'][0]['lakeSeed']['status'],'budget-insufficient')
        self.assertEqual(short['calibration']['evaluations'],2)
        self.assertEqual(len(short_calls),3)


if __name__ == '__main__':
    unittest.main()
