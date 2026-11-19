"""现场观测连接、字段预览与增量入库。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import ctypes
import json
import os
import uuid

import pandas as pd
import requests

from .common import canonical, now, within, write_json
from .data_exchange import VARIABLES, normalize, import_records, timestamp
from . import operational_store as store


COLLECTION = 'data-connections'
HISTORY = 'data-syncs'
KINDS = ('http', 'sqlserver', 'mysql')
FIELDS = ('time', 'station', 'variable', 'value', 'unit', 'step', 'quality', 'periodStart', 'periodEnd')


def _dpapi(data, decrypt=False):
    if os.name != 'nt':
        raise ValueError('连接凭据须在运行平台的Windows账户下保存')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError('Windows凭据加密文件无法读取' if decrypt else 'Windows凭据加密失败')
    try:
        return ctypes.string_at(target.data, target.length)
    finally:
        kernel.LocalFree(target.data)


def _public(value):
    result = deepcopy(value)
    result.pop('secretFile', None)
    result.pop('credentials', None)
    return result


def catalog(db):
    return {'connections': [_public(row) for row in store.list_records(db, COLLECTION)],
            'kinds': [{'id': 'http', 'label': 'HTTP JSON接口'},
                      {'id': 'sqlserver', 'label': 'SQL Server'},
                      {'id': 'mysql', 'label': 'MySQL / MariaDB'}]}


def get_connection(db, identifier):
    return _public(store.get(db, COLLECTION, identifier))


def _definition(payload, previous=None):
    result = deepcopy(previous or {})
    for key in ('name', 'kind', 'enabled', 'intervalMinutes', 'config', 'mapping', 'lookbackHours'):
        if key in payload:
            result[key] = deepcopy(payload[key])
    result.setdefault('enabled', False)
    result.setdefault('intervalMinutes', 5)
    result.setdefault('lookbackHours', 48)
    result.setdefault('config', {})
    result.setdefault('mapping', {})
    if result.get('kind') not in KINDS:
        raise ValueError('请选择HTTP JSON、SQL Server或MySQL连接')
    if not str(result.get('name', '')).strip():
        raise ValueError('请填写连接名称')
    result['name'] = str(result['name']).strip()
    result['enabled'] = bool(result['enabled'])
    result['intervalMinutes'] = int(result['intervalMinutes'])
    result['lookbackHours'] = float(result['lookbackHours'])
    if result['intervalMinutes'] < 1 or result['lookbackHours'] < 0:
        raise ValueError('更新间隔至少1分钟，回看小时数不得为负')
    if not isinstance(result['config'], dict) or not isinstance(result['mapping'], dict):
        raise ValueError('连接配置和字段映射须为对象')
    config = result['config']
    if result['kind'] == 'http':
        from urllib.parse import urlsplit
        url = urlsplit(config.get('url', ''))
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
            raise ValueError('请填写HTTP或HTTPS地址，认证信息在凭据中填写')
    elif not config.get('host') or not config.get('database'):
        raise ValueError('请填写数据库主机和数据库名称')
    zone = result['mapping'].get('timeZone', 'Asia/Shanghai')
    if zone not in ('Asia/Shanghai', 'UTC'):
        raise ValueError('来源时区须为Asia/Shanghai或UTC')
    datum = result['mapping'].get('levelDatum') or {'status': 'unverified'}
    if not isinstance(datum, dict) or datum.get('status') not in ('unverified', 'verified'):
        raise ValueError('水位基准请选择待核实或已核实与模型同基准')
    datum = {'status': datum['status'], 'name': str(datum.get('name') or '').strip(),
             'basis': str(datum.get('basis') or '').strip()}
    if datum['status'] == 'verified' and (not datum['name'] or not datum['basis']):
        raise ValueError('确认同基准须填写水位基准名称及核对依据')
    result['mapping']['levelDatum'] = datum
    return result


def _credentials(db, connection, supplied=None, replace=False):
    value = {}
    if not replace and connection.get('secretFile'):
        try:
            encrypted = within(db.workspace, connection['secretFile']).read_bytes()
            value = json.loads(_dpapi(encrypted, decrypt=True).decode('utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError('无法读取原连接凭据，请在编辑连接中选择“重新填写整套凭据”') from exc
    if supplied is not None or replace:
        if not isinstance(supplied, dict):
            raise ValueError('凭据须为对象')
        value.update(supplied)
    return value


def save_connection(db, payload, id=None):
    identifier = id or payload.get('id') or 'CONN-' + uuid.uuid4().hex[:20]
    previous = store.get(db, COLLECTION, identifier) if (id or payload.get('id')) else None
    value = _definition(payload, previous)
    value['id'] = identifier
    replace = bool(payload.get('replaceCredentials'))
    if 'credentials' in payload or replace:
        credentials = _credentials(db, value, payload.get('credentials'), replace=replace)
        if credentials:
            relative = Path('凭据') / '数据连接' / (uuid.uuid4().hex + '.bin')
            path = db.workspace / relative
            encrypted = _dpapi(canonical(credentials).encode('utf-8'))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(encrypted)
            value['secretFile'] = relative.as_posix()
        else:
            value.pop('secretFile', None)
        value['credentialFields'] = [key for key, item in credentials.items() if item]
        value['credentialConfigured'] = bool(value['credentialFields'])
    value.setdefault('credentialConfigured', False)
    value['updatedAt'] = now()
    value.setdefault('createdAt', value['updatedAt'])
    store.save(db, COLLECTION, value, identifier=identifier)
    return _public(value)


def set_enabled(db, id, enabled):
    value = store.get(db, COLLECTION, id)
    value['enabled'] = bool(enabled)
    value['updatedAt'] = now()
    store.save(db, COLLECTION, value, identifier=id)
    return _public(value)


def due_connections(db):
    current = datetime.now(timezone.utc)
    result = []
    for value in store.list_records(db, COLLECTION):
        last = value.get('lastAttemptAt') or value.get('lastSync', {}).get('at')
        if value.get('enabled') and (not last or current >= datetime.fromisoformat(last) + timedelta(minutes=value.get('intervalMinutes', 5))):
            result.append(_public(value))
    return result


def _resolve(db, payload):
    identifier = payload.get('connectionId') or payload.get('id')
    previous = store.get(db, COLLECTION, identifier) if identifier else None
    value = _definition(payload, previous)
    return value, _credentials(db, value, payload.get('credentials'), replace=bool(payload.get('replaceCredentials')))


def _plain(value):
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return value


def _at(value, path):
    for part in str(path or '').split('.'):
        if not part:
            continue
        if not isinstance(value, dict) or part not in value:
            raise ValueError('JSON记录路径或映射字段不存在：' + str(path))
        value = value[part]
    return value


def _source_bound(value, zone):
    instant = timestamp(value, 'Asia/Shanghai').tz_localize('Asia/Shanghai').tz_convert(zone)
    return instant.tz_localize(None).isoformat()


def _bounds(connection, request):
    zone = connection['mapping'].get('timeZone', 'Asia/Shanghai')
    start, end = request.get('start'), request.get('end')
    if not start and connection.get('cursor', {}).get('time'):
        start = (pd.Timestamp(connection['cursor']['time']) - pd.Timedelta(hours=connection['lookbackHours'])).isoformat()
    if start and end and timestamp(end, 'Asia/Shanghai') < timestamp(start, 'Asia/Shanghai'):
        raise ValueError('同步结束时间须不早于开始时间')
    return (_source_bound(start, zone) if start else None,
            _source_bound(end, zone) if end else None)


def _check_cancel(cancelled):
    if cancelled and cancelled():
        raise InterruptedError('资料同步已停止，保留原同步游标')


def _http_rows(connection, credentials, request, limit=None, cancelled=None, progress=None):
    config = connection['config']
    headers = dict(credentials.get('headers') or {})
    if credentials.get('bearerToken'):
        headers['Authorization'] = 'Bearer ' + credentials['bearerToken']
    if credentials.get('apiKey'):
        headers[credentials.get('apiKeyHeader') or 'X-API-Key'] = credentials['apiKey']
    params = dict(config.get('query') or {})
    start, end = _bounds(connection, request)
    for name, value in ((config.get('sinceParameter') or config.get('startParameter'), start),
                        (config.get('endParameter'), end)):
        if name and value:
            params[name] = value
    page = int(config.get('firstPage', 1))
    size = int(config.get('pageSize', 500))
    if size < 1:
        raise ValueError('每页记录数至少1条')
    result = []
    while True:
        _check_cancel(cancelled)
        if config.get('pageParameter'):
            params[config['pageParameter']] = page
        if config.get('pageSizeParameter'):
            params[config['pageSizeParameter']] = size
        try:
            response = requests.get(config['url'], params=params, headers=headers,
                                    timeout=(10, int(config.get('timeoutSeconds', 30))))
            response.raise_for_status()
            document = response.json()
        except requests.RequestException:
            raise ValueError('HTTP连接失败，请核对地址、网络和认证配置') from None
        except ValueError:
            raise ValueError('接口响应不是有效JSON') from None
        rows = _at(document, config.get('recordsPath', ''))
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError('JSON记录路径应指向记录数组或单条记录对象')
        result.extend(rows)
        if progress:
            progress(25, f'已读取{len(result)}条来源记录')
        if limit and len(result) >= limit:
            return result[:limit]
        if not config.get('pageParameter') or len(rows) < size:
            return result
        if page - int(config.get('firstPage', 1)) >= int(config.get('maxPages', 1000)) - 1:
            raise ValueError('来源分页超过配置上限，请缩小同步时段或增加maxPages')
        page += 1


def _sql_connect(connection, credentials):
    config = connection['config']
    try:
        if connection['kind'] == 'sqlserver':
            import pyodbc
            def escaped(item):
                return '{' + str(item).replace('}', '}}') + '}'
            server = config['host'] + (',' + str(config['port']) if config.get('port') else '')
            parts = {'DRIVER': config.get('driver', 'ODBC Driver 18 for SQL Server'), 'SERVER': server,
                     'DATABASE': config['database'], 'ApplicationIntent': 'ReadOnly',
                     'TrustServerCertificate': 'yes' if config.get('trustServerCertificate') else 'no'}
            if config.get('trustedConnection'):
                parts['Trusted_Connection'] = 'yes'
            else:
                parts.update(UID=credentials.get('user', ''), PWD=credentials.get('password', ''))
            return pyodbc.connect(';'.join(key + '=' + escaped(item) for key, item in parts.items()),
                                  readonly=True, autocommit=False, timeout=int(config.get('timeoutSeconds', 15)))
        import pymysql
        conn = pymysql.connect(host=config['host'], port=int(config.get('port', 3306)),
                               database=config['database'], user=credentials.get('user', ''),
                               password=credentials.get('password', ''), charset='utf8mb4', autocommit=False,
                               connect_timeout=int(config.get('timeoutSeconds', 15)),
                               read_timeout=int(config.get('timeoutSeconds', 30)))
        cursor = conn.cursor()
        try:
            cursor.execute('SET SESSION TRANSACTION READ ONLY')
        except Exception:
            conn.close()
            raise
        finally:
            cursor.close()
        return conn
    except ImportError:
        raise ValueError('SQL Server需要pyodbc和Microsoft ODBC驱动；MySQL/MariaDB需要pymysql') from None
    except Exception as error:
        if connection['kind'] == 'sqlserver' and error.args and error.args[0] == 'IM002':
            raise ValueError('未安装所选SQL Server驱动：' + config.get('driver', 'ODBC Driver 18 for SQL Server') +
                             '；请安装该驱动，或在ODBC驱动栏填写本机已安装的驱动名称') from None
        raise ValueError('只读数据库连接失败，请核对驱动、主机和账户权限') from None


def _select(conn, sql, args=()):
    cursor = conn.cursor()
    try:
        cursor.execute(sql, args) if args else cursor.execute(sql)
        names = [column[0] for column in cursor.description]
        return [{key: _plain(value) for key, value in zip(names, row)} for row in cursor.fetchall()]
    finally:
        cursor.close()


def _sql_schema(conn, connection):
    mysql = connection['kind'] == 'mysql'
    placeholder = '%s' if mysql else '?'
    schema = connection['config'].get('schema') or (connection['config']['database'] if mysql else 'dbo')
    tables = _select(conn, 'SELECT TABLE_SCHEMA AS table_schema,TABLE_NAME AS table_name FROM INFORMATION_SCHEMA.TABLES '
                     'WHERE TABLE_TYPE IN (\'BASE TABLE\',\'VIEW\') AND TABLE_SCHEMA=' + placeholder, (schema,))
    tables = [{'schema': row['table_schema'], 'table': row['table_name']} for row in tables]
    table = connection['config'].get('table')
    if not table:
        return tables, []
    if not any(row['schema'] == schema and row['table'] == table for row in tables):
        raise ValueError('所选数据表不在来源可读表目录中，请重新发现表结构')
    columns = _select(conn, 'SELECT COLUMN_NAME AS column_name,DATA_TYPE AS data_type FROM INFORMATION_SCHEMA.COLUMNS '
                      'WHERE TABLE_SCHEMA=' + placeholder + ' AND TABLE_NAME=' + placeholder + ' ORDER BY ORDINAL_POSITION',
                      (schema, table))
    return tables, [{'name': row['column_name'], 'type': row['data_type']} for row in columns]


def _quote(value, kind):
    return '`' + value.replace('`', '``') + '`' if kind == 'mysql' else '[' + value.replace(']', ']]') + ']'


def _sql_rows(connection, credentials, request, limit=None, discovery=False):
    conn = _sql_connect(connection, credentials)
    try:
        tables, columns = _sql_schema(conn, connection)
        if not connection['config'].get('table'):
            if discovery:
                return {'tables': tables, 'columns': [], 'samples': []}
            raise ValueError('请选择来源数据表')
        config, mapping, kind = connection['config'], connection['mapping'], connection['kind']
        known = {column['name'] for column in columns}
        requested = {mapping[field] for field in FIELDS if isinstance(mapping.get(field), str) and mapping[field]}
        requested.update(item['field'] for item in mapping.get('columns', []))
        time_column = config.get('timeColumn') or mapping.get('time')
        if time_column:
            requested.add(time_column)
        if requested - known:
            raise ValueError('映射包含来源表中不存在的字段：' + '、'.join(sorted(requested - known)))
        schema = config.get('schema') or (config['database'] if kind == 'mysql' else 'dbo')
        table = _quote(schema, kind) + '.' + _quote(config['table'], kind)
        selected = ','.join(_quote(name, kind) for name in sorted(requested)) if requested and not discovery else '*'
        sql = 'SELECT ' + (f'TOP ({int(limit)}) ' if limit and kind == 'sqlserver' else '') + selected + ' FROM ' + table
        start, end = _bounds(connection, request)
        clauses, args = [], []
        if (start or end) and not time_column:
            raise ValueError('增量同步须选择时间字段')
        for operator, bound in (('>=', start), ('<=', end)):
            if bound:
                clauses.append(_quote(time_column, kind) + operator + ('%s' if kind == 'mysql' else '?'))
                args.append(bound)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        if time_column:
            sql += ' ORDER BY ' + _quote(time_column, kind)
        if limit and kind == 'mysql':
            sql += ' LIMIT ' + str(int(limit))
        samples = _select(conn, sql, tuple(args))
        return {'tables': tables, 'columns': columns, 'samples': samples} if discovery else samples
    except ValueError:
        raise
    except Exception:
        raise ValueError('读取来源数据失败，请核对字段、时间类型及只读权限') from None
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def discover(db, payload):
    connection, credentials = _resolve(db, payload)
    try:
        if connection['kind'] != 'http':
            return _sql_rows(connection, credentials, payload, limit=5, discovery=True)
        rows = _http_rows(connection, credentials, payload, limit=5)
        def paths(row, prefix=''):
            result = []
            for key, value in row.items():
                name = prefix + str(key)
                result.extend(paths(value, name + '.') if isinstance(value, dict) else [name])
            return result
        return {'tables': [], 'columns': [{'name': name} for name in sorted({name for row in rows for name in paths(row)})],
                'samples': rows}
    except ValueError:
        raise
    except Exception:
        raise ValueError('读取来源结构失败，请核对连接及可读表权限') from None


def test_connection(db, payload):
    result = discover(db, payload)
    return {'status': 'connected', 'tables': len(result['tables']), 'columns': len(result['columns']),
            'sampleRecords': len(result['samples']), 'testedAt': now()}


def _stations(mapping):
    result = {row['id']: row for row in mapping.get('stations', [])}
    for value in mapping.get('stationMap', {}).values():
        if isinstance(value, dict):
            result[value['id']] = value
    return result


def _mapped(db, connection, rows):
    mapping = connection['mapping']
    defaults = mapping.get('defaults') or {}
    zone = mapping.get('timeZone', 'Asia/Shanghai')
    station_map, variable_map = mapping.get('stationMap', {}), mapping.get('variableMap', {})
    candidates = _stations(mapping)
    station_ids = {row['id'] for row in db.rows('SELECT id FROM stations')} | set(candidates)
    class StationView:
        def rows(self, sql):
            return [{'id': identifier} for identifier in station_ids]
    result = {}
    for index, raw in enumerate(rows, 1):
        try:
            common = {field: _at(raw, mapping[field]) for field in FIELDS if mapping.get(field)}
            definitions = mapping.get('columns') if mapping.get('format') == 'wide' else [None]
            if not definitions:
                raise ValueError('宽表须配置至少一个观测字段')
            for definition in definitions:
                record = {**defaults, **common}
                if definition:
                    record.update({key: definition[key] for key in ('variable', 'unit', 'step') if key in definition})
                    record['value'] = _at(raw, definition['field'])
                station = station_map.get(str(record.get('station', '')), record.get('station'))
                record['station'] = station['id'] if isinstance(station, dict) else station
                record['variable'] = variable_map.get(str(record.get('variable', '')), record.get('variable'))
                record.setdefault('step', 'irregular')
                value = record.get('value')
                missing_values = (definition or {}).get('missingValues', mapping.get('missingValues', []))
                if value is None or value == '' or any(value == marker or str(value) == str(marker) for marker in missing_values):
                    record.update(value=None, quality='missing')
                quality_map = mapping.get('qualityMap', {})
                if str(record.get('quality')) in quality_map:
                    record['quality'] = quality_map[str(record['quality'])]
                interval = (definition or {}).get('intervalMinutes', mapping.get('intervalMinutes'))
                if interval and not record.get('periodStart') and not record.get('periodEnd'):
                    mark = timestamp(record['time'], zone)
                    if mapping.get('timePosition', 'end') == 'start':
                        stop = mark + pd.Timedelta(minutes=float(interval))
                    else:
                        stop = mark
                    record['periodStart'] = (stop - pd.Timedelta(minutes=float(interval))).isoformat()
                    record['periodEnd'] = stop.isoformat()
                    if record['step'] != 'day':
                        record['time'] = stop.isoformat()
                    # Generated timestamps above already use the platform's local timezone.
                    if zone == 'UTC':
                        for field in ('time', 'periodStart', 'periodEnd'):
                            record[field] = pd.Timestamp(record[field]).tz_localize('Asia/Shanghai').tz_convert('UTC').isoformat()
                document = {'schemaVersion': 1, 'projectId': 'selinco', 'sourceKey': 'preview',
                            'timeZone': zone, 'records': [record]}
                normalized = normalize(StationView(), document)[0]
                key = tuple(normalized[field] for field in ('station', 'variable', 'step', 'time'))
                normalized.pop('row')
                if key in result and {k: v for k, v in result[key].items() if k != 'raw'} != {k: v for k, v in normalized.items() if k != 'raw'}:
                    raise ValueError('来源存在同一站点、要素和时次的冲突记录')
                result[key] = normalized
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f'来源第{index}条：{exc}') from None
    return list(result.values()), candidates


def _records(db, connection, credentials, request, limit=None, cancelled=None, progress=None):
    if connection['kind'] == 'http':
        rows = _http_rows(connection, credentials, request, limit, cancelled, progress)
    else:
        _check_cancel(cancelled)
        rows = _sql_rows(connection, credentials, request, limit)
    return rows, *_mapped(db, connection, rows)


def _exchange(row):
    return {**{key: row[key] for key in ('station', 'variable', 'step', 'time', 'unit', 'value', 'quality')},
            **({'periodStart': row['start'], 'periodEnd': row['end']} if row['start'] is not None else {})}


def preview(db, payload):
    connection, credentials = _resolve(db, payload)
    raw, records, stations = _records(db, connection, credentials, payload, limit=100)
    known = {row['id'] for row in db.rows('SELECT id FROM stations')}
    return {'sourceRecords': len(raw), 'records': len(records), 'samples': [_exchange(row) for row in records[:20]],
            'newStations': [value for key, value in stations.items() if key not in known],
            'start': min((row['time'] for row in records), default=None),
            'end': max((row['time'] for row in records), default=None), 'timeZone': 'Asia/Shanghai'}


def sync(db, request, folder=None, progress=None, cancelled=None):
    identifier = request['connectionId']
    connection = store.get(db, COLLECTION, identifier)
    at = now()
    receipt = {'connectionId': identifier, 'at': at, 'status': 'running'}
    connection['lastAttemptAt'] = at
    connection['lastSync'] = receipt
    store.save(db, COLLECTION, connection, identifier=identifier)
    try:
        credentials = _credentials(db, connection)
        raw, records, candidates = _records(db, connection, credentials, request,
                                             cancelled=cancelled, progress=progress)
        _check_cancel(cancelled)
        known = {row['id'] for row in db.rows('SELECT id FROM stations')}
        needed = {row['station']: row['variable'] for row in records if row['station'] not in known}
        with db.connect() as conn:
            for station, variable in needed.items():
                data = candidates[station]
                conn.execute('INSERT OR IGNORE INTO stations VALUES(?,?,?,?,?,?)',
                             (station, data.get('name') or station,
                              data.get('kind') or {'level': 'lake', 'flow': 'river'}.get(variable, 'rain'),
                              data.get('lon'), data.get('lat'),
                              canonical({'connectionId': identifier, 'datumVerified': False,
                                         'sourceMetadata': data.get('metadata', {})})))
        source_key = 'connection/' + identifier
        source_metadata = {'connectionId': identifier,
                           'levelDatum': connection['mapping'].get('levelDatum', {'status': 'unverified'})}
        prior_sources = db.rows('SELECT metadata FROM sources WHERE path=? ORDER BY rowid DESC LIMIT 1',
                               ('exchange/' + source_key,))
        metadata_changed = bool(prior_sources and
            json.loads(prior_sources[0]['metadata']).get('sourceMetadata') != source_metadata)
        previous = db.rows('''SELECT o.station_id AS station,o.variable,o.step,o.time,o.period_start AS start,
                    o.period_end AS end,o.unit,o.value,o.quality FROM observations o WHERE source_id=
                    (SELECT id FROM sources WHERE path=? ORDER BY rowid DESC LIMIT 1)''', ('exchange/' + source_key,))
        indexed = {tuple(row[field] for field in ('station', 'variable', 'step', 'time')): row for row in previous}
        changed = [row for row in records if indexed.get(tuple(row[field] for field in ('station', 'variable', 'step', 'time'))) !=
                   {key: value for key, value in row.items() if key != 'raw'}]
        sources = []
        imported = records if metadata_changed else changed
        for offset in range(0, len(imported), 50000):
            _check_cancel(cancelled)
            result = import_records(db, {'schemaVersion': 1, 'projectId': 'selinco', 'sourceKey': source_key,
                'timeZone': 'Asia/Shanghai', 'mode': 'append', 'duplicatePolicy': 'replace',
                'sourceMetadata': source_metadata,
                'records': [_exchange(row) for row in imported[offset:offset + 50000]]})
            sources.append(result['sourceId'])
            if progress:
                progress(70 + 25 * min(offset + 50000, len(imported)) / len(imported), '已整编观测资料')
        _check_cancel(cancelled)
        latest = max((row['time'] for row in records), default=None)
        current = connection.get('cursor', {}).get('time')
        if latest:
            connection['cursor'] = {'time': max(latest, current) if current else latest, 'updatedAt': now()}
        receipt.update(status='succeeded', sourceRecords=len(raw), records=len(records), changed=len(changed),
                       skipped=len(records) - len(changed), sources=sources,
                       variables=sorted({row['variable'] for row in records}), newStations=list(needed),
                       cursor=connection.get('cursor'), completedAt=now())
        connection['lastSuccessAt'] = receipt['completedAt']
    except Exception as exc:
        receipt.update(status='cancelled' if isinstance(exc, InterruptedError) else 'failed', completedAt=now(),
                       message=str(exc) if isinstance(exc, (ValueError, InterruptedError)) else '资料同步失败，请检查来源与映射配置')
        connection['lastSync'] = receipt
        store.save(db, COLLECTION, connection, identifier=identifier)
        store.save(db, HISTORY, receipt, prefix='SYNC-')
        if isinstance(exc, InterruptedError):
            raise InterruptedError(receipt['message']) from None
        raise ValueError(receipt['message']) from None
    connection['lastSync'] = receipt
    store.save(db, COLLECTION, connection, identifier=identifier)
    store.save(db, HISTORY, receipt, prefix='SYNC-')
    if folder:
        write_json(Path(folder) / '数据同步回执.json', receipt)
    if progress:
        progress(100, f'同步完成：更新{len(changed)}条，相同{len(records) - len(changed)}条')
    return receipt
