import unittest

import numpy as np
import pandas as pd

from backend.fast_calibration import DujaEvaluator
from backend.grid_simulation import lake_balance_hourly, run
from backend.model_core import initial_states, normalize_initial_hydrology, parameters


class InitialHydrologyTests(unittest.TestCase):
    def setUp(self):
        times = pd.date_range('2023-01-01', periods=24 * 14 + 1, freq='h')
        units = [{'id':'D', 'partition':'duja', 'kind':'land', 'gridIndex':0,
                  'areaKm2':12., 'elevation':5000.}]
        self.source = {'times':times,
            'spatial':{'version':'GRID-test','units':units,
                       'grids':[{'referenceElevation':5000.}], 'forcingGridVersion':'grid-test',
                       'lakeWeights':[1.], 'lakeAreaKm2':20., 'drainage':{'inflowComplete':False}},
            'manifest':{'id':'INPUT-test','dataSHA256':'test','transformationSHA256':'test'},
            'arrays':{'precipitation':np.full((len(times),1),.18),
                      'temperature':np.full((len(times),1),5.),
                      'evaporation':np.full((len(times),1),.02)}}
        self.request = {'inputId':'INPUT-test','spatialVersion':'GRID-test',
            'start':times[0].isoformat(),'end':times[-1].isoformat(),'parameters':parameters(),
            'search':{'objectiveProfile':'balanced'},'_modelIdentity':'test',
            '_observations':{'profile':'test','level':{},'flow':{}},
            'initialLakeTime':'2023-01-03T12:00:00','initialLakeLevel':4544.,
            'initialLakeSource':'fixture'}

    def test_default_state_and_invalid_initialization(self):
        units = self.source['spatial']['units']
        np.testing.assert_array_equal(initial_states(units, parameters()),
                                      initial_states(units, parameters(), {'soilFraction':.5}))
        for value in ({'soilFraction':-1}, {'soilFraction':1.01}, {'soilFraction':True},
                      {'soilFraction':float('nan')}, {'other':.5}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_initial_hydrology(value)

    def test_fast_and_full_share_initial_soil_and_change_flow(self):
        flows=[]
        for fraction in (.1,.5,.9):
            request={**self.request,'initialHydrology':{'soilFraction':fraction}}
            full=run(None,request,prepared=self.source)
            fast=DujaEvaluator(self.source,request)
            simulated=pd.Series({pd.Timestamp(r['date']):r['duja'] for r in full['daily']})
            aligned=simulated.reindex(fast.days).to_numpy(dtype=float)
            mask=np.isfinite(aligned) & np.isfinite(fast.daily(request['parameters']))
            np.testing.assert_allclose(aligned[mask],fast.daily(request['parameters'])[mask],atol=1e-9,rtol=1e-11)
            self.assertEqual(full['initialHydrology']['soilFraction'],fraction)
            self.assertEqual(full['checkpoint']['initialHydrology']['soilFraction'],fraction)
            flows.append(aligned[mask])
        self.assertGreater(float(np.max(np.abs(flows[0]-flows[-1]))),1e-3)

    def test_lake_balance_split_matches_continuous(self):
        times=self.source['times'];count=len(times)
        initial={'time':times[30].isoformat(),'level':4544.}
        total=np.linspace(1,2,count);rain=np.full(count,.1);evap=np.full(count,.07)
        whole=lake_balance_hourly(times,total,rain,evap,20e6,initial)
        first=lake_balance_hourly(times[:100],total[:100],rain[:100],evap[:100],20e6,initial)
        second=lake_balance_hourly(times[100:],total[100:],rain[100:],evap[100:],20e6,initial,first[4],first[5])
        for index in range(4):
            np.testing.assert_array_equal(whole[index],np.concatenate((first[index],second[index])))
        self.assertEqual(whole[4:],second[4:])


if __name__=='__main__':
    unittest.main()
