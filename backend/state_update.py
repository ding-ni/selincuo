"""实况状态更新：把连续模型状态逐日推进到最近的日界，并保存为新的检查点。

逐日判定驱动资料：
  1. ERA5-Land 再分析覆盖、且降水订正规则与率定成果一致的日子，直接使用；
  2. 其余日子用雨量站日降水：反距离权重插值，再按多年同期降水空间分布展布到各格点
     （站点比例法）；也可手工录入流域面雨量，按多年同期分布展布；
  3. 既无站点也无手录的日子，用多年同期（前后7天）平均降水，单独标注。
气温、蒸发能力：站网暂无气温观测，采用多年同期同一时刻的逐时平均值。
湖位：主站水位与模型同基准时，以日均水位在当日12时校正湖位，其后按水量平衡推进。
率定参数、湖泊方案和数值模型均不改变；基底始终取最近的再分析一致状态，
再分析资料回补后重新更新，即可替换站点驱动段。
"""
from copy import deepcopy
import json
import math

import numpy as np
import pandas as pd

from .common import now, read_json, write_json

KIND = 'state-update'
STATION_RADIUS_KM = 100.
CLIMATOLOGY_HALF_WINDOW_DAYS = 7
SLC_ELEVATION_M = 4544.6


def _today():
    return pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None).normalize()


def _published_states():
    from .published_results import catalog, result_file
    items = []
    for row in catalog():
        try:
            state = read_json(result_file(row['id'], 'final-state.json'))
        except (FileNotFoundError, ValueError, OSError):
            continue
        if not state or state.get('schemaVersion') != 3:
            continue
        items.append({'id': row['id'], 'name': row.get('name', row['id']), 'kind': 'published',
                      'stateTime': state['validAt'], 'eraConsistent': True, 'forcing': '再分析资料（率定成果）',
                      'spatialVersion': state['identity']['spatialVersion'], 'inputId': state['inputId']})
    return items


def state_chain(db):
    """All continuous states that can start an update or a forecast, newest first."""
    items = _published_states()
    for row in db.rows("SELECT id,name,result,created_at FROM tasks WHERE kind=? AND status='succeeded' AND trashed=0", (KIND,)):
        result = json.loads(row['result'] or '{}')
        if not (db.workspace / 'tasks' / row['id'] / 'checkpoint.json').is_file():
            continue
        items.append({'id': row['id'], 'name': row['name'] or result.get('name') or row['id'], 'kind': KIND,
                      'stateTime': result.get('end'), 'eraConsistent': bool(result.get('eraConsistent')),
                      'forcing': result.get('forcingLabel'), 'baseResultId': result.get('baseResultId'),
                      'spatialVersion': result.get('spatialVersion'), 'inputId': result.get('inputId'),
                      'createdAt': row['created_at']})
    items.sort(key=lambda x: (x.get('stateTime') or '', x.get('createdAt') or ''), reverse=True)
    if items:
        items[0]['latest'] = True
    return items


def latest_state_id(db):
    chain = state_chain(db)
    return chain[0]['id'] if chain else None


def _base(db):
    """Newest reanalysis-consistent state: a published result or the ERA5 part of an update."""
    from .forecasts import source_context
    best = None
    for item in _published_states():
        if best is None or item['stateTime'] > best[1]:
            best = (item['id'], item['stateTime'], None)
    for row in db.rows("SELECT id,result FROM tasks WHERE kind=? AND status='succeeded' AND trashed=0", (KIND,)):
        result = json.loads(row['result'] or '{}')
        path = db.workspace / 'tasks' / row['id'] / 'era-checkpoint.json'
        if result.get('eraEnd') and path.is_file() and (best is None or result['eraEnd'] > best[1]):
            best = (row['id'], result['eraEnd'], path)
    if best is None:
        raise ValueError('没有可用于状态更新的连续模型状态')
    context = source_context(db, best[0])
    if best[2] is not None:
        context = {**context, 'state': read_json(best[2])}
    context['baseResultId'] = context.get('baseResultId') or best[0]
    return context


def _haversine_km(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _day_of_year(times):
    return (pd.DatetimeIndex(times) - pd.Timedelta(nanoseconds=1)).dayofyear.to_numpy()


def _window(doy_all, doy, half=CLIMATOLOGY_HALF_WINDOW_DAYS):
    d = np.abs(doy_all - doy)
    return np.minimum(d, 366 - d) <= half


def _rain_stations(db, spatial):
    """Located precipitation stations within the radius of the basin forcing grid."""
    grids = spatial['grids']
    lons = np.array([g['lon'] for g in grids]); lats = np.array([g['lat'] for g in grids])
    # Only grids that actually feed basin units or the lake surface define the search area.
    used = {m['gridIndex'] for group in spatial.get('aggregation') or [] for m in group['members']}
    used |= {u['gridIndex'] for u in spatial['units'] if 'gridIndex' in u}
    used |= set(np.flatnonzero(np.asarray(spatial['lakeWeights']) > 0).tolist())
    basin = np.array(sorted(used), dtype=int)
    from .observation_query import catalog
    rows = [s for s in catalog(db)['stations'] if s.get('lon') is not None and s.get('lat') is not None
            and any(v['id'] == 'precipitation' for v in s.get('variables', []))]
    result = []
    for r in rows:
        distance = np.array([_haversine_km(r['lon'], r['lat'], x, y) for x, y in zip(lons, lats)])
        if distance[basin].min() <= STATION_RADIUS_KM:
            result.append({'id': r['id'], 'name': r['name'], 'lon': float(r['lon']), 'lat': float(r['lat']),
                           'gridIndex': int(np.argmin(distance)), 'distanceKm': distance})
    return result


def _daily_values(db, station, variable, start, end):
    """Quality-controlled daily values only for the update window (same rules as the monitoring pages)."""
    from .observation_query import SeriesView
    from .observations import daily_series
    try:
        values = daily_series(SeriesView(db), station, variable, start=start.isoformat(),
                              end=(end + pd.Timedelta(days=1)).isoformat())
    except (ValueError, KeyError):
        return {}
    return {pd.Timestamp(t).normalize(): float(v) for t, v in values.items()
            if np.isfinite(v) and start <= pd.Timestamp(t).normalize() <= end}


def _era_extent(db, context, target, verify=True):
    """Last day boundary up to which reanalysis with the calibrated correction rule is available."""
    from .forecasts import _history
    from .model_forcing import current_product
    state_time = pd.Timestamp(context['state']['validAt'])
    ids = [context['state']['inputId']]
    try:
        current = current_product(db)['id']
        if current not in ids: ids.insert(0, current)
    except (OSError, ValueError, KeyError):
        pass
    best = None
    for identifier in ids:
        manifest = read_json(db.workspace / 'inputs/products' / identifier / 'manifest.json')
        if not manifest:
            continue
        candidate = min(pd.Timestamp(manifest['end']).floor('D'), target)
        if candidate <= state_time or (best is not None and candidate <= best):
            continue
        identity = context['state']['identity']
        if (manifest.get('transformationSHA256') != identity.get('forcingTransformationSHA256') or
                manifest.get('landTransformationSHA256') != identity.get('landForcingTransformationSHA256')):
            continue
        if not verify:
            best = candidate; continue
        try:
            _history(db, context, candidate)
            best = candidate
        except (ValueError, OSError, KeyError):
            continue
    return best


class _Climate:
    """Multi-year same-period means from the calibration reanalysis, before the base state.

    Computed once per (input, base time) and cached as a small file, so later updates and
    field use never need to reload the full reanalysis arrays.
    """

    def __init__(self, db, context):
        from .forecast_inputs import CLIMATOLOGY_CACHE_LOCK, _weights
        self.spatial = context['spatial']
        input_id = context['state']['inputId']; base = pd.Timestamp(context['state']['validAt'])
        self.manifest = read_json(db.workspace / 'inputs/products' / input_id / 'manifest.json')
        folder = db.workspace / 'inputs' / 'climatology'
        path = folder / f'{input_id}-{base:%Y%m%d%H}-w{CLIMATOLOGY_HALF_WINDOW_DAYS}.npz'
        if not path.is_file():
            with CLIMATOLOGY_CACHE_LOCK:
                if not path.is_file():
                    folder.mkdir(parents=True, exist_ok=True)
                    self._build(db, input_id, base, path)
        with np.load(path, allow_pickle=False) as archive:
            self.precipitation = archive['precipitation']; self.temperature = archive['temperature']
            self.evaporation = archive['evaporation']
        self.weights = _weights(self.spatial)
        self.areas = np.array([u['areaKm2'] for u in self.spatial['units']])

    def _build(self, db, input_id, base, path):
        from .grid_simulation import prepare
        source = prepare(db, input_id, self.spatial['version'])
        times = source['times']; ids = np.flatnonzero(np.asarray(times <= base))
        doy = _day_of_year(times[ids]); hour = pd.DatetimeIndex(times[ids]).hour.to_numpy()
        arrays = source['arrays']; n = len(self.spatial['grids'])
        p = np.empty((366, n)); t = np.empty((366, 24, n), dtype=np.float32); e = np.empty_like(t)
        for d in range(1, 367):
            window = ids[_window(doy, d)]; hours = hour[_window(doy, d)]
            p[d - 1] = np.mean(arrays['precipitation'][window], axis=0) * 24.
            for h in range(24):
                selected = window[hours == h]
                t[d - 1, h] = np.mean(arrays['temperature'][selected], axis=0)
                e[d - 1, h] = np.mean(arrays['evaporation'][selected], axis=0)
        partial = path.with_name(path.name + '.partial.npz')
        np.savez_compressed(partial, precipitation=p, temperature=t, evaporation=e)
        partial.replace(path)
        from .forecast_inputs import release_forcing_cache
        release_forcing_cache()

    def daily_precipitation(self, date):
        """Mean daily precipitation (mm/d) of each forcing grid for the ±7 day window."""
        return self.precipitation[pd.Timestamp(date).dayofyear - 1]

    def hourly(self, variable, time):
        d = int(_day_of_year([time])[0]) - 1; h = pd.Timestamp(time).hour
        return np.asarray((self.temperature if variable == 'temperature' else self.evaporation)[d, h], dtype=float)

    def basin_mean(self, grid_values):
        return float(np.average(grid_values @ self.weights.T, weights=self.areas))


def _grid_precipitation(climate, stations, date, observed, manual):
    """Daily grid precipitation (mm) and the method used for this day."""
    clim = climate.daily_precipitation(date)
    if manual is not None:
        mean = climate.basin_mean(clim)
        values = np.full_like(clim, float(manual)) if mean < .05 else float(manual) * clim / mean
        return values, 'manual'
    active = [s for s in stations if s['id'] in observed]
    if not active:
        return clim.copy(), 'climatology'
    d = np.maximum(np.stack([s['distanceKm'] for s in active]), 1.)
    w = 1. / d ** 2; w = w / w.sum(axis=0)
    p = np.array([observed[s['id']] for s in active])
    station_clim = np.array([clim[s['gridIndex']] for s in active])
    idw_p = (w * p[:, None]).sum(axis=0); idw_clim = (w * station_clim[:, None]).sum(axis=0)
    values = np.where(idw_clim >= .2, clim * idw_p / np.maximum(idw_clim, 1e-9), idw_p)
    return np.maximum(values, 0.), 'station'


def _level_observations(db, start, end):
    """One same-datum main-station level per day, kept at its observed hour."""
    from .observation_query import SeriesView, level_source_datum_ok
    rows = SeriesView(db).observations('SLC', 'level', start=start.isoformat(),
                                       end=(end + pd.Timedelta(days=1)).isoformat(), include_source=True)
    result = {}
    for r in rows:
        if r['value'] is None or r['quality'] not in ('valid', 'qualified') or not level_source_datum_ok(r):
            continue
        t = pd.Timestamp(r['time'])
        date = t.normalize()
        if not start <= date <= end:
            continue
        # Daily values describe the day; instantaneous readings are used at their own hour.
        hour = 12 if r['step'] == 'day' else min(24, max(1, int(round((t - date) / pd.Timedelta(hours=1)))))
        score = abs(hour - 12)
        if date not in result or score < result[date]['score']:
            result[date] = {'level': float(r['value']), 'hour': hour, 'time': r['time'], 'score': score}
    return result


def _target(payload):
    today = _today()
    value = payload.get('end')
    if not value:
        return today
    end = pd.Timestamp(value).normalize()
    if end > today:
        raise ValueError('状态只能更新到今天（北京时间00时）为止')
    return end


def _overrides(payload):
    result = {}
    for row in payload.get('daily') or []:
        date = pd.Timestamp(row['date']).normalize()
        item = {}
        for key in ('precipitationMm', 'lakeLevelM'):
            value = row.get(key)
            if value is None or value == '':
                continue
            value = float(value)
            if not np.isfinite(value) or (key == 'precipitationMm' and not 0 <= value <= 300):
                raise ValueError(f'{date.date()} 面雨量须为0—300 mm')
            if key == 'lakeLevelM' and not 4530 <= value <= 4560:
                raise ValueError(f'{date.date()} 湖位超出色林错合理范围')
            item[key] = value
        if item:
            result[date] = item
    return result


MODES = ('station', 'climatology', 'reanalysis')


def _mode(payload):
    mode = payload.get('mode') or 'station'
    if mode not in MODES:
        raise ValueError('更新方式须为再分析资料、雨量站与手工录入或多年同期')
    return mode


def _schedule(db, context, target, overrides, climate=None, verify=True, mode='station'):
    """Per-day forcing choice from the base state to the target day boundary."""
    from .observation_query import main_level_datum_ok
    state_time = pd.Timestamp(context['state']['validAt'])
    if target <= state_time:
        return {'days': [], 'eraEnd': None, 'stations': []}
    era_end = _era_extent(db, context, target, verify)
    climate = climate or _Climate(db, context)
    stations = _rain_stations(db, context['spatial'])
    first, last = state_time.normalize(), target - pd.Timedelta(days=1)
    rain = {s['id']: _daily_values(db, s['id'], 'precipitation', first, last) for s in stations}
    datum = main_level_datum_ok(db)
    levels = _level_observations(db, first, last) if datum else {}
    days = []
    for date in pd.date_range(first, last, freq='D'):
        manual = overrides.get(date, {})
        observed = {sid: values[date] for sid, values in rain.items() if date in values}
        era = era_end is not None and date + pd.Timedelta(days=1) <= era_end
        if era:
            grid, method = None, 'era5'
        elif mode == 'climatology':
            grid, method = _grid_precipitation(climate, stations, date, {}, None)
        else:
            grid, method = _grid_precipitation(climate, stations, date, observed, manual.get('precipitationMm'))
        reading = levels.get(date)
        if 'lakeLevelM' in manual:
            level, hour, source = manual['lakeLevelM'], 12, 'manual'
        elif reading:
            level, hour, source = reading['level'], reading['hour'], 'SLC'
        else:
            level, hour, source = None, None, None
        days.append({'date': date, 'method': method, 'grid': grid, 'stations': observed,
                     'basinPrecipitationMm': None if grid is None else climate.basin_mean(grid),
                     'lakeLevelM': level, 'lakeLevelHour': hour, 'lakeLevelSource': source,
                     'lakeLevelTime': reading['time'] if source == 'SLC' else None})
    return {'days': days, 'eraEnd': era_end, 'stations': stations, 'datumVerified': datum, 'climate': climate}


METHOD_LABELS = {'era5': '再分析', 'station': '雨量站', 'manual': '手工录入', 'climatology': '多年同期'}


def _day_forcing(context, climate, day, times):
    """Unit and lake forcing for one non-reanalysis day from grid precipitation and climatology."""
    from .grid_simulation import unit_forcing
    from .forcing import lake_precipitation_series
    from .land_precipitation import validate_manifest
    spatial = context['spatial']
    arrays = {'precipitation': np.repeat(day['grid'][None, :] / 24., 24, axis=0),
              'temperature': np.stack([climate.hourly('temperature', t) for t in times]),
              'evaporation': np.stack([climate.hourly('evaporation', t) for t in times])}
    ids = np.arange(24)
    manifest = climate.manifest
    if day['method'] == 'climatology':
        # Climatology comes from raw reanalysis: apply the same correction rule as calibration.
        rain, temp, evap = unit_forcing(spatial, arrays, ids, context['parameters'], context['regions'],
                                        validate_manifest(manifest), times)
        lake_rain = lake_precipitation_series(manifest, spatial, arrays, times, ids)
    else:
        # Station or manual totals are observed values: no reanalysis correction.
        rain, temp, evap = unit_forcing(spatial, arrays, ids, context['parameters'], context['regions'])
        lake_rain = arrays['precipitation'] @ np.asarray(spatial['lakeWeights'])
    lake_evap = arrays['evaporation'] @ np.asarray(spatial['lakeWeights'])
    return rain, temp, evap, np.asarray(lake_rain, dtype=float), lake_evap


def _advance_day(context, parent, times, forcing, anchor, curve, outlet, anchor_hour=12):
    from .forecasts import _hydrology
    from . import lake_curves
    rain, temp, evap, lake_rain, lake_evap = forcing
    q, final, _ = _hydrology(context, parent, times, rain, temp, evap)
    lake_evap = lake_evap * context['parameters']['lakeCoefficient']
    level0 = float(parent['lake']['level']); correction = None
    if anchor is None:
        lake = lake_curves.hourly_balance(q['total'], lake_rain, lake_evap, level0, curve, outlet)
        levels, outflow = lake['lakeLevel'], lake['outflow']
    else:
        # The observed level replaces the simulated level at the end of its observation hour.
        k = int(anchor_hour)
        a = lake_curves.hourly_balance(q['total'][:k], lake_rain[:k], lake_evap[:k], level0, curve, outlet)
        correction = float(anchor) - float(a['lakeLevel'][-1])
        if k < 24:
            b = lake_curves.hourly_balance(q['total'][k:], lake_rain[k:], lake_evap[k:], float(anchor), curve, outlet)
            levels = np.concatenate([a['lakeLevel'][:-1], [float(anchor)], b['lakeLevel']])
            outflow = np.concatenate([a['outflow'], b['outflow']])
        else:
            levels = np.concatenate([a['lakeLevel'][:-1], [float(anchor)]]); outflow = a['outflow']
    final['lake'] = {**parent['lake'], 'level': float(levels[-1]), 'validAt': times[-1].isoformat()}
    row = {k: float(np.mean(q[k])) for k in ('duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total')}
    row.update(lakeLevel=float(np.mean(levels)), lakeLevelEnd=float(levels[-1]), outflow=float(np.mean(outflow)),
               lakeRainMm=float(np.sum(lake_rain)), lakeEvaporationMm=float(np.sum(lake_evap)),
               levelCorrectionM=correction)
    return final, row


def _finish_state(state, end, extra):
    from .grid_simulation import model_identity
    state = deepcopy(state)
    state['validAt'] = end.isoformat()
    state['identity'] = {**state['identity'], 'modelImplementation': model_identity()}
    state.pop('continuousLakeVolume', None)
    state['stateUpdate'] = extra
    return state


def _base_daily(db, context, days=120):
    from .published_results import result_file
    from . import lake_revision
    base = context['baseResultId']
    path = db.workspace / 'tasks' / base / 'daily.csv'
    try:
        frame = pd.read_csv(path if path.is_file() else result_file(base, 'daily.csv'), encoding='utf-8-sig')
    except (FileNotFoundError, OSError, ValueError):
        return []
    end = pd.Timestamp(context['state']['validAt']).date().isoformat()
    frame = frame[frame['date'] < end].tail(days)
    model = lake_revision.active(db)
    revised = {r['date']: r['level'] for r in model['daily']} if model and model.get('sourceResultId') == base else {}
    rows = []
    for _, r in frame.iterrows():
        value = lambda k: None if k not in r or pd.isna(r[k]) else float(r[k])
        rows.append({'date': r['date'], **{k: value(k) for k in ('duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total',
                     'observedLevel', 'observedFlow', 'lakeRainMm', 'lakeEvaporationMm')},
                     'lakeLevel': revised.get(r['date'], value('lakeLevel')), 'forcing': r.get('forcing', '再分析')})
    return rows


def run(db, request, folder, progress=None, cancelled=None):
    from .forecasts import _history, _lake_setup
    from .grid_simulation import unit_forcing, prefix_identity
    from .forcing import lake_precipitation_series
    from .land_precipitation import validate_manifest
    context = _base(db)
    mode, target = _mode_target(db, context, request, True)
    state_time = pd.Timestamp(context['state']['validAt'])
    if target <= state_time:
        raise ValueError('模型状态已到 ' + state_time.strftime('%Y-%m-%d %H:%M') + '，无需更新')
    if not context['state'].get('lake') or context['state']['lake'].get('level') is None:
        raise ValueError('基底状态缺少连续湖位')
    schedule = _schedule(db, context, target, {} if mode == 'climatology' else _overrides(request), mode=mode)
    climate = schedule['climate']; curve, outlet = _lake_setup(db, context)
    era = _history(db, context, schedule['eraEnd']) if schedule['eraEnd'] is not None else None
    flows = _daily_values(db, 'DUJA', 'flow', state_time.normalize(), target)
    parent = deepcopy(context['state']); rows = []; era_state = None
    for k, day in enumerate(schedule['days']):
        if cancelled and cancelled(): raise InterruptedError('已停止状态更新，原状态未改变')
        times = pd.date_range(day['date'] + pd.Timedelta(hours=1), periods=24, freq='h')
        if day['method'] == 'era5':
            source, indexes, _ = era; ids = indexes[k * 24:(k + 1) * 24]
            rain, temp, evap = unit_forcing(context['spatial'], source['arrays'], ids, context['parameters'],
                                            context['regions'], validate_manifest(source['manifest']), source['times'])
            lake_rain = lake_precipitation_series(source['manifest'], context['spatial'], source['arrays'], source['times'], ids)
            lake_evap = source['arrays']['evaporation'][ids] @ np.asarray(context['spatial']['lakeWeights'])
            forcing = (rain, temp, evap, np.asarray(lake_rain, dtype=float), lake_evap)
        else:
            forcing = _day_forcing(context, climate, day, times)
        parent, row = _advance_day(context, parent, times, forcing, day['lakeLevelM'], curve, outlet, day['lakeLevelHour'] or 12)
        row.update(date=day['date'].date().isoformat(), forcing=METHOD_LABELS[day['method']],
                   basinPrecipitationMm=day['basinPrecipitationMm'], observedLevel=day['lakeLevelM'],
                   observedFlow=flows.get(day['date']))
        rows.append(row)
        if day['method'] == 'era5' and (k + 1 == len(schedule['days']) or schedule['days'][k + 1]['method'] != 'era5'):
            source = era[0]
            era_state = _finish_state(parent, times[-1], {'segment': 'reanalysis'})
            era_state['inputId'] = source['manifest']['id']
            era_state['forcingPrefixSHA256'] = prefix_identity(source, times[-1].isoformat())
            write_json(folder / 'era-checkpoint.json', era_state)
        if progress: progress(95 * (k + 1) / len(schedule['days']), f'{day["date"]:%m-%d} {METHOD_LABELS[day["method"]]}驱动')
    return _save(db, context, request, folder, schedule, parent, rows, era_state, target)


def _save(db, context, request, folder, schedule, parent, rows, era_state, target):
    counts = {k: sum(d['method'] == k for d in schedule['days']) for k in METHOD_LABELS}
    label = ' · '.join(f'{METHOD_LABELS[k]} {v} 天' for k, v in counts.items() if v)
    anchors = [r for r in rows if r['levelCorrectionM'] is not None]
    era_consistent = counts['era5'] == len(rows)
    extra = {'baseResultId': context['baseResultId'], 'baseStateTime': context['state']['validAt'],
             'counts': counts, 'forcingLabel': label, 'createdAt': now(),
             'temperature': 'multi-year same-period hourly mean from calibration reanalysis' if not era_consistent else 'reanalysis',
             'precipitation': 'IDW station ratio on multi-year same-period pattern; manual areal totals spread by the same pattern',
             'lakeLevelAnchors': len(anchors)}
    state = era_state if era_consistent and era_state is not None else _finish_state(parent, target, extra)
    state['stateUpdate'] = extra
    write_json(folder / 'checkpoint.json', state)
    columns = ['date', 'duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total', 'lakeLevel', 'observedLevel',
               'observedFlow', 'lakeRainMm', 'lakeEvaporationMm', 'outflow', 'basinPrecipitationMm', 'forcing']
    daily = _base_daily(db, context) + rows
    pd.DataFrame([{c: r.get(c) for c in columns} for r in daily]).to_csv(folder / 'daily.csv', index=False, encoding='utf-8-sig')
    final_level = float(state['lake']['level'])
    return {'name': request.get('name') or f'实况更新至 {target:%Y-%m-%d}', 'kind': KIND,
            'baseResultId': context['baseResultId'], 'start': context['state']['validAt'], 'end': target.isoformat(),
            'eraEnd': era_state['validAt'] if era_state is not None else None, 'eraConsistent': era_consistent,
            'counts': counts, 'forcingLabel': label or '无', 'lakeLevelAnchors': len(anchors),
            'finalLakeLevelM': final_level, 'spatialVersion': context['spatial']['version'],
            'inputId': state['inputId'], 'parameters': state.get('parameters'),
            'parameterTransfer': state.get('parameterTransfer'),
            'lakeModelId': (context.get('lakeModel') or {}).get('id'),
            'days': [{k: r.get(k) for k in ('date', 'forcing', 'basinPrecipitationMm', 'total', 'duja', 'lakeLevel',
                                             'observedLevel', 'levelCorrectionM', 'outflow')} for r in rows],
            'note': '站点及多年同期驱动段为业务实况推进，不等同率定期再分析驱动；再分析资料到位后可重新更新替换。'}



def _mode_target(db, context, payload, verify):
    mode, target = _mode(payload), _target(payload)
    if mode == 'reanalysis':
        era = _era_extent(db, context, target, verify)
        if era is None:
            raise ValueError('再分析资料尚未覆盖模型状态之后的日期；请先在“资料更新—再分析资料”中下载，或改用雨量站与手工录入、多年同期更新')
        target = era
    return mode, target


def plan(db, payload):
    context = _base(db)
    mode, target = _mode_target(db, context, payload, False)
    schedule = _schedule(db, context, target, _overrides(payload), verify=False, mode=mode)
    chain = state_chain(db)
    counts = {k: sum(d['method'] == k for d in schedule['days']) for k in METHOD_LABELS}
    era = _era_extent(db, context, _target(payload), False)
    return {'mode': mode, 'reanalysisEnd': era.isoformat() if era is not None else None,
            'base': {'id': context['baseResultId'], 'stateTime': context['state']['validAt'],
                     'lakeLevelM': context['state'].get('lake', {}).get('level')},
            'latest': chain[0] if chain else None, 'target': target.isoformat(),
            'eraEnd': schedule['eraEnd'].isoformat() if schedule['eraEnd'] is not None else None,
            'datumVerified': schedule.get('datumVerified', False), 'counts': counts,
            'stations': [{'id': s['id'], 'name': s['name'],
                          'nearestKm': float(s['distanceKm'].min())} for s in schedule['stations']],
            'days': [{'date': d['date'].date().isoformat(), 'method': d['method'],
                      'methodLabel': METHOD_LABELS[d['method']], 'stations': d['stations'],
                      'basinPrecipitationMm': d['basinPrecipitationMm'], 'lakeLevelM': d['lakeLevelM'],
                      'lakeLevelSource': d['lakeLevelSource'], 'lakeLevelTime': d['lakeLevelTime']} for d in schedule['days']]}
