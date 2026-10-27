"""Tiny existing-workspace fixtures; upgrades never import observations/releases."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import os

BASE=Path(__file__).resolve().parents[1]
APP=Path(os.environ.get('SELINCO_TEST_APP_ROOT',str(BASE if (BASE/'backend/common.py').exists() else 'F:/色林错预警预报平台')))
os.environ.setdefault('SELINCO_APP_ROOT',str(APP))
if not (BASE/'backend/common.py').exists():
    package=sys.modules.get('backend') or types.ModuleType('backend')
    package.__path__=[str(BASE/'backend'),str(APP/'backend')];sys.modules['backend']=package
elif str(APP) not in sys.path:sys.path.insert(0,str(APP))
from backend.common import sha_file,write_json,canonical
from backend.workspace_upgrade import supplement_default_input,idle_workspace
from backend.database import Database


class WorkspaceUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.workspace=self.root/'old';self.db=Database(self.workspace)
        self.seed=self.root/'seed';self.app=self.root/'app'
        source=self.seed/'inputs/products/INPUT-frozen';source.mkdir(parents=True)
        (source/'forcing.npz').write_bytes(b'tiny frozen input data')
        self.manifest={'id':'INPUT-frozen','projectId':'selinco','data':'forcing.npz',
                       'dataSHA256':sha_file(source/'forcing.npz'),'spatialVersion':'SPATIAL-test','name':'frozen'}
        write_json(source/'manifest.json',self.manifest)
        write_json(self.seed/'seed-report.json',{'schemaVersion':2,'projectId':'selinco','defaultInputId':'INPUT-frozen',
                   'inputManifest':self.manifest,'spatialVersion':'SPATIAL-test',
                   'resultIdentity':{'inputId':'INPUT-frozen','inputSHA256':self.manifest['dataSHA256']}})
        write_json(self.app/'data/derived/spatial/manifest.json',{'version':'SPATIAL-test'})
        self.db.set_setting('release-sentinel',{'result':'keep'})

    def tearDown(self):self.temp.cleanup()

    def test_missing_default_added_once_with_backup_and_existing_records_unchanged(self):
        with self.db.connect() as c:
            c.execute("INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result) VALUES('JOB-keep','simulation','succeeded','old','old','{}','{}')")
            c.execute("INSERT INTO schemes VALUES('SCHEME-keep','keep','old','{}')")
            c.execute('CREATE TABLE operational_releases(id TEXT PRIMARY KEY,payload TEXT)')
            c.execute("INSERT INTO operational_releases VALUES('RELEASE-keep','unchanged')")
        before=self.db.summary()['counts']
        with idle_workspace(self.workspace):result=supplement_default_input(self.workspace,self.seed,self.app)
        self.assertTrue(result['changed']);self.assertTrue(Path(result['backup']).is_file())
        self.assertEqual(self.db.setting('release-sentinel'),{'result':'keep'})
        self.assertEqual(self.db.task('JOB-keep')['status'],'succeeded')
        self.assertEqual(self.db.items('schemes')[0]['id'],'SCHEME-keep')
        self.assertEqual(self.db.rows('SELECT * FROM operational_releases'),[{'id':'RELEASE-keep','payload':'unchanged'}])
        after=self.db.summary()['counts']
        self.assertEqual(after,{**before,'input_products':1})
        with sqlite3.connect(result['backup']) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM input_products').fetchone()[0],0)
        self.assertFalse(supplement_default_input(self.workspace,self.seed,self.app)['changed'])

    def test_conflicting_directory_and_same_id_manifest_never_overwritten(self):
        destination=self.workspace/'inputs/products/INPUT-frozen';destination.mkdir(parents=True)
        (destination/'sentinel').write_text('keep')
        with self.assertRaisesRegex(ValueError,'目录已存在'):supplement_default_input(self.workspace,self.seed,self.app)
        self.assertEqual((destination/'sentinel').read_text(),'keep')
        with self.db.connect() as c:
            c.execute('INSERT INTO input_products VALUES(?,?,?,?)',('INPUT-frozen','old','old',canonical({**self.manifest,'dataSHA256':'different'})))
        with self.assertRaisesRegex(ValueError,'同ID'):supplement_default_input(self.workspace,self.seed,self.app)

    def test_corrupt_seed_and_unknown_workspace_rejected(self):
        source=self.seed/'inputs/products/INPUT-frozen/forcing.npz';source.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'摘要校验'):supplement_default_input(self.workspace,self.seed,self.app)
        self.assertEqual(self.db.items('input_products'),[])
        with self.assertRaisesRegex(ValueError,'合法身份'):supplement_default_input(self.root/'foreign',self.seed,self.app)

    def test_stale_running_request_preserved_then_jobs_recovers_interrupted(self):
        with self.db.connect() as c:
            c.execute("INSERT INTO tasks(id,kind,status,created_at,updated_at,request) VALUES('JOB-running','simulation','running','old','old',?)",(canonical({'inputId':'INPUT-original','parameters':{'KC':.7}}),))
        original=self.db.task('JOB-running')['request']
        with idle_workspace(self.workspace):result=supplement_default_input(self.workspace,self.seed,self.app)
        self.assertTrue(result['changed'])
        self.assertEqual(self.db.task('JOB-running')['status'],'running')
        self.assertEqual(self.db.task('JOB-running')['request'],original)
        from backend.jobs import Jobs
        jobs=Jobs(self.db)
        try:
            self.assertEqual(self.db.task('JOB-running')['status'],'interrupted')
            self.assertEqual(self.db.task('JOB-running')['request'],original)
        finally:jobs.executor.shutdown(wait=True)

    @unittest.skipUnless(os.name=='nt','Windows instance lock')
    def test_running_workspace_lock_rejects_upgrade(self):
        with idle_workspace(self.workspace):
            with self.assertRaisesRegex(ValueError,'已有服务运行'):
                with idle_workspace(self.workspace):pass

    def test_launcher_reuses_same_version_and_rejects_old_version(self):
        path=BASE/'app_entry.py'
        spec=importlib.util.spec_from_file_location('upgrade_launcher_fixture',path)
        launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
        from io import BytesIO
        health={'projectId':'selinco','workspace':str(self.workspace),'version':'same'}
        with patch('urllib.request.urlopen',return_value=BytesIO(json.dumps(health).encode())):
            self.assertTrue(launcher.running_instance(8879,self.workspace,'same'))
        with patch('urllib.request.urlopen',return_value=BytesIO(json.dumps(health).encode())):
            with self.assertRaisesRegex(ValueError,'其他版本'):launcher.running_instance(8879,self.workspace,'new')


if __name__=='__main__':unittest.main(verbosity=2)
