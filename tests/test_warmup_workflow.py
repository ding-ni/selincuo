from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import uuid

import pandas as pd

from backend.warmup_workflow import run


class WarmupWorkflowTests(TestCase):
    def test_selected_parameters_alt_stores_and_resume(self):
        folder = Path('build') / ('预热流程核对-' + uuid.uuid4().hex[:10])
        source = {'times': pd.date_range('2019-01-01', '2022-05-01', freq='h')}
        request = {'start':'2019-01-01T00:00:00', 'end':'2022-05-01T00:00:00',
                   'inputId':'forcing', 'spatialVersion':'spatial', '_modelIdentity':'model',
                   '_observations':{'flow':{}, 'level':{}}, 'initialHydrology':{'soilFraction':.5}}
        final = {'parameters':{'WM':125.}, 'initialization':{'time':'2021-04-23T12:00:00'}}
        calls = []
        def simulate(_db, options, **_kwargs):
            self.assertEqual(options['parameters'], final['parameters'])
            calls.append((options['start'], options['end'], options['initialHydrology']['soilFraction']))
            return {'case':len(calls)}
        with patch('backend.warmup_workflow.simulate', side_effect=simulate), \
                patch('backend.warmup_workflow.snapshot', return_value={'areaWeighted':{'snowSolidMm':1.}}), \
                patch('backend.warmup_workflow.comparison', return_value={'passes':True}), \
                patch('backend.warmup_workflow.budget', return_value={'monthly':[], 'annual':[]}):
            first = run(None, request, source, final, folder, lambda *_:None, lambda:False)
            call_count = len(calls)
            second = run(None, request, source, final, folder, lambda *_:None, lambda:False)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), call_count)
        self.assertEqual(call_count, 15)  # three anchor snapshots per case, three alternate periods
        self.assertEqual(len(first['comparisons']), 3)
        self.assertEqual(len(first['anchorStates']), 4)
        self.assertTrue(first['allComparisonsPass'])
        self.assertTrue(first['recentComparisonPass'])
        self.assertTrue(first['initialConditionPass'])

    def test_earlier_nonconverged_reference_does_not_reject_adjacent_comparison(self):
        folder = Path('build') / ('预热相邻核对-' + uuid.uuid4().hex[:10])
        source = {'times': pd.date_range('2018-01-01', '2022-05-01', freq='h')}
        request = {'start':'2018-01-01T00:00:00', 'end':'2022-05-01T00:00:00',
                   'inputId':'forcing', 'spatialVersion':'spatial', '_modelIdentity':'model',
                   '_observations':{'flow':{}, 'level':{}}, 'initialHydrology':{'soilFraction':.5}}
        final = {'parameters':{'WM':125.}, 'initialization':{'time':'2021-04-23T12:00:00'}}
        def simulate(_db, options, **_kwargs):return {'start':options['start']}
        def compare(_final, alternate):return {'passes':not alternate['start'].startswith('2020')}
        with patch('backend.warmup_workflow.simulate', side_effect=simulate), \
                patch('backend.warmup_workflow.snapshot', return_value={'areaWeighted':{'snowSolidMm':1.}}), \
                patch('backend.warmup_workflow.comparison', side_effect=compare), \
                patch('backend.warmup_workflow.budget', return_value={'monthly':[], 'annual':[]}):
            result = run(None, request, source, final, folder, lambda *_:None, lambda:False)
        self.assertFalse(result['comparisons']['2020-01-01-0.5']['passes'])
        self.assertTrue(result['recentComparisonPass'])
        self.assertTrue(result['initialConditionPass'])
        self.assertTrue(result['allComparisonsPass'])
