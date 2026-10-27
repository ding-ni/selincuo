import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import pandas as pd
from backend.dashboard import map_frame

class MapRainfallTests(unittest.TestCase):
    def test_themed_units_and_corrected_lake_reconstruct_the_card_mean(self):
        spatial={'version':'aggregation-test','schemaVersion':3,
                 'grids':[{'id':'a','lon':89,'lat':32},{'id':'b','lon':90,'lat':32}],
                 'units':[{'id':'u1','areaKm2':10},{'id':'u2','areaKm2':30}],
                 'weights':[[.75,.25],[.1,.9]],'lakeWeights':[.2,.8],'lakeAreaKm2':20}
        times=pd.date_range('2024-02-28','2024-03-01',freq='h')
        arrays={'time':times,'precipitation':np.tile([1.,2.],(len(times),1))}
        with tempfile.TemporaryDirectory() as temp:
            product=Path(temp)/'inputs/products/test';product.mkdir(parents=True)
            (product/'manifest.json').write_text(json.dumps({'spatialVersion':'test'}))
            (product/'forcing.npz').write_bytes(b'identity fixture')
            with patch('backend.grid_spatial.load',return_value=spatial),patch('backend.dashboard.load_input',return_value=({},arrays)),patch('backend.dashboard.lake_precipitation_factor',return_value=.5):
                frame=map_frame(SimpleNamespace(workspace=Path(temp)),'test','2024-02-29','precipitation','aggregation-test')
        self.assertTrue(frame['complete']);self.assertEqual(frame['displaySupport'],'model-units-and-lake')
        self.assertEqual([r['value'] for r in frame['cells']],[24.,48.])
        self.assertAlmostEqual(frame['unitValues'][0]['value'],30.)
        self.assertAlmostEqual(frame['unitValues'][1]['value'],45.6)
        self.assertAlmostEqual(frame['lakeMean'],21.6)
        reconstructed=(10*30+30*45.6+20*21.6)/60
        self.assertAlmostEqual(frame['domainMean'],reconstructed)
        self.assertEqual(frame['domainAreaKm2'],60.)

if __name__=='__main__':unittest.main()
