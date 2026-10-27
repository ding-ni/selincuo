import unittest
import numpy as np
import pandas as pd
from unittest.mock import patch
from backend.forcing import lake_precipitation_factor
from backend.simulation import prepare, _prepared_cache
from scripts.refine_lake_input import fit_coefficient


class LakeRefinementTests(unittest.TestCase):
    def test_lake_only_conversion_does_not_change_upstream_rain(self):
        times=pd.date_range('2024-01-01',periods=2,freq='h')
        raw={'time':times,'precipitation':np.array([[2.,4.],[4.,8.]]),'temperature':np.zeros((2,2)),'evaporation':np.ones((2,2))}
        spatial={'version':'unit-test','weights':[[.25,.75]],'lakeWeights':[.5,.5],
                 'grids':[{'referenceElevation':4000},{'referenceElevation':5000}],
                 'units':[{'elevation':4500}]}
        manifest={'lakePrecipitationCorrection':{'method':'station-point-annual-ratio-v1','scope':'lake-only','factor':.5}}
        _prepared_cache.clear()
        try:
            with patch('backend.simulation.read_json',return_value=spatial),patch('backend.simulation.load_input',return_value=(manifest,raw)):
                source=prepare(type('DB',(),{'workspace':'unit-test'})(),'x')
            np.testing.assert_allclose(source['precipitation'][:,0],[3.5,7.])
            np.testing.assert_allclose(source['lakeRain'],[1.5,3.])
        finally:_prepared_cache.clear()
        for factor in (True,-1,float('nan'),3):
            with self.assertRaises(ValueError):lake_precipitation_factor({'lakePrecipitationCorrection':{**manifest['lakePrecipitationCorrection'],'factor':factor}})

    def test_evaporation_fit_ignores_2025_and_is_bounded(self):
        index=pd.date_range('2024-01-01',periods=800,freq='D')
        lo=pd.Series(10.,index=index);hi=pd.Series(8.7,index=index)
        obs=pd.Series(9.4,index=index)
        a=fit_coefficient(obs,lo,hi)
        obs.loc[obs.index.year==2025]=-1e6
        self.assertEqual(a,fit_coefficient(obs,lo,hi))
        self.assertAlmostEqual(a[0],.8)
        obs.loc[obs.index.year==2024]=-10
        self.assertEqual(fit_coefficient(obs,lo,hi)[0],1.5)


if __name__=='__main__':unittest.main()
