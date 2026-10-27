import json,tempfile,unittest
from pathlib import Path
import numpy as np
import pandas as pd
from backend.database import Database
from backend.common import ROOT,read_json,canonical,sha_file,now
from backend.forcing_extension import extend
from backend.forcing import load_input

class ForcingExtensionTests(unittest.TestCase):
 def product(self,db,identifier,start,values):
  folder=db.workspace/'inputs/products'/identifier;folder.mkdir(parents=True)
  times=pd.date_range(start,periods=len(values),freq='h')
  array=np.array(values,dtype=float)[:,None]
  path=folder/'forcing.npz'
  np.savez_compressed(path,time=times.values.astype('datetime64[ns]').astype('int64'),precipitation=array,temperature=array,evaporation=array)
  m={'id':identifier,'name':identifier,'projectId':'selinco','createdAt':now(),'start':times[0].isoformat(),'end':times[-1].isoformat(),'hours':len(times),'spatialVersion':read_json(ROOT/'data/derived/spatial/manifest.json')['version'],'data':'forcing.npz','dataSHA256':sha_file(path)}
  (folder/'manifest.json').write_text(canonical(m),encoding='utf-8')
  with db.connect() as c:c.execute('INSERT INTO input_products VALUES(?,?,?,?)',(identifier,identifier,now(),canonical(m)))
  return m
 def test_extend_reuses_matching_history_and_rejects_gaps_or_conflicts(self):
  with tempfile.TemporaryDirectory() as tmp:
   db=Database(tmp)
   first=self.product(db,'OLD','2026-01-01T01:00',[1,2,3,4])
   new=self.product(db,'NEW','2026-01-01T04:00',[4,5,6,7])
   merged=extend(db,new);self.assertEqual(merged['hours'],7)
   _,arrays=load_input(db,merged['id']);np.testing.assert_array_equal(arrays['precipitation'][:,0],[1,2,3,4,5,6,7])
   self.assertEqual(sha_file(db.workspace/'inputs/products/OLD/forcing.npz'),first['dataSHA256'])
   self.assertEqual(extend(db,new)['id'],merged['id'])
   gap=self.product(db,'GAP','2026-01-03T01:00',[1])
   with self.assertRaisesRegex(ValueError,'缺测'):extend(db,gap)
   conflict=self.product(db,'CONFLICT','2026-01-01T04:00',[99])
   with self.assertRaisesRegex(ValueError,'不一致'):extend(db,conflict)
if __name__=='__main__':unittest.main()
