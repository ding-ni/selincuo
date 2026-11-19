import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from backend.common import canonical
from backend.database import Database
from backend import data_connections as connections
from backend import operational_store as store


class SourceHandler(BaseHTTPRequestHandler):
    records = []
    seen = []
    failing = False

    def log_message(self, *_):
        pass

    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        type(self).seen.append({'query': query, 'authorization': self.headers.get('Authorization'),
                                'key': self.headers.get('X-Device-Key')})
        if self.failing:
            self.send_response(503)
            self.end_headers()
            return
        page, size = int(query.get('page', ['1'])[0]), int(query.get('size', ['2'])[0])
        body = canonical({'data': {'rows': self.records[(page - 1) * size:page * size]}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, args=()):
        self.connection.calls.append((sql, args))
        if 'INFORMATION_SCHEMA.TABLES' in sql:
            self.description = [('table_schema',), ('table_name',)]
            self.rows = [(self.connection.schema, '监测资料')]
        elif 'INFORMATION_SCHEMA.COLUMNS' in sql:
            self.description = [('column_name',), ('data_type',)]
            self.rows = [('采样时间', 'datetime'), ('站号', 'varchar'), ('湖位', 'float')]
        elif sql.startswith('SET '):
            self.description, self.rows = [], []
        else:
            self.description = [('采样时间',), ('站号',), ('湖位',)]
            self.rows = [('2026-11-01T01:00:00', 'K01', 4543.2)]
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, schema):
        self.schema, self.calls, self.closed, self.rolled_back = schema, [], False, False

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class ConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), SourceHandler)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix='selinco-connections-'))
        self.db = Database(self.workspace)
        with self.db.connect() as conn:
            conn.execute('INSERT INTO stations VALUES(?,?,?,?,?,?)', ('SLC', '主站', 'lake', 89., 32., '{}'))
        SourceHandler.records = [
            {'Time': '2026-11-01T00:00:00Z', 'Code': 'K01', 'H': 4543.2},
            {'Time': '2026-11-01T01:00:00Z', 'Code': 'K01', 'H': 4543.3},
            {'Time': '2026-11-01T02:00:00Z', 'Code': 'K01', 'H': -9999}]
        SourceHandler.seen, SourceHandler.failing = [], False

    def payload(self):
        return {'name': '现场湖位', 'kind': 'http', 'enabled': True,
                'config': {'url': f'http://127.0.0.1:{self.server.server_port}/data', 'recordsPath': 'data.rows',
                           'pageParameter': 'page', 'pageSizeParameter': 'size', 'pageSize': 2,
                           'sinceParameter': 'since', 'endParameter': 'until'},
                'mapping': {'format': 'wide', 'time': 'Time', 'station': 'Code', 'timeZone': 'UTC',
                            'stationMap': {'K01': 'SLC'}, 'missingValues': [-9999],
                            'columns': [{'field': 'H', 'variable': 'level', 'unit': 'm', 'step': 'irregular'}]}}

    def test_http_real_pagination_preview_increment_revision_and_failure_cursor(self):
        payload = self.payload()
        saved = connections.save_connection(self.db, payload)
        request = {'connectionId': saved['id']}
        discovered = connections.discover(self.db, request)
        self.assertEqual([v['name'] for v in discovered['columns']], ['Code', 'H', 'Time'])
        preview = connections.preview(self.db, request)
        self.assertEqual((preview['records'], self.db.summary()['counts']['observations']), (3, 0))
        self.assertEqual(preview['samples'][0]['time'], '2026-11-01T08:00:00')
        first = connections.sync(self.db, request)
        self.assertEqual((first['changed'], first['skipped']), (3, 0))
        self.assertEqual(self.db.observations('SLC', 'level')[-1]['quality'], 'missing')
        self.assertEqual(SourceHandler.seen[-1]['query']['page'], ['2'])
        again = connections.sync(self.db, request)
        self.assertEqual((again['changed'], again['skipped']), (0, 3))
        self.assertEqual(self.db.summary()['counts']['sources'], 1)
        self.assertEqual(SourceHandler.seen[-1]['query']['since'], ['2026-10-30T02:00:00'])
        SourceHandler.records[0]['H'] = 4543.9
        revised = connections.sync(self.db, request)
        self.assertEqual(revised['changed'], 1)
        self.assertEqual(self.db.observations('SLC', 'level')[0]['value'], 4543.9)
        self.assertEqual(self.db.summary()['counts']['sources'], 2)
        cursor = connections.get_connection(self.db, saved['id'])['cursor']
        last_success = connections.get_connection(self.db, saved['id'])['lastSuccessAt']
        self.assertEqual(last_success, revised['completedAt'])
        SourceHandler.failing = True
        with self.assertRaisesRegex(ValueError, 'HTTP连接失败'):
            connections.sync(self.db, request)
        current = connections.get_connection(self.db, saved['id'])
        self.assertEqual(current['cursor'], cursor)
        self.assertEqual(current['lastSync']['status'], 'failed')
        self.assertEqual(current['lastSuccessAt'], last_success)
        self.assertFalse(connections.due_connections(self.db))
        connections.set_enabled(self.db, saved['id'], False)
        self.assertFalse(connections.get_connection(self.db, saved['id'])['enabled'])

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_dpapi_credentials_are_encrypted_and_used_by_http(self):
        payload = self.payload()
        payload['credentials'] = {'bearerToken': 'test-bearer-secret', 'apiKey': 'station-api-secret',
                                  'apiKeyHeader': 'X-Device-Key'}
        saved = connections.save_connection(self.db, payload)
        self.assertNotIn('credentials', saved)
        self.assertNotIn('secretFile', saved)
        private = store.get(self.db, connections.COLLECTION, saved['id'])
        encrypted = (self.workspace / private['secretFile']).read_bytes()
        for secret in ('test-bearer-secret', 'station-api-secret'):
            self.assertNotIn(secret, canonical(connections.catalog(self.db)))
            self.assertNotIn(secret.encode(), encrypted)
            self.assertNotIn(secret, canonical(private))
        connections.test_connection(self.db, {'connectionId': saved['id']})
        self.assertEqual(SourceHandler.seen[-1]['authorization'], 'Bearer test-bearer-secret')
        self.assertEqual(SourceHandler.seen[-1]['key'], 'station-api-secret')
        connections.save_connection(self.db, {'name': '重命名'}, id=saved['id'])
        connections.test_connection(self.db, {'connectionId': saved['id']})
        self.assertEqual(SourceHandler.seen[-1]['key'], 'station-api-secret')

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_restore_connection_can_replace_unreadable_credentials_without_losing_progress(self):
        payload = self.payload()
        payload['credentials'] = {'bearerToken': 'old-bearer', 'apiKey': 'old-api-key', 'apiKeyHeader': 'X-Device-Key'}
        saved = connections.save_connection(self.db, payload)
        identifier = saved['id']
        connections.sync(self.db, {'connectionId': identifier})
        connections.save_connection(self.db, {'credentials': {'bearerToken': 'same-account'}}, id=identifier)
        connections.test_connection(self.db, {'connectionId': identifier})
        self.assertEqual(SourceHandler.seen[-1]['authorization'], 'Bearer same-account')
        self.assertEqual(SourceHandler.seen[-1]['key'], 'old-api-key')

        previous = store.get(self.db, connections.COLLECTION, identifier)
        # The restored machine cannot decrypt its predecessor's file.
        old = connections._dpapi
        def moved_machine(data, decrypt=False):
            if decrypt:
                raise ValueError('凭据属于另一台电脑')
            return old(data)
        replacement = {'id': identifier, 'replaceCredentials': True, 'credentials': {'bearerToken': 'new-bearer'}}
        with patch.object(connections, '_dpapi', side_effect=moved_machine):
            with self.assertRaisesRegex(ValueError, '重新填写整套凭据'):
                connections.test_connection(self.db, {'connectionId': identifier})
            connections.test_connection(self.db, replacement)
            new = connections.save_connection(self.db, replacement)
        connections.test_connection(self.db, {'connectionId': identifier})
        self.assertEqual(SourceHandler.seen[-1]['authorization'], 'Bearer new-bearer')
        self.assertIsNone(SourceHandler.seen[-1]['key'])
        for field in ('id', 'cursor', 'mapping', 'lastSuccessAt'):
            self.assertEqual(new[field], previous[field])
        self.assertTrue((self.workspace / previous['secretFile']).is_file())

        result = connections.save_connection(self.db, {'id': identifier, 'replaceCredentials': True, 'credentials': {}})
        self.assertFalse(result['credentialConfigured'])
        connections.test_connection(self.db, {'connectionId': identifier})
        self.assertIsNone(SourceHandler.seen[-1]['authorization'])

    def test_long_mapping_station_registration_intervals_and_invalid_batch(self):
        connection = self.payload()
        connection['mapping'] = {'time': 'at', 'station': 'site', 'variable': 'kind', 'value': 'value',
            'timeZone': 'UTC', 'variableMap': {'rain': 'precipitation'},
            'stationMap': {'remote': {'id': 'NEW-SITE', 'name': '新增站', 'lon': 89.1, 'lat': 32.1}},
            'defaults': {'unit': 'mm', 'step': 'irregular'}, 'intervalMinutes': 30}
        rows, candidates = connections._mapped(self.db, connection, [
            {'at': '2026-11-01T00:30:00Z', 'site': 'remote', 'kind': 'rain', 'value': 2}])
        self.assertEqual((rows[0]['start'], rows[0]['end']), ('2026-11-01T08:00:00', '2026-11-01T08:30:00'))
        self.assertEqual(rows[0]['station'], 'NEW-SITE')
        self.assertIn('NEW-SITE', candidates)
        SourceHandler.records = [{'at': '2026-11-01T00:30:00Z', 'site': 'remote', 'kind': 'rain', 'value': 2}]
        saved = connections.save_connection(self.db, connection)
        self.assertEqual(connections.preview(self.db, {'connectionId': saved['id']})['newStations'][0]['name'], '新增站')
        self.assertFalse(self.db.rows("SELECT id FROM stations WHERE id='NEW-SITE'"))
        connections.sync(self.db, {'connectionId': saved['id']})
        self.assertEqual(self.db.rows("SELECT name FROM stations WHERE id='NEW-SITE'")[0]['name'], '新增站')
        SourceHandler.records = [{'at': '2026-11-02T00:30:00Z', 'site': 'unknown', 'kind': 'rain', 'value': -5}]
        prior = connections.get_connection(self.db, saved['id'])['cursor']
        with self.assertRaises(ValueError):
            connections.sync(self.db, {'connectionId': saved['id']})
        self.assertEqual(connections.get_connection(self.db, saved['id'])['cursor'], prior)

    def sql_payload(self, kind):
        return {'name': '现场数据库', 'kind': kind, 'config': {'host': 'server', 'database': 'hydro',
                    'schema': 'hydro' if kind == 'mysql' else 'dbo', 'table': '监测资料'},
                'mapping': {'format': 'wide', 'time': '采样时间', 'station': '站号',
                            'stationMap': {'K01': 'SLC'}, 'timeZone': 'Asia/Shanghai',
                            'columns': [{'field': '湖位', 'variable': 'level', 'unit': 'm', 'step': 'irregular'}]}}

    def test_sqlserver_discovery_identifiers_parameters_and_readonly_connection(self):
        fake, options = FakeConnection('dbo'), {}
        def connect(*args, **kwargs):
            options.update(kwargs)
            options['dsn'] = args[0]
            return fake
        with patch.dict(sys.modules, {'pyodbc': SimpleNamespace(connect=connect)}):
            payload = self.sql_payload('sqlserver')
            result = connections.discover(self.db, {**payload, 'start': '2026-11-01', 'end': '2026-11-02'})
            self.assertEqual(result['columns'][0]['name'], '采样时间')
            self.assertTrue(options['readonly'])
            self.assertIn('ApplicationIntent={ReadOnly}', options['dsn'])
            sql, args = fake.calls[-1]
            self.assertIn('FROM [dbo].[监测资料]', sql)
            self.assertIn('[采样时间]>=?', sql)
            self.assertNotIn('2026-11-01', sql)
            self.assertEqual(args, ('2026-11-01T00:00:00', '2026-11-02T00:00:00'))
            self.assertTrue(fake.closed and fake.rolled_back)
            payload['config']['table'] = '监测资料; DROP TABLE sources'
            with self.assertRaisesRegex(ValueError, '可读表目录'):
                connections.discover(self.db, payload)
            self.assertFalse(any('DROP TABLE' in sql for sql, _ in fake.calls))

    def test_mysql_read_only_transaction_quoted_columns_and_mapping(self):
        fake, options = FakeConnection('hydro'), {}
        def connect(**kwargs):
            options.update(kwargs)
            return fake
        with patch.dict(sys.modules, {'pymysql': SimpleNamespace(connect=connect)}):
            payload = self.sql_payload('mysql')
            result = connections.preview(self.db, {**payload, 'start': '2026-11-01'})
            self.assertEqual(result['samples'][0]['value'], 4543.2)
            self.assertFalse(options['autocommit'])
            self.assertEqual(fake.calls[0][0], 'SET SESSION TRANSACTION READ ONLY')
            sql, args = fake.calls[-1]
            self.assertIn('FROM `hydro`.`监测资料`', sql)
            self.assertIn('`采样时间`>=%s', sql)
            self.assertIn('LIMIT 100', sql)
            self.assertEqual(args, ('2026-11-01T00:00:00',))
            self.assertTrue(all(sql.startswith(('SELECT', 'SET SESSION TRANSACTION READ ONLY')) for sql, _ in fake.calls))

    def test_sqlserver_missing_driver_names_the_required_installation(self):
        def missing_driver(*args, **kwargs):
            raise Exception('IM002', 'Data source name not found and no default driver specified')
        with patch.dict(sys.modules, {'pyodbc': SimpleNamespace(connect=missing_driver)}):
            with self.assertRaisesRegex(ValueError, 'ODBC Driver 18 for SQL Server.*已安装的驱动名称'):
                connections.test_connection(self.db, self.sql_payload('sqlserver'))

    def test_cancelled_sync_keeps_cursor_and_no_import(self):
        saved = connections.save_connection(self.db, self.payload())
        with self.assertRaisesRegex(InterruptedError, '已停止'):
            connections.sync(self.db, {'connectionId': saved['id']}, cancelled=lambda: True)
        current = connections.get_connection(self.db, saved['id'])
        self.assertNotIn('cursor', current)
        self.assertEqual(current['lastSync']['status'], 'cancelled')
        self.assertEqual(self.db.summary()['counts']['observations'], 0)

    def test_credential_read_failure_is_recorded_before_scheduler_retry(self):
        saved = connections.save_connection(self.db, self.payload())
        with patch.object(connections, '_credentials', side_effect=OSError('credential unavailable')):
            with self.assertRaisesRegex(ValueError, '资料同步失败'):
                connections.sync(self.db, {'connectionId': saved['id']})
        current = connections.get_connection(self.db, saved['id'])
        self.assertEqual(current['lastSync']['status'], 'failed')
        self.assertEqual(current['lastAttemptAt'], current['lastSync']['at'])
        self.assertFalse(connections.due_connections(self.db))

    def test_explicit_backfill_period_overrides_cursor_without_regressing_it(self):
        saved = connections.save_connection(self.db, self.payload())
        connections.sync(self.db, {'connectionId': saved['id']})
        cursor = connections.get_connection(self.db, saved['id'])['cursor']['time']
        SourceHandler.seen = []
        SourceHandler.records = [SourceHandler.records[0]]
        result = connections.sync(self.db, {'connectionId': saved['id'],
            'start': '2026-10-01T08:00:00', 'end': '2026-10-02T08:00:00'})
        self.assertEqual(SourceHandler.seen[0]['query']['since'], ['2026-10-01T00:00:00'])
        self.assertEqual(SourceHandler.seen[0]['query']['until'], ['2026-10-02T00:00:00'])
        self.assertEqual(result['cursor']['time'], cursor)

    def test_main_station_forecast_requires_frozen_source_datum_confirmation(self):
        from backend.forecasts import _latest_main
        import pandas as pd
        with self.db.connect() as conn:
            conn.execute('UPDATE stations SET metadata=? WHERE id=?',
                         (canonical({'datumVerified': True}), 'SLC'))
        saved = connections.save_connection(self.db, self.payload())
        first = connections.sync(self.db, {'connectionId': saved['id']})
        with self.assertRaisesRegex(ValueError, '现场来源尚未确认'):
            _latest_main(self.db, pd.Timestamp('2026-11-01T10:00:00'))
        mapping = saved['mapping']
        mapping['levelDatum'] = {'status': 'verified', 'name': '模型工作基准', 'basis': ''}
        with self.assertRaisesRegex(ValueError, '核对依据'):
            connections.save_connection(self.db, {'mapping': mapping}, saved['id'])
        mapping['levelDatum']['basis'] = '现场联测记录2026-11-01'
        connections.save_connection(self.db, {'mapping': mapping}, saved['id'])
        # Changing the connection does not retroactively bless an old source.
        with self.assertRaisesRegex(ValueError, '现场来源尚未确认'):
            _latest_main(self.db, pd.Timestamp('2026-11-01T10:00:00'))
        confirmed = connections.sync(self.db, {'connectionId': saved['id']})
        self.assertEqual(confirmed['changed'], 0)
        self.assertNotEqual(first['sources'], confirmed['sources'])
        self.assertEqual(self.db.summary()['counts']['sources'], 2)
        level = _latest_main(self.db, pd.Timestamp('2026-11-01T10:00:00'))
        self.assertEqual(level['level'], 4543.3)
        sources = self.db.rows('SELECT metadata FROM sources ORDER BY rowid')
        self.assertEqual(json.loads(sources[0]['metadata'])['sourceMetadata']['levelDatum']['status'], 'unverified')
        self.assertEqual(json.loads(sources[1]['metadata'])['sourceMetadata']['levelDatum'], mapping['levelDatum'])
        with self.db.connect() as conn:
            conn.execute('UPDATE stations SET metadata=? WHERE id=?', ('{}', 'SLC'))
        with self.assertRaisesRegex(ValueError, '主站水位基准尚未确认'):
            _latest_main(self.db, pd.Timestamp('2026-11-01T10:00:00'))

    def test_state_update_uses_eligible_same_day_level_without_hiding_unverified_observations(self):
        import pandas as pd
        from backend import state_update
        from backend.data_exchange import import_records
        date = pd.Timestamp('2026-11-01')
        with self.db.connect() as conn:
            conn.execute('UPDATE stations SET metadata=? WHERE id=?',
                         (canonical({'verticalDatum': {'role': 'working-reference'}}), 'SLC'))
        SourceHandler.records = [{'Time': '2026-11-01T04:00:00Z', 'Code': 'K01', 'H': 4544.1}]
        saved = connections.save_connection(self.db, self.payload())
        connections.sync(self.db, {'connectionId': saved['id']})
        self.assertEqual(state_update._level_observations(self.db, date, date), {})
        self.assertEqual(self.db.observations('SLC', 'level')[0]['value'], 4544.1)

        import_records(self.db, {'schemaVersion': 1, 'projectId': 'selinco', 'timeZone': 'Asia/Shanghai',
            'sourceKey': '既有主站整编', 'records': [{
                'station': 'SLC', 'variable': 'level', 'time': '2026-11-01T08:00:00',
                'step': 'irregular', 'value': 4543.4, 'unit': 'm', 'quality': 'valid'}]})
        chosen = state_update._level_observations(self.db, date, date)[date]
        self.assertEqual((chosen['level'], chosen['hour']), (4543.4, 8))

        mapping = saved['mapping']
        mapping['levelDatum'] = {'status': 'verified', 'name': '工作基准', 'basis': '现场联测记录'}
        connections.save_connection(self.db, {'mapping': mapping}, id=saved['id'])
        self.assertEqual(state_update._level_observations(self.db, date, date)[date], chosen)
        connections.sync(self.db, {'connectionId': saved['id']})
        chosen = state_update._level_observations(self.db, date, date)[date]
        self.assertEqual((chosen['level'], chosen['hour']), (4544.1, 12))


if __name__ == '__main__':
    unittest.main()
