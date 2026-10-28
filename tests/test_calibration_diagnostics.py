"""Tests for fixed-target diagnostics, parameter scope, and shared budgets."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend import calibration_diagnostics, grid_calibration
from backend.calibration import calibrate
from backend.calibration_config import FLOW_SELECTABLE_KEYS, FLOW_FORBIDDEN_KEYS, ROUTING_KEYS, SNOW_KEYS, PROFILES
from backend.common import digest, sha_file
from backend.model_core import DEFAULT_PARAMETERS, PARAMETER_DEFINITIONS


class FlowCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.dates = pd.date_range('2023-01-01', periods=40)
        self.periods = {'warmupStart':'2022-01-01T00:00:00', 'trainingStart':'2023-01-01T00:00:00',
                        'trainingEnd':'2024-01-01T00:00:00', 'evaluationStart':'2024-01-01T00:00:00',
                        'evaluationEnd':'2025-01-01T00:00:00'}
        self.observations = {'profile':'synthetic',
            'flow':{d.isoformat():float(8+i*.06) for i,d in enumerate(self.dates)},
            'level':{'2023-01-02T00:00:00':4544.,'2024-02-01T00:00:00':4545.}}
        self.request = {'target':'duja','inputId':'test','spatialVersion':'test','start':'2022-01-01T00:00:00',
            'end':'2025-01-01T00:00:00','parameters':deepcopy(DEFAULT_PARAMETERS),
            'partitionParameters':{'duja':{'WM':150.,'EX':1.2}},'periods':deepcopy(self.periods),
            'search':{'scope':'duja','keys':['WM','EX'],'bounds':{d['key']:[d['min'],d['max']] for d in PARAMETER_DEFINITIONS},
                      'budgets':{'runoff':8,'snow':5,'routing':5},'seed':42,'seeds':[42,142],
                      'objectiveProfile':'balanced'},'schemeSnapshot':{'configurationSHA256':'test'},
            '_observations':deepcopy(self.observations), '_fixedInitialization':None}

    def execute(self, request=None, fail_candidate=False, recorded=None):
        req = deepcopy(request or self.request)
        calls = []
        def simulated(db, payload, *args, **kwargs):
            calls.append(deepcopy(payload))
            p = {**payload['parameters'], **payload.get('partitionParameters', {}).get('duja', {})}
            is_candidate = bool(payload.get('_metricsOnly'))
            i = np.arange(40, dtype=float)
            q = 8.4 + i*.045 + (p['WM']-150)*.002*np.sin(i/6) + (p['tauHours']-24)*.002*np.cos(i/5) + (p['snowDdf']-4)*.02*np.sin(i/4)
            # EX is deliberately inactive in this synthetic controlled experiment.
            if fail_candidate and is_candidate and len(calls) > 1:
                q[2] = np.nan
            rows = [{'date':d.isoformat(), 'observedFlow':payload['_observations']['flow'].get(d.isoformat()),
                     'duja':float(q[j]), 'observedLevel':None,'lakeLevel':None} for j,d in enumerate(self.dates)]
            rows += [{'date':'2024-02-01T00:00:00','observedFlow':payload['_observations']['flow'].get('2024-02-01T00:00:00'),
                      'duja':10.,'observedLevel':payload['_observations']['level'].get('2024-02-01T00:00:00'), 'lakeLevel':4544.}]
            return {'daily':rows, 'parameters':deepcopy(payload['parameters']),
                    'partitionParameters':deepcopy(payload['partitionParameters']), 'initialization':None}
        with patch.object(grid_calibration,'prepare',return_value={}), \
             patch.object(grid_calibration,'simulate',side_effect=simulated), \
             patch.object(grid_calibration,'write_json') as writer:
            result=grid_calibration.run(None,req,Path('unused'),lambda *_:None,lambda:False)
            if recorded is not None:
                recorded.extend((call.args[0], deepcopy(call.args[1])) for call in writer.call_args_list)
        return result,calls

    def test_training_mask_observations_weights_and_optimizer_are_frozen_in_progress_and_result(self):
        recorded=[]
        result,_=self.execute(recorded=recorded)
        pairs=[{'date':date.isoformat(),'observed':self.observations['flow'][date.isoformat()]}
               for date in self.dates]
        expected={'fixedTrainingDates':[pair['date'] for pair in pairs],
                  'fixedTrainingObservationSHA256':digest(pairs),
                  'objectiveWeights':PROFILES['balanced']['weights'],
                  'calibrationImplementationSHA256':sha_file(calibration_diagnostics.__file__)}
        for key,value in expected.items():
            self.assertEqual(result['calibration'][key],value)
            for path,progress in recorded:
                if path.name=='search-progress.json':
                    self.assertEqual(progress[key],value)

    def test_actual_local_response_freezes_only_tested_no_response_and_counts_all_phases(self):
        result,calls=self.execute()
        stage=result['calibration']['stages'][0]
        diagnostic=stage['diagnostics']
        self.assertEqual(diagnostic['trainingPairs'],40)
        self.assertEqual(diagnostic['parameters']['WM']['status'],'responsive')
        self.assertEqual(diagnostic['parameters']['EX']['status'],'no-local-response')
        self.assertEqual(stage['fixedParameters'],['EX'])
        self.assertEqual(diagnostic['svd']['keys'],['WM'])
        self.assertEqual(diagnostic['svd']['rank'],1)
        self.assertEqual(stage['evaluations'],8)
        self.assertEqual([s['seed'] for s in stage['starts']],[42,142])
        self.assertEqual(sum(s['evaluations'] for s in stage['starts'])+3,8)
        self.assertEqual(len(calls),9)  # eight counted candidate evaluations and final reporting run
        self.assertFalse(stage['converged'])
        self.assertEqual(stage['termination'],'budget-exhausted')

    def test_small_budget_does_not_claim_unchecked_parameters_are_insensitive(self):
        req=deepcopy(self.request)
        req['search']['budgets']['runoff']=1
        result,calls=self.execute(req)
        stage=result['calibration']['stages'][0]
        self.assertEqual(stage['evaluations'],1)
        self.assertEqual(stage['fixedParameters'],[])
        self.assertEqual(stage['diagnostics']['parameters']['EX']['status'],'not-tested')
        self.assertEqual(stage['termination'],'budget-exhausted')
        self.assertEqual(len(calls),2)

    def test_held_out_observations_and_lake_level_do_not_rank_duja_candidates(self):
        first,calls=self.execute()
        changed=deepcopy(self.request)
        changed['_observations']['flow']['2024-02-01T00:00:00']=99999.
        changed['_observations']['level']['2024-02-01T00:00:00']=4999.
        second,_=self.execute(changed)
        self.assertEqual(first['calibration']['trace'],second['calibration']['trace'])
        self.assertEqual(first['parameters'],second['parameters'])
        self.assertEqual(first['partitionParameters'],second['partitionParameters'])
        self.assertNotEqual(first['calibration']['assessments']['validation']['lake']['rmse'],
                            second['calibration']['assessments']['validation']['lake']['rmse'])
        self.assertFalse(second['calibration']['targets']['lake']['usedForObjective'])
        self.assertTrue(all('2024-02-01T00:00:00' not in c['_observations']['level'] for c in calls[:-1]))

    def test_missing_fixed_candidate_day_is_an_error(self):
        with self.assertRaisesRegex(ValueError,'固定训练流量配对日期'):
            self.execute(fail_candidate=True)

    def test_shared_transfer_keeps_single_parameter_source(self):
        req=deepcopy(self.request)
        req['parameterTransfer']={'mode':'duja-shared'}
        req['partitionParameters']={}
        result,calls=self.execute(req)
        self.assertTrue(all(c['parameterTransfer']['mode']=='duja-shared' for c in calls))
        self.assertTrue(all(c['partitionParameters']=={} for c in calls))
        self.assertGreater(len({c['parameters']['WM'] for c in calls}),1)
        self.assertEqual(result['parameters'],calls[-1]['parameters'])

    def test_parameter_groups_and_blocked_parameters_are_enforced(self):
        self.assertEqual(len(FLOW_SELECTABLE_KEYS),22)
        self.assertEqual(len(SNOW_KEYS),9)
        self.assertIn('lagHours',ROUTING_KEYS)
        self.assertEqual(set(FLOW_FORBIDDEN_KEYS),{'iceSublimationFraction','iceSublimationMaxMmHour','lakeCoefficient'})
        for key in FLOW_FORBIDDEN_KEYS:
            req=deepcopy(self.request);req['search']['keys']=[key]
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'不可选'):
                self.execute(req)
        req=deepcopy(self.request);req['search']['scope']='global'
        with self.assertRaisesRegex(ValueError,'杜加控制区'):
            self.execute(req)

    def test_independent_stages_share_neither_budget_nor_frozen_other_groups(self):
        req=deepcopy(self.request)
        req['search']['keys']=['WM','snowDdf','tauHours']
        result,_=self.execute(req)
        self.assertEqual([s['stage'] for s in result['calibration']['stages']],['runoff','snow','routing'])
        self.assertEqual([s['evaluations'] for s in result['calibration']['stages']],[8,5,5])
        self.assertEqual(result['calibration']['evaluations'],18)

    def test_legacy_uniform_rejects_duja_override_of_searched_parameter(self):
        req={'stage':'runoff','calibrationPartition':'uniform','parameters':deepcopy(DEFAULT_PARAMETERS),
             'partitionParameters':{'duja':{'WM':200.}}}
        with self.assertRaisesRegex(ValueError,'被杜加分区覆盖'):
            calibrate(None,req,Path('unused'))


if __name__ == '__main__':
    unittest.main()
