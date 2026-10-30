from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import uuid

import numpy as np

from backend.calibration_objective import validate_objective, combined_score
from backend.calibration_config import flow_objective
from backend.common import digest, write_json
from backend.model_core import DEFAULT_PARAMETERS
from backend.two_stage_calibration import validate_options
from backend.lake_constrained_calibration import _refine


class ContinuousDualTests(unittest.TestCase):
    def test_initial_candidate_stays_inside_the_unchanged_basin_budget(self):
        from backend.dual_calibration import select_candidates
        initial={'WM':120.}
        rows=[{'parameters':{'WM':float(i)},'candidateId':digest({'WM':float(i)}),
               'flowObjective':float(i)} for i in range(60,131)]
        chosen=select_candidates(rows,['WM'],{'WM':[40.,300.]},initial,8)
        self.assertEqual(len(chosen),8)
        self.assertIn(digest(initial),[r['candidateId'] for r in chosen])
        self.assertEqual(chosen[0]['parameters'],{'WM':60.})

    def test_continuous_options_do_not_reintroduce_hard_gates(self):
        options=validate_options({'selectionMode':'continuous-dual','flowConstraints':{'minNse':.7,'maxAbsPbias':5}})
        self.assertIsNone(options['flowConstraints'])
        self.assertIsNone(options['candidateSourceTaskId'])
        self.assertFalse(options['reverseCheck'])
        self.assertEqual(options['objective']['weights'],{'flow':.5,'lake':.5})
        old=validate_options({'selectionMode':'flow-constrained-lake'})
        self.assertEqual(old['flowConstraints']['minNse'],.7)

    def test_custom_objective_is_numeric_and_validated(self):
        objective=validate_objective()
        self.assertAlmostEqual(combined_score(.2,1.4,objective),.8)
        objective['weights']={'flow':.8,'lake':.2}
        self.assertAlmostEqual(combined_score(.2,1.4,objective),.44)
        metric={'count':50,'nse':.4,'logNse':.2,'pbias':12.}
        self.assertNotEqual(flow_objective(metric,weights={'nse':1.,'logNse':0.,'volume':0.}),
                            flow_objective(metric,weights={'nse':0.,'logNse':0.,'volume':1.}))
        with self.assertRaises(ValueError):validate_objective({**objective,'weights':{'flow':1.,'lake':1.}})

    def test_joint_search_evaluates_low_nse_and_continues_below_lake_target(self):
        folder=Path('build')/('连续双目标核验-'+uuid.uuid4().hex[:10])
        folder.mkdir(parents=True)
        options=validate_options({'selectionMode':'continuous-dual','jointBudget':6,'seeds':[42]})
        request={'twoStage':options,'parameters':dict(DEFAULT_PARAMETERS),
                 'search':{'keys':['WM'],'bounds':{'WM':[40.,300.]}}}
        records=[]
        for i,x in enumerate((80.,120.,180.,240.)):
            records.append({'candidateId':str(i),'parameters':{'WM':x},'flowObjective':2.+i,
                'trainingLake':{'jointScore':.2+i*.1,'training':{'scoreComponents':{'rmse':.2}}}})
        cached={r['candidateId']:r for r in records}
        seen=[]
        def evaluate(row,*_):
            seen.append(row)
            record={**row,'trainingLake':{'jointScore':.1+row['parameters']['WM']/1000}}
            cached[row['candidateId']]=record
            return record
        with patch('backend.lake_constrained_calibration._flow',return_value=(2.,{'nse':-2.,'pbias':60.})),\
             patch('backend.lake_constrained_calibration._evaluate_candidate',side_effect=evaluate):
            report=_refine(None,None,request,None,None,folder,records,cached,lambda *_:None,lambda:False)
        self.assertEqual(report['evaluations'],6)
        self.assertEqual(len(seen),6)
        self.assertNotEqual(report['termination'],'training-target-reached')

    def test_bound_configuration_reuses_snapshot_and_rejects_modified_request(self):
        from backend.calibration_configuration import prepare_request
        folder=Path('build')/('普通率定配置核验-'+uuid.uuid4().hex[:10])
        manifest={'id':'input','dataSHA256':'frozen'}
        request={'inputId':'input','parameters':{'WM':100.},'_observations':{'flow':{'2024':4}},
                 '_modelIdentity':'code','researchSchemeId':'source'}
        content={'implementation':{'version':1},'request':request,'inputManifestSHA256':digest(manifest)}
        identifier=digest(content)
        write_json(folder/'calibration-configurations'/ (identifier+'.json'),{'content':content})
        db=SimpleNamespace(workspace=folder,setting=lambda *_:identifier,
                           items=lambda _: [{'id':'input','manifest':manifest}])
        payload={'researchSchemeId':'adopted','calibrationConfigurationSHA256':identifier}
        with patch('backend.calibration_configuration.implementation',return_value={'version':1}):
            first=prepare_request(db,payload)
            second=prepare_request(db,payload)
            self.assertEqual(first,second)
            self.assertEqual(first['_observations'],request['_observations'])
            self.assertEqual(first['parameters'],{'WM':100.})
            with self.assertRaisesRegex(ValueError,'保存变更'):prepare_request(db,{**payload,'parameters':{'WM':200.}})
        with patch('backend.calibration_configuration.implementation',return_value={'version':2}):
            with self.assertRaisesRegex(ValueError,'版本已更新'):prepare_request(db,payload)


if __name__=='__main__':unittest.main()
