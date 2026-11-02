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
