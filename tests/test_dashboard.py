import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook

from backend.common import ROOT
from backend.database import Database
from backend.observations import import_observations
from backend.dashboard import aggregate_frame, monitoring, map_frame, parse_day
from backend.forcing import register_era5


class DashboardTests(unittest.TestCase):
    def test_day_uses_24_end_hour_values_and_never_fills_gaps(self):
        times=pd.date_range('2024-01-01T00:00','2024-01-02T00:00',freq='h')
        values=np.arange(25.)[:,None]
        daily,hours=aggregate_frame(times,values,'2024-01-01')
        self.assertEqual(hours,24)
        self.assertEqual(daily[0],sum(range(1,25)))
        avg,_=aggregate_frame(times,values,'2024-01-01','mean')
        self.assertEqual(avg[0],12.5)
        missing,count=aggregate_frame(times.delete(10),np.delete(values,10,axis=0),'2024-01-01')
        self.assertIsNone(missing)
        self.assertEqual(count,23)
        with self.assertRaises(ValueError):parse_day('2024-01-01T00:00')

    def test_observation_revision_invalidates_dashboard_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Database(Path(temp)/'work')
            raw=Path(temp)/'raw/blq';raw.mkdir(parents=True)
            path=raw/'2024_雨量.xlsx'
            wb=Workbook();wb.active.append(['时间','雨量']);wb.active.append([pd.Timestamp('2024-01-01T10:15').to_pydatetime(),2.5]);wb.save(path)
            import_observations(db,Path(temp)/'raw')
            first=monitoring(db)
            station=next(s for s in first['stations'] if s['id']=='BLQ')
            self.assertEqual(station['daily'][0]['value'],2.5)
            self.assertFalse(first['liveConnected'])
            wb.active.cell(2,2,3.5);wb.save(path)
            import_observations(db,Path(temp)/'raw')
            second=monitoring(db)
            self.assertEqual(next(s for s in second['stations'] if s['id']=='BLQ')['daily'][0]['value'],3.5)
            self.assertTrue(all(not s['located'] for s in second['stations'] if s['id'].startswith('SLC-')))

    def test_real_input_map_matches_registered_forcing_and_rejects_missing_day(self):
        nc=ROOT/'workspace/downloads/era5-land/era5-land-202301.nc'
        if not nc.is_file():self.skipTest('真实格网尚未下载')
        with tempfile.TemporaryDirectory() as temp:
            db=Database(temp)
            product=register_era5([nc],db)
            row=map_frame(db,product['id'],'2023-01-10','precipitation')
            self.assertTrue(row['complete'])
            self.assertEqual(len(row['cells']),535)
            self.assertTrue(all(r['value']>=0 for r in row['cells']))
            self.assertGreaterEqual(row['domainMean'],0)
            missing=map_frame(db,product['id'],'2024-01-10','precipitation')
            self.assertFalse(missing['complete'])
            self.assertEqual(missing['cells'],[])

