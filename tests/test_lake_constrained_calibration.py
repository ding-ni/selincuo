from copy import deepcopy
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend.lake_calibration import freeze_window, joint_assess, joint_fit
from backend.lake_constrained_calibration import _candidate, choose_candidates, _eligible, _refine, rescore, run_selection
from backend.model_core import DEFAULT_PARAMETERS
from backend.two_stage_calibration import defaults, validate_options


class LakeConstrainedTests(unittest.TestCase):
    def make_lake_frame(self):
        dates = pd.date_range('2021-04-23', '2023-12-31', freq='D')
        days = (dates-dates[0]).days.to_numpy(dtype=float)
        base = 4543.66 + .3*np.sin(days*2*np.pi/365.25) + days*.0003
        slope = -days/1000.
        observed = base + (.82-1.)*slope
        frame = pd.DataFrame({'baseLevel': base, 'slope': slope, 'observedLevel': observed}, index=dates)
        observations = {t.isoformat(): float(v) for t, v in zip(dates, observed)}
        return frame, observations, {'time': '2021-04-23T12:00:00'}

    def test_four_component_fit_uses_frozen_dates_and_finds_known_coefficient(self):
        frame, obs, anchor = self.make_lake_frame()
        window = freeze_window(obs, '2021-04-23', '2023-01-01', anchor)
        fit = joint_fit(frame, 1., [.2, 1.5], window, anchor)
        self.assertAlmostEqual(fit['value'], .82, places=3)
        self.assertLess(fit['training']['rmse'], 1e-6)
        self.assertLess(fit['jointScore'], 1e-5)
        self.assertEqual(window['seasonYears'], [2021, 2022])
        self.assertEqual(fit['fixedTrainingObservationSHA256'], window['observationSHA256'])
        changed = frame.copy()
        changed.loc[changed.index >= '2023-01-01', 'observedLevel'] += 7.
        second = joint_fit(changed, 1., [.2, 1.5], window, anchor)
        self.assertEqual(fit['value'], second['value'])
        self.assertEqual(fit['jointScore'], second['jointScore'])
        changed.loc[window['dates'][5], 'baseLevel'] = np.nan
        with self.assertRaisesRegex(ValueError, '固定实测日期'):
            joint_assess(changed, .82, 1., window)

    def test_partial_year_is_excluded_from_peak_criteria(self):
        frame, obs, anchor = self.make_lake_frame()
        obs = {k: v for k, v in obs.items() if pd.Timestamp(k) < pd.Timestamp('2023-09-15')}
        window = freeze_window(obs, '2021-04-23', '2023-09-15', anchor)
        self.assertEqual(window['seasonYears'], [2021, 2022])
        score = joint_assess(frame, .82, 1., window)
        self.assertEqual(score['seasonYears'], [2021, 2022])

    def test_selected_parameters_and_flow_gate_are_enforced(self):
        initial = dict(DEFAULT_PARAMETERS)
        selected = ['WM', 'KC']
        bounds = {'WM': [40., 300.], 'KC': [.2, 1.5]}
        result = _candidate(initial, {'WM': 200., 'KC': .9, 'snowDdf': 9.}, selected, bounds)
        self.assertEqual(result['WM'], 200.)
        self.assertEqual(result['snowDdf'], initial['snowDdf'])
        self.assertIsNone(_candidate(initial, {'WM': 301., 'KC': .9}, selected, bounds))
        constraint = {'minNse': .7, 'maxAbsPbias': 5.}
        self.assertTrue(_eligible({'nse': .7, 'pbias': -5.}, constraint))
        self.assertFalse(_eligible({'nse': .6999, 'pbias': 0.}, constraint))
        self.assertFalse(_eligible({'nse': .8, 'pbias': 5.0001}, constraint))

    def test_selection_retains_best_flow_and_deterministic_diversity(self):
        rows = [{'candidateId': str(i).zfill(4), 'flowObjective': float(i),
                 'parameters': {'WM': 40.+2.*i, 'KC': .2+(i%10)*.1}}
                for i in range(100)]
        bounds = {'WM': [40., 300.], 'KC': [.2, 1.5]}
        chosen = choose_candidates(rows, ['WM', 'KC'], bounds, 80)
        self.assertEqual(len(chosen), 80)
        self.assertEqual([r['candidateId'] for r in chosen[:64]], [str(i).zfill(4) for i in range(64)])
        self.assertEqual(chosen, choose_candidates(rows, ['WM', 'KC'], bounds, 80))
        self.assertEqual(len(set(r['candidateId'] for r in chosen)), 80)

    def test_old_options_stay_flow_only_and_joint_budget_is_total(self):
        self.assertEqual(validate_options({'mode': 'both'})['selectionMode'], 'flow-only')
        options = defaults()
        options['selectionMode'] = 'flow-constrained-lake'
        options['jointBudget'] = 2001
        with self.assertRaisesRegex(ValueError, '2000'):
            validate_options(options)

    def test_rescore_restarts_with_new_flow_scores_and_keeps_unselected_fixed(self):
        import json
        folder = Path('build')/('候选复评核对-'+uuid.uuid4().hex[:10])
        folder.mkdir(parents=True)
        source = folder/'old-trace.jsonl'
        with source.open('x', encoding='utf-8') as output:
            for i, wm in enumerate((145., 150., 155., 150.)):
                output.write(json.dumps({'evaluation': i+1, 'parameters': {'WM': wm, 'snowDdf': 9.},
                                         'metrics': {'nse': -99.}})+'\n')
        class Evaluator:
            days = pd.date_range('2024-01-01', periods=40)
            observed = np.linspace(10., 80., 40)
            profile = 'balanced'
            calls = 0
            def mask(self, *_): return np.ones(40, dtype=bool)
            def daily(self, candidate):
                self.calls += 1
                assert candidate['snowDdf'] == DEFAULT_PARAMETERS['snowDdf']
                return self.observed*candidate['WM']/150.
        evaluator = Evaluator()
        options = defaults()
        request = {'parameters': dict(DEFAULT_PARAMETERS), 'twoStage': options,
                   'search': {'keys': ['WM'], 'bounds': {'WM': [40., 300.]}},
                   'periods': {'trainingStart':'2024-01-01', 'trainingEnd':'2025-01-01',
                               'evaluationStart':'2025-01-01', 'evaluationEnd':'2026-01-01'}}
        with self.assertRaises(InterruptedError):
            rescore(evaluator, request, source, folder, lambda *_: None, lambda: evaluator.calls >= 2)
        rows, eligible, _ = rescore(evaluator, request, source, folder, lambda *_: None, lambda: False)
        self.assertEqual(evaluator.calls, 3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(eligible), 3)
        self.assertTrue(all(row['flow']['nse'] > .7 for row in rows))
        self.assertTrue(all(row['flow']['nse'] != -99. for row in rows))

    def test_parallel_rescore_drains_stopped_batch_in_source_order_and_resumes(self):
        from collections import OrderedDict
        import json
        import threading
        folder = Path('build')/('并行复评核对-'+uuid.uuid4().hex[:10])
        folder.mkdir(parents=True)
        source = folder/'old-trace.jsonl'
        with source.open('x', encoding='utf-8') as output:
            for i in range(1, 25):
                # One repeated vector must be scored only once.
                wm = 100.+(i-2 if i == 7 else i-1)
                output.write(json.dumps({'evaluation':i, 'parameters':{'WM':wm}})+'\n')
        class Evaluator:
            days = pd.date_range('2024-01-01', periods=40)
            observed = np.linspace(10., 80., 40)
            profile = 'balanced'
            def __init__(self):
                self._sources_cache = OrderedDict()
                self.source_evaluations = self.routing_evaluations = 0
                self.shared = {'calls':[], 'caches':set(), 'lock':threading.Lock(),
                               'stop':threading.Event(), 'stopAfter':5}
            def sources(self, *_): pass
            def mask(self, *_): return np.ones(40, dtype=bool)
            def daily(self, candidate):
                with self.shared['lock']:
                    self.shared['calls'].append(candidate['WM'])
                    self.shared['caches'].add(id(self._sources_cache))
                    if self.shared['stopAfter'] and len(self.shared['calls']) >= self.shared['stopAfter']:
                        self.shared['stop'].set()
                return self.observed*candidate['WM']/110.
        evaluator = Evaluator()
        request = {'parameters':dict(DEFAULT_PARAMETERS), 'twoStage':defaults(),
                   'search':{'keys':['WM'], 'bounds':{'WM':[40., 300.]}},
                   'periods':{'trainingStart':'2024-01-01', 'trainingEnd':'2025-01-01',
                              'evaluationStart':'2025-01-01', 'evaluationEnd':'2026-01-01'}}
        with patch('backend.lake_constrained_calibration.DujaEvaluator', Evaluator):
            with self.assertRaises(InterruptedError):
                rescore(evaluator, request, source, folder, lambda *_:None,
                        evaluator.shared['stop'].is_set, workers=3, block_size=4)
            with (folder/'duja-rescore.jsonl').open(encoding='utf-8') as stream:
                stopped = [json.loads(line) for line in stream]
            self.assertEqual(len(stopped), 12)
            self.assertEqual([row['sourceEvaluation'] for row in stopped],
                             [1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13])
            self.assertEqual(len(evaluator.shared['calls']), 12)
            self.assertEqual(len(evaluator.shared['caches']), 3)
            evaluator.shared['stop'].clear()
            evaluator.shared['stopAfter'] = None
            resumed, _, _ = rescore(evaluator, request, source, folder, lambda *_:None,
                                    evaluator.shared['stop'].is_set, workers=3, block_size=4)
            serial_folder = folder/'serial'
            serial_folder.mkdir()
            serial, _, _ = rescore(Evaluator(), request, source, serial_folder, lambda *_:None,
                                    lambda:False, workers=1, block_size=4)
        self.assertEqual(len(evaluator.shared['calls']), 23)
        self.assertEqual(resumed, serial)
        self.assertEqual([row['sourceEvaluation'] for row in resumed],
                         [i for i in range(1, 25) if i != 7])

    def test_joint_refinement_resumes_population_and_total_budget(self):
        folder = Path('build')/('联合接续核对-'+uuid.uuid4().hex[:10])
        folder.mkdir(parents=True)
        options = defaults()
        options['jointBudget'] = 18
        request = {'parameters': dict(DEFAULT_PARAMETERS), 'twoStage': options,
                   'search': {'keys': ['WM'], 'bounds': {'WM': [40., 300.]}}}
        def sample(wm):
            from backend.common import digest
            values = {'WM': float(wm)}
            return {'candidateId': digest(values), 'parameters': values,
                    'trainingLake': {'jointScore': abs(wm-160.)/100.+2.,
                                     'training': {'scoreComponents': {'rmse': 2., 'trend': 2.,
                                                                      'amplitude': 2., 'peakTiming': 2.}}}}
        starting = [sample(v) for v in (80., 120., 180., 240.)]
        def candidate(record, _request, _basin, _window, _folder, cached, _cancelled):
            row = sample(record['parameters']['WM'])
            cached[row['candidateId']] = row
            return row
        def flow(*_): return .1, {'nse': .8, 'pbias': 0.}
        cached = {row['candidateId']: deepcopy(row) for row in starting}
        def stopped(percent, _):
            if percent >= 72.: raise InterruptedError('requested stop')
        with patch('backend.lake_constrained_calibration._flow', side_effect=flow), \
                patch('backend.lake_constrained_calibration._evaluate_candidate', side_effect=candidate):
            with self.assertRaises(InterruptedError):
                _refine(None, None, request, None, None, folder, starting, cached, stopped, lambda: False)
            resumed = _refine(None, None, request, None, None, folder, starting, cached, lambda *_: None, lambda: False)
            fresh_folder = folder/'fresh'
            fresh_folder.mkdir()
            fresh_cache = {row['candidateId']: deepcopy(row) for row in starting}
            fresh = _refine(None, None, request, None, None, fresh_folder, starting, fresh_cache, lambda *_: None, lambda: False)
        self.assertEqual(resumed['evaluations'], 18)
        self.assertEqual(resumed['starts'], fresh['starts'])

    def test_candidate_workflow_records_separate_training_and_all_data_fits(self):
        import json
        from types import SimpleNamespace
        from backend.common import sha_file
        folder = Path('build')/('湖位流程核对-'+uuid.uuid4().hex[:10])
        task_folder = folder/'tasks'/'historic'/'all-data'
        task_folder.mkdir(parents=True)
        trace = task_folder/'search-trace.jsonl'
        with trace.open('x', encoding='utf-8') as output:
            for index, wm in enumerate((145., 150., 155.)):
                output.write(json.dumps({'evaluation': index+1, 'parameters': {'WM': wm}})+'\n')
        dates = pd.date_range('2021-01-01', '2026-09-13')
        day = (dates-dates[0]).days.to_numpy(dtype=float)
        slope = -day/1000.
        level = 4543.66 + .25*np.sin(day*2*np.pi/365.25) + day*.0002
        observation = {t.isoformat(): float(v) for t, v in zip(dates, level+(.9-1.)*slope)}
        flow_days = pd.date_range('2024-01-01', '2025-12-31')
        flow_values = 70.+40.*np.sin(np.arange(len(flow_days))*2*np.pi/365.25)
        options = defaults()
        options.update(selectionMode='flow-constrained-lake', candidateSourceTaskId='historic',
                       candidateLimit=3, jointBudget=0)
        request = {'twoStage': options, 'parameters': dict(DEFAULT_PARAMETERS),
                   'search': {'keys': ['WM'], 'bounds': {'WM': [40., 300.], 'lakeCoefficient': [.2, 1.5]}},
                   'periods': {'trainingStart':'2024-01-01', 'trainingEnd':'2025-01-01',
                               'evaluationStart':'2025-01-01', 'evaluationEnd':'2026-01-01'},
                   'start':'2020-01-01T00:00:00', 'end':'2026-09-14T00:00:00',
                   'inputId':'synthetic', 'spatialVersion':'synthetic', '_modelIdentity':'synthetic',
                   '_candidateSourceTraceSHA256':sha_file(trace),
                   '_observations':{'level':observation, 'flow':{}},
                   '_fixedInitialization':{'time':'2021-04-23T12:00:00', 'level':4543.66}}
        class FakeDuja:
            def __init__(self, *_args, **_kwargs):
                self.days, self.observed, self.profile = flow_days, flow_values, 'balanced'
            def mask(self, start, end):
                return np.asarray((self.days>=pd.Timestamp(start)) & (self.days<pd.Timestamp(end)))
            def daily(self, candidate): return self.observed*candidate['WM']/150.
        class FakeBasin:
            def __init__(self, _source, current):
                self.initial, self.lake_area = current['_fixedInitialization'], 2400e6
            def daily(self, candidate, _cancelled):
                shifted = level + (candidate['WM']-150.)*.001
                values = {'duja':np.full(len(dates),10.), 'zajia':np.full(len(dates),20.),
                          'zagen':np.full(len(dates),12.), 'boqu':np.full(len(dates),3.),
                          'ali':np.full(len(dates),5.), 'fringe':np.full(len(dates),2.),
                          'total':np.full(len(dates),42.), 'lakeLevel':shifted, 'baseLevel':shifted,
                          'slope':slope, 'lakeRainMm':np.ones(len(dates)), 'lakeEvaporationMm':np.ones(len(dates)),
                          'lakeDeltaVolumeM3':np.ones(len(dates)), 'lakeInflowM3':np.ones(len(dates)),
                          'lakePrecipitationM3':np.ones(len(dates)), 'lakeEvaporationM3':np.ones(len(dates)),
                          'observedLevel':[observation[t.isoformat()] for t in dates]}
                return pd.DataFrame(values, index=dates)
        database = SimpleNamespace(workspace=folder, task=lambda _id:{'kind':'two-stage-calibration',
                                  'status':'succeeded','result':{'calibrationWorkflow':{'flowFits':[]}}})
        output_folder = folder/'new-result'
        with patch('backend.lake_constrained_calibration.DujaEvaluator', FakeDuja), \
                patch('backend.lake_constrained_calibration.BasinEvaluator', FakeBasin), \
                patch('backend.lake_constrained_calibration._fast_parity', return_value={'synthetic':True}):
            first = run_selection(database, request, {}, output_folder, lambda *_:None, lambda:False)
            second = run_selection(database, request, {}, output_folder, lambda *_:None, lambda:False)
        self.assertEqual(first, second)
        self.assertEqual(first['totalRescored'], 3)
        self.assertEqual(first['basinCandidatesEvaluated'], 3)
        self.assertFalse(first['trainingUsesValidationLake'])
        self.assertTrue(first['finalUsesAllLakeObservations'])
        self.assertTrue(first['flowParameterSelectionUsesLakeLevel'])
        self.assertEqual(first['parameters']['KC'], DEFAULT_PARAMETERS['KC'])
        self.assertFalse(first['adoptionComparison']['eligible'])
        split_request = deepcopy(request)
        split_request['twoStage']['finalRefit'] = False
        split_folder = folder/'time-check-only'
        with patch('backend.lake_constrained_calibration.DujaEvaluator', FakeDuja), \
                patch('backend.lake_constrained_calibration.BasinEvaluator', FakeBasin), \
                patch('backend.lake_constrained_calibration._fast_parity', return_value={'synthetic':True}):
            split = run_selection(database, split_request, {}, split_folder, lambda *_:None, lambda:False)
        self.assertFalse(split['finalUsesAllLakeObservations'])
        self.assertFalse(split['adoptionComparison']['eligible'])
        self.assertFalse((split_folder/'all-lake-fits.jsonl').exists())


class FastBasinParityTests(unittest.TestCase):
    def test_fast_daily_flow_and_lake_match_complete_model(self):
        from backend.fast_basin_calibration import BasinEvaluator
        from backend.grid_simulation import run
        count = 24*550 + 1
        times = pd.date_range('2020-01-01', periods=count, freq='h')
        phase = np.arange(count)
        arrays = {'precipitation': np.column_stack([np.where(phase%41 < 3, .7, 0.),
                                                    np.where(phase%31 < 2, 1.1, 0.)]),
                  'temperature': np.column_stack([3.+8.*np.sin(phase/1400), -1.+6.*np.sin(phase/1400)]),
                  'evaporation': np.full((count, 2), .08)}
        units = [{'id': part, 'partition': part, 'kind': 'glacier' if part == 'zagen' else 'land',
                  'areaKm2': 50.+i*7, 'elevation': 4900.+i*70, 'gridIndex': i%2}
                 for i, part in enumerate(('duja', 'zajia_rest', 'zagen', 'boqu', 'ali', 'fringe'))]
        spatial = {'version': 'synthetic', 'units': units,
                   'grids': [{'referenceElevation': 4500.}, {'referenceElevation': 4700.}],
                   'lakeWeights': [0.5, 0.5], 'lakeAreaKm2': 2400., 'forcingGridVersion': 'grid',
                   'drainage': {}}
        source = {'spatial': spatial, 'arrays': arrays, 'times': times,
                  'manifest': {'id': 'synthetic-input', 'dataSHA256': 'test'}}
        observation = {'profile': 'synthetic', 'flow': {}, 'level': {}}
        anchor = {'time': '2021-04-23T12:00:00', 'level': 4543.66, 'method': 'test'}
        request = {'start': times[0].isoformat(), 'end': times[-1].isoformat(),
                   'inputId': 'synthetic-input', 'spatialVersion': 'synthetic',
                   'parameters': dict(DEFAULT_PARAMETERS), 'partitionParameters': {},
                   'parameterTransfer': {'mode': 'duja-shared'}, 'routingMode': 'source-separated',
                   '_observations': observation, '_fixedInitialization': anchor,
                   '_modelIdentity': 'synthetic-implementation', '_metricsOnly': True}
        for fraction in (.1, .5, .9):
            request['initialHydrology'] = {'soilFraction': fraction}
            fast = BasinEvaluator(source, request).daily(request['parameters'])
            with patch('backend.grid_simulation.prefix_identity', return_value='synthetic'), \
                    patch('backend.grid_spatial.topology_complete', return_value=True):
                full = run(None, request, prepared=source)
            reference = pd.DataFrame(full['daily'])
            reference.index = pd.to_datetime(reference.date)
            for name in ('duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total', 'lakeLevel'):
                np.testing.assert_allclose(reference[name].reindex(fast.index), fast[name],
                                           rtol=0., atol=1e-7, equal_nan=True, err_msg=name)


if __name__ == '__main__':
    unittest.main()
