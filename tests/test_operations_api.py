import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.database import Database
from backend.jobs import Jobs
from backend import operational_store as store, operations_api
from backend.common import canonical, now


class OperationalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_online_backup_precedes_business_table_and_preserves_data(self):
        self.db.set_setting('existing-business', {'adopted': 'original'})
        store.save(self.db, 'lake-curves', {'name': '测试关系'}, prefix='CURVE-')
        store.list_records(self.db, 'forecasts')
        backups = list((self.db.workspace / 'backups/业务扩展前备份').glob('*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(str(backups[0])) as connection:
            self.assertEqual(json.loads(connection.execute("SELECT value FROM settings WHERE key='existing-business'").fetchone()[0]), {'adopted': 'original'})
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='operational_records'").fetchone())
        self.assertEqual(self.db.setting('existing-business'), {'adopted': 'original'})

    def test_hover_shows_latest_values_keeps_zero_and_hides_file_names(self):
        with self.db.connect() as connection:
            connection.execute('INSERT INTO stations VALUES(?,?,?,?,?,?)', ('TEST', '测试站', 'rain', 89., 32., '{}'))
            connection.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)', ('SRC', '现场.json', 'test', 0, now(), '{}'))
            for i, (day, value) in enumerate([('2026-11-01', 2.), ('2026-11-02', 0.)]):
                connection.execute('INSERT INTO observations(source_id,station_id,variable,time,step,unit,value,raw_text,quality,sheet,cell) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    ('SRC', 'TEST', 'precipitation', day+'T00:00:00', 'day', 'mm', value, str(value), 'valid', 'data', str(i)))
        server = SimpleNamespace(db=self.db)
        _, value = operations_api.get(server, 'station-hover', {'stationId': 'TEST', 'day': '2026-10-01'})
        rain = value['groups'][0]
        self.assertEqual((value['title'], value['kindLabel'], rain['label']), ('测试站', '雨量站', '降水'))
        self.assertEqual(rain['latest'], {'value': 0., 'time': '2026-11-02', 'kind': 'daily-total'})
        self.assertEqual([d['value'] for d in rain['daily'][-2:]], [2., 0.])
        self.assertEqual(rain['sum7d'], 2.)
        self.assertNotIn('现场.json', json.dumps(value, ensure_ascii=False))

    def test_forecast_sources_include_only_full_states_and_real_state_time(self):
        state = {'schemaVersion': 3, 'validAt': '2026-10-21T00:00:00', 'inputId': 'INPUT', 'identity': {'spatialVersion': 'SPATIAL'}}
        path = self.db.workspace / 'final-state.json'
        path.write_text(canonical(state), encoding='utf-8')
        with patch('backend.published_results.catalog', return_value=[{'id': 'OLD'}, {'id': 'FULL', 'name': '完整成果'}]), patch('backend.published_results.result_file', side_effect=[FileNotFoundError(), path]):
            _, result = operations_api.get(SimpleNamespace(db=self.db), 'forecasts/sources', {})
        self.assertEqual([r['id'] for r in result['items']], ['FULL'])
        self.assertEqual(result['items'][0]['stateTime'], state['validAt'])

    def test_evaporation_reference_endpoint_returns_read_only_preview(self):
        expected = {'sourceResultId': 'JOB-test', 'daily': [{'date': '2026-10-22', 'evaporationMm': 6.}]}
        with patch('backend.forecast_inputs.preview_evaporation', return_value=expected) as preview:
            handled, result = operations_api.post(SimpleNamespace(db=self.db, jobs=None), 'forecast-inputs/evaporation-preview', {'sourceResultId': 'JOB-test'})
        self.assertTrue(handled)
        self.assertEqual(result, expected)
        preview.assert_called_once_with(self.db, {'sourceResultId': 'JOB-test'})

    def test_queued_report_preserves_catalog_and_download_contract(self):
        jobs = Jobs(self.db)
        try:
            handled, task = operations_api.post(SimpleNamespace(db=self.db, jobs=jobs), 'reports', {'reportType': 'monitoring-daily', 'date': '2026-11-15'})
            self.assertTrue(handled)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                task = self.db.task(task['id'])
                if task['status'] not in ('queued', 'running'):
                    break
                time.sleep(.05)
            self.assertEqual(task['status'], 'succeeded', task.get('message'))
            result = task['result']
            self.assertEqual(store.get(self.db, 'reports', result['id'])['taskId'], task['id'])
            from backend.maintenance import artifact_file
            for file in result['artifacts']:
                resolved = artifact_file(self.db, task, file['name'])
                self.assertTrue(resolved.is_file())
        finally:
            jobs.executor.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
