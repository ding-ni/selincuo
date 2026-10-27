"""One versioned observation contract for spreadsheet and future API adapters.

No remote fetching is performed here. File and JSON transports share validation,
unit conversion, source revision handling and the existing observation store.
"""
from collections import defaultdict
from pathlib import Path
import math
import shutil

import pandas as pd

from .common import canonical, digest, now, sha_file, within

VARIABLES = {
    'precipitation': {'mm': 1., 'm': 1000.},
    'temperature': {'degC': 1., 'K': 1.},
    'evaporation': {'mm': 1., 'm': 1000.},
    'flow': {'m3/s': 1., 'L/s': .001},
    'level': {'m': 1., 'cm': .01},
    'area': {'km2': 1., 'm2': 1e-6},
    'snowWaterEquivalent': {'mm': 1., 'm': 1000.},
}
UNITS = {k: next(iter(v)) for k, v in VARIABLES.items()}
STEPS = ('irregular', 'hour', 'day')
QUALITIES = ('valid', 'qualified', 'missing', 'ice_or_stopped')


def contract():
    return {'schemaVersion': 1, 'projectId': 'selinco',
            'observationEndpoint': '/api/model/data-import',
            'fileEndpoint': '/api/model/data-import-file',
            'templateEndpoint': '/api/model/data-template',
            'timeZone': 'Asia/Shanghai', 'variables': VARIABLES,
            'steps': STEPS, 'qualities': QUALITIES,
            'requiredColumns': ['station', 'variable', 'time', 'value', 'unit', 'step'],
            'optionalColumns': ['periodStart', 'periodEnd', 'quality'],
            'futureMeteorology': {'enabled': False, 'requiredFields': [
                'issueTime', 'validTime', 'leadHours', 'member', 'gridId', 'variable', 'value', 'unit'],
                'storage': 'separate-versioned-gridded-input-product'},
            'note': 'Point observations do not replace spatial meteorological forcing.'}


def timestamp(value, zone):
    try:
        time = pd.Timestamp(value)
        if pd.isna(time):
            raise ValueError()
        if time.tzinfo is None:
            time = time.tz_localize(zone)
        time = time.tz_convert('Asia/Shanghai').tz_localize(None)
        if time.microsecond or time.nanosecond:
            raise ValueError()
        return time
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('时间须为明确的日期时间，精确至秒') from exc


def normalize(db, document):
    if document.get('schemaVersion') != 1 or document.get('projectId') != 'selinco':
        raise ValueError('资料格式版本或项目身份不匹配')
    zone = document.get('timeZone', 'Asia/Shanghai')
    if zone not in ('Asia/Shanghai', 'UTC'):
        raise ValueError('时间定义须为Asia/Shanghai或UTC')
    source = document.get('sourceKey')
    if not isinstance(source, str) or not source.strip() or len(source) > 160:
        raise ValueError('须指定稳定的资料来源编号sourceKey')
    rows = document.get('records')
    if not isinstance(rows, list) or not 0 < len(rows) <= 50000:
        raise ValueError('每批应有1至50000条记录')
    stations = {s['id'] for s in db.rows('SELECT id FROM stations')}
    normalized, seen = [], set()
    for index, row in enumerate(rows, 1):
        try:
            station, variable, step = row['station'], row['variable'], row['step']
            if station not in stations or variable not in VARIABLES or step not in STEPS:
                raise ValueError('站点、变量或时间尺度不支持')
            unit = row['unit']
            if unit not in VARIABLES[variable]:
                raise ValueError('单位不支持，禁止猜测单位')
            time = timestamp(row['time'], zone)
            if step == 'day' and time != time.normalize():
                raise ValueError('逐日资料时间须为当地日00:00')
            quality = row.get('quality', 'valid')
            if quality not in QUALITIES:
                raise ValueError('质量标记不支持')
            raw = row.get('value')
            if quality in ('missing', 'ice_or_stopped'):
                value = None
            else:
                if isinstance(raw, bool) or raw is None or str(raw).strip() == '':
                    raise ValueError('有效记录缺少数值')
                value = float(raw) * VARIABLES[variable][unit]
                if variable == 'temperature' and unit == 'K':
                    value -= 273.15
                if not math.isfinite(value):
                    raise ValueError('数值须有限')
                if variable in ('precipitation', 'evaporation', 'flow', 'area', 'snowWaterEquivalent') and value < 0:
                    raise ValueError('降水、蒸发、流量、面积及雪水当量不得为负')
            a, b = row.get('periodStart'), row.get('periodEnd')
            if step == 'day':
                a = timestamp(a, zone) if a else time
                b = timestamp(b, zone) if b else time + pd.Timedelta(days=1)
            elif step == 'hour':
                a = timestamp(a, zone) if a else time - pd.Timedelta(hours=1)
                b = timestamp(b, zone) if b else time
            else:
                a = timestamp(a, zone) if a else None
                b = timestamp(b, zone) if b else None
            if (a is None) != (b is None) or (a is not None and a >= b):
                raise ValueError('统计时段起止须成对且递增')
            if variable in ('precipitation', 'evaporation') and step == 'irregular' and a is None:
                raise ValueError('不规则时段增量须提供periodStart与periodEnd')
            if a is not None and not (a <= time <= b):
                raise ValueError('记录时间须位于其统计时段内')
            if step == 'day' and (a != time or b != time + pd.Timedelta(days=1)):
                raise ValueError('日统计时段须对应标记日期的完整当地日')
            if step == 'hour' and (b != time or b-a != pd.Timedelta(hours=1) or time.minute or time.second):
                raise ValueError('小时记录须标记整点区间结束时刻')
            key = (station, variable, step, time.isoformat())
            if key in seen:
                raise ValueError('批内存在重复站点、变量与时次')
            seen.add(key)
            normalized.append({'station': station, 'variable': variable, 'step': step,
                               'time': time.isoformat(), 'start': a.isoformat() if a is not None else None,
                               'end': b.isoformat() if b is not None else None, 'unit': UNITS[variable],
                               'value': value, 'quality': quality, 'raw': '' if raw is None else str(raw), 'row': index})
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f'第{index}条：{exc}') from exc
    return normalized


def import_records(db, document, archive=None):
    records = normalize(db, document)  # Validate every row before any database mutation.
    logical = 'exchange/' + document['sourceKey'].strip()
    identity = {'schemaVersion': 1, 'sourceKey': logical,
                'records': sorted([{k:v for k,v in r.items() if k != 'row'} for r in records],
                                  key=lambda r:(r['station'],r['variable'],r['step'],r['time']))}
    checksum = digest(identity)
    identifier = 'SRC-' + checksum[:24]
    metadata = {'adapter': 'observation-exchange-v1', 'timeZone': document.get('timeZone', 'Asia/Shanghai'),
                'sourceKey': document['sourceKey'].strip(), 'fullSnapshot': True,
                'schemaVersion': 1}
    groups = defaultdict(list)
    for row in records:
        groups[(row['station'], row['variable'], row['step'])].append(row)
    with db.connect() as conn:
        exists = conn.execute('SELECT id FROM sources WHERE id=?', (identifier,)).fetchone()
        if exists:
            latest = conn.execute('SELECT id FROM sources WHERE path=? ORDER BY rowid DESC LIMIT 1', (logical,)).fetchone()
            return {'status': 'unchanged', 'sourceId': identifier, 'records': len(records),
                    'active': latest['id'] == identifier}
        if archive:
            archive = Path(archive)
            dest = db.workspace / 'observation-sources' / identifier / archive.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archive, dest)
            metadata.update(archivedPath=dest.relative_to(db.workspace).as_posix(), fileSHA256=sha_file(dest))
        conn.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',
                     (identifier, logical, checksum, len(canonical(identity).encode('utf-8')), now(), canonical(metadata)))
        for row in records:
            conn.execute('''INSERT INTO observations(source_id,station_id,variable,time,period_start,period_end,step,unit,value,raw_text,quality,sheet,cell) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                         (identifier, row['station'], row['variable'], row['time'], row['start'], row['end'],
                          row['step'], row['unit'], row['value'], row['raw'], row['quality'], 'data', str(row['row'])))
        for (station, variable, step), block in groups.items():
            conn.execute('INSERT INTO coverage VALUES(?,?,?,?,?,?,?,?,?)',
                         (identifier, station, variable, step,
                          min(r['start'] or r['time'] for r in block), max(r['end'] or r['time'] for r in block),
                          sum(r['value'] is not None for r in block), len(block),
                          canonical({'coverageConfirmed': False, 'zeroForAbsent': False,
                                     'policy': 'explicit-records-only; missing intervals are not zero'})))
        conn.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                     (now(), 'observations.exchange', canonical({'id': identifier, 'records': len(records)})))
    return {'status': 'imported', 'sourceId': identifier, 'records': len(records), 'active': True}


def import_file(db, request):
    """Map known export headers once; no reliance on filename or directory names."""
    path = within(db.workspace / 'incoming', request['filename'])
    mapping = request.get('mapping', {})
    if not isinstance(mapping, dict) or set(mapping)-set(contract()['requiredColumns']+contract()['optionalColumns']):
        raise ValueError('字段映射包含未知字段')
    if path.suffix.lower() == '.xlsx':
        frame = pd.read_excel(path, sheet_name=request.get('sheet', 0), dtype=object)
    elif path.suffix.lower() == '.csv':
        frame = pd.read_csv(path, dtype=object, encoding='utf-8-sig')
    else:
        raise ValueError('统一导入支持xlsx或UTF-8 CSV；旧版xls继续使用原资料适配器')
    if len(set(frame.columns)) != len(frame.columns):
        raise ValueError('表头重复')
    if any(column not in frame.columns for column in mapping.values()):
        raise ValueError('字段映射的表头不存在')
    frame = frame.rename(columns={source: target for target, source in mapping.items()})
    if len(set(frame.columns)) != len(frame.columns) or len(set(mapping.values())) != len(mapping):
        raise ValueError('字段映射必须一一对应')
    records = []
    defaults = request.get('defaults', {})
    for row in frame.to_dict('records'):
        normalized = {k: v for k, v in row.items() if k in contract()['requiredColumns']+contract()['optionalColumns'] and pd.notna(v)}
        records.append({**defaults, **normalized})
    return import_records(db, {**request, 'records': records}, path)
