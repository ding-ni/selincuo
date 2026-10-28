"""Drainage must follow verified downhill paths and retain unknown edges."""
import unittest

import numpy as np

from scripts.核查90米地形排水 import direct_targets, downhill
from scripts.核查90米连续平地排水 import classify as classify_linked
from scripts.核查色林错平地出口 import classify


class Drainage90mTests(unittest.TestCase):
    def setUp(self):
        self.z = np.full((7, 7), 30, np.int16)
        self.valid = np.ones((7, 7), dtype=np.bool_)
        self.domain = np.ones((7, 7), dtype=np.bool_)
        self.unresolved = np.zeros((7, 7), dtype=np.bool_)
        self.targets = np.zeros((7, 7), np.uint8)
        self.z[3, 1:5] = [12, 11, 10, 9]
        self.unresolved[3, 1:4] = True
        self.targets[3, 4] = 6

    def calculate(self):
        directions = downhill(self.z, self.valid, self.domain, self.unresolved,
                              .0008333, .0008333, 32., .0008333)
        direct = direct_targets(directions, self.valid, self.domain, self.unresolved, self.targets)
        flat, status = classify(self.z, self.valid, self.domain, self.targets, directions, self.unresolved)
        return directions, direct, flat, status

    def test_strict_downhill_reaches_duja_and_is_not_lost(self):
        directions, direct, flat, status = self.calculate()
        self.assertEqual(list(direct[3, 1:4]), [6, 6, 6])
        self.assertEqual(status['target'], 3)
        self.assertFalse(flat.any())  # original flat helper reports these separately

    def test_masked_neighbour_blocks_a_downhill_path(self):
        self.valid[2, 2] = False
        directions, direct, flat, status = self.calculate()
        self.assertEqual(int(directions[3, 2]), 10)
        self.assertEqual(int(direct[3, 1]), 8)
        self.assertEqual(int(direct[3, 2]), 8)
        self.assertEqual(int(direct[3, 3]), 8)
        self.assertFalse(flat.any())

    def test_equal_steepest_directions_are_not_arbitrarily_selected(self):
        self.z.fill(30)
        self.unresolved.fill(False)
        self.unresolved[3, 3] = True
        self.z[3, 3] = 10
        self.z[3, 2] = self.z[3, 4] = 9
        self.targets[3, 2], self.targets[3, 4] = 1, 6
        directions, direct, flat, status = self.calculate()
        self.assertEqual(int(directions[3, 3]), 9)
        self.assertEqual(int(direct[3, 3]), 8)
        self.assertFalse(flat.any())

    def test_upstream_flat_can_reach_uniquely_resolved_lower_flat(self):
        z = np.full((9, 9), 30, np.int16)
        z[4, 2:7] = [12, 12, 10, 10, 9]
        valid = np.ones(z.shape, dtype=np.bool_)
        domain = valid.copy()
        unresolved = np.zeros(z.shape, dtype=np.bool_)
        unresolved[4, 2:6] = True
        targets = np.zeros(z.shape, np.uint8)
        targets[4, 6] = 5
        pointers = downhill(z, valid, domain, unresolved, .0008333, .0008333, 32., .0008333)
        flat, _ = classify(z, valid, domain, targets, pointers, unresolved)
        resolved, components = classify_linked(z, valid, domain, unresolved, targets, pointers)
        self.assertEqual(int(flat[4, 2]), 0)
        self.assertEqual(list(resolved[4, 2:6]), [5, 5, 5, 5])

    def test_unknown_lower_flat_remains_unknown_upstream(self):
        z = np.full((9, 9), 30, np.int16)
        z[4, 2:6] = [12, 12, 10, 10]
        valid = np.ones(z.shape, dtype=np.bool_)
        domain = valid.copy()
        unresolved = np.zeros(z.shape, dtype=np.bool_)
        unresolved[4, 2:6] = True
        targets = np.zeros(z.shape, np.uint8)
        pointers = downhill(z, valid, domain, unresolved, .0008333, .0008333, 32., .0008333)
        resolved, _ = classify_linked(z, valid, domain, unresolved, targets, pointers)
        self.assertTrue(np.all(resolved[4, 2:6] >= 7))


if __name__ == '__main__':
    unittest.main()
