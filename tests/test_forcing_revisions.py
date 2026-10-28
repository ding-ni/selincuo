import tempfile,unittest
from contextlib import nullcontext
from pathlib import Path
import numpy as np
import pandas as pd
from backend.database import Database
from backend.common import ROOT,read_json,write_json,sha_file,canonical,now
from backend.forcing_revisions import merge_products
from backend.forcing import load_input

class OfficialRevisionTests(unittest.TestCase):
    def product(self,db,identifier,start,count,shift=0.):
        times=pd.date_range(start,periods=count,freq='h');folder=db.workspace/'inputs/products'/identifier;folder.mkdir(parents=True)
        values=np.arange(count,dtype=float)[:,None]+shift
        np.savez_compressed(folder/'forcing.npz',time=times.asi8,precipitation=values,temperature=values+10,evaporation=values*.01)
        value={'id':identifier,'projectId':'selinco','spatialVersion':read_json(ROOT/'data/derived/spatial/manifest.json')['version'],'conversionVersion':'test-hours-v1','data':'forcing.npz','dataSHA256':sha_file(folder/'forcing.npz'),'start':times[0].isoformat(),'end':times[-1].isoformat(),'hours':count,'sources':[]}
        write_json(folder/'manifest.json',value)
        with db.connect() as c:c.execute('INSERT INTO input_products VALUES(?,?,?,?)',(identifier,identifier,now(),canonical(value)))
        return value
    def test_revised_overlap_creates_new_input_without_overwriting_parent(self):
        with nullcontext(tempfile.mkdtemp(prefix='selinco-forcing-revision-')) as temp:
            db=Database(temp);base=self.product(db,'base','2025-01-01',4);fresh=self.product(db,'fresh','2025-01-01T02:00',4,shift=20.)
            merged=merge_products(db,'base','fresh');_,a=load_input(db,merged['id'])
            self.assertEqual(a['precipitation'][:,0].tolist(),[0.,1.,20.,21.,22.,23.])
            self.assertEqual(merged['revisionSelection']['changedHistoricalHours'],2)
            self.assertEqual(sha_file(db.workspace/'inputs/products/base/forcing.npz'),base['dataSHA256'])
            self.assertEqual(merge_products(db,'base','fresh')['id'],merged['id'])
    def test_exact_extension_and_gap_rejection(self):
        with nullcontext(tempfile.mkdtemp(prefix='selinco-forcing-gap-')) as temp:
            db=Database(temp);self.product(db,'base','2025-01-01',4);self.product(db,'next','2025-01-01T02:00',4,shift=2.)
            merged=merge_products(db,'base','next');self.assertEqual(merged['revisionSelection']['changedHistoricalHours'],0)
            self.product(db,'gap','2025-01-02',2)
            with self.assertRaises(ValueError):merge_products(db,'base','gap')
if __name__=='__main__':unittest.main()
