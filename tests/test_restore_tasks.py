import io
import json
import hashlib
import shutil
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.common import canonical, now, sha_file, write_json
from backend.database import Database
from backend.jobs import Jobs
from backend import maintenance, server


class RestoreTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.app=self.root/'app'
        write_json(self.app/'data/derived/spatial/manifest.json',{'version':'fixture-space'})
        self.db=Database(self.root/'source')
        self.patch_root=patch.multiple(maintenance,ROOT=self.app)
        self.patch_root.start();self.addCleanup(self.patch_root.stop)
        self.patch_jobs=patch('backend.jobs.ROOT',self.app)
        self.patch_jobs.start();self.addCleanup(self.patch_jobs.stop)

    def archive(self, bad_sha=False):
        with self.db.connect() as c:c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        values={'workspace-identity.json':(self.db.workspace/'workspace-identity.json').read_bytes(),
                'database/hydrology.sqlite3':self.db.path.read_bytes(),'payload.bin':b'x'*(2*1024*1024+17)}
        files=[{'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in values.items()]
        if bad_sha:files[-1]['sha256']='0'*64
        manifest={'projectId':'selinco','schemaVersion':1,'spatialVersion':'fixture-space','files':files}
        archive=self.db.workspace/'incoming/fixture.zip';archive.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as zipped:
            for name,data in values.items():zipped.writestr(name,data)
            zipped.writestr('backup-manifest.json',canonical(manifest))
        return archive,manifest

    def backup_task(self, archive):
        path=self.db.workspace/'exports/fixture.zip';path.parent.mkdir(exist_ok=True);shutil.copy2(archive,path)
        artifact={'name':path.name,'filename':path.name,'storage':'exports','ownerTaskId':'backup','bytes':path.stat().st_size,
                  'sha256':sha_file(path),'mtimeNs':path.stat().st_mtime_ns,'complete':True}
        with self.db.connect() as c:
            c.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result) VALUES(?,?,?,?,?,?,?)',
                      ('backup','backup','succeeded',now(),now(),'{}',canonical({'artifacts':[artifact]})))
        return artifact

    def wait(self,jobs,identifier):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            task=self.db.task(identifier)
            if task['status'] in ('succeeded','failed','cancelled'):return task
            time.sleep(.02)
        self.fail('restore task timed out')

    def jobs(self):
        jobs=Jobs(self.db)
        self.addCleanup(lambda:jobs.executor.shutdown(wait=True))
        return jobs

    def test_zip_upload_limit_space_and_megabyte_reads(self):
        self.assertEqual(server.upload_size_limit('.zip'),50*1024**3)
        self.assertEqual(server.upload_size_limit('.nc'),4*1024**3)
        destination=self.root/'upload/test.zip'
        with patch('backend.server.shutil.disk_usage',return_value=SimpleNamespace(free=1024**4)):
            with self.assertRaisesRegex(ValueError,'4GiB'):
                server.receive_upload(io.BytesIO(),destination,4*1024**3+1,'.nc')
            with self.assertRaisesRegex(ValueError,'未完成'):
                server.receive_upload(io.BytesIO(),destination,4*1024**3+1,'.zip')
        self.assertFalse(destination.exists())
        with patch('backend.server.shutil.disk_usage',return_value=SimpleNamespace(free=1)),self.assertRaisesRegex(ValueError,'空间不足'):
            server.receive_upload(io.BytesIO(b'x'),self.root/'upload/no-space.zip',1,'.zip')
        reads=[]
        class Reader(io.BytesIO):
            def read(self,size=-1):reads.append(size);return super().read(size)
        data=b'x'*(3*1024*1024+7)
        server.receive_upload(Reader(data),self.root/'upload/valid.zip',len(data),'.zip')
        self.assertLessEqual(max(reads),1024*1024)
        self.assertEqual((self.root/'upload/valid.zip').read_bytes(),data)

    def test_successful_restore_from_local_backup_is_queued_and_validated(self):
        archive,manifest=self.archive();self.backup_task(archive)
        preview=maintenance.restore_request_preview(self.db,{'backupTaskId':'backup'})
        self.assertEqual(preview['backupTaskId'],'backup')
        jobs=self.jobs()
        request={'backupTaskId':'backup','token':preview['token'],'clientRequestId':'restore-id'}
        task=jobs.submit('restore-workspace',request)
        self.assertEqual(jobs.submit('restore-workspace',request)['id'],task['id'])
        result=self.wait(jobs,task['id'])
        self.assertEqual(result['status'],'succeeded',result['message'])
        self.assertTrue(result['result']['restartRequired'])
        self.assertEqual(result['result']['validatedSHA256Files'],len(manifest['files']))
        self.assertEqual(result['result']['databaseIntegrity'],'ok')
        compact=server.Handler.get(SimpleNamespace(path='/api/model/tasks',db=self.db,send_json=lambda value:value))
        compact_restore=next(row for row in compact['tasks'] if row['id']==task['id'])
        self.assertTrue(compact_restore['result']['restartRequired'])
        self.assertEqual(compact_restore['result']['workspace'],result['result']['workspace'])
        self.assertTrue(self.db.path.is_file())
        pointer=json.loads((self.app/'config/active-workspace.json').read_text(encoding='utf-8'))
        self.assertEqual((self.app/pointer['workspace']).resolve(),Path(result['result']['workspace']))
        with self.assertRaisesRegex(ValueError,'重启'):
            jobs.submit('backup',{})

    def test_cancelled_queued_restore_does_not_switch_pointer(self):
        archive,_=self.archive();preview=maintenance.restore_request_preview(self.db,{'filename':archive.name})
        jobs=self.jobs();gate=threading.Event();jobs.executor.submit(lambda:gate.wait(5))
        task=jobs.submit('restore-workspace',{'filename':archive.name,'token':preview['token']})
        with self.assertRaisesRegex(ValueError,'恢复'):
            jobs.submit('backup',{})
        jobs.stop(task['id']);gate.set()
        self.assertEqual(self.wait(jobs,task['id'])['status'],'cancelled')
        self.assertFalse((self.app/'config/active-workspace.json').exists())

    def test_failed_sha_or_running_cancel_preserves_active_pointer(self):
        archive,_=self.archive(bad_sha=True)
        original={'workspace':'old'};write_json(self.app/'config/active-workspace.json',original)
        preview=maintenance.restore_request_preview(self.db,{'filename':archive.name});jobs=self.jobs()
        task=jobs.submit('restore-workspace',{'filename':archive.name,'token':preview['token']})
        self.assertEqual(self.wait(jobs,task['id'])['status'],'failed')
        self.assertEqual(json.loads((self.app/'config/active-workspace.json').read_text(encoding='utf-8')),original)
        archive,_=self.archive();cancelled=[False];progress=[]
        def stop_on_chunk(value,message):progress.append((value,message));cancelled[0]=True
        with self.assertRaises(InterruptedError):
            maintenance.restore_workspace(archive,self.root/'cancelled',stop_on_chunk,lambda:cancelled[0])
        self.assertTrue(progress)
        self.assertEqual(json.loads((self.app/'config/active-workspace.json').read_text(encoding='utf-8')),original)

    def test_bad_token_absolute_path_and_insufficient_restore_disk_fail(self):
        archive,_=self.archive();jobs=self.jobs()
        with self.assertRaisesRegex(ValueError,'预览'):
            jobs.submit('restore-workspace',{'filename':archive.name,'token':'wrong'})
        for request in ({'filename':'C:/elsewhere.zip'},{'filename':'../elsewhere.zip'},{'filename':archive.name,'backupTaskId':'backup'}):
            with self.assertRaises(ValueError):maintenance.restore_request_preview(self.db,request)
        with patch('backend.maintenance.shutil.disk_usage',return_value=SimpleNamespace(free=1)),self.assertRaisesRegex(ValueError,'空间不足'):
            maintenance.restore_workspace(archive,self.root/'no-space')
        self.assertFalse((self.app/'config/active-workspace.json').exists())

    def test_pointer_write_failure_does_not_activate_or_report_success(self):
        archive,_=self.archive();preview=maintenance.restore_request_preview(self.db,{'filename':archive.name})
        original={'workspace':'old'};write_json(self.app/'config/active-workspace.json',original)
        from backend import jobs as jobs_module
        real_write=jobs_module.write_json
        def reject_pointer(path,value):
            if Path(path)==self.app/'config/active-workspace.json':raise OSError('fixture pointer disk failure')
            return real_write(path,value)
        jobs=self.jobs()
        with patch('backend.jobs.write_json',side_effect=reject_pointer):
            task=jobs.submit('restore-workspace',{'filename':archive.name,'token':preview['token']})
            failed=self.wait(jobs,task['id'])
        self.assertEqual(failed['status'],'failed')
        self.assertIn('pointer disk failure',failed['message'])
        self.assertEqual(json.loads((self.app/'config/active-workspace.json').read_text(encoding='utf-8')),original)
        self.assertFalse(jobs.restart_required)

    def test_pending_and_completed_restore_block_all_old_workspace_writes(self):
        for pending,restart in ((True,False),(False,True)):
            jobs=SimpleNamespace(restore_pending=pending,restart_required=restart)
            for path in ('data-import','data-import-file','coverage','parameters','schemes','operational-release','states/one','tasks/one/metadata','tasks/one/cleanup-execute','backup','restore'):
                with self.assertRaisesRegex(ValueError,'旧工作区暂停写入'):
                    server.guard_restore_writes(jobs,path)
            for path in ('tasks','restore-preview','tasks/one/stop','shutdown'):
                server.guard_restore_writes(jobs,path)
            with self.assertRaisesRegex(ValueError,'旧工作区暂停写入'):
                server.guard_restore_writes(jobs,'upload',uploading=True)
        server.guard_restore_writes(SimpleNamespace(restore_pending=False,restart_required=False),'coverage')


if __name__=='__main__':unittest.main()
