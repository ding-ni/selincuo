import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.common import canonical, digest, now, sha_file, write_json
from backend.database import Database
from backend.grid_storage import FORMAT
from backend.published_results import available, hourly_zarr
from scripts.assemble_release import bundle_business_result


class PackagedResultZarrTests(unittest.TestCase):
    def test_published_zarr_exports_npz_into_export_task(self):
        import numpy as np
        from backend.grid_storage import Writer
        from backend.maintenance import artifact_file, export_result
        with tempfile.TemporaryDirectory(prefix='published-zarr-export-') as tmp:
            root=Path(tmp);db=Database(root/'workspace')
            source=root/'published';source.mkdir()
            writer=Writer(source,2,1,1)
            writer.put(0,2,{'time':np.array([1,2]),'flow':np.arange(14,dtype=float).reshape(2,7)})
            writer.finish()
            identifier='JOB-published'
            task={'id':identifier,'status':'succeeded','result':{'storageFormat':'zarr-v2','identity':{'inputId':'INPUT-best'}}}
            output=db.workspace/'tasks/JOB-export';output.mkdir(parents=True)
            with patch('backend.business_context.result_task',return_value=task), \
                 patch('backend.business_context.result_folder',return_value=source), \
                 patch('backend.published_results.catalog',return_value=[{'id':identifier}]), \
                 patch('backend.published_results.result_file') as published_file:
                result=export_result(db,{'taskId':identifier,'format':'hourly.npz'},output)
            published_file.assert_not_called()
            self.assertFalse((source/'hourly.npz').exists())
            self.assertEqual(result['artifacts'][0]['storage'],'task')
            self.assertEqual(result['artifacts'][0]['ownerTaskId'],'JOB-export')
            path=artifact_file(db,{'status':'succeeded','result':result},'hourly.npz')
            self.assertEqual(path,output/'hourly.npz')
            with np.load(path,allow_pickle=False) as values:
                np.testing.assert_array_equal(values['time'],[1,2])
                np.testing.assert_array_equal(values['flow'],np.arange(14,dtype=float).reshape(2,7))

    def test_published_result_queries_skip_archive_wide_chunk_hashing(self):
        from backend.result_query import _open_source
        identifier='JOB-read-best'
        task={'id':identifier,'status':'succeeded','result':{'storageFormat':'zarr-v2'}}
        item={'id':identifier,'directory':identifier,'files':{'hourly-manifest.json':{}}}
        with tempfile.TemporaryDirectory(prefix='published-result-read-') as tmp:
            root=Path(tmp)
            checkpoint=root/'final-state.json';write_json(checkpoint,{'identity':{'inputId':'INPUT-best'}})
            zarr=root/identifier/'hourly.zarr'
            with patch('backend.published_results.catalog',return_value=[item]), \
                 patch('backend.published_results.detail',return_value=task), \
                 patch('backend.published_results.result_file',return_value=checkpoint), \
                 patch('backend.published_results.hourly_zarr',return_value=zarr) as published_zarr, \
                 patch('backend.grid_storage.open_arrays',return_value={'time':[1,2]}) as open_arrays:
                loaded_task,arrays,state=_open_source(None,identifier)
        published_zarr.assert_called_once_with(identifier,verify_files=False)
        self.assertIs(loaded_task,task)
        self.assertEqual(arrays['time'],[1,2])
        self.assertEqual(state,{'identity':{'inputId':'INPUT-best'}})

    def test_adopted_task_is_frozen_as_hash_checked_static_zarr_result(self):
        with tempfile.TemporaryDirectory(prefix='packaged-result-zarr-') as tmp:
            root=Path(tmp);workspace=root/'workspace';db=Database(workspace)
            identifier='JOB-packaged-best';input_id='INPUT-best';spatial='AGG-best'
            parameters={'KC':.7};identity={'inputId':input_id,'spatialVersion':spatial}
            task_folder=workspace/'tasks'/identifier;zarr=task_folder/'hourly.zarr'
            (zarr/'time').mkdir(parents=True)
            chunks={'.zattrs':b'{"format":"'+FORMAT.encode()+b'","complete":true}',
                    '.zgroup':b'{"zarr_format":2}',
                    'time/.zarray':b'{"shape":[2],"chunks":[2],"dtype":"<i8"}',
                    'time/0':b'0123456789abcdef'}
            files={}
            for name,contents in chunks.items():
                path=zarr/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(contents)
                files[name]=sha_file(path)
            manifest={'format':FORMAT,'files':files,'sha256':digest(files),'complete':True}
            write_json(task_folder/'hourly-manifest.json',manifest)
            (task_folder/'daily.csv').write_text('date,lakeLevel\n2026-01-01,4544.0\n',encoding='utf-8')
            checkpoint=task_folder/'checkpoint.json';write_json(checkpoint,{'identity':identity})
            result={'identity':identity,'inputId':input_id,'spatialVersion':spatial,'parameters':parameters,
                    'partitionParameters':{},'storageFormat':'zarr-v2','checkpointSHA256':digest({'identity':identity}),
                    'hourlyArtifact':{**manifest}}
            write_json(task_folder/'result.json',result)
            request={'researchSchemeId':'SCHEME-best'}
            with db.connect() as connection:
                connection.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,name,request,result) VALUES(?,?,?,?,?,?,?,?)',
                                   (identifier,'two-stage-calibration','succeeded',now(),now(),'best',canonical(request),canonical(result)))
            business={'resultId':identifier,'name':'色林错连续模拟方案','inputId':input_id,'spatialVersion':spatial,
                      'resultIdentitySHA256':digest(identity),
                      'parameterPayload':{'parameters':parameters,'partitionParameters':{}}}
            entry=bundle_business_result(workspace,business,root)
            self.assertEqual(entry['id'],identifier)
            self.assertTrue(entry['businessDefault'])
            self.assertIn('hourly.zarr/time/0',entry['files'])
            with patch('backend.published_results.ROOT',root), \
                 patch('backend.published_results.catalog',return_value=[entry]):
                self.assertTrue(available(identifier))
                self.assertEqual(hourly_zarr(identifier,verify_files=True),root/'data/final-results'/identifier/'hourly.zarr')
                chunk=root/'data/final-results'/identifier/'hourly.zarr/time/0'
                chunk.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError,'摘要校验失败'):
                    hourly_zarr(identifier,verify_files=True)


if __name__=='__main__':unittest.main()
