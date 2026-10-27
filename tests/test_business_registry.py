"""Contract checks against packaged metadata; all database mutations are isolated."""
import copy
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parents[1]
APP = Path(os.environ.get('SELINCO_TEST_APP_ROOT',
           str(BASE if (BASE/'backend/common.py').exists() else 'F:/色林错预警预报平台'))).resolve()
os.environ.setdefault('SELINCO_APP_ROOT', str(APP))
if (BASE/'stage/backend').is_dir():
    package = types.ModuleType('backend')
    package.__path__ = [str(BASE/'stage/backend'), str(APP/'backend')]
    sys.modules['backend'] = package
elif str(APP) not in sys.path:
    sys.path.insert(0,str(APP))
from backend.database import Database
from backend.common import canonical, digest, now
from backend import release_registry as registry
from backend import business_context as context
from backend.published_results import current_id, detail


def evidence():
    return {'protocolId': registry.PROTOCOL['id'], 'lakeMode': 'continuous',
            'years': {year: {'lake': {'commonCount': 200, 'baseline': {'rmse': .1, 'bias': -.02},
                                     'candidate': {'rmse': .08, 'bias': -.01}},
                            'flow': {'commonCount': 200, 'baseline': {'nse': .7, 'pbias': -3},
                                     'candidate': {'nse': .7, 'pbias': 2}}}
                      for year in ('2024', '2025')},
            'checks': {key: True for key in ('initialStateStable', 'waterBalanceValid', 'parameterBoundsValid',
                       'inputProvenanceValid', 'fullArtifactsVerified', 'strictContinuationVerified',
                       'limitationsArchived', 'commonDatesVerified')},
            'stability': {'lakeMaximumDifferenceM': .005, 'flowDifferenceRmseM3s': .1, 'observedFlowMeanM3s': 50}}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='registry-contract-', dir=BASE)
        self.db = Database(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_migration_preserves_and_backs_up_database(self):
        self.db.set_setting('sentinel', {'keep': 17})
        before = registry.current(self.db)
        self.assertFalse(before['admissionPassed'])
        self.assertFalse(self.db.rows("SELECT name FROM sqlite_master WHERE name='operational_releases'"))
        registry.migrate(self.db)
        registry.migrate(self.db)
        self.assertEqual(self.db.setting('sentinel'), {'keep': 17})
        self.assertEqual(len(self.db.rows('SELECT * FROM operational_releases')), 1)
        backup = Path(self.folder.name)/'database/pre-release-registry-v2.sqlite3'
        with sqlite3.connect(backup) as connection:
            self.assertEqual(json.loads(connection.execute("SELECT value FROM settings WHERE key='sentinel'").fetchone()[0]), {'keep': 17})
            self.assertEqual(connection.execute('SELECT MAX(version) FROM schema_version').fetchone()[0], 1)

    def test_admission_conflicts_missing_samples_and_initialization(self):
        good = evidence()
        self.assertTrue(registry.admission(good)['passed'])
        cases = []
        e = copy.deepcopy(good);e['years']['2025']['flow']['candidate']['nse'] = .69;cases.append(e)
        e = copy.deepcopy(good);e['years']['2024']['lake']['candidate']['bias'] = .03;cases.append(e)
        e = copy.deepcopy(good);e['years']['2025']['flow']['commonCount'] = 0;cases.append(e)
        e = copy.deepcopy(good);e['years']['2025']['flow']['candidate']['pbias'] = float('nan');cases.append(e)
        e = copy.deepcopy(good);e['lakeMode'] = 'window';cases.append(e)
        e = copy.deepcopy(good);e['stability']['lakeMaximumDifferenceM'] = .219;cases.append(e)
        e = copy.deepcopy(good);e['stability'] = {};cases.append(e)
        e = copy.deepcopy(good);e['years']['2025']['lake']['candidate']['rmse'] = -.1;cases.append(e)
        e = copy.deepcopy(good)
        for year in e['years'].values():year['lake']['candidate']['rmse'] = .099
        cases.append(e)
        for e in cases:
            with self.subTest(e=e):self.assertFalse(registry.admission(e)['passed'])

    def seed_evaluation(self):
        registry.migrate(self.db)
        release = registry.current(self.db)
        e = evidence()
        task = detail(current_id())
        task = copy.deepcopy(task)
        task['id'] = 'JOB-synthetic-contract-only'
        task['result']['modelFamily'] = 'grid-continuous-v1'
        snapshot = {'scheme': task['result']['scheme'], 'identity': task['result']['identity'],
                    'parameters': task['result']['parameters'],
                    'partitionParameters': task['result']['partitionParameters']}
        revision = 'REV-synthetic-contract-only'
        e.update(baselineReleaseId=release['id'], schemeRevisionId=release['schemeRevisionId'],
                 resultId=task['id'], resultIdentitySHA256=digest(task['result']['identity']))
        e['schemeRevisionId'] = revision
        with self.db.connect() as connection:
            connection.execute('INSERT INTO scheme_revisions VALUES(?,?)', (revision, canonical(snapshot)))
            connection.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result) VALUES(?,?,?,?,?,?,?)',
                               (task['id'], 'simulation', 'succeeded', now(), now(), canonical(task['request']), canonical(task['result'])))
            connection.execute('INSERT INTO evaluation_records VALUES(?,?)', ('E-test-only', canonical(e)))
        return release

    def test_client_scores_cannot_authorize_publication(self):
        registry.migrate(self.db)
        release = registry.current(self.db)
        with self.assertRaisesRegex(ValueError, '后端归档'):
            registry.publish(self.db, {**evidence(), 'expectedReleaseId': release['id'], 'evaluationId': 'absent'})
        self.assertEqual(registry.current(self.db)['id'], release['id'])

    def test_publish_optimistic_lock_runtime_guard_and_atomic_rollback(self):
        release = self.seed_evaluation()
        request = {'expectedReleaseId': release['id'], 'evaluationId': 'E-test-only'}
        with patch.object(registry, 'runtime_supports', return_value=False):
            with self.assertRaisesRegex(ValueError, '派发尚未接通'):registry.publish(self.db, request)
        self.assertEqual(len(self.db.rows('SELECT * FROM operational_releases')), 1)
        # Mocking runtime is explicit: no scientific or runtime certification is
        # inferred from this synthetic transaction test.
        with patch.object(registry, 'runtime_supports', return_value=True):
            published = registry.publish(self.db, request)
        with self.assertRaisesRegex(ValueError, '已变化'):registry.publish(self.db, request)
        restored = registry.publish(self.db, {'action': 'rollback', 'expectedReleaseId': published['id']})
        self.assertEqual(restored['resultId'], release['resultId'])
        self.assertEqual(restored['schemeRevisionId'], release['schemeRevisionId'])
        self.assertEqual(restored['status'], 'protected-legacy')
        self.assertFalse(restored['admissionPassed'])
        self.assertEqual(len(self.db.rows('SELECT * FROM operational_releases')), 3)
        self.assertEqual(len(self.db.rows("SELECT * FROM audit WHERE action LIKE 'operational.%'")), 2)

    def test_identity_tampering_rejected_without_pointer_change(self):
        release = self.seed_evaluation()
        with self.db.connect() as c:
            e=json.loads(c.execute('SELECT payload FROM evaluation_records').fetchone()[0])
            e['resultIdentitySHA256']='forged'
            c.execute('UPDATE evaluation_records SET payload=?', (canonical(e),))
        with self.assertRaisesRegex(ValueError, '身份不一致'):
            registry.publish(self.db, {'expectedReleaseId': release['id'], 'evaluationId': 'E-test-only'})
        self.assertEqual(registry.current(self.db)['id'], release['id'])

    def test_real_packaged_summary_and_exclusive_date_plan(self):
        task = detail(current_id())
        result = task['result']
        scheme = importlib.import_module('backend.operational_scheme').definition()
        manifest = {'spatialVersion': result['spatialVersion'], 'start': result['start'],
                    'end': '2026-01-01T07:00:00', 'hours': 26312,
                    'transformationSHA256': scheme['inputRequirement']['transformationSHA256']}
        with self.db.connect() as c:
            c.execute('INSERT INTO input_products VALUES(?,?,?,?)', (result['inputId'], 'fixture metadata', now(), canonical(manifest)))
        summary = context.result_summary(self.db, task['id'])
        self.assertEqual(summary['evaluation']['mode'], 'window')
        self.assertAlmostEqual(summary['evaluation']['annual']['2025']['lake']['rmse'], .06523442125833821)
        self.assertEqual(summary['evaluation']['annual']['2025']['lake']['count'], 255)
        self.assertEqual(summary['unitCount'], 30)
        self.assertFalse(summary['evaluation']['commonDatesCompared'])
        query = {'schemeId': scheme['id'], 'schemeRevision': str(scheme['revision']),
                 'start': '2025-01-01T00:00', 'end': '2026-01-01T00:00'}
        plan = context.simulation_plan(self.db, query)
        self.assertEqual(plan['action'], 'reuse')
        self.assertEqual(plan['inputId'], result['inputId'])
        self.assertEqual(plan['requestedCoverage']['endExclusive'], '2026-01-01T00:00:00')
        with self.assertRaises(ValueError):context.simulation_plan(self.db, {**query, 'schemeRevision':'2.5'})
        business=context.business_context(self.db)
        self.assertEqual(business['defaultResultInputId'], result['inputId'])
        self.assertEqual(business['forcing']['latest']['lastCompleteDay'], '2025-12-31')
        self.assertIsNone(business['observations']['mainStation']['lastValidDay'])
        self.assertFalse(business['capabilities']['arbitraryRevisionRuntimeAvailable'])
        with self.db.connect() as c:
            extended={**manifest, 'end':'2026-10-21T07:00:00', 'hours':33000}
            c.execute('INSERT INTO input_products VALUES(?,?,?,?)', ('INPUT-new-metadata', 'extended metadata', now(), canonical(extended)))
        plan=context.simulation_plan(self.db, {**query, 'inputId':'INPUT-new-metadata', 'end':'2026-10-21T00:00'})
        self.assertEqual(plan['action'], 'rerun')
        self.assertIsNone(plan['resultId'])
        self.assertEqual(plan['calculationStart'], '2023-01-01T00:00:00')
        self.assertTrue(any('输入或方案' in r for r in plan['reasons']))

    def test_actual_grid_candidate_summary_read_only(self):
        workspace=APP/'build/grid-revision-20261026/workspace'
        if not (workspace/'database/hydrology.sqlite3').exists():self.skipTest('development reference database unavailable')
        class ReadOnlyDatabase(Database):
            def __init__(self):
                self.workspace=workspace
                self.path=workspace/'database/hydrology.sqlite3'
            @contextmanager
            def connect(self):
                c=sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True)
                c.row_factory=sqlite3.Row
                c.execute('PRAGMA query_only=ON')
                try:yield c
                finally:c.close()
        summary=context.result_summary(ReadOnlyDatabase(), 'JOB-23ce217f8f464765b150')
        self.assertEqual(summary['evaluation']['mode'], 'continuous')
        self.assertEqual(summary['role'], 'research-candidate')
        self.assertEqual(summary['unitCount'], 1597)
        self.assertEqual(summary['evaluation']['annual']['2026']['flow']['count'], 0)
        self.assertIsNone(summary['evaluation']['annual']['2026']['flow']['nse'])
        self.assertAlmostEqual(summary['evaluation']['annual']['2025']['lake']['rmse'], .6483005736757277)

    def test_http_startup_summary_and_release_rejection(self):
        from backend.server import Platform
        server = Platform(('127.0.0.1', 0), self.folder.name)
        thread=threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        url='http://127.0.0.1:'+str(server.server_port)
        try:
            with urlopen(url+'/api/model/result-summary?'+urlencode({'resultId':current_id()})) as response:
                self.assertEqual(json.load(response)['evaluation']['mode'], 'window')
            artifact_payload={'artifacts':[{'name':'hourly.npz','downloadUrl':'/api/model/task-artifact?taskId=JOB-export&name=hourly.npz',
                                            'bytes':128,'complete':True}], 'daily':[1]*10000}
            with self.db.connect() as c:
                c.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result) VALUES(?,?,?,?,?,?,?)',
                          ('JOB-export','export-result','succeeded',now(),now(),canonical({'researchSchemeId':'SCHEME-fixture','inputId':'INPUT-fixture'}),canonical(artifact_payload)))
            with urlopen(url+'/api/model/tasks') as response:
                rows=json.load(response)['tasks']
            exported=next(row for row in rows if row['id']=='JOB-export')
            self.assertIn('task-artifact',exported['result']['artifacts'][0]['downloadUrl'])
            self.assertEqual(exported['request']['researchSchemeId'],'SCHEME-fixture')
            self.assertNotIn('daily',exported['result'])
            self.assertEqual(exported['termination'],'completed')
            self.assertEqual(exported['heartbeatAt'],exported['updated_at'])
            payload={'expectedReleaseId':registry.current(self.db)['id'], 'evaluationId':'fake', **evidence()}
            request=Request(url+'/api/model/operational-release', canonical(payload).encode('utf-8'), {'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as failure:urlopen(request)
            self.assertEqual(failure.exception.code, 400)
            self.assertIn('后端归档', json.load(failure.exception)['error'])
        finally:
            server.shutdown();server.server_close();server.jobs.executor.shutdown();thread.join(2)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    unittest.main(verbosity=2)
