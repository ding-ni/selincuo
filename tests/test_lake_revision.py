import unittest

import numpy as np

from backend import lake_curves
from backend.lake_revision import simulate, weir_flow, breach_state, outlet_function


class LakeRevisionTests(unittest.TestCase):
    curve = lake_curves.default_curve()

    def test_vh_balance_closes_without_outlet(self):
        q = np.full(240, 150.); rain = np.full(240, .05); evap = np.full(240, .2)
        levels, outflow = simulate(q, rain, evap, 1.1, 4544.0, self.curve)
        v0 = lake_curves.storage(4544.0, self.curve); v1 = lake_curves.storage(levels[-1], self.curve)
        area = [lake_curves.area(h, self.curve) for h in np.r_[4544.0, levels[:-1]]]
        expected = (q * 3600. + (rain - 1.1 * evap) / 1000. * np.asarray(area)).sum()
        self.assertAlmostEqual(v1 - v0, expected, delta=abs(expected) * 1e-9 + 1.)
        self.assertFalse(outflow.any())

    def test_breach_flows_only_above_crest_and_grows_with_head(self):
        self.assertEqual(weir_flow(0., 60.), 0.)
        self.assertGreater(weir_flow(.4, 60.), weir_flow(.2, 60.))
        outlet = {'crestM': 4544.0, 'widthM': 120., 'erosionDays': 10.}
        self.assertEqual(breach_state(0, 4544.6, outlet), (4544.6, 60.))
        self.assertEqual(breach_state(240, 4544.6, outlet), (4544.0, 120.))
        zero = np.zeros(48)
        below, out_below = simulate(zero, zero, zero, 1., 4543.9, self.curve, 0, 4544.6, outlet)
        self.assertFalse(out_below.any())
        _, out_above = simulate(zero, zero, zero, 1., 4544.6, self.curve, 0, 4544.6, outlet)
        self.assertTrue((out_above[1:] > 0).all())
        model = {'outlet': {'currentCrestM': 4544.0, 'currentWidthM': 60., 'coefficient': .577}}
        flow = outlet_function(model, self.curve)
        self.assertEqual(flow(4543.9), 0.)
        self.assertGreater(flow(4544.3), flow(4544.1))


if __name__ == '__main__':
    unittest.main()
