"""New contracts: topology completeness, purpose periods and separate runoff."""
import unittest
from copy import deepcopy
import numpy as np
import pandas as pd
from backend.grid_spatial import topology_complete
from backend.grid_simulation import run
from backend.model_core import DEFAULT_PARAMETERS

class NumericalContractTests(unittest.TestCase):
    def test_topology_requires_every_receiver_and_no_positive_unknown(self):
        spatial={'units':[{'id':'one','partition':'fringe','areaKm2':1}],
                 'drainage':{'inflowComplete':True,'unresolvedAreaKm2':0,'topology':{
                     'nodes':['selinco'],'boundaries':[],'unitReceivers':{'one':'selinco'},
                     'internalExchangesPaired':True,'unresolvedReceivers':[]}}}
        self.assertTrue(topology_complete(spatial))
        for change in ('tiny-unknown','missing-unit','invalid-node','unpaired'):
            bad=deepcopy(spatial)
            if change=='tiny-unknown':bad['drainage']['unresolvedAreaKm2']=1e-12
            if change=='missing-unit':bad['drainage']['topology']['unitReceivers']={}
            if change=='invalid-node':bad['drainage']['topology']['unitReceivers']['one']='other'
            if change=='unpaired':bad['drainage']['topology']['internalExchangesPaired']=False
            with self.subTest(change=change):self.assertFalse(topology_complete(bad))

    def test_noon_anchor_periods_and_runoff_destinations_are_explicit(self):
        times=pd.date_range('2022-12-30',periods=97,freq='h')
        parts=['duja','external','unresolved']
        spatial={'version':'round3-test','units':[{'id':p,'partition':p,'kind':'land','areaKm2':1.,'gridIndex':0,'elevation':5000.} for p in parts],
                 'grids':[{'referenceElevation':5000.}],'forcingGridVersion':'test','lakeAreaKm2':2.,'lakeWeights':[1.],
                 'drainage':{'inflowComplete':False,'unresolvedAreaKm2':1.}}
        source={'times':times,'spatial':spatial,'manifest':{'id':'test','dataSHA256':'raw','transformationSHA256':'rule'},
                'arrays':{'precipitation':np.full((len(times),1),.4),'temperature':np.full((len(times),1),5.),'evaporation':np.zeros((len(times),1))}}
        request={'inputId':'test','spatialVersion':'round3-test','start':times[0].isoformat(),'end':times[-1].isoformat(),
                 'parameters':deepcopy(DEFAULT_PARAMETERS),'_modelIdentity':'round3-numeric',
                 '_observations':{'profile':'test','level':{'2023-01-01T00:00:00':4544},'flow':{}},
                 '_fixedInitialization':None,'periods':{'trainingStart':'2022-12-31','trainingEnd':'2023-01-02',
                 'evaluationStart':'2023-01-02','evaluationEnd':'2023-01-04'}}
        result=run(None,request,prepared=source)
        self.assertIsNone(result['initialization'])
        self.assertIn('率定',result['metrics']['2023']['purpose'])
        self.assertIn('检验',result['metrics']['2023']['purpose'])
        self.assertNotEqual(result['metrics']['2023']['purpose'],'预热')
        for row in result['daily']:
            if row['forcingHours']!=24:continue
            self.assertIsNone(row['lakeLevel'])
            self.assertAlmostEqual(row['excludedRunoff'],row['externalRunoffM3']+row['unresolvedRunoffM3'],places=6)
            self.assertGreater(row['duja'],0)

if __name__=='__main__':unittest.main()
