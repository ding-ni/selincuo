"""Small real artifacts exercise the same queue/streaming paths as large exports."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import numpy as np
from test_business_registry import BASE, Database, canonical, digest, now
from backend.jobs import Jobs
from backend import maintenance, grid_storage


class BackgroundArtifactTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(prefix='artifact-contract-',dir=BASE)
        self.db=Database(self.folder.name)
        self.jobs=Jobs(self.db)

    def tearDown(self):
        self.jobs.executor.shutdown(wait=True)
        self.folder.cleanup()

    def source(self,zarr=False):
        identifier='JOB-source-fixture'
        folder=self.db.workspace/'tasks'/identifier;folder.mkdir(parents=True)
        result={'identity':{'modelImplementation':'fixture-only'},'storageFormat':'zarr-v2' if zarr else 'npz'}
        if zarr:
            writer=grid_storage.Writer(folder,6,2,3)
            self.expected={key:np.arange(np.prod(writer.root[key].shape),dtype=writer.root[key].dtype).reshape(writer.root[key].shape)
                           for key in writer.root.array_keys()}
            writer.put(0,6,self.expected);result['hourlyArtifact']=writer.finish()
        else:np.savez(folder/'hourly.npz',lakeLevel=np.arange(16,dtype='f8'))
        (folder/'daily.csv').write_text('date,value\n2025-01-01,1\n',encoding='utf-8')
        with self.db.connect() as c:
            c.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result) VALUES(?,?,?,?,?,?,?)',
                      (identifier,'simulation','succeeded',now(),now(),'{}',canonical(result)))
        return identifier,folder

    def wait(self,identifier):
        limit=time.monotonic()+10
        while time.monotonic()<limit:
            task=self.db.task(identifier)
            if task['status'] not in ('running','queued','stopping'):return task
            time.sleep(.025)
        self.fail('small fixture task exceeded 10s')

    def test_queue_returns_promptly_deduplicates_and_download_validates(self):
        source,_=self.source()
        gate=threading.Event();self.jobs.executor.submit(gate.wait,5)
        started=time.monotonic()
        task=self.jobs.submit('export-result',{'taskId':source,'format':'hourly.npz','clientRequestId':'one'})
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual(task['status'],'queued')
        duplicate=self.jobs.submit('export-result',{'taskId':source,'format':'hourly.npz','clientRequestId':'two'})
        self.assertEqual(task['id'],duplicate['id'])
        with self.assertRaises(ValueError):maintenance.artifact_file(self.db,self.db.task(task['id']),'hourly.npz')
        gate.set();finished=self.wait(task['id'])
        self.assertEqual(finished['status'],'succeeded',finished['message'])
        artifact=finished['result']['artifacts'][0]
        self.assertTrue(artifact['complete']);self.assertEqual(len(artifact['sha256']),64)
        output=maintenance.artifact_file(self.db,finished,'hourly.npz')
        np.testing.assert_array_equal(np.load(output)['lakeLevel'],np.arange(16))
        again=self.jobs.submit('export-result',{'taskId':source,'format':'hourly.npz'})
        self.assertEqual(again['id'],task['id'])
        output.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'已变化'):maintenance.artifact_file(self.db,finished,'hourly.npz')

    def test_grid_export_preserves_all_float64_arrays_without_writing_source(self):
        source,folder=self.source(zarr=True)
        before={p.relative_to(folder).as_posix():p.stat().st_mtime_ns for p in folder.rglob('*') if p.is_file()}
        task=self.jobs.submit('export-result',{'taskId':source,'format':'hourly.npz'})
        finished=self.wait(task['id']);self.assertEqual(finished['status'],'succeeded',finished['message'])
        path=maintenance.artifact_file(self.db,finished,'hourly.npz')
        self.assertEqual(path.parent.name,task['id'])
        self.assertFalse((folder/'hourly.npz').exists())
        self.assertEqual(before,{p.relative_to(folder).as_posix():p.stat().st_mtime_ns for p in folder.rglob('*') if p.is_file()})
        with np.load(path,allow_pickle=False) as actual:
            self.assertEqual(set(actual.files),set(self.expected))
            for key,value in self.expected.items():
                np.testing.assert_array_equal(actual[key],value)
                self.assertEqual(actual[key].dtype,value.dtype)

    def test_cancellation_and_restart_never_publish_partial_artifacts(self):
        source,folder=self.source(zarr=True)
        destination=self.db.workspace/'cancelled-output'
        calls=[0]
        def cancelled():calls[0]+=1;return calls[0]>2
        with self.assertRaises(InterruptedError):grid_storage.export_npz(folder,destination,cancelled=cancelled)
        self.assertFalse((destination/'hourly.npz').exists())
        self.assertTrue((destination/'hourly-export.partial').exists())
        gate=threading.Event();self.jobs.executor.submit(gate.wait,5)
        task=self.jobs.submit('export-result',{'taskId':source,'format':'daily.csv'})
        self.jobs.stop(task['id']);gate.set();stopped=self.wait(task['id'])
        self.assertEqual(stopped['status'],'cancelled')
        self.assertEqual(Jobs.public(stopped)['termination'],'user-stopped')
        with self.db.connect() as c:c.execute("UPDATE tasks SET status='running' WHERE id=?",(task['id'],))
        another=Jobs(self.db)
        try:self.assertEqual(Jobs.public(self.db.task(task['id']))['termination'],'service-restarted')
        finally:another.executor.shutdown(wait=True)

    def test_backup_is_complete_restorable_and_stoppable(self):
        self.source()
        self.db.set_setting('sentinel',{'restore':27})
        task=self.jobs.submit('backup',{})
        finished=self.wait(task['id']);self.assertEqual(finished['status'],'succeeded',finished['message'])
        artifact=finished['result']['artifacts'][0]
        path=maintenance.artifact_file(self.db,finished,artifact['name'])
        manifest=maintenance.restore_preview(path)
        self.assertTrue(any(r['path']=='database/hydrology.sqlite3' for r in manifest['files']))
        restored=self.db.workspace/'restored'
        maintenance.restore_workspace(path,restored)
        restored_db=Database(restored)
        self.assertEqual(restored_db.setting('sentinel'),{'restore':27})
        before=list((self.db.workspace/'exports').glob('*.zip'))
        with self.assertRaises(InterruptedError):maintenance.backup_workspace(self.db,cancelled=lambda:True)
        self.assertEqual(before,list((self.db.workspace/'exports').glob('*.zip')))

    def test_long_task_heartbeat_and_failed_export_remain_explicit(self):
        source,_=self.source()
        actual=maintenance.export_result
        def delayed(*args,**kwargs):time.sleep(2.3);return actual(*args,**kwargs)
        with patch.object(maintenance,'export_result',delayed):
            task=self.jobs.submit('export-result',{'taskId':source,'format':'daily.csv'})
            finished=self.wait(task['id'])
        self.assertEqual(finished['status'],'succeeded',finished['message'])
        self.assertNotEqual(finished['created_at'],finished['updated_at'])
        with patch.object(maintenance,'export_result',side_effect=OSError('fixture disk full')):
            task=self.jobs.submit('export-result',{'taskId':source,'format':'checkpoint.json'})
            failed=self.wait(task['id'])
        self.assertEqual(failed['status'],'failed')
        self.assertIn('disk full',failed['message'])
        self.assertIsNone(failed['result'])
        with self.assertRaises(ValueError):maintenance.artifact_file(self.db,failed,'checkpoint.json')

    def test_published_checkpoint_maps_to_packaged_final_state(self):
        from backend.published_results import current_id
        task=self.jobs.submit('export-result',{'taskId':current_id(),'format':'checkpoint.json'})
        finished=self.wait(task['id'])
        self.assertEqual(finished['status'],'succeeded',finished['message'])
        artifact=finished['result']['artifacts'][0]
        self.assertEqual(artifact['name'],'checkpoint.json')
        self.assertEqual(artifact['filename'],'final-state.json')
        path=maintenance.artifact_file(self.db,finished,'checkpoint.json')
        self.assertEqual(path.name,'final-state.json')


if __name__=='__main__':unittest.main(verbosity=2)
