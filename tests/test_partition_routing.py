# coding: utf-8
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from backend.model_diagnostics import partition_budgets, flow_timing_report
from backend.calibration import calibrate
from backend.calibration_config import ROUTING_KEYS
from backend.model_core import parameters


class PartitionRoutingTests(unittest.TestCase):
    def test_partition_remainder_and_initial_routing_storage_conserve_without_double_counting(self):
        times=pd.date_range('2024-01-31T01:00',periods=48,freq='h')
        units=[{'partition':'duja','areaKm2':1,'kind':'land'}, {'partition':'zajia_rest','areaKm2':2,'kind':'land'}]
        diag=np.zeros((48,2,9)); diag[:,:,0]=1.
        # All rainfall produces runoff. Initial channel storage drains 10m3/h.
        out={'duja':np.full(48,1010/3600), 'zajia':np.full(48,3030/3600)}
        routed={'duja':480-np.arange(1,49)*10, 'zajia_rest':960-np.arange(1,49)*20}
        source={'precipitation':np.ones((48,2)), 'evaporation':np.ones((48,2))*2}
        report=partition_budgets(times,source,np.arange(48),diag,np.zeros((48,2,8)),np.zeros((2,8)),out,routed,
                                 {'units':units},parameters(),{'duja':480,'zajia_rest':960})
        self.assertEqual(len(report),2)
        self.assertTrue(all(r['balanceMaxErrorM3']<1e-9 for r in report))
        self.assertAlmostEqual(sum(m['routedRunoffM3'] for r in report for m in r['monthly']),out['zajia'].sum()*3600)
        self.assertEqual([m['month'] for m in report[0]['monthly']],['2024-01','2024-02'])
        self.assertAlmostEqual(report[1]['monthly'][0]['precipitationMm'],24)

    def test_timing_uses_only_paired_complete_days_and_does_not_zero_fill(self):
        times=pd.date_range('2024-04-01T01:00',periods=49,freq='h')
        result=flow_timing_report(times,{'duja':np.ones(49)*8}, {'flow':{'2024-04-01':10,'2024-04-03':100}})
        m=result['monthly'][0]
        self.assertEqual(m['matchedDays'],1)
        self.assertAlmostEqual(m['pbias'],-20)
        self.assertEqual(m['observedVolumeM3'],864000)

    def test_routing_stage_freezes_generation_and_never_uses_validation_in_search(self):
        source={'times':pd.DatetimeIndex(['2023-01-01','2026-01-01'])}
        base=parameters({'KC':.73,'KI':.1,'WM':220})
        def run(future):
            def simulation(db,request,**kwargs):
                p=parameters(request['parameters'])
                self.assertEqual({k:v for k,v in p.items() if k not in ROUTING_KEYS}, {k:v for k,v in base.items() if k not in ROUTING_KEYS})
                loss=(p['baseflowTauHours']-1500)**2/1e7+(p['interflowTauHours']-100)**2/1e5
                return {'parameters':p,'metrics':{'2024':{'flow':{'count':100,'nse':1-loss,'logNse':1-loss,'pbias':0}}, '2025':{'flow':{'nse':future}}}}
            with tempfile.TemporaryDirectory() as tmp, patch('backend.calibration.prepare',return_value=source),patch('backend.calibration.run_simulation',side_effect=simulation):
                return calibrate(None,{'stage':'routing','inputId':'test','parameters':base,'maxEvaluations':40},Path(tmp))
        a,b=run(-10),run(10)
        self.assertEqual(a['calibration']['trace'],b['calibration']['trace'])
        self.assertEqual(a['parameters'],b['parameters'])
        self.assertEqual(a['calibration']['searchParameters'],ROUTING_KEYS)
        self.assertEqual(a['calibration']['evaluations'],40)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            calibrate(None,{'stage':'routing','routingMode':'lumped'},Path(tmp))

    def test_invalid_sensitivity_candidate_is_skipped_without_losing_other_experiments(self):
        from backend.sensitivity import analyze
        def simulation(db,request,**kwargs):
            p=parameters(request['parameters'])
            return {'parameters':p,'metrics':{'2024':{'flow':{'count':100,'nse':.8,'logNse':.8,'pbias':0},'lake':{'count':100,'rmse':.1}}}}
        with tempfile.TemporaryDirectory() as tmp,patch('backend.sensitivity.prepare',return_value={}),patch('backend.sensitivity.run_simulation',side_effect=simulation):
            a=analyze(None,{'inputId':'test','parameters':parameters({'interflowTauHours':200,'baseflowTauHours':200})},Path(tmp))
        self.assertEqual(len(a['sensitivity']['skipped']),2)
        self.assertGreater(len(a['sensitivity']['rows']),5)


if __name__=='__main__':unittest.main()
