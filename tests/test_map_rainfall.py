import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import pandas as pd
from backend.dashboard import map_frame
from backend.map_ranges import range_for, _cache
from backend.common import digest
from backend.grid_spatial import AGGREGATE_STRATEGY

class MapRainfallTests(unittest.TestCase):
    def test_raw_grid_display_and_corrected_lake_reconstruct_the_card_mean(self):
        spatial={'version':'aggregation-test','schemaVersion':3,
                 'grids':[{'id':'a','lon':89,'lat':32},{'id':'b','lon':90,'lat':32}],
                 'units':[{'id':'u1','areaKm2':10},{'id':'u2','areaKm2':30}],
                 'weights':[[.75,.25],[.1,.9]],'lakeWeights':[.2,.8],'lakeAreaKm2':20}
        times=pd.date_range('2024-02-28','2024-03-01',freq='h')
        arrays={'time':times,'precipitation':np.tile([1.,2.],(len(times),1))}
        from tempfile import mkdtemp
        from contextlib import nullcontext
        with nullcontext(mkdtemp(prefix='selinco-map-rain-')) as temp:
            product=Path(temp)/'inputs/products/test';product.mkdir(parents=True)
            (product/'manifest.json').write_text(json.dumps({'spatialVersion':'test'}))
            (product/'forcing.npz').write_bytes(b'identity fixture')
            with patch('backend.grid_spatial.load',return_value=spatial),patch('backend.dashboard.load_input',return_value=({},arrays)),patch('backend.dashboard.lake_precipitation_factor',return_value=.5):
                frame=map_frame(SimpleNamespace(workspace=Path(temp)),'test','2024-02-29','precipitation','aggregation-test')
        self.assertTrue(frame['complete']);self.assertEqual(frame['displaySupport'],'raw-grid')
        self.assertEqual([r['value'] for r in frame['cells']],[24.,48.])
        self.assertAlmostEqual(frame['unitValues'][0]['value'],30.)
        self.assertAlmostEqual(frame['unitValues'][1]['value'],45.6)
        self.assertAlmostEqual(frame['lakeMean'],21.6)
        reconstructed=(10*30+30*45.6+20*21.6)/60
        self.assertAlmostEqual(frame['domainMean'],reconstructed)
        self.assertEqual(frame['domainAreaKm2'],60.)

    def test_replay_color_range_uses_raw_grid_rather_than_lake_correction(self):
        from tempfile import mkdtemp
        temp=Path(mkdtemp(prefix='selinco-map-range-'))
        product=temp/'inputs/products/input';product.mkdir(parents=True)
        (product/'manifest.json').write_text(json.dumps({'spatialVersion':'space'}))
        times=pd.date_range('2024-02-28','2024-03-01',freq='h')
        source={'times':times,'arrays':{'precipitation':np.tile([1.,2.],(len(times),1))}}
        with patch('backend.grid_simulation.prepare',return_value=source):
            frame=range_for(SimpleNamespace(workspace=temp),{'inputId':'input','variable':'precipitation','start':'2024-02-29','end':'2024-02-29'})
        self.assertEqual(frame['displaySupport'],'raw-grid')
        self.assertEqual((frame['minimum'],frame['maximum'],frame['count']),(24.,48.,2))
        _cache.clear()
        with patch('backend.grid_simulation.prepare',side_effect=AssertionError('persistent cache missed')):
            self.assertEqual(range_for(SimpleNamespace(workspace=temp),{'inputId':'input','variable':'precipitation','start':'2024-02-29','end':'2024-02-29'}),frame)

    def test_station_corrected_land_uses_model_unit_forcing_but_grid_stays_raw(self):
        from tempfile import mkdtemp
        temp=Path(mkdtemp(prefix='selinco-map-corrected-'))
        product=temp/'inputs/products/corrected';product.mkdir(parents=True)
        (product/'manifest.json').write_text(json.dumps({'spatialVersion':'corrected-space'}))
        (product/'forcing.npz').write_bytes(b'identity fixture')
        spatial={'version':'corrected-space','schemaVersion':3,'unitStrategy':AGGREGATE_STRATEGY,
                 'grids':[{'id':'a','lon':89.,'lat':32.},{'id':'b','lon':90.,'lat':32.}],
                 'units':[{'id':'u1','areaKm2':10.,'partition':'duja'},{'id':'u2','areaKm2':30.,'partition':'ali'}],
                 'weights':[[1.,0.],[0.,1.]],'lakeWeights':[.2,.8],'lakeAreaKm2':20.,
                 'aggregation':[{'unitId':'u1','members':[{'gridIndex':0,'areaWeight':1.,'elevationOffsetM':0.}]},
                                {'unitId':'u2','members':[{'gridIndex':1,'areaWeight':1.,'elevationOffsetM':0.}]}]}
        profile={'method':'station-warm-rain-spatial-v1','scope':'land-warm-rain','temperatureThresholdC':5.,
                 'memberLapseRateCPerM':-.0065,'groupLapseRateBoundsCPerM':[-.01,-.002],
                 'modelRainTemperatureMaxC':5.,'radiusKm':100.,'distanceRegularizationKm':5.,
                 'parentDataSHA256':'source','stations':{'station':{'lon':89.,'lat':32.,
                 'seasonFactors':{'DJF':2.,'MAM':2.,'JJA':2.,'SON':2.}}}}
        manifest={'dataSHA256':'source','landPrecipitationCorrection':profile,'landTransformationSHA256':digest(profile)}
        times=pd.date_range('2024-06-01','2024-06-03',freq='h');shape=(len(times),2)
        arrays={'time':times,'precipitation':np.tile([1.,2.],(len(times),1)),
                'temperature':np.full(shape,10.),'evaporation':np.zeros(shape)}
        with patch('backend.grid_spatial.load',return_value=spatial),patch('backend.dashboard.load_input',return_value=(manifest,arrays)),patch('backend.dashboard.lake_precipitation_factor',return_value=.5):
            frame=map_frame(SimpleNamespace(workspace=temp),'corrected','2024-06-02','precipitation','corrected-space')
        self.assertEqual([c['value'] for c in frame['cells']],[24.,48.])
        self.assertGreater(frame['unitValues'][0]['value'],24.)
        self.assertGreaterEqual(frame['unitValues'][1]['value'],48.)
        self.assertAlmostEqual(frame['lakeMean'],21.6)
        expected=(10*frame['unitValues'][0]['value']+30*frame['unitValues'][1]['value']+20*21.6)/60
        self.assertAlmostEqual(frame['domainMean'],expected)

if __name__=='__main__':unittest.main()
