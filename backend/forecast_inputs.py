"""Independent future weather scenarios on the frozen hydrological support."""
from copy import deepcopy
from datetime import datetime, timezone
import gzip
from http.client import IncompleteRead
import json
from pathlib import Path
import time
import uuid
import zlib
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from .common import now, within, write_json
from . import operational_store as store

COLLECTION = 'forecast-inputs'
ENSEMBLE_MODEL = 'ecmwf_ifs025_ensemble'
ENSEMBLE_URL = 'https://ensemble-api.open-meteo.com/v1/ensemble'


def hour(value, label='时刻'):
    time = pd.Timestamp(value)
    if pd.isna(time) or time.tzinfo or time.minute or time.second or time.microsecond:
        raise ValueError(label + '须为北京时间整小时')
    return time


def _context(db, payload):
    from .forecasts import source_context
    if not payload.get('sourceResultId'):
        raise ValueError('请选择初始状态来源成果')
    return source_context(db, payload['sourceResultId'])


def _regions(spatial):
    result = {'uniform': list(range(len(spatial['units'])))}
    for i, unit in enumerate(spatial['units']):
        result.setdefault(unit['partition'], []).append(i)
        basin = unit.get('basin', unit['partition'])
        if basin != unit['partition']:
            result.setdefault(basin, []).append(i)
    return result


def _weights(spatial):
    if spatial.get('aggregation'):
        matrix = np.zeros((len(spatial['units']), len(spatial['grids'])))
        for i, group in enumerate(spatial['aggregation']):
            for member in group['members']:
                matrix[i, member['gridIndex']] += member['areaWeight']
        return matrix
    matrix = np.zeros((len(spatial['units']), len(spatial['grids'])))
    for i, unit in enumerate(spatial['units']):
        matrix[i, unit['gridIndex']] = 1.
    return matrix


def seasonal_evaporation(db, context, times, reference_input_id=None):
    """Month/hour E0 from actual source ERA values, restricted to pre-issue data."""
    from .grid_simulation import prepare
    source_id = reference_input_id or context['state']['inputId']
    source = prepare(db, source_id, context['spatial']['version'])
    prior = source['times'] <= times[0] - pd.Timedelta(hours=1)
    if not prior.any(): raise ValueError('没有起报前的ERA蒸发能力资料')
    spatial = context['spatial']; weights = _weights(spatial)
    previous = source['times'][prior]; raw = source['arrays']['evaporation']
    month = (previous - pd.Timedelta(nanoseconds=1)).month
    hour_of_day = previous.hour
    samples = {}; unit = np.empty((len(times), len(spatial['units']))); lake = np.empty(len(times))
    old_indexes = np.flatnonzero(prior)
    for i, time in enumerate(times):
        key = ((time - pd.Timedelta(nanoseconds=1)).month, time.hour)
        if key not in samples:
            ids = old_indexes[(month == key[0]) & (hour_of_day == key[1])]
            if not len(ids): raise ValueError(f'历史ERA未覆盖{key[0]}月{key[1]}时的蒸发参考')
            values = np.mean(raw[ids], axis=0)
            samples[key] = (values @ weights.T, float(values @ np.asarray(spatial['lakeWeights'])), len(ids))
        unit[i], lake[i], _ = samples[key]
    return unit, lake, {'method': 'seasonal-hourly-original-era', 'referenceInputId': source_id,
                        'referenceEnd': previous[-1].isoformat(),
                        'samples': [{'month': m, 'hour': h, 'count': v[2]} for (m, h), v in sorted(samples.items())],
                        'unit': 'mm/h potential open-water evaporation'}


def preview_evaporation(db, payload):
    issue = hour(payload.get('issueTime'), '起报时刻')
    days = int(payload.get('days', 10))
    if not 7 <= days <= 10 or issue.hour:
        raise ValueError('逐日蒸发参考须从北京时间00时起报，支持7—10天')
    context = _context(db, payload)
    times = pd.date_range(issue + pd.Timedelta(hours=1), periods=days * 24, freq='h')
    unit, lake, basis = seasonal_evaporation(db, context, times, (payload.get('evaporation') or {}).get('referenceInputId'))
    spatial = context['spatial']; mapping = _regions(spatial)
    areas = np.array([u['areaKm2'] for u in spatial['units']])
    daily = []
    for d in range(days):
        block = slice(d * 24, (d + 1) * 24)
        totals = unit[block].sum(axis=0)
        regional = {key: float(np.average(totals[indexes], weights=areas[indexes])) for key, indexes in mapping.items()}
        daily.append({'date': (issue + pd.Timedelta(days=d)).date().isoformat(),
                      'evaporationMm': regional['uniform'], 'regions': regional,
                      'lakeEvaporationMm': float(lake[block].sum())})
    return {'sourceResultId': payload['sourceResultId'], 'issueTime': issue.isoformat(),
            'days': days, 'unit': 'mm/day', 'daily': daily, 'basis': basis}


def precipitation_ratios(db, payload):
    """Same-month ERA precipitation of each sub-basin relative to the basin mean, before issue."""
    from .grid_simulation import prepare
    issue = hour(payload.get('issueTime'), '起报时刻')
    context = _context(db, payload)
    spatial = context['spatial']
    source = prepare(db, context['state']['inputId'], spatial['version'])
    times = source['times']
    month = (times - pd.Timedelta(nanoseconds=1)).month
    ids = np.flatnonzero((month == issue.month) & (times <= issue))
    if not len(ids):
        raise ValueError('历史ERA未覆盖同月降水')
    totals = source['arrays']['precipitation'][ids].sum(axis=0)
    unit = totals @ _weights(spatial).T
    areas = np.array([u['areaKm2'] for u in spatial['units']])
    mapping = _regions(spatial)
    basin = float(np.average(unit, weights=areas))
    if basin <= 0:
        raise ValueError('历史同月降水为零，不能按比例分配')
    ratios = {key: float(np.average(unit[idx], weights=areas[idx]) / basin) for key, idx in mapping.items() if key != 'uniform'}
    ratios['lake'] = float(totals @ np.asarray(spatial['lakeWeights']) / basin)
    years = len(set((times[ids] - pd.Timedelta(nanoseconds=1)).year))
    return {'month': int(issue.month), 'years': years, 'basinMonthlyMm': basin / max(1, years), 'ratios': ratios}


def _save(db, context, payload, times, arrays, metadata, member_ids=None):
    identifier = 'FWEATHER-' + uuid.uuid4().hex[:20]
    folder = db.workspace / '预报气象' / identifier
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / '逐时气象.npz'
    members = member_ids or ['control']
    expected = (len(members), len(times), len(context['spatial']['units']))
    for key in ('precipitation', 'temperature', 'evaporation'):
        if np.shape(arrays[key]) != expected or not np.isfinite(arrays[key]).all():
            raise ValueError('未来逐时' + key + '数据不完整')
    for key in ('lakePrecipitation', 'lakeEvaporation', 'lakeTemperature'):
        if np.shape(arrays[key]) != expected[:2] or not np.isfinite(arrays[key]).all():
            raise ValueError('未来逐时湖面气象数据不完整')
    for key in ('precipitation', 'evaporation', 'lakePrecipitation', 'lakeEvaporation'):
        if np.any(arrays[key] < 0): raise ValueError('未来降水和蒸发不能为负')
    np.savez_compressed(path, time=times.asi8, **arrays)
    value = {'name': payload.get('name') or '未来气象输入', 'sourceResultId': payload['sourceResultId'],
             'spatialVersion': context['spatial']['version'], 'unitIds': [u['id'] for u in context['spatial']['units']],
             'unitAreasKm2': [u['areaKm2'] for u in context['spatial']['units']],
             'issueTime': (times[0] - pd.Timedelta(hours=1)).isoformat(), 'start': times[0].isoformat(),
             'end': times[-1].isoformat(), 'hours': len(times), 'memberIds': members, 'memberCount': len(members),
             'requestedDays': metadata.get('requestedDays', payload.get('days', 10)), 'actualDays': len(times) / 24.,
             'dataPath': path.relative_to(db.workspace).as_posix(), 'metadata': metadata,
             'temperatureSupport': 'model-unit-elevation', 'precipitationSupport': 'scenario-final-values',
             'warmRainCorrectionApplied': False, 'timeZone': 'Asia/Shanghai',
             'timeMeaning': 'end of hourly accumulation interval',
             'daily': deepcopy(payload.get('daily', []))}
    saved = store.save(db, COLLECTION, value, identifier=identifier)
    write_json(folder / '气象输入说明.json', saved)
    return saved


def catalog(db):
    return {'items': store.list_records(db, COLLECTION), 'defaultDays': 10,
            'minimumDays': 7, 'maximumDays': 10, 'ensembleDefaultDays': 15, 'ensembleMaximumDays': 15,
            'ensembleModel': ENSEMBLE_MODEL, 'ensembleMembers': 51}


def detail(db, identifier):
    record = store.get(db, COLLECTION, identifier)
    with np.load(within(db.workspace, record['dataPath']), allow_pickle=False) as archive:
        times = pd.DatetimeIndex(archive['time'].astype('datetime64[ns]'))
        weights = np.asarray(record['unitAreasKm2'], dtype=float)
        weights = weights / weights.sum()
        record['preview'] = [{'time': t.isoformat(), 'precipitationMm': float(archive['precipitation'][0, i] @ weights),
                              'temperatureC': float(archive['temperature'][0, i] @ weights),
                              'lakePrecipitationMm': float(archive['lakePrecipitation'][0, i]),
                              'lakeEvaporationMm': float(archive['lakeEvaporation'][0, i])} for i, t in enumerate(times)]
    return record


def _rain(total, first, last, shape):
    first, last, total = float(first), float(last), float(total)
    if not np.isfinite([first, last, total]).all() or total < 0 or first < 0 or last > 24 or first >= last:
        raise ValueError('日雨量须非负，降雨时段须在0—24时内且起点早于终点')
    edges = np.arange(25, dtype=float)
    left, right = np.maximum(edges[:-1], first), np.minimum(edges[1:], last)
    width = last - first
    if shape == 'uniform': weights = np.maximum(0, right - left)
    elif shape == 'triangle':
        def integral(x):
            z = np.clip((x - first) / width, 0, 1)
            return np.where(z <= .5, 2 * z * z, 1 - 2 * (1 - z) ** 2)
        weights = np.maximum(0, integral(right) - integral(left))
    else: raise ValueError('降雨过程仅支持均匀或三角形分配')
    weights = weights / weights.sum()
    return weights * total


def _temperature(spatial, regions, indexes, value, reference=None):
    units = spatial['units']; elevations = np.array([u['elevation'] for u in units])
    areas = np.array([units[i]['areaKm2'] for i in indexes])
    origin = float(reference) if reference is not None else float(np.average(elevations[indexes], weights=areas))
    if not np.isfinite([float(value), origin]).all(): raise ValueError('气温与参考高程须为有效数值')
    lapse = np.array([regions.get(units[i]['partition'], regions['_base'])['lapseRate'] for i in indexes])
    return float(value) + (elevations[indexes] - origin) * lapse


def _evaporation_value(value):
    result = float(value)
    if not np.isfinite(result) or result < 0: raise ValueError('蒸发能力须为非负有效数值')
    return result


def create_manual(db, payload):
    context = _context(db, payload); spatial = context['spatial']; regions = {**context['regions'], '_base': context['parameters']}
    issue = hour(payload.get('issueTime'), '起报时刻')
    days = int(payload.get('days', 10))
    if not 7 <= days <= 10: raise ValueError('手动情景支持7—10天')
    daily = payload.get('daily', [])
    if len(daily) != days or issue.hour:
        raise ValueError('逐日情景须从北京时间00时起报，并逐日填齐7—10天')
    times = pd.date_range(issue + pd.Timedelta(hours=1), periods=days * 24, freq='h')
    count = len(spatial['units']); p = np.empty((len(times), count)); t = np.empty_like(p)
    explicit_evap = np.full_like(p, np.nan); explicit_lake_evap = np.full(len(times), np.nan)
    lake_p = np.empty(len(times)); lake_t = np.empty(len(times)); mapping = _regions(spatial)
    for d, row in enumerate(daily):
        date = issue.normalize() + pd.Timedelta(days=d)
        if row.get('date') and pd.Timestamp(row['date']).normalize() != date:
            raise ValueError('逐日情景日期须连续且不重复')
        block = slice(d * 24, (d + 1) * 24)
        rain = _rain(row['precipitationMm'], row.get('rainStartHour', 0), row.get('rainEndHour', 24), row.get('rainShape', 'uniform'))
        temp = float(row['temperatureC'])
        p[block] = rain[:, None]; t[block] = _temperature(spatial, regions, mapping['uniform'], temp, row.get('temperatureReferenceElevationM'))
        if row.get('evaporationMm') is not None: explicit_evap[block] = _evaporation_value(row['evaporationMm']) / 24.
        overrides = row.get('overrides') or {}
        for region in sorted(overrides, key=lambda r: -len(mapping.get(r, []))):
            override = overrides[region]
            if region not in mapping or region == 'uniform': raise ValueError('未知预报赋值区域：' + region)
            indexes = mapping[region]
            if 'precipitationMm' in override:
                local = _rain(override['precipitationMm'], override.get('rainStartHour', row.get('rainStartHour', 0)), override.get('rainEndHour', row.get('rainEndHour', 24)), override.get('rainShape', row.get('rainShape', 'uniform')))
                p[block, indexes] = local[:, None]
            if 'temperatureC' in override:
                t[block, indexes] = _temperature(spatial, regions, indexes, override['temperatureC'], override.get('temperatureReferenceElevationM'))
            if override.get('evaporationMm') is not None:
                explicit_evap[block, indexes] = _evaporation_value(override['evaporationMm']) / 24.
        lake = row.get('lake') or {}
        lake_p[block] = _rain(lake.get('precipitationMm', row['precipitationMm']), lake.get('rainStartHour', row.get('rainStartHour', 0)), lake.get('rainEndHour', row.get('rainEndHour', 24)), lake.get('rainShape', row.get('rainShape', 'uniform')))
        lake_t[block] = float(lake.get('temperatureC', temp))
        if lake.get('evaporationMm', row.get('evaporationMm')) is not None:
            explicit_lake_evap[block] = _evaporation_value(lake.get('evaporationMm', row.get('evaporationMm'))) / 24.
    evap, lake_evap, basis = seasonal_evaporation(db, context, times, (payload.get('evaporation') or {}).get('referenceInputId'))
    override_count = int(np.isfinite(explicit_evap).sum())
    lake_override_count = int(np.isfinite(explicit_lake_evap).sum())
    evap = np.where(np.isfinite(explicit_evap), explicit_evap, evap)
    lake_evap = np.where(np.isfinite(explicit_lake_evap), explicit_lake_evap, lake_evap)
    basis.update(manualUnitHours=override_count, manualLakeHours=lake_override_count,
                 manualDefinition='daily E0 divided evenly over 24 hours; lakeCoefficient applied only in lake balance')
    return _save(db, context, payload, times, {'precipitation': p[None], 'temperature': t[None], 'evaporation': evap[None],
                 'lakePrecipitation': lake_p[None], 'lakeEvaporation': lake_evap[None], 'lakeTemperature': lake_t[None]},
                 {'kind': 'manual-daily', 'evaporation': basis, 'spatialAssignment': 'uniform-with-explicit-region-overrides',
                  'temperature': 'regional mean at area-weighted elevation; one lapse correction to each unit',
                  'precipitation': 'user scenario, no historical station multiplier', 'source': payload.get('source', '用户手动天气情景')})


def _excel_rows(path):
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = book.active; values = iter(sheet.values); headers = next(values, ())
        aliases = {'时间': 'time', '区域': 'region', '降水(mm)': 'precipitationMm', '气温(℃)': 'temperatureC',
                   '蒸发(mm)': 'evaporationMm', '参考高程(m)': 'temperatureReferenceElevationM'}
        names = [aliases.get(str(v).strip(), str(v).strip()) for v in headers]
        return [{key: value for key, value in zip(names, row) if value is not None} for row in values if any(v is not None for v in row)]
    finally: book.close()


def import_hourly(db, payload):
    context = _context(db, payload); spatial = context['spatial']; regions = {**context['regions'], '_base': context['parameters']}
    rows = payload.get('rows')
    source_path = payload.get('path')
    if rows is None:
        if not source_path: raise ValueError('请提供逐时Excel或逐时记录')
        rows = _excel_rows(within(db.workspace, source_path))
    issue = hour(payload['issueTime'], '起报时刻'); days = int(payload.get('days', 10))
    if not 7 <= days <= 10: raise ValueError('逐时情景支持7—10天')
    times = pd.date_range(issue + pd.Timedelta(hours=1), periods=days * 24, freq='h'); slots = {t: i for i, t in enumerate(times)}
    p = np.full((len(times), len(spatial['units'])), np.nan); t = np.full_like(p, np.nan); e = np.full_like(p, np.nan)
    lp = np.full(len(times), np.nan); lt = lp.copy(); le = lp.copy(); mapping = _regions(spatial); seen = set()
    normalized = []
    for row in rows:
        time = hour(row['time']); region = str(row.get('region', 'uniform')); key = (time, region)
        if key in seen or time not in slots: raise ValueError('逐时输入时刻重复或超出所选时段')
        seen.add(key); normalized.append((time, region, row))
    # Global assignments precede explicit regions, independent of spreadsheet row order.
    for time, region, row in sorted(normalized, key=lambda r: (r[0], r[1] != 'uniform')):
        i = slots[time]
        if region == 'lake':
            lp[i] = float(row['precipitationMm']); lt[i] = float(row['temperatureC'])
            if 'evaporationMm' in row: le[i] = _evaporation_value(row['evaporationMm'])
            continue
        if region not in mapping: raise ValueError('未知逐时赋值区域：' + region)
        indexes = mapping[region]; p[i, indexes] = float(row['precipitationMm'])
        t[i, indexes] = _temperature(spatial, regions, indexes, row['temperatureC'], row.get('temperatureReferenceElevationM'))
        if 'evaporationMm' in row: e[i, indexes] = _evaporation_value(row['evaporationMm'])
        if region == 'uniform':
            lp[i] = p[i, 0]; lt[i] = float(row['temperatureC'])
            if 'evaporationMm' in row: le[i] = _evaporation_value(row['evaporationMm'])
    if not np.isfinite(p).all() or not np.isfinite(t).all() or not np.isfinite(lp).all():
        raise ValueError('逐时Excel未完整覆盖各单元和湖面的降水、气温；缺测不补零')
    seasonal, seasonal_lake, basis = seasonal_evaporation(db, context, times, (payload.get('evaporation') or {}).get('referenceInputId'))
    e = np.where(np.isfinite(e), e, seasonal); le = np.where(np.isfinite(le), le, seasonal_lake)
    return _save(db, context, payload, times, {'precipitation': p[None], 'temperature': t[None], 'evaporation': e[None],
                 'lakePrecipitation': lp[None], 'lakeEvaporation': le[None], 'lakeTemperature': lt[None]},
                 {'kind': 'hourly-excel', 'sourceFile': source_path, 'evaporation': basis,
                  'temperature': 'one correction from declared region reference elevation', 'records': len(rows)})


def parse_ensemble(response, requested_times=None, allow_tail=False):
    hourly = response.get('hourly') or {}; units = response.get('hourly_units') or {}
    times = pd.DatetimeIndex(pd.to_datetime(hourly.get('time', []), utc=True)).tz_convert('Asia/Shanghai').tz_localize(None)
    if not len(times) or (len(times) > 1 and not np.all(np.diff(times.asi8) == 3600 * 10**9)):
        raise ValueError('集合天气时间轴缺失或非连续小时')
    indexes = np.arange(len(times))
    if requested_times is not None:
        indexes = times.get_indexer(requested_times)
        if np.any(indexes < 0):
            first_missing = int(np.flatnonzero(indexes < 0)[0])
            if not allow_tail or first_missing == 0 or np.any(indexes[first_missing:] >= 0):
                raise ValueError('集合天气实际覆盖不足所选预见期；未延长或补零')
            indexes = indexes[:first_missing]
    member_keys = {}
    for key in hourly:
        if key == 'precipitation': member_keys['control'] = key
        elif key.startswith('precipitation_member'):
            member_keys['member' + key.rsplit('member', 1)[1].zfill(2)] = key
    ids = sorted(member_keys, key=lambda x: -1 if x == 'control' else int(x[6:]))
    if len(ids) != 51: raise ValueError(f'ECMWF集合须有51个完整成员，实际为{len(ids)}')
    values = {}; common_hours = len(indexes)
    for variable, prefix in [('precipitation', 'precipitation'), ('temperature', 'temperature_2m')]:
        columns = []
        for member in ids:
            key = prefix + member_keys[member][len('precipitation'):]
            if key not in hourly: raise ValueError('集合天气缺少' + key)
            array = np.asarray(hourly[key], dtype=float)
            if array.shape != (len(times),): raise ValueError('集合天气逐时数组长度不一致')
            array = array[indexes]
            finite = np.isfinite(array)
            if not finite.all():
                first_missing = int(np.flatnonzero(~finite)[0])
                if not allow_tail or first_missing == 0 or finite[first_missing:].any():
                    raise ValueError('集合天气存在中间缺测或起点缺测；不能补零')
                common_hours = min(common_hours, first_missing)
            unit = units.get(key, units.get(prefix))
            if unit != ('mm' if variable == 'precipitation' else '°C'):
                raise ValueError('集合天气单位不符合毫米及摄氏度要求')
            if variable == 'precipitation': array = np.maximum(array, 0.)
            columns.append(array)
        values[variable] = np.stack(columns)
    return times[indexes[:common_hours]], ids, {k: v[:, :common_hours] for k, v in values.items()}


def _request_json(url):
    request = Request(url, headers={'User-Agent': 'SelincoPlatform/forecast',
                                    'Accept-Encoding': 'gzip'})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=60) as response:
                body = response.read()
                if response.headers.get('Content-Encoding') == 'gzip':
                    body = gzip.decompress(body)
            return json.loads(body.decode('utf-8'))
        except HTTPError as error:
            if (error.code not in (408, 425, 429) and error.code < 500) or attempt == 2:
                raise
        except (IncompleteRead, URLError, TimeoutError, ConnectionError, EOFError,
                gzip.BadGzipFile, zlib.error, UnicodeDecodeError, json.JSONDecodeError):
            if attempt == 2:
                raise ValueError('Open-Meteo集合响应截断或不可解析，已重试3次') from None
        time.sleep(.25 * (2 ** attempt))


def fetch_ensemble(db, request, folder=None, progress=None, cancelled=None):
    context = _context(db, request); spatial = context['spatial']; grids = spatial['grids']
    issue = hour(request.get('issueTime') or pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None).floor('h'), '起报时刻')
    days = int(request.get('days', 15))
    if not 7 <= days <= 15: raise ValueError('集合天气支持请求7—15天，以实际共同完整覆盖为准')
    # ECMWF IFS025 support; multiple model grid points share one downloaded cell.
    cells = sorted({(round(float(g['lat']) * 4) / 4, round(float(g['lon']) * 4) / 4) for g in grids})
    grid_to_cell = [cells.index((round(float(g['lat']) * 4) / 4, round(float(g['lon']) * 4) / 4)) for g in grids]
    identifier = 'FETCH-' + uuid.uuid4().hex[:12]
    output = Path(folder) if folder else db.workspace / '预报气象' / identifier
    output.mkdir(parents=True, exist_ok=True); all_data = []; selected_times = None; members = None; provenance = []
    locations_per_request = 4
    for offset in range(0, len(cells), locations_per_request):
        if cancelled and cancelled(): raise InterruptedError('已停止集合天气获取')
        points = cells[offset:offset + locations_per_request]
        params = {'latitude': ','.join(str(p[0]) for p in points), 'longitude': ','.join(str(p[1]) for p in points),
                  'hourly': 'temperature_2m,precipitation', 'models': ENSEMBLE_MODEL,
                  'elevation': ','.join('nan' for _ in points),
                  'cell_selection': 'nearest', 'timezone': 'UTC', 'forecast_days': min(16, days + 1)}
        url = ENSEMBLE_URL + '?' + urlencode(params)
        fetched = now(); response = _request_json(url)
        rows = response if isinstance(response, list) else [response]
        if len(rows) != len(points): raise ValueError('集合天气返回的格点数量不符')
        write_json(output / f'集合气象原响应-{offset // locations_per_request + 1:03d}.json', response)
        for point, row in zip(points, rows):
            wanted = pd.date_range(issue + pd.Timedelta(hours=1), periods=days * 24, freq='h')
            times, ids, data = parse_ensemble(row, wanted, allow_tail=True)
            if selected_times is None: selected_times, members = wanted, ids
            if ids != members: raise ValueError('集合天气各格点成员不一致')
            all_data.append(data)
            negative = [float(value) for key, values in (row.get('hourly') or {}).items()
                        if key.startswith('precipitation') for value in values if value is not None and float(value) < 0]
            provenance.append({'requestedLatitude': point[0], 'requestedLongitude': point[1],
                               'returnedLatitude': row.get('latitude'), 'returnedLongitude': row.get('longitude'),
                               'utcOffsetSeconds': row.get('utc_offset_seconds'), 'elevation': row.get('elevation'),
                               'modelRunTime': row.get('model_run_time') or row.get('initialization_time'),
                               'retrievedAt': fetched, 'url': url, 'actualStart': times[0].isoformat(), 'actualEnd': times[-1].isoformat(),
                               'negativePrecipitationCount': len(negative), 'negativePrecipitationMinimumMm': min([0., *negative]),
                               'negativePrecipitationSumMm': sum(negative)})
        if progress: progress(80 * min(offset + len(points), len(cells)) / len(cells), f'获取ECMWF集合格点 {min(offset + len(points), len(cells))}/{len(cells)}')
    common_hours = min(x['precipitation'].shape[1] for x in all_data)
    selected_times = selected_times[:common_hours]
    raw_p = np.stack([x['precipitation'][:, :common_hours] for x in all_data], axis=2)[:, :, grid_to_cell]
    raw_t = np.stack([x['temperature'][:, :common_hours] for x in all_data], axis=2)[:, :, grid_to_cell]
    weights = _weights(spatial); p = raw_p @ weights.T; temp = raw_t @ weights.T
    for i, unit in enumerate(spatial['units']):
        if spatial.get('aggregation'):
            dz = sum(m['areaWeight'] * m['elevationOffsetM'] for m in spatial['aggregation'][i]['members'])
        else: dz = unit['elevation'] - grids[unit['gridIndex']]['referenceElevation']
        temp[:, :, i] += dz * context['regions'].get(unit['partition'], context['parameters'])['lapseRate']
    evap, lake_evap, basis = seasonal_evaporation(db, context, selected_times, (request.get('evaporation') or {}).get('referenceInputId'))
    lake_weights = np.asarray(spatial['lakeWeights'])
    metadata = {'kind': 'ensemble-api', 'provider': 'Open-Meteo', 'model': ENSEMBLE_MODEL, 'memberCount': len(members),
                'generationTime': now(), 'providerRunTime': next((x['modelRunTime'] for x in provenance if x['modelRunTime']), None),
                'requestedDays': days, 'actualHours': common_hours, 'actualDays': common_hours / 24.,
                'coverageTruncated': common_hours < days * 24,
                'locationsPerRequest': locations_per_request,
                'elevationParameter': 'nan', 'cellSelection': 'nearest', 'temperatureCorrection': 'one model elevation correction',
                'temperatureElevationReference': 'existing source DEM cell averages; ECMWF model orography is not supplied by this endpoint',
                'biasCorrection': 'none; future weather separate from historical warm-rain factors', 'evaporation': basis,
                'negativePrecipitationTreatment': 'finite negative provider precipitation clipped to zero; null values remain missing',
                'gridToDownloadedCell': grid_to_cell, 'downloadedCells': provenance,
                'sourceFolder': output.relative_to(db.workspace).as_posix() if db.workspace in output.parents else str(output),
                'memberDependence': 'one ECMWF ensemble; probabilities are uncalibrated member proportions'}
    saved = _save(db, context, request, selected_times, {'precipitation': p, 'temperature': temp,
                  'evaporation': np.broadcast_to(evap, p.shape).copy(), 'lakePrecipitation': raw_p @ lake_weights,
                  'lakeEvaporation': np.broadcast_to(lake_evap, (len(members), len(selected_times))).copy(),
                  'lakeTemperature': raw_t @ lake_weights}, metadata, members)
    if progress: progress(100, f'51成员未来气象已保存，共同有效覆盖{common_hours}小时')
    return saved
