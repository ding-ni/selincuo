import copy
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend.common import canonical,now,write_json
from backend.database import Database
from backend.grid_storage import Writer
from backend import result_query as query,grid_storage


class ResultQueryCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.db=Database(self.root/'one')
        query.clear_source_cache();self.addCleanup(query.clear_source_cache)

    def fixture(self,db=None,identifier='same',offset=0):
        db=db or self.db;folder=db.workspace/'tasks'/identifier;folder.mkdir(parents=True)
        writer=Writer(folder,49,2,3)
        arrays={key:np.arange(np.prod(writer.root[key].shape),dtype=writer.root[key].dtype).reshape(writer.root[key].shape)+offset for key in writer.root.array_keys()}
        arrays['time']=pd.date_range('2024-01-01',periods=49,freq='h').asi8
        writer.put(0,49,arrays);artifact=writer.finish()
        units=[{'id':'u1','partition':'duja'},{'id':'u2','partition':'ali'}]
        result={'units':units,'spatialVersion':'fixture','identity':{'spatialVersion':'fixture','modelImplementation':'fixture'},
                'storageFormat':'zarr-v2','hourlyArtifact':artifact,'partitions':['duja','ali','fringe']}
        state={'identity':{'spatialVersion':'fixture','modelImplementation':'fixture'},'unitStates':[[0]*8]*2}
        write_json(folder/'checkpoint.json',state)
        with db.connect() as c:
            c.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result) VALUES(?,?,?,?,?,?,?)',
                      (identifier,'simulation','succeeded',now(),now(),'{}',canonical(result)))
        return folder,arrays

    def test_zarr_values_metadata_frame_and_series_match_without_reopening(self):
        folder,arrays=self.fixture()
        real=grid_storage.open_arrays
        with patch('backend.grid_storage.open_arrays',wraps=real) as opened,patch('backend.grid_spatial.load',return_value={}),patch.object(query,'spatial_base',return_value='/fixture/'),patch.object(query,'compatible',return_value=False):
            meta=query.metadata(self.db,'same')
            self.assertEqual(meta['hours'],49)
            self.assertEqual(meta['start'],'2024-01-01T00:00:00')
            frame=query.read(self.db,{'resultId':'same','variable':'swe','time':'2024-01-02T00:00:00','step':'day'},frame=True)
            self.assertEqual([row['value'] for row in frame['values']],arrays['unitStates'][24,:,0].tolist())
            series=query.read(self.db,{'resultId':'same','variable':'WU','unit':'1','start':'2024-01-01','end':'2024-01-03','step':'day'})
            self.assertEqual([row['value'] for row in series['rows']],arrays['unitStates'][[24,48],1,2].tolist())
            query.metadata(self.db,'same')
            self.assertEqual(opened.call_count,1)
        info=query.source_cache_info()
        self.assertEqual(info['entries'],1);self.assertEqual(info['zarrHandles'],1)
        self.assertEqual(info['timeAxisBytes'],49*8)
        self.assertEqual(info['legacyArrayBytes'],0)

    def test_task_update_checkpoint_manifest_and_time_chunk_stat_invalidate(self):
        folder,arrays=self.fixture();real=query._open_source
        with patch.object(query,'_open_source',wraps=real) as opened:
            query.source(self.db,'same');query.source(self.db,'same');self.assertEqual(opened.call_count,1)
            with self.db.connect() as c:c.execute("UPDATE tasks SET updated_at=?,name=? WHERE id='same'",('2099-01-01','changed'))
            self.assertEqual(query.source(self.db,'same')[0]['name'],'changed');self.assertEqual(opened.call_count,2)
            state=json.loads((folder/'checkpoint.json').read_text());state['marker']='checkpoint-change';write_json(folder/'checkpoint.json',state)
            self.assertEqual(query.source(self.db,'same')[2]['marker'],'checkpoint-change');self.assertEqual(opened.call_count,3)
            manifest=json.loads((folder/'hourly-manifest.json').read_text());manifest['testRevision']=1;write_json(folder/'hourly-manifest.json',manifest)
            query.source(self.db,'same');self.assertEqual(opened.call_count,4)
            import zarr
            mutable=zarr.open_group(str(folder/'hourly.zarr'),mode='r+');new_time=arrays['time']+3600*10**9;mutable['time'][:]=new_time
            self.assertEqual(query._entry(self.db,'same')['times'][0],pd.Timestamp('2024-01-01T01:00:00'))
            self.assertEqual(opened.call_count,5)

    def test_same_id_in_different_workspaces_never_shares_source(self):
        self.fixture(offset=0);other=Database(self.root/'two');self.fixture(other,offset=100)
        one=query.read(self.db,{'resultId':'same','variable':'swe','time':'2024-01-01T00:00:00','step':'hour'},frame=True)
        two=query.read(other,{'resultId':'same','variable':'swe','time':'2024-01-01T00:00:00','step':'hour'},frame=True)
        self.assertEqual(two['values'][0]['value']-one['values'][0]['value'],100)
        self.assertEqual(query.source_cache_info()['entries'],2)

    def test_concurrent_same_cold_source_builds_once_and_capacity_is_eight(self):
        self.fixture();real=query._open_source;barrier=threading.Barrier(8)
        def slow(*args):time.sleep(.04);return real(*args)
        def read():barrier.wait();return query._entry(self.db,'same')['times'][0]
        with patch.object(query,'_open_source',side_effect=slow) as opened,ThreadPoolExecutor(max_workers=8) as executor:
            values=list(executor.map(lambda _:read(),range(8)))
            self.assertEqual(len(set(values)),1);self.assertEqual(opened.call_count,1)
        for i in range(9):
            self.fixture(identifier='result-'+str(i));query.source(self.db,'result-'+str(i))
        self.assertEqual(query.source_cache_info()['entries'],8)
        self.assertEqual(query.source_cache_info()['timeAxisBytes'],8*49*8)

    def test_failed_status_invalidates_and_never_returns_old_success(self):
        self.fixture();query.source(self.db,'same')
        with self.db.connect() as c:c.execute("UPDATE tasks SET status='failed' WHERE id='same'")
        with self.assertRaisesRegex(ValueError,'已完成'):
            query.source(self.db,'same')

    def test_legacy_npz_replacement_invalidates_and_array_retention_is_two(self):
        def legacy(identifier,offset):
            folder=self.db.workspace/'tasks'/identifier;folder.mkdir(parents=True)
            np.savez(folder/'hourly.npz',time=pd.date_range('2024-01-01',periods=2,freq='h').asi8,
                     lakeLevel=np.array([4544.,4544.1]),unitStates=np.full((2,1,8),offset,dtype='f8'))
            write_json(folder/'checkpoint.json',{'identity':{'modelImplementation':'fixture'}})
            with self.db.connect() as c:
                c.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result) VALUES(?,?,?,?,?,?,?)',
                          (identifier,'simulation','succeeded',now(),now(),'{}',canonical({'units':[{'id':'unit'}]})))
            return folder
        first=legacy('legacy-0',1);query.source(self.db,'legacy-0')
        np.savez(first/'hourly.npz',time=pd.date_range('2024-01-01',periods=2,freq='h').asi8,
                 lakeLevel=np.array([4544.,4544.1]),unitStates=np.full((2,1,8),99,dtype='f8'))
        self.assertEqual(query.source(self.db,'legacy-0')[1]['unitStates'][0,0,0],99)
        for i in range(1,4):legacy('legacy-'+str(i),i);query.source(self.db,'legacy-'+str(i))
        self.assertEqual(query.source_cache_info()['legacyArrayEntries'],2)
        self.assertEqual(query.source_cache_info()['entries'],2)


if __name__=='__main__':unittest.main()
