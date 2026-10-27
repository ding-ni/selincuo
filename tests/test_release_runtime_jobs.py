"""Real queue/storage with tiny input fixtures and instrumented numerical runners.

Tests integration dispatch and identity preservation; they do not certify new
hydrological accuracy or execute production-size numerical experiments.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from backend.common import canonical,digest,now,sha_file,write_json
from backend.database import Database
from backend.jobs import Jobs
from backend.model_core import parameters
from backend.release_registry import SCHEMA
from backend import release_runtime as runtime,business_context


class ReleaseRuntimeJobTests(unittest.TestCase):
    def check_family(self,grid):
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(tmp);folder=db.workspace/'inputs/products/frozen';folder.mkdir(parents=True)
            np.savez_compressed(folder/'forcing.npz',time=np.arange(2,dtype='i8'),precipitation=np.zeros((2,1)),temperature=np.ones((2,1)),evaporation=np.ones((2,1)))
            manifest={'id':'frozen','data':'forcing.npz','dataSHA256':sha_file(folder/'forcing.npz'),'spatialVersion':'space','start':'2022-12-01T00:00:00','end':'2026-01-01T07:00:00','transformationSHA256':'transform'}
            write_json(folder/'manifest.json',manifest)
            with db.connect() as c:c.execute('INSERT INTO input_products VALUES(?,?,?,?)',('frozen','frozen',now(),canonical(manifest)))
            obs={'profile':'quality','level':{'2023-04-20T00:00:00':4544.03},'flow':{}}
            initial={'time':'2023-04-20T12:00:00','level':4544.03,'method':'first-quality-daily-level-at-noon','source':'quality','reference':'SLC-working-datum'}
            p=parameters({});scheme={'id':'frozen-scheme','revision':2,'spatialVersion':'space'}
            identity={'modelImplementation':'actual-implementation','routingMode':'source-separated',
                      'observationSnapshotSHA256' if grid else 'observationSHA256':digest(obs),'initializationSHA256':digest(initial)}
            request={'inputId':'frozen','parameters':p,'partitionParameters':{},'routingMode':'source-separated','start':'2023-01-01T00:00:00','end':'2026-01-01T00:00:00',
                     '_observations':obs,'_modelIdentity':'actual-implementation','schemeSnapshot':scheme,'spatialVersion':'space',
                     'researchSchemeId':'mutable-research','researchConfiguration':{'parameters':p},'periods':{'warmupStart':'2023-01-01','evaluationEnd':'2026-01-01'}}
            result={'inputId':'frozen','parameters':p,'partitionParameters':{},'routingMode':'source-separated','scheme':scheme,'identity':identity,
                    'spatialVersion':'space','initialization':initial,'daily':[{'date':'2025-01-01'},{'date':'2025-12-31'}]}
            if grid:request['modelFamily']=result['modelFamily']='grid-continuous-v1'
            task={'id':'published-result','status':'succeeded','request':request,'result':result}
            snapshot=runtime.freeze_revision(db,task,{'fullArtifactsVerified':True,'strictContinuationVerified':True})
            with db.connect() as c:
                c.executescript(SCHEMA)
                c.execute('INSERT INTO scheme_revisions VALUES(?,?)',('revision',canonical(snapshot)))
                c.execute('INSERT INTO operational_releases(id,scheme_revision_id,result_id,published_at,action) VALUES(?,?,?,?,?)',('release','revision','published-result',now(),'published'))
            release={'id':'release','resultId':'published-result','schemeRevisionId':'revision'}
            captured=[]
            def runner(db,request,folder,progress,cancelled):
                captured.append(request)
                self.assertEqual(request['_observations'],obs)
                self.assertEqual(request['_modelIdentity'],'actual-implementation')
                self.assertNotIn('researchSchemeId',request)
                return {'identity':identity,'daily':[]}
            jobs=Jobs(db)
            try:
                with patch('backend.release_registry.current',return_value=release),patch('backend.business_context.result_task',return_value=task),patch('backend.grid_simulation.model_identity',return_value='actual-implementation'),patch.object(runtime,'model_identity',return_value='actual-implementation'),patch('backend.jobs.model_identity',return_value='actual-implementation'),patch('backend.jobs.observation_snapshot',side_effect=AssertionError('must not recapture legacy observations')),patch('backend.grid_simulation.observations',side_effect=AssertionError('must not recapture grid observations')),patch('backend.research_schemes.prepare_request',side_effect=AssertionError('must not read mutable scheme')),patch('backend.forcing_latest.provenance_summary',return_value={'fixture':True}),patch('backend.grid_simulation.run',side_effect=runner) as grid_run,patch('backend.jobs.run_simulation',side_effect=runner) as legacy_run:
                    query={'expectedReleaseId':'release','start':'2025-01-01','end':'2026-01-01','clientRequestId':'release-runtime-fixture'}
                    self.assertEqual(business_context.simulation_plan(db,query)['action'],'reuse')
                    submitted=jobs.submit('simulation',query)
                    duplicate=jobs.submit('simulation',query);self.assertEqual(submitted['id'],duplicate['id'])
                    deadline=time.monotonic()+10
                    while time.monotonic()<deadline:
                        complete=db.task(submitted['id'])
                        if complete['status'] not in ('queued','running'):break
                        time.sleep(.02)
                    self.assertEqual(complete['status'],'succeeded',complete['message'])
                    self.assertEqual(len(captured),1)
                    self.assertEqual(grid_run.call_count,1 if grid else 0)
                    self.assertEqual(legacy_run.call_count,0 if grid else 1)
                    self.assertEqual(complete['request']['expectedReleaseId'],'release')
                    self.assertEqual(complete['request']['parameters'],p)
            finally:jobs.executor.shutdown(wait=True)

    def test_grid_release_adapter_dispatches_without_mutable_template_or_observation_recapture(self):self.check_family(True)
    def test_legacy_release_adapter_keeps_frozen_request_and_uses_legacy_runner(self):self.check_family(False)

    def test_grid_continuation_captures_current_observations_not_parent_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp)
            manifest = {'id': 'input', 'spatialVersion': 'space', 'start': '2023-01-01T00:00:00',
                        'end': '2026-01-01T00:00:00', 'sources': []}
            checkpoint = {'schemaVersion': 3, 'identity': {'spatialVersion': 'space'},
                          'partitionParameters': {}, 'validAt': '2024-12-31T23:00:00'}
            old = {'profile': 'old', 'level': {}, 'flow': {}}
            fresh = {'profile': 'new', 'level': {'2025-01-01T00:00:00': 4544.2}, 'flow': {}}
            with db.connect() as conn:
                conn.execute('INSERT INTO input_products VALUES(?,?,?,?)', ('input', 'input', now(), canonical(manifest)))
                conn.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result) VALUES(?,?,?,?,?,?,?)',
                             ('parent', 'simulation', 'succeeded', now(), now(), canonical({'_observations': old}),
                              canonical({'checkpointSHA256': digest(checkpoint)})))
                conn.execute('INSERT INTO states VALUES(?,?,?,?,?,?,?)',
                             ('state', 'parent', now(), checkpoint['validAt'], canonical(checkpoint), '', 0))
            captured = []
            def runner(db, request, folder, progress, cancelled):
                captured.append(request)
                return {'identity': {}, 'daily': []}
            jobs = Jobs(db)
            try:
                with patch('backend.grid_simulation.observations', return_value=fresh), \
                     patch('backend.grid_simulation.model_identity', return_value='current-model'), \
                     patch('backend.grid_simulation.run', side_effect=runner):
                    submitted = jobs.submit('continuation', {'stateId': 'state', 'inputId': 'input',
                        'parameters': parameters({}), 'start': '2025-01-01T00:00:00', 'end': '2025-02-01T00:00:00'})
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        result = db.task(submitted['id'])
                        if result['status'] not in ('queued', 'running'):
                            break
                        time.sleep(.02)
                    self.assertEqual(result['status'], 'succeeded', result['message'])
                    self.assertEqual(captured[0]['_observations']['profile'], 'new')
                    self.assertEqual(captured[0]['_observations']['level']['2025-01-01T00:00:00'], 4544.2)
                    self.assertEqual(captured[0]['_parentState'], checkpoint)
                    self.assertEqual(db.task('parent')['request']['_observations'], old)
            finally:
                jobs.executor.shutdown(wait=True)


if __name__=='__main__':unittest.main()
