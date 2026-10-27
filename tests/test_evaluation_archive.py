import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

BASE=Path(__file__).resolve().parents[1]
APP=Path(os.environ.get('SELINCO_TEST_APP_ROOT','F:/色林错预警预报平台'))
os.environ.setdefault('SELINCO_APP_ROOT',str(APP))
if 'backend' not in sys.modules:
    package=types.ModuleType('backend')
    package.__path__=[str(BASE/'backend'),str(APP/'backend')]
    sys.modules['backend']=package
from backend.database import Database
from backend.common import canonical,sha_file,digest
from backend.release_registry import migrate,current,publish,PROTOCOL
from backend.evaluation_archive import archive,metrics,verify_input_binding
from backend import evaluation_archive


class EvaluationArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=BASE)
        self.db=Database(self.temp.name);migrate(self.db)
        self.release=current(self.db)
        self.make_task('BASE');self.make_task('CAND',candidate=True)
        self.request={'protocolId':PROTOCOL['id'],'baselineReleaseId':self.release['id'],
                      'baselineResultId':'BASE','candidateResultId':'CAND'}

    def tearDown(self):self.temp.cleanup()

    def make_task(self,identifier,candidate=False):
        rows=[]
        for year in (2024,2025):
            for index,observed in enumerate((1,2,3),1):
                rows.append({'date':f'{year}-01-0{index}T00:00:00','observedLevel':observed,'observedFlow':observed,
                             'lakeLevel':observed+(1 if not candidate else .5),
                             'duja':observed+(1 if not candidate else .5)})
        result={'daily':rows,'modelFamily':'grid-continuous-v1','identity':{'tag':identifier},
                'initialization':{'time':'2023-04-20T12:00:00','level':4544.03},
                'observationProfile':'quality-v2','parameters':{'KC':.7},'scheme':{'id':identifier}}
        with self.db.connect() as c:
            c.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result) VALUES(?,?,?,?,?,?,?)',
                      (identifier,'simulation','succeeded','time','time','{}',canonical(result)))

    def change(self,identifier,modify):
        task=self.db.task(identifier);modify(task['result'])
        with self.db.connect() as c:c.execute('UPDATE tasks SET result=? WHERE id=?',(canonical(task['result']),identifier))

    def run_archive(self,**kwargs):
        return archive(self.db,self.request,Path(self.temp.name)/'tasks/evaluation-fixture',**kwargs)

    def stored(self,identifier):return json.loads(self.db.rows('SELECT payload FROM evaluation_records WHERE id=?',(identifier,))[0]['payload'])

    def test_independent_expected_scores_and_archive_does_not_publish(self):
        result=self.run_archive();record=self.stored(result['evaluationId'])
        lake=record['years']['2024']['lake']
        self.assertEqual(lake['commonCount'],3)
        self.assertEqual(lake['baseline']['rmse'],1)
        self.assertEqual(lake['baseline']['mae'],1)
        self.assertEqual(lake['baseline']['bias'],1)
        self.assertEqual(lake['baseline']['nse'],-.5)
        self.assertEqual(lake['baseline']['pbias'],50)
        self.assertEqual(lake['candidate']['rmse'],.5)
        self.assertEqual(lake['candidate']['nse'],.625)
        self.assertEqual(lake['candidate']['pbias'],25)
        self.assertFalse(record['admission']['passed'])
        self.assertEqual(record['comparisonStatus'],'not-comparable')
        self.assertEqual(record['diagnostics']['initialStateStable'],'unknown')
        self.assertTrue(record['archiveComplete']);self.assertEqual(len(record['artifacts']),3)
        for artifact in record['artifacts']:
            self.assertEqual(sha_file(Path(self.temp.name)/record['archiveDirectory']/artifact['path']),artifact['sha256'])
        self.assertEqual(current(self.db)['id'],self.release['id'])
        with self.assertRaises(ValueError):publish(self.db,{'expectedReleaseId':self.release['id'],'evaluationId':result['evaluationId']})
        self.assertTrue(self.run_archive()['reused'])

    def test_missing_data_not_interpolated_observation_conflict_not_comparable(self):
        self.change('CAND',lambda r:r['daily'][0].update(observedLevel=None))
        self.change('CAND',lambda r:r['daily'][1].update(observedFlow=99))
        result=self.run_archive();record=self.stored(result['evaluationId'])
        self.assertEqual(record['years']['2024']['lake']['commonCount'],2)
        self.assertEqual(record['years']['2024']['flow']['commonCount'],2)
        self.assertEqual(len(record['observationConflicts']),1)
        self.assertFalse(record['checks']['commonDatesVerified'])

    def test_window_continuous_and_duplicate_days_rejected(self):
        self.change('BASE',lambda r:r.update(initialization={'lakeLevel':'first-reliable-value-per-observation-window'}))
        result=self.run_archive();record=self.stored(result['evaluationId'])
        self.assertEqual(record['baselineIdentity']['lakeMode'],'window')
        self.assertEqual(record['comparisonStatus'],'not-comparable')
        self.change('CAND',lambda r:r['daily'].append(copy.deepcopy(r['daily'][0])))
        with self.assertRaisesRegex(ValueError,'重复日期'):self.run_archive()

    def test_window_anchor_union_is_excluded_instead_of_counting_fitted_values(self):
        for identifier in ('BASE','CAND'):
            task=self.db.task(identifier);result=task['result']
            result['initialization']={'lakeLevel':'first-reliable-value-per-observation-window'}
            result['diagnostics']={'lakeWindows':[] if identifier=='CAND' else [{'start':'2024-01-01T08:00:00'},{'start':'2025-01-01T12:00:00'}]}
            for row in result['daily']:
                if row['date'][8:10]=='01':row['lakeLevel']=row['observedLevel']
            request={'_observations':{'windows':[{'start':'2024-01-02T00:00:00'},{'start':'2025-01-02T00:00:00'}]}} if identifier=='CAND' else {}
            with self.db.connect() as c:c.execute('UPDATE tasks SET result=?,request=? WHERE id=?',(canonical(result),canonical(request),identifier))
        record=self.stored(self.run_archive()['evaluationId'])
        for year in ('2024','2025'):
            lake=record['years'][year]['lake']
            self.assertEqual(lake['commonCount'],1)
            self.assertEqual(lake['baseline']['rmse'],1)
            self.assertEqual(lake['candidate']['rmse'],.5)
            self.assertEqual(record['years'][year]['flow']['commonCount'],3)
        excluded=record['initializationExclusions']
        self.assertEqual(excluded['excludedCommonDates'],['2024-01-01','2024-01-02','2025-01-01','2025-01-02'])
        self.assertEqual(excluded['excludedCommonDatesSHA256'],digest(excluded['excludedCommonDates']))
        for per_result in excluded['perResult']:
            self.assertEqual(per_result['datesSHA256'],digest(per_result['dates']))
        self.assertTrue(excluded['lakeSamplesMetadataVerified'])
        self.assertEqual(record['comparisonStatus'],'not-comparable')

    def test_conflicting_result_and_request_window_dates_are_not_silently_pooled(self):
        task=self.db.task('BASE');result=task['result']
        result['initialization']={'lakeLevel':'first-reliable-value-per-observation-window'}
        result['diagnostics']={'lakeWindows':[{'start':'2025-01-01T08:00:00'}]}
        request={'_observations':{'windows':[{'start':'2025-01-02T00:00:00'}]}}
        with self.db.connect() as c:c.execute('UPDATE tasks SET result=?,request=? WHERE id=?',(canonical(result),canonical(request),'BASE'))
        record=self.stored(self.run_archive()['evaluationId'])
        self.assertEqual(record['years']['2025']['lake']['commonCount'],0)
        self.assertFalse(record['initializationExclusions']['lakeSamplesMetadataVerified'])
        self.assertTrue(any('窗口初始化日期冲突' in reason for reason in record['comparisonReasons']))
        result['start']='2025-01-01T00:00:00';result['end']='2025-12-31T23:00:00'
        request['_observations']['windows']=[{'start':'2023-04-20T12:00:00'},{'start':'2025-01-01T08:00:00'}]
        with self.db.connect() as c:c.execute('UPDATE tasks SET result=?,request=? WHERE id=?',(canonical(result),canonical(request),'BASE'))
        exclusion=evaluation_archive.initialization_exclusions(self.db.task('BASE'))
        self.assertTrue(exclusion['metadataVerified'])
        self.assertEqual(exclusion['dates'],['2023-04-20','2025-01-01'])

    def test_missing_window_metadata_has_no_qualified_lake_samples(self):
        self.change('BASE',lambda r:r.update(initialization={'lakeLevel':'first-reliable-value-per-observation-window'}))
        record=self.stored(self.run_archive()['evaluationId'])
        self.assertEqual(record['years']['2025']['lake']['commonCount'],0)
        self.assertIsNone(record['years']['2025']['lake']['baseline']['rmse'])
        self.assertFalse(record['initializationExclusions']['lakeSamplesMetadataVerified'])
        self.assertTrue(any('窗口初始化缺少' in reason for reason in record['comparisonReasons']))
        self.assertEqual(record['years']['2025']['flow']['commonCount'],3)

    def test_continuation_does_not_remove_days_other_than_frozen_actual_anchor(self):
        task=self.db.task('BASE');task['result']['initialization']={'lakeLevel':'first-reliable-value-per-observation-window'}
        task['result']['diagnostics']={'lakeWindows':[{'start':'2023-04-20T12:00:00'}]}
        with self.db.connect() as c:c.execute('UPDATE tasks SET kind=?,result=? WHERE id=?',('continuation',canonical(task['result']),'BASE'))
        record=self.stored(self.run_archive()['evaluationId'])
        self.assertEqual(record['years']['2025']['lake']['commonCount'],3)
        self.assertEqual(record['initializationExclusions']['excludedCommonDates'],[])

    def test_real_published_window_pairs_match_independent_frozen_2025_score(self):
        from backend.published_results import detail,current_id
        from backend.release_runtime import _packaged_observations
        published=detail(current_id());baseline=published['result']
        observation=_packaged_observations(published['id'],baseline['identity']['observationSHA256'])
        self.assertIsNotNone(observation,'real protected observation resource required for this test')
        candidate=copy.deepcopy(published);candidate['id']='REAL-window-reproduction'
        candidate['result']['diagnostics']={'lakeWindows':[{'start':w['start']} for w in observation['windows']]}
        with self.db.connect() as c:
            c.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result) VALUES(?,?,?,?,?,?,?)',
                      (candidate['id'],'simulation','succeeded','time','time',canonical(candidate.get('request',{})),canonical(candidate['result'])))
        request={**self.request,'baselineResultId':published['id'],'candidateResultId':candidate['id']}
        outcome=archive(self.db,request,Path(self.temp.name)/'tasks/real-window')
        record=self.stored(outcome['evaluationId']);lake=record['years']['2025']['lake']
        # Independent score is frozen in the protected original, not computed by this writer.
        expected=baseline['metrics']['2025']['lake']
        self.assertEqual(lake['commonCount'],expected['count'])
        self.assertEqual(lake['commonCount'],255)
        for side in ('baseline','candidate'):
            for key in ('rmse','mae','bias'):
                self.assertAlmostEqual(lake[side][key],expected[key],places=11)
        self.assertIn('2025-04-09',record['initializationExclusions']['excludedCommonDates'])
        self.assertIn('packaged-verified-observation.windows',record['initializationExclusions']['perResult'][0]['sources'])
        self.assertFalse(record['admission']['passed'])

    def test_client_scores_unknown_protocol_cancel_and_mutation_checks(self):
        with self.assertRaisesRegex(ValueError,'禁止提交分数'):
            archive(self.db,{**self.request,'checks':{'initialStateStable':True}},Path(self.temp.name)/'x')
        with self.assertRaises(ValueError):archive(self.db,{**self.request,'protocolId':'other'},Path(self.temp.name)/'x')
        with self.assertRaises(InterruptedError):self.run_archive(cancelled=lambda:True)
        self.assertEqual(self.db.rows('SELECT id FROM evaluation_records'),[])
        result=self.run_archive();record=self.stored(result['evaluationId'])
        (Path(self.temp.name)/record['archiveDirectory']/'lake-common-pairs.csv').write_text('tampered')
        with self.assertRaisesRegex(ValueError,'不完整'):self.run_archive()

    def test_late_cancel_and_changed_release_commit_nothing(self):
        flag=[False]
        def progress(value,message):
            if value>=95:flag[0]=True
        with self.assertRaises(InterruptedError):self.run_archive(progress=progress,cancelled=lambda:flag[0])
        self.assertEqual(self.db.rows('SELECT id FROM evaluation_records'),[])
        with self.assertRaisesRegex(ValueError,'默认发布已变化'):
            archive(self.db,{**self.request,'baselineReleaseId':'old'},Path(self.temp.name)/'x')

    def test_empty_and_constant_observations_keep_undefined_scores(self):
        self.assertIsNone(metrics([])['rmse'])
        value=metrics([{'observed':0,'simulated':1},{'observed':0,'simulated':2}])
        self.assertIsNone(value['nse']);self.assertIsNone(value['pbias'])

    def test_result_mutation_before_commit_is_not_archived(self):
        def progress(value,message):
            if value>=95:self.change('CAND',lambda r:r['daily'][0].update(lakeLevel=10))
        with self.assertRaisesRegex(ValueError,'发生变化'):self.run_archive(progress=progress)
        self.assertEqual(self.db.rows('SELECT id FROM evaluation_records'),[])

    def test_frozen_identity_hashes_installed_source_and_missing_source_rejects(self):
        root=Path(self.temp.name)/'installed'
        source=root/'backend/evaluation_archive.py';source.parent.mkdir(parents=True)
        source.write_text('installed real implementation',encoding='utf-8')
        with patch.object(sys,'frozen',True,create=True),patch.object(evaluation_archive,'ROOT',root),patch.object(evaluation_archive,'__file__',str(root/'_internal/backend/not-present.pyc')):
            self.assertEqual(evaluation_archive.implementation_identity(),sha_file(source))
        with patch.object(sys,'frozen',True,create=True),patch.object(evaluation_archive,'ROOT',root/'missing'):
            with self.assertRaises(FileNotFoundError):evaluation_archive.implementation_identity()

    def test_complete_candidate_freezes_revision_in_same_archive_transaction(self):
        manifest={'id':'INPUT-fixture','dataSHA256':'frozen-input-sha','spatialVersion':'SPATIAL-fixture'}
        with self.db.connect() as c:
            c.execute('INSERT INTO input_products VALUES(?,?,?,?)',('INPUT-fixture','fixture','now',canonical(manifest)))
        task=self.db.task('CAND');result=task['result']
        result['modelFamily']='legacy-test'
        result.update(inputId='INPUT-fixture',spatialVersion='SPATIAL-fixture',routingMode='source-separated')
        result['identity'].update(inputId='INPUT-fixture',inputSHA256='frozen-input-sha')
        request={'inputId':'INPUT-fixture','start':'2023-01-01T00:00:00','end':'2026-01-01T00:00:00',
                 '_modelIdentity':'fixture-model-identity','_observations':{'profile':'quality-v2','level':{'2024-01-01':1}}}
        with self.db.connect() as c:c.execute('UPDATE tasks SET request=?,result=? WHERE id=?',(canonical(request),canonical(result),'CAND'))
        archived=self.run_archive();record=self.stored(archived['evaluationId'])
        self.assertTrue(record['schemeRevisionId'].startswith('REV-'))
        rows=self.db.rows('SELECT payload FROM scheme_revisions WHERE id=?',(record['schemeRevisionId'],))
        snapshot=json.loads(rows[0]['payload'])
        self.assertEqual(snapshot['sourceResultId'],'CAND')
        self.assertEqual(snapshot['frozenRequest']['_observations'],request['_observations'])
        self.assertEqual(snapshot['runtimeEvidence'],{})
        self.assertFalse(snapshot['admissionPassed'])
        self.assertEqual(current(self.db)['id'],self.release['id'])
        self.assertFalse(record['checks']['strictContinuationVerified'])

    def test_tiny_grid_prefix_binding_and_historical_change_refused(self):
        import numpy as np
        import pandas as pd
        from backend import grid_simulation
        from backend.common import digest,write_json
        root=Path(self.temp.name)/'inputs/products/INPUT-tiny';root.mkdir(parents=True)
        times=pd.date_range('2024-01-01T01:00:00',periods=3,freq='h')
        arrays={'time':times.values.astype('int64'),'precipitation':np.ones((3,1)),
                'temperature':np.zeros((3,1)),'evaporation':np.zeros((3,1))}
        np.savez(root/'forcing.npz',**arrays)
        manifest={'id':'INPUT-tiny','data':'forcing.npz','dataSHA256':sha_file(root/'forcing.npz'),
                  'transformationSHA256':'transform','spatialVersion':'SPATIAL-tiny'}
        write_json(root/'manifest.json',manifest)
        source={'manifest':manifest,'spatial':{'forcingGridVersion':'grid'},'times':times,'arrays':arrays}
        observations={'profile':'quality-v2','level':{}}
        identity={'forcingGridSHA256':'grid','forcingTransformationSHA256':'transform',
                  'observationSnapshotSHA256':digest(observations)}
        checkpoint={'identity':identity,'validAt':times[-1].isoformat(),'inputId':'INPUT-tiny',
                    'forcingPrefixSHA256':grid_simulation.prefix_identity(source,times[-1])}
        task={'id':'CAND','request':{'inputId':'INPUT-tiny','_observations':observations},
              'result':{'inputId':'INPUT-tiny','modelFamily':'grid-continuous-v1','spatialVersion':'GRID-test',
                        'identity':identity,'end':times[-1].isoformat(),'checkpoint':checkpoint,
                        'checkpointSHA256':digest(checkpoint)}}
        snapshot={'inputManifest':manifest,'frozenRequest':{'inputId':'INPUT-tiny'}}
        with patch.object(grid_simulation,'prepare',return_value=source):
            self.assertEqual(verify_input_binding(self.db,task,snapshot)['method'],'grid-checkpoint-input-prefix')
        arrays['precipitation'][0,0]=2
        np.savez(root/'forcing.npz',**arrays)
        manifest['dataSHA256']=sha_file(root/'forcing.npz');write_json(root/'manifest.json',manifest)
        with patch.object(grid_simulation,'prepare',return_value=source):
            with self.assertRaisesRegex(ValueError,'历史气象前缀'):verify_input_binding(self.db,task,snapshot)


if __name__=='__main__':unittest.main(verbosity=2)
