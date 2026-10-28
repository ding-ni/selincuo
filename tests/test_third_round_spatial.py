"""Selection guards for the conditional, non-water shallow-pit candidate."""
import unittest
from shapely.geometry import box, mapping

from scripts.构建第三轮连续地形空间 import targets
from backend.grid_spatial import topology_complete


def polygon(code, offset):
    return {'type':'Feature','properties':{'code':code},'geometry':mapping(box(offset,0,offset+.4,.4))}


class TerrainCandidateTests(unittest.TestCase):
    def test_only_direct_or_shallow_nonwater_chains_reach_known_targets(self):
        actual={'features':[polygon(1,0),polygon(8,1),polygon(100,2),polygon(1000,3),
                            polygon(1001,4),polygon(1002,5),polygon(1003,6)]}
        thresholds={'pits':[{'rawCode':1000,'maxChainPitDepthM':2.,'potentialReceiverCode':4},
                            {'rawCode':1001,'maxChainPitDepthM':2.01,'potentialReceiverCode':4},
                            {'rawCode':1002,'maxChainPitDepthM':1.,'potentialReceiverCode':100},
                            {'rawCode':1003,'maxChainPitDepthM':None,'potentialReceiverCode':1}]}
        result,counts=targets(actual,thresholds)
        self.assertAlmostEqual(result['zajia_rest'].area,.16)
        self.assertAlmostEqual(result['fringe'].area,.16)
        self.assertAlmostEqual(result['ali'].area,.16)
        self.assertEqual(counts,{'direct':2,'shallowPit':1,'remaining':4})

    def test_missing_or_duplicate_pit_identity_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'缺少溢流证据'):
            targets({'features':[polygon(1000,0)]},{'pits':[]})
        with self.assertRaisesRegex(ValueError,'编码重复'):
            targets({'features':[]},{'pits':[{'rawCode':1000},{'rawCode':1000}]})

    def test_unresolved_area_or_unpaired_exchanges_never_claim_complete(self):
        base={'units':[{'id':'a','partition':'ali','areaKm2':1}],
              'drainage':{'inflowComplete':True,'unresolvedAreaKm2':0,
                          'topology':{'nodes':['lake'],'boundaries':[],
                                      'unitReceivers':{'a':'lake'},'unresolvedReceivers':[],
                                      'internalExchangesPaired':True}}}
        self.assertTrue(topology_complete(base))
        from copy import deepcopy
        for mutation in ('unknown-area','unknown-unit','unpaired','missing-receiver'):
            item=deepcopy(base)
            if mutation=='unknown-area':item['drainage']['unresolvedAreaKm2']=.0001
            elif mutation=='unknown-unit':item['units'].append({'id':'b','partition':'unresolved','areaKm2':.0001})
            elif mutation=='unpaired':item['drainage']['topology']['internalExchangesPaired']=False
            else:item['drainage']['topology']['unitReceivers']={}
            with self.subTest(mutation=mutation):self.assertFalse(topology_complete(item))


if __name__=='__main__':
    unittest.main()
