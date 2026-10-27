import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend.common import digest, sha_file, write_json, read_json
from backend import release_runtime as runtime


class FixtureDB:
    def __init__(self,workspace):self.workspace=Path(workspace);self.products=[];self.snapshot=None
    def items(self,name):
        if name!='input_products':raise AssertionError('mutable scheme/template lookup forbidden: '+name)
        return copy.deepcopy(self.products)
    def rows(self,sql,args=()):return [{'payload':json.dumps(self.snapshot)}]


class ReleaseRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.db=FixtureDB(self.tmp.name)
        self.obs={'profile':'quality','level':{'2023-04-20T00:00:00':4544.03},'flow':{}}
        self.scheme={'id':'frozen-grid','revision':2,'spatialVersion':'grid','initialization':{'origin':'2023-01-01T00:00:00'}}
        self.initial={'time':'2023-04-20T12:00:00','level':4544.03,'method':'first-quality-daily-level-at-noon','source':'quality','reference':'SLC-working-datum'}
        self.input('frozen',6)
        self.result={'scheme':self.scheme,'identity':{'modelImplementation':'implementation','observationSnapshotSHA256':digest(self.obs),
                     'initializationSHA256':digest(self.initial),'spatialVersion':'grid'},'inputId':'frozen','parameters':{'KC':1},
                     'partitionParameters':{},'routingMode':'source-separated','modelFamily':'grid-continuous-v1','spatialVersion':'grid',
                     'initialization':self.initial,'daily':[{'date':'2024-01-01'},{'date':'2025-12-31'}]}
        self.request={'inputId':'frozen','parameters':{'KC':1},'partitionParameters':{},'routingMode':'source-separated',
                      'modelFamily':'grid-continuous-v1','spatialVersion':'grid','schemeSnapshot':self.scheme,
                      'start':'2023-01-01T00:00:00','end':'2026-01-01T00:00:00','_observations':self.obs,'_modelIdentity':'implementation',
                      'researchSchemeId':'mutable-id','researchConfiguration':{'name':'frozen config','parameters':{'KC':1}},
                      'periods':{'warmupStart':'2023-01-01T00:00:00','evaluationEnd':'2026-01-01T00:00:00'}}
        self.task={'id':'result','status':'succeeded','request':self.request,'result':self.result}
        self.release={'id':'release','resultId':'result','schemeRevisionId':'revision'}
        self.db.snapshot=runtime.freeze_revision(self.db,self.task,{'fullArtifactsVerified':True,'strictContinuationVerified':True})
        self.patch_release=patch('backend.release_registry.current',return_value=self.release);self.patch_release.start();self.addCleanup(self.patch_release.stop)
        self.patch_task=patch('backend.business_context.result_task',side_effect=lambda db,id:copy.deepcopy(self.task));self.patch_task.start();self.addCleanup(self.patch_task.stop)
        self.patch_model=patch('backend.grid_simulation.model_identity',return_value='implementation');self.patch_model.start();self.addCleanup(self.patch_model.stop)
        self.query={'expectedReleaseId':'release','start':'2025-01-01','end':'2026-01-01'}

    def input(self,id,n,changed=False):
        folder=self.db.workspace/'inputs/products'/id;folder.mkdir(parents=True)
        rain=np.arange(n*2,dtype='f8').reshape(n,2)
        if changed:rain[0,0]+=1
        np.savez_compressed(folder/'forcing.npz',time=np.arange(n,dtype='i8'),precipitation=rain,temperature=rain+1,evaporation=rain+2)
        manifest={'id':id,'data':'forcing.npz','dataSHA256':sha_file(folder/'forcing.npz'),'spatialVersion':'forcing-space',
                  'transformationSHA256':'transform','start':'2022-12-01T00:00:00','end':'2026-01-01T07:00:00' if n==6 else '2027-01-01T07:00:00'}
        write_json(folder/'manifest.json',manifest);self.db.products.append({'id':id,'manifest':manifest});return manifest,folder/'forcing.npz'

    def test_reuse_locked_release_does_not_need_new_runtime_evidence(self):
        self.db.snapshot.pop('frozenRequest');self.db.snapshot.pop('snapshotSHA256')
        plan=runtime.simulation_plan(self.db,self.query)
        self.assertEqual(plan['action'],'reuse');self.assertFalse(plan['createsTask'])
        with self.assertRaisesRegex(ValueError,'expectedReleaseId'):
            runtime.simulation_plan(self.db,{**self.query,'expectedReleaseId':'old'})

    def test_grid_request_keeps_actual_frozen_parameters_anchor_and_observations(self):
        prepared=runtime.prepare_request(self.db,self.query)
        self.assertEqual(prepared['_observations'],self.obs)
        self.assertEqual(prepared['researchConfiguration'],self.request['researchConfiguration'])
        self.assertEqual(prepared['periods'],self.request['periods'])
        self.assertEqual(prepared['parameters'],self.result['parameters'])
        self.assertNotIn('researchSchemeId',prepared)
        self.assertEqual(prepared['start'],'2023-01-01T00:00:00')
        self.assertEqual(prepared['evaluationStart'],'2025-01-01T00:00:00')
        self.assertEqual(prepared['_modelIdentity'],'implementation')

    def test_missing_runtime_evidence_or_changed_model_blocks_grid_compute(self):
        self.db.snapshot['runtimeEvidence']['strictContinuationVerified']=False;self.db.snapshot.pop('snapshotSHA256')
        with self.assertRaisesRegex(ValueError,'严格续算'):
            runtime.prepare_request(self.db,self.query)
        self.db.snapshot['runtimeEvidence']['strictContinuationVerified']=True
        with patch('backend.grid_simulation.model_identity',return_value='new-model'),self.assertRaisesRegex(ValueError,'数值实现'):
            runtime.prepare_request(self.db,self.query)

    def test_exact_extension_passes_changed_historical_values_or_transformation_fail(self):
        new_manifest,new_path=self.input('extended',10)
        baseline=self.db.products[0]['manifest'];base_path=self.db.workspace/'inputs/products/frozen/forcing.npz'
        evidence=runtime.verify_input_extension(baseline,base_path,new_manifest,new_path)
        self.assertTrue(evidence['verified']);self.assertLessEqual(evidence['maximumReadBytes'],1024*1024)
        prepared=runtime.prepare_request(self.db,{**self.query,'inputId':'extended'})
        self.assertEqual(prepared['inputId'],'extended')
        bad,bad_path=self.input('changed',10,True)
        with self.assertRaisesRegex(ValueError,'历史输入前缀'):
            runtime.verify_input_extension(baseline,base_path,bad,bad_path)
        with self.assertRaisesRegex(ValueError,'transformationSHA256'):
            runtime.verify_input_extension(baseline,base_path,{**new_manifest,'transformationSHA256':'new'},new_path)

    def test_legacy_existing_prepare_reconstructs_matching_observations_without_spoofing_hash(self):
        result=self.task['result'];result.pop('modelFamily');result['identity']={'observationSHA256':digest(self.obs),'modelImplementation':'old-implementation'}
        result['scheme']={'id':'legacy','revision':2};self.task['request']={}
        self.db.snapshot={'scheme':result['scheme'],'identity':result['identity'],'parameters':result['parameters'],
                          'partitionParameters':{},'status':'protected-legacy','routingMode':'source-separated'}
        def prepare(db,query):return {**query,'start':'2023-01-01T00:00:00','parameters':{'KC':1},'partitionParameters':{},'schemeSnapshot':result['scheme']}
        with patch('backend.operational_scheme.prepare_request',side_effect=prepare),patch('backend.simulation.observation_snapshot',return_value=self.obs),patch.object(runtime,'model_identity',return_value='current-implementation'):
            plan=runtime.adapt(self.db,self.query,require_compute=True)
        self.assertTrue(plan['matchingFrozenObservation']);self.assertFalse(plan['matchingFrozenModelImplementation'])
        self.assertFalse(plan['resultIdentityExpectedToMatch'])
        self.assertEqual(plan['preparedRequest']['_modelIdentity'],'current-implementation')
        with patch('backend.operational_scheme.prepare_request',side_effect=prepare),patch('backend.simulation.observation_snapshot',return_value={'changed':True}),self.assertRaisesRegex(ValueError,'冻结观测快照'):
            runtime.prepare_request(self.db,self.query)

    def test_actual_protected_snapshot_without_top_level_routing_uses_consistent_result_identity(self):
        from backend.published_results import detail,current_id
        task=detail(current_id());result=task['result']
        snapshot={'scheme':result['scheme'],'identity':result['identity'],'parameters':result['parameters'],
                  'partitionParameters':result.get('partitionParameters',{}),'initialization':result['scheme']['initialization'],
                  'status':'protected-legacy','admissionPassed':False}
        self.task=task;self.db.snapshot=snapshot
        self.db.products[0]['id']=result['inputId']
        manifest={**self.db.products[0]['manifest'],'spatialVersion':result['scheme']['spatialVersion'],'dataSHA256':result['identity']['inputSHA256']}
        self.db.products[0]['manifest']=manifest
        prepared={**task['request'],'parameters':result['parameters'],'partitionParameters':result.get('partitionParameters',{}),
                  'routingMode':result['identity']['routingMode'],'schemeSnapshot':result['scheme']}
        frozen_observation={'trusted-reconstructed-snapshot':True}
        real_digest=runtime.digest
        def observation_digest(value):return result['identity']['observationSHA256'] if value==frozen_observation else real_digest(value)
        with patch('backend.operational_scheme.prepare_request',return_value=prepared),patch('backend.simulation.observation_snapshot',return_value=frozen_observation),patch.object(runtime,'digest',side_effect=observation_digest),patch.object(runtime,'_product',return_value=(manifest,Path('verified-input-fixture'))),patch.object(runtime,'model_identity',return_value='current-hash'):
            outcome=runtime.adapt(self.db,self.query,require_compute=True)
        self.assertTrue(outcome['matchingFrozenObservation'])
        self.assertEqual(outcome['preparedRequest']['routingMode'],result['identity']['routingMode'])
        self.assertEqual(outcome['preparedRequest']['inputId'],result['inputId'])
        self.assertFalse(outcome['resultIdentityExpectedToMatch'])
        with self.assertRaisesRegex(ValueError,'汇流结构'):
            runtime._routing_mode(snapshot,result,{'routingMode':'lumped' if result['identity']['routingMode']!='lumped' else 'source-separated'})

    def test_packaged_real_snapshot_preserves_unreferenced_source_without_using_current_seed(self):
        from backend.published_results import detail,current_id
        task=detail(current_id());result=task['result'];expected=result['identity']['observationSHA256']
        resource_root=Path(__file__).resolve().parents[1]
        index=read_json(resource_root/'config/operational/published-observations.json')
        self.assertIsNotNone(index,'Actual published observation resource must accompany this test/app')
        resource=index['snapshots'][task['id']]
        original=read_json(resource_root/'config/operational'/resource['file'])
        self.assertEqual(digest(original),expected)
        self.assertEqual(len(original['sources']),33)
        current_seed=copy.deepcopy(original)
        current_seed['sources']=[s for s in current_seed['sources'] if s['id']!='SRC-c3ef5df400a1caaa0f540a85']
        self.assertEqual(len(current_seed['sources']),32)
        self.assertNotEqual(digest(current_seed),expected)
        self.assertEqual(current_seed['flow'],original['flow'])
        self.assertEqual(current_seed['level'],original['level'])
        self.task=task
        self.db.snapshot={'scheme':result['scheme'],'identity':result['identity'],'parameters':result['parameters'],
                          'partitionParameters':result.get('partitionParameters',{}),'initialization':result['scheme']['initialization'],
                          'status':'protected-legacy','admissionPassed':False}
        manifest={**self.db.products[0]['manifest'],'spatialVersion':result['scheme']['spatialVersion'],
                  'dataSHA256':result['identity']['inputSHA256']}
        prepared={**task['request'],'parameters':result['parameters'],'partitionParameters':result.get('partitionParameters',{}),
                  'routingMode':result['identity']['routingMode'],'schemeSnapshot':result['scheme']}
        with patch.object(runtime,'ROOT',resource_root),patch('backend.operational_scheme.prepare_request',return_value=prepared),patch('backend.simulation.observation_snapshot',side_effect=AssertionError('Do not reconstruct the differing seed ledger')),patch.object(runtime,'_product',return_value=(manifest,Path('verified-input-fixture'))),patch.object(runtime,'model_identity',return_value='real-current-model'):
            outcome=runtime.adapt(self.db,self.query,require_compute=True)
        self.assertEqual(outcome['preparedRequest']['_observations'],original)
        self.assertEqual(digest(outcome['preparedRequest']['_observations']),expected)
        self.assertEqual(outcome['preparedRequest']['_modelIdentity'],'real-current-model')
        self.assertFalse(outcome['matchingFrozenModelImplementation'])
        self.assertTrue(outcome['matchingFrozenObservation'])
        self.assertFalse(outcome['resultIdentityExpectedToMatch'])

    def test_existing_packaged_snapshot_tampering_and_path_escape_never_fall_back(self):
        root=Path(self.tmp.name)/'installation'
        directory=root/'config/operational';directory.mkdir(parents=True)
        original={'sources':[{'id':'unreferenced-station-metadata','sha256':'source'}],
                  'flow':{'2025-01-01T00:00:00':1},'level':{},'windows':[]}
        expected=digest(original);resource=directory/'snapshot.json';write_json(resource,original)
        index={'schemaVersion':1,'projectId':'selinco','snapshots':{'protected':{
            'file':'snapshot.json','fileSHA256':sha_file(resource),'observationSHA256':expected}}}
        write_json(directory/'published-observations.json',index)
        with patch.object(runtime,'ROOT',root):
            self.assertEqual(runtime._packaged_observations('protected',expected),original)
            self.assertIsNone(runtime._packaged_observations('absent-result',expected))
            missing_fixture=resource.with_name('missing-fixture.json')
            resource.rename(missing_fixture)
            with self.assertRaisesRegex(FileNotFoundError,'文件缺失'):
                runtime._packaged_observations('protected',expected)
            missing_fixture.rename(resource)
            resource.write_text('{"tampered":true}',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'文件摘要'):
                runtime._packaged_observations('protected',expected)
            index['snapshots']['protected']['fileSHA256']=sha_file(resource)
            write_json(directory/'published-observations.json',index)
            with self.assertRaisesRegex(ValueError,'快照与原成果'):
                runtime._packaged_observations('protected',expected)
            index['snapshots']['protected']['file']='../../outside.json'
            write_json(directory/'published-observations.json',index)
            with self.assertRaisesRegex(ValueError,'路径超出'):
                runtime._packaged_observations('protected',expected)

    def test_missing_packaged_snapshot_keeps_strict_current_fallback(self):
        root=Path(self.tmp.name)/'empty-installation';root.mkdir()
        with patch.object(runtime,'ROOT',root):
            self.assertIsNone(runtime._packaged_observations('protected','expected'))
        result=self.task['result'];result.pop('modelFamily')
        result['identity']={'observationSHA256':digest(self.obs),'modelImplementation':'old'}
        result['scheme']={'id':'legacy','revision':2};self.task['request']={}
        self.db.snapshot={'scheme':result['scheme'],'identity':result['identity'],'parameters':result['parameters'],
                          'partitionParameters':{},'status':'protected-legacy','routingMode':'source-separated'}
        def prepare(db,query):return {**query,'start':'2023-01-01T00:00:00','parameters':{'KC':1},'partitionParameters':{},'schemeSnapshot':result['scheme']}
        with patch.object(runtime,'ROOT',root),patch('backend.operational_scheme.prepare_request',side_effect=prepare),patch('backend.simulation.observation_snapshot',return_value={'changed':True}),self.assertRaisesRegex(ValueError,'冻结观测快照'):
            runtime.prepare_request(self.db,self.query)


if __name__=='__main__':unittest.main()
