from copy import deepcopy
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend.calibration_search import run_fit
from backend.common import digest
from backend.fast_calibration import DujaEvaluator, array_metrics
from backend.grid_simulation import unit_forcing
from backend.lake_calibration import fit as fit_lake
from backend.model_core import DEFAULT_PARAMETERS, PARAMETER_DEFINITIONS
from backend.parameter_regions import run_region_kernel, region_initial_states
from backend.routing import route_partition
from backend.two_stage_calibration import defaults, validate_options


class TwoStageTests(unittest.TestCase):
    def test_fast_kernel_matches_full_unit_physics_and_routing(self):
        count=2401
        times=pd.date_range('2024-01-01',periods=count,freq='h')
        phase=np.arange(count)
        arrays={'precipitation':np.column_stack([np.where(phase%24<3,1.,0.),np.where(phase%30<4,.7,0.)]),
                'temperature':np.column_stack([5+10*np.sin(phase/240),2+8*np.sin(phase/240)]),
                'evaporation':np.full((count,2),.08)}
        units=[{'id':'land','partition':'duja','kind':'land','areaKm2':200.,'elevation':4800.,'gridIndex':0},
               {'id':'ice','partition':'duja','kind':'glacier','areaKm2':20.,'elevation':5200.,'gridIndex':1}]
        spatial={'units':units,'grids':[{'referenceElevation':4500.},{'referenceElevation':4700.}]}
        request={'start':times[0].isoformat(),'end':times[-1].isoformat(),'parameters':dict(DEFAULT_PARAMETERS),
                 '_observations':{'flow':{}},'search':{'objectiveProfile':'balanced'}}
        for aggregate in (False,True):
            current=deepcopy(spatial)
            if aggregate:
                current.update(unitStrategy='partition-elevation-surface-area-weighted-v1',aggregation=[
                    {'unitId':u['id'],'members':[{'gridIndex':u['gridIndex'],'areaWeight':.6,'elevationOffsetM':300.},
                                               {'gridIndex':1-u['gridIndex'],'areaWeight':.4,'elevationOffsetM':500.}]}
                    for u in units])
            evaluator=DujaEvaluator({'spatial':current,'arrays':arrays,'times':times},request)
            for p in (dict(DEFAULT_PARAMETERS),{**DEFAULT_PARAMETERS,'WM':170.,'lapseRate':-.005,'lagHours':2.5}):
                r,t,e=unit_forcing(current,arrays,np.arange(count),p,{})
                diagnostic,history,state=run_region_kernel(r,t,e,np.array([False,True]),
                    region_initial_states(units,p,{}),units,p,{})
                volumes=(diagnostic[:,:,:4]*np.array([200.,20.])[None,:,None]*1000).sum(axis=1)
                expected=route_partition(volumes,{},p,'source-separated')[0]
                np.testing.assert_allclose(evaluator.hourly(p),expected,rtol=1e-10,atol=1e-9)
            before=evaluator.source_evaluations
            evaluator.hourly({**p,'tauHours':40.})
            self.assertEqual(evaluator.source_evaluations,before)

    def test_lake_fit_recovers_conditional_coefficient_without_changing_flow(self):
        dates=pd.date_range('2021-04-23',periods=200)
        slope=-np.linspace(0,1.2,len(dates))
        base=4543.66+np.linspace(0,.8,len(dates))
        frame=pd.DataFrame({'baseLevel':base,'slope':slope,'observedLevel':base+(.83-1.)*slope},index=dates)
        result=fit_lake(frame,1.,[.2,1.5],dates[0].isoformat(),dates[-1].isoformat(),{'time':dates[0].isoformat()})
        self.assertAlmostEqual(result['value'],.83,places=10)
        self.assertLess(result['training']['rmse'],1e-10)
        limited=fit_lake(frame,1.,[1.,1.5],dates[0].isoformat(),dates[-1].isoformat(),{'time':dates[0].isoformat()})
        self.assertEqual(limited['value'],1.)
        self.assertTrue(limited['boundary'])

    def test_optimizer_stop_resume_preserves_mask_population_and_budget(self):
        folder=Path('build')/('两阶段搜索核对-'+uuid.uuid4().hex[:10])
        class Evaluator:
            profile='balanced'
            calls=0
            def mask(self,a,b):return np.array([True]*40)
            def evidence(self,mask):return {'trainingPairs':40,'fixedTrainingDates':['training']*40,'fixedTrainingObservationSHA256':'fixed'}
            def objective(self,p,mask):
                self.calls+=1
                value=((p['WM']-190)/100)**2+.1
                return value,{'count':40,'nse':1-value}
            def assess(self,p,a,b):return {'count':40,'nse':1-((p['WM']-190)/100)**2-.1}
        evaluator=Evaluator()
        options=defaults();options.update(budgets={'runoff':30,'snow':1,'routing':1,'joint':30},allowExtension=False)
        request={'twoStage':options,'search':{'keys':['WM'],'bounds':{'WM':[40.,300.]}},
                 'schemeSnapshot':{'configurationSHA256':'test'},'_modelIdentity':'test','inputId':'synthetic'}
        args=(evaluator,request,folder,'synthetic','2024','2025','2025','2026',dict(DEFAULT_PARAMETERS),lambda *_:None)
        with self.assertRaises(InterruptedError):
            run_fit(*args,lambda:evaluator.calls>=8)
        first_calls=evaluator.calls
        result=run_fit(*args,lambda:False)
        self.assertGreater(result['evaluations'],first_calls)
        self.assertLessEqual(result['evaluations'],60)
        self.assertEqual(result['fixedTrainingObservationSHA256'],'fixed')
        self.assertEqual(result['seeds'],[42,142,242])
        self.assertTrue(result['validationExcludedFromSearch'])
        repeated=run_fit(*args,lambda:False)
        self.assertEqual(repeated,result)
        # Jobs can raise from the progress callback after the objective has
        # already completed. This stop must not lose the population update.
        interrupted_folder=folder/'progress-stop'
        evaluator.calls=0
        def stop_after_objective(*_):
            if evaluator.calls==8:raise InterruptedError('stopped from progress')
        interrupted_args=(*args[:2],interrupted_folder,*args[3:-1],stop_after_objective)
        with self.assertRaises(InterruptedError):run_fit(*interrupted_args,lambda:False)
        recovered=run_fit(*interrupted_args[:-1],lambda *_:None,lambda:False)
        from backend.common import read_json
        state=read_json(interrupted_folder/'optimizer-state.json')
        self.assertEqual(state['evaluations'],sum(s['used'] for s in state['records']))
        self.assertEqual(recovered['parameters'],result['parameters'])
        self.assertEqual(recovered['evaluations'],result['evaluations'])
        self.assertTrue(list(interrupted_folder.glob('population-*.json')))

    def test_options_keep_flow_and_lake_stages_separate(self):
        config=validate_options(defaults())
        self.assertEqual(sum(config['budgets'].values()),18000)
        self.assertEqual(config['seeds'],[42,142,242])
        bad=defaults();bad['lakePeriods']['trainingEnd']='2026-01-01T00:00:00'
        with self.assertRaises(ValueError):validate_options(bad)

    def test_lake_only_prepare_reuses_matching_fixed_hydrology(self):
        from backend.two_stage_calibration import prepare
        from types import SimpleNamespace
        options=defaults();options['mode']='lake'
        value={'schemeSnapshot':{'twoStage':options},'parameters':dict(DEFAULT_PARAMETERS),
            'partitionParameters':{},'parameterTransfer':{'mode':'duja-shared'},
            'search':{'keys':[]},'start':'2020-01-01T00:00:00','end':'2026-01-01T00:00:00',
            'inputId':'forcing','spatialVersion':'spatial','routingMode':'source-separated'}
        base={**deepcopy(value),'end':'2026-10-21T00:00:00',
              'parameterSources':{'hydrology':{'method':'Duja-flow-all-data-refit'}}}
        db=SimpleNamespace(items=lambda _: [{'id':'forcing','manifest':{'end':'2026-10-21T07:00:00'}}],
            setting=lambda *_: {'resultId':'approved'},task=lambda _: {'id':'approved','status':'succeeded','result':base})
        with patch('backend.research_schemes.prepare_request',side_effect=lambda *_a,**_k:deepcopy(value)):
            prepared=prepare(db,{'researchSchemeId':'scheme'})
            self.assertEqual(prepared['lakeOnlyBaseTaskId'],'approved')
            self.assertEqual(prepared['_fixedHydrologySource']['sourceTaskId'],'approved')
            base['parameters']['WM']+=1
            self.assertNotIn('lakeOnlyBaseTaskId',prepare(db,{'researchSchemeId':'scheme'}))

    def test_all_data_reports_preserve_original_split_scores(self):
        from backend.result_evaluation import report
        fit={'fit':'2024→2025','training':{'count':229,'nse':.7},'validation':{'count':211,'nse':.3},
             'trainingPeriod':{'start':'2024-01-01','end':'2025-01-01'},
             'validationPeriod':{'start':'2025-01-01','end':'2026-01-01'}}
        result=report({'request':{'periods':{'trainingStart':'2024-01-01'}},
            'result':{'modelFamily':'grid-continuous-v1','daily':[{'date':'2024-01-01'}],
                      'calibrationWorkflow':{'flowFits':[fit],'lake':None,'finalRefit':True}}})
        self.assertTrue(result['evaluationUsedForSearch'])
        self.assertEqual(result['dataUse'],'split-checks-then-all-data-refit')
        self.assertEqual(result['periods']['flow-0-validation']['flow']['nse'],.3)

    def test_saved_draft_does_not_hide_the_actual_adopted_revision(self):
        from backend.research_schemes import list_schemes
        from types import SimpleNamespace
        rows=[{'id':'adopted','name':'采用','payload':{'schemaVersion':2,'familyId':'family','revision':1}},
              {'id':'draft','name':'草稿','payload':{'schemaVersion':2,'familyId':'family','revision':2}}]
        db=SimpleNamespace(items=lambda _:deepcopy(rows),setting=lambda key,default=None:
            {'schemeId':'adopted','familyId':'family'} if key=='business-adopted-scheme-v1' else default)
        with patch('backend.research_schemes.builtin',return_value=None):
            self.assertEqual([r['id'] for r in list_schemes(db)],['adopted','draft'])

    @unittest.skipUnless(__import__('os').name=='nt','Windows file sharing behavior')
    def test_windows_reader_conflict_retries_atomic_progress_write(self):
        from backend import common
        folder=Path('build')/('两阶段进度写入核对-'+uuid.uuid4().hex[:10])
        path=folder/'search-progress.json'
        common.write_json(path,{'evaluations':1})
        replace=common.os.replace
        attempts=[]
        def occupied_then_replace(source,destination):
            attempts.append(source)
            if len(attempts)<3:
                error=PermissionError('reader sharing conflict');error.winerror=32
                raise error
            return replace(source,destination)
        with patch('backend.common.os.replace',side_effect=occupied_then_replace),patch('backend.common.time.sleep'):
            common.write_json(path,{'evaluations':2})
        self.assertEqual(len(attempts),3)
        self.assertEqual(common.read_json(path),{'evaluations':2})


if __name__=='__main__':unittest.main()
