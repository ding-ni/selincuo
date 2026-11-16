import tempfile
import unittest
from pathlib import Path

import numpy as np

from backend.lake_curves import default_curve, validate, storage, level, area, comparison_area, hourly_balance, save_curve, catalog
from backend.database import Database


class LakeCurveTests(unittest.TestCase):
    def test_quadratic_inverse_area_and_volume_conservation(self):
        curve = default_curve(); heights = np.linspace(4537, 4545, 81)
        np.testing.assert_allclose(level(storage(heights, curve), curve), heights, atol=1e-10)
        h = 4543.; epsilon = 1e-3
        derivative = (storage(h + epsilon, curve) - storage(h - epsilon, curve)) / (2 * epsilon)
        self.assertAlmostEqual(derivative / area(h, curve), 1., places=8)
        rain = np.array([2., 0., 3.]); evap = np.array([.1, .2, .3]); q = np.array([20., 30., 40.])
        result = hourly_balance(q, rain, evap, h, curve)
        expected = q * 3600 + (rain - evap) / 1000 * result['areaM2']
        np.testing.assert_allclose(np.diff(np.r_[storage(h, curve), result['storageM3']]), expected, atol=1e-5)
        np.testing.assert_allclose(storage(result['lakeLevel'], curve), result['storageM3'], atol=1e-4)

    def test_table_offset_and_range(self):
        curve = validate({'name': '表格', 'type': 'table', 'minLevel': 10, 'maxLevel': 12, 'offsetM': 10,
                          'points': [{'level': 10, 'storageKm3': 1}, {'level': 11, 'storageKm3': 3}, {'level': 12, 'storageKm3': 6}]})
        self.assertEqual(storage(.5, curve), 2e9)
        self.assertEqual(level(2e9, curve), .5)
        self.assertEqual(area(1.5, curve), 3e9)
        with self.assertRaisesRegex(ValueError, '适用范围'): storage(3, curve)
        with self.assertRaisesRegex(ValueError, '严格递增'):
            validate({**curve, 'points': [{'level': 10, 'storageKm3': 1}, {'level': 12, 'storageKm3': .5}]})

    def test_quadratic_increasing_branch_outside_polynomial_origin(self):
        curve = validate({'name': '增函数', 'type': 'quadratic', 'minLevel': 4541., 'maxLevel': 4545., 'offsetM': 0.,
                          'coefficients': {'a': 1., 'b': -1., 'c': 10., 'referenceLevel': 4540.}})
        heights = np.array([4541., 4542., 4545.])
        np.testing.assert_allclose(level(storage(heights, curve), curve), heights)

    def test_saved_curve_is_new_version_and_default_is_preserved(self):
        db = Database(Path(tempfile.mkdtemp(prefix='selinco-lake-curve-')))
        original = default_curve(); saved = save_curve(db, {**original, 'name': '正式待核曲线'})
        self.assertNotEqual(saved['id'], original['id'])
        self.assertEqual(default_curve(), original)
        self.assertEqual(len(catalog(db)['items']), 2)

    def test_independent_area_fit_is_recorded_for_comparison_only(self):
        curve = default_curve()
        self.assertAlmostEqual(comparison_area(4540., curve) / 1e6, 2245.603203)
        self.assertAlmostEqual(area(4540., curve) / 1e6, 2283.942)
        rain = np.array([2., 0., 3.]); evap = np.array([.1, .2, .3]); q = np.array([20., 30., 40.])
        result = hourly_balance(q, rain, evap, 4540., curve)
        without_comparison = {key: value for key, value in curve.items() if key != 'areaComparison'}
        baseline = hourly_balance(q, rain, evap, 4540., without_comparison)
        np.testing.assert_array_equal(result['lakeLevel'], baseline['lakeLevel'])
        np.testing.assert_array_equal(result['areaM2'], baseline['areaM2'])
        db = Database(Path(tempfile.mkdtemp(prefix='selinco-lake-area-fit-')))
        saved = save_curve(db, {**curve, 'name': '面积对照曲线'})
        self.assertEqual(saved['areaComparison'], curve['areaComparison'])


if __name__ == '__main__': unittest.main()
