import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook

from backend.common import ROOT, read_json
from backend.database import Database
from backend.observations import import_observations, daily_series, confirm_coverage
from backend.maintenance import backup_workspace, restore_workspace
from backend.simulation import run_simulation, observation_snapshot
from backend.forcing import register_era5
from backend.server import Platform


class IntegrationTests(unittest.TestCase):
    def test_observation_revisions_minutes_flags_and_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            db = Database(folder / 'work')
            source = folder / 'raw/blq'
            source.mkdir(parents=True)
            path = source / '2024_雨量.xlsx'
            wb = Workbook()
            wb.active.append(['时间', '雨量'])
            wb.active.append([pd.Timestamp('2024-01-01 05:17').to_pydatetime(), 2.5])
            wb.active.append([pd.Timestamp('2024-01-04 09:31').to_pydatetime(), 1.5])
            wb.save(path)
            import_observations(db, folder / 'raw')
            first = db.summary()['counts']['observations']
            import_observations(db, folder / 'raw')
            self.assertEqual(db.summary()['counts']['observations'], first)
            original = db.observations('BLQ', 'precipitation')
            self.assertEqual(original[0]['time'], '2024-01-01T05:17:00')
            before = daily_series(db, 'BLQ', 'precipitation')
            self.assertTrue(np.isnan(before.get(pd.Timestamp('2024-01-02'), np.nan)))
            confirm_coverage(db, {'sourceId': original[0]['source_id'], 'station': 'BLQ', 'variable': 'precipitation',
                                 'start': '2024-01-01', 'end': '2024-01-06', 'stops': '2024-01-03,2024-01-04'})
            series = daily_series(db, 'BLQ', 'precipitation')
            self.assertEqual(series[pd.Timestamp('2024-01-02')], 0)
            self.assertTrue(np.isnan(series.get(pd.Timestamp('2024-01-03'), np.nan)))
            wb.active.cell(2, 2, 3.5)
            wb.save(path)
            import_observations(db, folder / 'raw')
            self.assertEqual(db.summary()['counts']['sources'], 2)
            self.assertEqual(db.observations('BLQ', 'precipitation')[0]['value'], 3.5)
            self.assertEqual(db.rows('SELECT COUNT(*) n FROM observations')[0]['n'], 4)
            archive = backup_workspace(db)
            destination = folder / 'restored'
            restore_workspace(db.workspace / 'exports' / archive['filename'], destination)
            restored = Database(destination)
            self.assertEqual(restored.summary()['counts'], db.summary()['counts'])
            self.assertEqual(len(list((destination / 'observation-sources').rglob('*.xlsx'))), 2)

    def test_real_forcing_full_simulation_resume(self):
        nc = ROOT / 'workspace/downloads/era5-land/era5-land-202301.nc'
        if not nc.is_file():
            self.skipTest('真实NetCDF尚未下载')
        with tempfile.TemporaryDirectory() as temp:
            db = Database(temp)
            source = register_era5([nc], db)
            a = source['start']
            request = {'inputId': source['id'], 'start': a, 'end': '2023-01-08T23:00:00', '_observations': observation_snapshot(db)}
            full = run_simulation(db, request, Path(temp) / 'full')
            split = '2023-01-04T17:00:00'
            part = run_simulation(db, {**request, 'end': split}, Path(temp) / 'part')
            tail = run_simulation(db, {**request, 'start': '2023-01-04T18:00:00', '_parentState': part['checkpoint']}, Path(temp) / 'tail')
            self.assertEqual(full['checkpoint'], tail['checkpoint'])
            with np.load(Path(temp) / 'full/hourly.npz') as f, np.load(Path(temp) / 'part/hourly.npz') as p, np.load(Path(temp) / 'tail/hourly.npz') as t:
                for key in ('flow', 'unitStates', 'unitDiagnostics', 'routingStorage'):
                    np.testing.assert_allclose(f[key], np.concatenate([p[key], t[key]]), atol=1e-9, rtol=1e-12)
            self.assertEqual(full['resultSource'], 'model-backend')
            self.assertLess(full['diagnostics']['landGlacierMaxBalanceErrorMm'], 1e-9)
            with self.assertRaises(ValueError):
                run_simulation(db, {**request, 'projectId': 'foreign'})
            with self.assertRaises(ValueError):
                run_simulation(db, {**request, 'start': '2023-01-04T19:00:00', '_parentState': part['checkpoint']})

    def test_api_project_and_origin_isolation(self):
        with tempfile.TemporaryDirectory() as temp:
            server = Platform(('127.0.0.1', 0), temp)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            def call(path, data=None, origin=None):
                headers = {'Content-Type': 'application/json'}
                if origin:
                    headers['Origin'] = origin
                req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data else None, headers=headers)
                with urllib.request.urlopen(req) as response:
                    return json.load(response)
            try:
                self.assertEqual(call('/api/health')['projectId'], 'selinco')
                boot = call('/api/model/bootstrap')
                self.assertFalse(boot['forecast']['enabled'])
                with self.assertRaises(urllib.error.HTTPError):
                    call('/api/model/tasks', {'kind': 'simulation', 'projectId': 'foreign'})
                with self.assertRaises(urllib.error.HTTPError):
                    call('/api/model/backup', {}, 'http://foreign.example')
                self.assertEqual(boot['database']['counts']['tasks'], 0)
            finally:
                server.shutdown()
                server.jobs.close()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
