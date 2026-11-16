"""Offline lake terrain keeps native samples and Chinese report dependencies."""
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from scripts.assemble_release import copy_risk_terrain


class RiskReleaseTests(unittest.TestCase):
    def test_offline_crop_preserves_native_resolution_and_avoids_overwrite(self):
        folder = Path(__file__).resolve().parents[1]/'build'/('离线地形检查-'+uuid.uuid4().hex[:10])
        folder.mkdir(parents=True)
        source = folder/'source-dem.tif'
        transform = from_origin(88.,33.,.01,.01)
        values = np.full((300,300),4543.5,dtype='float32')
        with rasterio.open(source,'w',driver='GTiff',width=300,height=300,count=1,dtype='float32',
                crs='EPSG:4326',transform=transform,nodata=-9999.) as dataset:
            dataset.write(values,1)
        with patch('backend.risk_scenarios._dem_paths',return_value=[source]), \
             patch('backend.risk_scenarios._lake_geometry',return_value=box(89.,31.7,89.1,31.8)):
            target = copy_risk_terrain(folder/'package')
            with rasterio.open(target) as dataset:
                self.assertAlmostEqual(dataset.res[0],.01)
                self.assertEqual(dataset.tags()['source'],'Copernicus GLO-90')
                self.assertTrue(np.all(dataset.read(1)==4543.5))
                self.assertLess(dataset.width,100)
            with self.assertRaisesRegex(ValueError,'已存在'):
                copy_risk_terrain(folder/'package')


if __name__ == '__main__':
    unittest.main()
