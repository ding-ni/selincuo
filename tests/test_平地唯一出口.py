from unittest import TestCase, main

import numpy as np

from scripts.核查色林错平地出口 import DIRECTIONS, classify


def strict_d8(z):
    """Construct downhill pointers; a plateau cell never points sideways."""
    pointer = np.zeros(z.shape, dtype=np.uint8)
    for y in range(1, z.shape[0] - 1):
        for x in range(1, z.shape[1] - 1):
            best = 0.0
            for code, (dy, dx) in enumerate(DIRECTIONS, 1):
                slope = (z[y, x] - z[y + dy, x + dx]) / np.hypot(dy, dx)
                if slope > best + 1e-12:
                    best = slope
                    pointer[y, x] = code
    return pointer


class FlatOutletTests(TestCase):
    def setUp(self):
        self.z = np.full((9, 9), 20.0)
        self.valid = np.ones((9, 9), dtype=bool)
        self.domain = np.ones((9, 9), dtype=bool)
        self.targets = np.zeros((9, 9), dtype=np.uint8)
        self.unresolved = np.zeros((9, 9), dtype=bool)
        # The start has no lower neighbour; a different plateau cell reaches the outlet.
        self.z[4, 3:5] = 10
        self.unresolved[4, 3] = True

    def run_classification(self):
        pointer = strict_d8(self.z)
        self.assertEqual(pointer[4, 3], 0)
        return classify(self.z, self.valid, self.domain, self.targets, pointer, self.unresolved)

    def test_equal_plateau_with_one_reachable_outlet_is_resolved(self):
        self.z[4, 5] = 9
        self.targets[4, 5] = 5
        self.z[4, 2] = 11
        self.unresolved[4, 2] = True
        result, counts = self.run_classification()
        self.assertEqual(int(strict_d8(self.z)[4, 2]), 3)
        self.assertEqual([int(result[4, x]) for x in (2, 3)], [5, 5])
        self.assertEqual(counts['unique'], 2)

    def test_multiple_outlets_to_same_target_can_be_resolved(self):
        self.z[3, 5] = self.z[5, 5] = 9
        self.targets[3, 5] = self.targets[5, 5] = 5
        result, counts = self.run_classification()
        self.assertEqual(result[4, 3], 5)
        self.assertEqual(counts['unique'], 1)

    def test_multiple_targets_are_not_chosen_by_iteration_order(self):
        self.z[3, 5] = self.z[5, 5] = 9
        self.targets[3, 5], self.targets[5, 5] = 5, 1
        result, counts = self.run_classification()
        self.assertEqual(result[4, 3], 0)
        self.assertEqual(counts['multiple_targets'], 1)

    def test_unknown_downstream_sink_blocks_an_apparently_known_outlet(self):
        self.z[3, 5] = self.z[5, 5] = 9
        self.targets[3, 5] = 5
        result, counts = self.run_classification()
        self.assertEqual(result[4, 3], 0)
        self.assertEqual(counts['uncertain_exit'], 1)

    def test_domain_edge_blocks_an_apparently_known_outlet(self):
        self.z[3, 5], self.z[4, 5] = 9, 10
        self.targets[3, 5] = 5
        self.domain[4, 5] = False
        result, counts = self.run_classification()
        self.assertEqual(result[4, 3], 0)
        self.assertEqual(counts['uncertain_exit'], 1)

    def test_nodata_blocks_an_apparently_known_outlet(self):
        self.z[3, 5] = 9
        self.targets[3, 5] = 5
        self.valid[5, 5] = False
        result, counts = self.run_classification()
        self.assertEqual(result[4, 3], 0)
        self.assertEqual(counts['uncertain_exit'], 1)

    def test_strict_low_is_not_filled(self):
        self.z[4, 4] = 20
        result, counts = self.run_classification()
        self.assertEqual(result[4, 3], 0)
        self.assertEqual(counts['strict_local_low'], 1)


if __name__ == '__main__':
    main()
