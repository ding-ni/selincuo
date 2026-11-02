"""Re-estimate the registered warm-rain correction after data updates."""
from __future__ import annotations

from collections import defaultdict
import json
import math
import zipfile

import numpy as np
import pandas as pd

from .common import canonical, digest, now
from .land_precipitation import METHOD, register_product

STATIONS = ('SLC', 'DUJA', 'BLQ', '115DB')
SEASONS = ('DJF', 'MAM', 'JJA', 'SON')
MIN_COVERAGE = .8
MIN_WET_DAYS = 5
MIN_ERA_MM = 20.
START_YEAR = 2024
PENDING_SETTING = 'pending-precipitation-input'
STATUS_SETTING = 'precipitation-correction-status'


def _season_year(day):
    month = day.month
    return day.year + 1 if month == 12 else day.year


def _season(day):
    if day.month in (12, 1, 2):
        return 'DJF'
    if day.month in (3, 4, 5):
        return 'MAM'
    if day.month in (6, 7, 8):
        return 'JJA'
    return 'SON'


def _season_bounds(year, season):
    if season == 'DJF':
        return pd.Timestamp(year - 1, 12, 1), pd.Timestamp(year, 3, 1)
    month = {'MAM': 3, 'JJA': 6, 'SON': 9}[season]
    return pd.Timestamp(year, month, 1), pd.Timestamp(year, month + 3, 1)


def _daily_grid(times, precipitation, temperature):
    times = pd.DatetimeIndex(times)
    dates = (times - pd.Timedelta(nanoseconds=1)).normalize()
    result = {}
    for day in pd.DatetimeIndex(dates.unique()).sort_values():
        indexes = np.flatnonzero(dates == day)
        if len(indexes) != 24 or not np.isfinite(precipitation[indexes]).all() or not np.isfinite(temperature[indexes]).all():
            continue
        rain = precipitation[indexes]
        temp = temperature[indexes]
        result[day] = {'total': float(rain.sum()),
                        'warm': float(rain[temp >= 5.].sum()),
                        'minimumTemperature': float(temp.min()),
                        'warmHours': int((temp >= 5.).sum())}
    return result


def _read_selected_columns(archive, name, row_start, row_end, columns):
    """Stream a compressed NPY member and retain only the four station cells."""
    with archive.open(name + '.npy') as stream:
        version = np.lib.format.read_magic(stream)
        if version == (1, 0):
            shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream)
        elif version == (2, 0):
            shape, fortran, dtype = np.lib.format.read_array_header_2_0(stream)
        else:
            raise ValueError('气象输入数组版本不受支持')
        if len(shape) != 2 or fortran or dtype.kind not in 'iuf':
            raise ValueError('气象输入数组布局不受支持')
        rows, width = shape
        if not 0 <= row_start <= row_end <= rows or any(not 0 <= col < width for col in columns):
            raise ValueError('站点格网索引超出气象输入范围')
        result = np.empty((row_end - row_start, len(columns)), dtype=np.float64)
        block_rows = max(1, 32 * 1024 * 1024 // (width * dtype.itemsize))
        cursor = 0
        for start in range(0, rows, block_rows):
            count = min(block_rows, rows - start)
            size = count * width * dtype.itemsize
            raw = stream.read(size)
            if len(raw) != size:
                raise ValueError('气象输入数组数据不完整')
            lo, hi = max(start, row_start), min(start + count, row_end)
            if lo < hi:
                block = np.frombuffer(raw, dtype=dtype).reshape(count, width)
                offset = lo - row_start
                result[offset:offset + hi - lo] = block[lo - start:hi - start, columns]
                cursor += hi - lo
        if cursor != row_end - row_start or stream.read(1):
            raise ValueError('气象输入数组长度与清单不一致')
    return result


def _confirmed_coverages(db, station):
    rows = db.rows('''SELECT c.*,s.imported_at FROM coverage c JOIN sources s ON s.id=c.source_id
        WHERE c.station_id=? AND c.variable='precipitation'
        AND c.step IN ('day','hour','irregular')
        AND s.rowid IN (SELECT MAX(rowid) FROM sources GROUP BY path)
        ORDER BY s.imported_at DESC,s.rowid DESC''', (station,))
    output = []
    for row in rows:
        try:
            metadata = json.loads(row['metadata'])
        except (TypeError, ValueError):
            continue
        if not metadata.get('coverageConfirmed') or not metadata.get('zeroForAbsent'):
            continue
        if row['step'] == 'irregular' and metadata.get('eventTimeMeaning') != 'occurrence-time':
            continue
        output.append({**row, 'metadata': metadata})
    return output


def _coverage_day_set(rows, step, first_day, last_day):
    covered = set()
    stopped = []
    for row in rows:
        if row['step'] != step or not row.get('start') or not row.get('end'):
            continue
        start, end = pd.Timestamp(row['start']), pd.Timestamp(row['end'])
        if step == 'irregular':
            lo = start.ceil('D')
            hi = end.floor('D') - pd.Timedelta(days=1)
        else:
            lo, hi = start.floor('D'), end.floor('D')
        lo, hi = max(lo, first_day), min(hi, last_day)
        if lo <= hi:
            covered.update(pd.date_range(lo, hi, freq='D'))
        for value in row['metadata'].get('stoppedIntervals', []):
            stopped.append((pd.Timestamp(value[0]), pd.Timestamp(value[1])))
    if stopped:
        covered = {day for day in covered if not any(day < end and day + pd.Timedelta(days=1) > start
                                                     for start, end in stopped)}
    return covered


def _station_daily(db, station, coverages, first_day, last_day, era, station_temperature, times):
    days = pd.date_range(first_day, last_day, freq='D')
    daily_cover = _coverage_day_set(coverages, 'day', first_day, last_day)
    hourly_cover = _coverage_day_set(coverages, 'hour', first_day, last_day)
    event_coverages = [row for row in coverages if row['step'] == 'irregular']
    event_cover = _coverage_day_set(coverages, 'irregular', first_day, last_day)
    obs_rows = db.observations(station, 'precipitation', limit=None)
    daily_values = {}
    invalid_daily = set()
    hourly_values = defaultdict(list)
    invalid_hourly = set()
    for row in obs_rows:
        stamp = pd.Timestamp(row['time'])
        if row['step'] == 'day':
            day = stamp.floor('D')
            if row.get('value') is None or row.get('quality') not in ('valid', 'qualified') or float(row['value']) < 0:
                invalid_daily.add(day)
            else:
                daily_values[day] = float(row['value'])
        elif row['step'] == 'hour':
            if row.get('value') is None or row.get('quality') not in ('valid', 'qualified') or float(row['value']) < 0:
                invalid_hourly.add((stamp - pd.Timedelta(nanoseconds=1)).floor('D'))
            else:
                hourly_values[stamp].append(float(row['value']))

    hourly_daily = {}
    hourly_groups = defaultdict(list)
    for stamp, values in hourly_values.items():
        hourly_groups[(stamp - pd.Timedelta(nanoseconds=1)).floor('D')].append((stamp, values[-1]))
    for day, values in hourly_groups.items():
        if len(values) == 24 and len({stamp for stamp, _ in values}) == 24:
            hourly_daily[day] = float(sum(value for _, value in values))

    event_daily = {}
    for coverage in event_coverages:
        start, end = pd.Timestamp(coverage['start']), pd.Timestamp(coverage['end'])
        lo, hi = max(start.ceil('D'), first_day), min(end.floor('D') - pd.Timedelta(days=1), last_day)
        if lo > hi:
            continue
        source_rows = db.rows('''SELECT station_id,time,value,quality,source_id FROM observations
            WHERE source_id=? AND station_id=? AND variable='precipitation' AND step='irregular'
            ORDER BY time,id''', (coverage['source_id'], station))
        increments = db.rows('''SELECT * FROM (SELECT o.station_id,o.time,o.value,o.quality,o.source_id,
                ROW_NUMBER() OVER(PARTITION BY o.time ORDER BY s.rowid DESC,o.id DESC) rn
            FROM observations o JOIN sources s ON s.id=o.source_id
            WHERE o.station_id=? AND o.variable='precipitation' AND o.step='irregular'
              AND o.time>=? AND o.time<? AND s.path LIKE 'native-excel/%'
              AND s.rowid>(SELECT rowid FROM sources WHERE id=?)) WHERE rn=1''',
            (station, start.isoformat(), end.isoformat(), coverage['source_id']))
        merged_events = {row['time']: row for row in source_rows}
        merged_events.update({row['time']: row for row in increments})
        grouped = defaultdict(list)
        for row in merged_events.values():
            # Irregular events are actual occurrence times; midnight belongs
            # to the new Beijing day, unlike hourly accumulation endpoints.
            day = pd.Timestamp(row['time']).floor('D')
            if lo <= day <= hi:
                grouped[day].append(row)
        for day in pd.date_range(lo, hi, freq='D'):
            if day not in event_cover:
                continue
            event_rows = grouped.get(day, [])
            valid = all(row.get('value') is not None and row.get('quality') in ('valid', 'qualified')
                        and float(row['value']) >= 0 for row in event_rows)
            value = sum(float(row['value']) for row in event_rows) if valid else np.nan
            # The most recently confirmed source owns overlapping event days.
            event_daily.setdefault(day, {'value': value, 'valid': valid,
                                         'sourceId': coverage['source_id'], 'rows': event_rows})

    result = {}
    for day in days:
        if day not in era:
            continue
        if day in daily_cover:
            if day in invalid_daily:
                continue
            observed = daily_values.get(day, 0.)
            source = 'daily'
        elif day in hourly_cover and day in hourly_daily:
            if day in invalid_hourly:
                continue
            observed = hourly_daily[day]
            source = 'hourly'
        elif day in event_daily:
            entry = event_daily[day]
            if not entry['valid']:
                continue
            observed = entry['value']
            source = 'events'
        else:
            continue

        event = event_daily.get(day)
        if source in ('daily', 'hourly') and event:
            if not event['valid'] or not np.isfinite(event['value']) or abs(float(observed) - float(event['value'])) > 1e-6:
                continue

        if source == 'events':
            # Events are paired to the containing ERA hour; cold events are not
            # included in the warm-rain numerator.
            warm_observed = 0.
            valid_time = True
            for row in event['rows']:
                endpoint = pd.Timestamp(row['time']).floor('h') + pd.Timedelta(hours=1)
                pos = times.get_indexer([endpoint])[0]
                if pos < 0 or not np.isfinite(station_temperature[pos]):
                    valid_time = False
                    break
                if station_temperature[pos] >= 5.:
                    warm_observed += float(row['value'])
            if not valid_time:
                continue
            if era[day]['warmHours'] == 0:
                continue
            result[day] = {'coverage': True, 'eligible': True, 'observed': warm_observed,
                           'era': era[day]['warm'], 'source': source}
        elif era[day]['minimumTemperature'] >= 5.:
            result[day] = {'coverage': True, 'eligible': True, 'observed': float(observed),
                           'era': era[day]['total'], 'source': source}
        else:
            result[day] = {'coverage': True, 'eligible': False, 'observed': None,
                           'era': None, 'source': source}
    return result, obs_rows


def estimate_profile(db, input_id):
    """Build the existing warm-rain rule from confirmed, quality-valid samples."""
    products = db.items('input_products')
    product = next((row for row in products if row['id'] == input_id), None)
    if not product:
        raise ValueError('待订正气象输入不存在')
    manifest = product['manifest']
    from .grid_spatial import load as load_spatial
    spatial = load_spatial(manifest['spatialVersion'])
    locations = {row['id']: row for row in db.rows(
        "SELECT id,lon,lat FROM stations WHERE id IN ('SLC','DUJA','BLQ','115DB')")}
    if not locations:
        return None, {'reason': 'missing-station-locations'}

    first_year = max(START_YEAR, pd.Timestamp(manifest['start']).year)
    last_available = pd.Timestamp(manifest['end']).normalize() - pd.Timedelta(days=1)
    if last_available < pd.Timestamp(first_year, 1, 1):
        return None, {'reason': 'forcing-period-too-short'}
    first_day = pd.Timestamp(first_year - 1, 12, 1)
    last_day = last_available

    grid_indices = {}
    for station in STATIONS:
        location = locations.get(station)
        if not location or location['lon'] is None or location['lat'] is None:
            continue
        distances = [((grid['lon'] - location['lon']) * math.cos(math.radians(location['lat']))) ** 2 +
                     (grid['lat'] - location['lat']) ** 2 for grid in spatial['grids']]
        grid_indices[station] = int(np.argmin(distances))
    if not grid_indices:
        return None, {'reason': 'missing-station-locations'}

    # Decode one compressed array at a time; retain only the four nearest cells.
    from .common import within, sha_file
    from pathlib import Path
    folder = within(db.workspace / 'inputs/products', input_id)
    data_path = within(folder, manifest['data'])
    if sha_file(data_path) != manifest.get('dataSHA256'):
        raise ValueError('订正来源气象摘要不匹配')
    needed = sorted(set(grid_indices.values()))
    with zipfile.ZipFile(data_path) as archive:
        with archive.open('time.npy') as stream:
            times = pd.DatetimeIndex(np.load(stream, allow_pickle=False).astype('datetime64[ns]'))
        if not len(times) or not np.all(np.diff(times.asi8) == 3600 * 10**9):
            raise ValueError('订正来源气象小时序列不连续')
        start_pos = int(times.searchsorted(first_day, side='left'))
        end_pos = int(times.searchsorted(last_day + pd.Timedelta(days=1), side='right'))
        selected_times = times[start_pos:end_pos]
        precip = _read_selected_columns(archive, 'precipitation', start_pos, end_pos, needed)
        temp = _read_selected_columns(archive, 'temperature', start_pos, end_pos, needed)
    col = {grid_index: i for i, grid_index in enumerate(needed)}

    observations = {}
    summaries = {}
    snapshot = []
    profile_stations = {}
    usable_groups = 0
    for station, grid_index in grid_indices.items():
        station_pos = col[grid_index]
        era = _daily_grid(selected_times, precip[:, station_pos], temp[:, station_pos])
        coverage = _confirmed_coverages(db, station)
        station_daily, obs_rows = _station_daily(db, station, coverage, first_day, last_day,
                                                  era, temp[:, station_pos], selected_times)
        observations[station] = station_daily
        snapshot.extend([{'station': station, 'date': day.isoformat(), **sample}
                         for day, sample in station_daily.items()])
        snapshot.extend([{'coverage': row['source_id'], 'station': station, 'step': row['step'],
                          'start': row['start'], 'end': row['end'],
                          'metadata': row['metadata']} for row in coverage])
        location = locations[station]
        profile_stations[station] = {'lon': float(location['lon']), 'lat': float(location['lat']),
                                     'gridId': spatial['grids'][grid_index]['id'], 'seasonFactors': {}}

    for station, daily in observations.items():
        records = []
        for year in range(START_YEAR, last_day.year + 2):
            for season in SEASONS:
                start, end = _season_bounds(year, season)
                if start > last_day:
                    continue
                season_days = pd.date_range(start, end - pd.Timedelta(days=1), freq='D')
                covered = [day for day in season_days if day in daily]
                eligible = [daily[day] for day in covered if daily[day]['eligible']]
                gauge = float(sum(row['observed'] for row in eligible))
                era = float(sum(row['era'] for row in eligible))
                wet = sum(row['observed'] > 0 for row in eligible)
                usable = (len(season_days) > 0 and len(covered) / len(season_days) >= MIN_COVERAGE and
                          wet >= MIN_WET_DAYS and era >= MIN_ERA_MM and gauge > 0)
                records.append({'seasonYear': year, 'season': season,
                                'coverageDays': len(covered), 'expectedDays': len(season_days),
                                'warmRainDays': len(eligible), 'wetDays': wet,
                                'gaugeMm': gauge, 'eraMm': era, 'usableForFactor': bool(usable)})
        summaries[station] = records
        for season in SEASONS:
            chosen = [row for row in records if row['season'] == season and row['usableForFactor']]
            gauge = sum(row['gaugeMm'] for row in chosen)
            era_total = sum(row['eraMm'] for row in chosen)
            if gauge > 0 and era_total > 0:
                profile_stations[station]['seasonFactors'][season] = gauge / era_total
                usable_groups += 1

    profile = {'schemaVersion': 1, 'method': METHOD, 'scope': 'land-warm-rain',
        'parentDataSHA256': manifest['dataSHA256'], 'parentInputId': input_id,
        'temperatureThresholdC': 5.0, 'memberLapseRateCPerM': -0.0065,
        'groupLapseRateBoundsCPerM': [-0.01, -0.002], 'modelRainTemperatureMaxC': 5.0,
        'radiusKm': 100.0, 'distanceRegularizationKm': 5.0,
        'eligibility': {'minimumCoverageFraction': MIN_COVERAGE, 'minimumObservedWarmRainDays': MIN_WET_DAYS,
                        'minimumEraWarmMm': MIN_ERA_MM, 'startYear': START_YEAR,
                        'estimatedThrough': last_day.date().isoformat()},
        'eventTemperaturePairing': {'eventTime': '北京时间实际发生时刻',
            'eventInterval': '[floor(event,h), floor(event,h)+1h)',
            'eraTemperatureEndpoint': 'floor(event,h)+1h',
            'dailyInterval': '[当天00:00,次日00:00)，对应ERA结束时刻当天01:00至次日00:00'},
        'observationSnapshotSHA256': digest(sorted(snapshot, key=lambda row: canonical(row))),
        'interpretation': '按现行暖雨规则，仅以已确认覆盖且质量有效的站点降水估计；2017—2023应用属于时间外推。',
        'stations': profile_stations}
    return profile, {'usableStationSeasons': usable_groups, 'stationSeasons': summaries,
                     'observationSnapshotSHA256': profile['observationSnapshotSHA256'],
                     'stationsWithConfirmedCoverage': sorted(observations)}


def _previous_profile(db, active):
    profile = (active or {}).get('manifest', {}).get('landPrecipitationCorrection')
    if isinstance(profile, dict) and profile.get('method') == METHOD:
        return profile
    return None


def refresh(db, source_input_id=None, activate=True):
    """Create and optionally activate a new immutable correction input."""
    from .model_forcing import CURRENT_SETTING, current_product, verify_product
    from .model_forcing import _product as model_product

    active = current_product(db)
    pending = db.setting(PENDING_SETTING)
    pending_id = pending.get('inputId') if isinstance(pending, dict) else None
    base_id = source_input_id or pending_id or active['id']
    if source_input_id is None and not pending_id:
        profile = _previous_profile(db, active)
        parent_id = profile.get('parentInputId') if profile else None
        parent = next((row for row in db.items('input_products') if row['id'] == parent_id), None)
        if (parent and parent['manifest'].get('dataSHA256') == active['manifest'].get('dataSHA256')
                and parent['manifest'].get('spatialVersion') == active['manifest'].get('spatialVersion')):
            base_id = parent_id
    try:
        profile, diagnostics = estimate_profile(db, base_id)
        old_profile = _previous_profile(db, active)
        if not profile:
            raise ValueError('没有可用的站点雨量样本')
        fresh_count = diagnostics['usableStationSeasons']
        reused = 0
        old_stations = (old_profile or {}).get('stations', {})
        for station, value in profile['stations'].items():
            old = old_stations.get(station, {})
            same_location = (old.get('lon') == value['lon'] and old.get('lat') == value['lat'])
            for season in SEASONS:
                if season not in value['seasonFactors'] and same_location and season in old.get('seasonFactors', {}):
                    value['seasonFactors'][season] = old['seasonFactors'][season]
                    reused += 1
        if fresh_count == 0:
            reason = '已确认覆盖内没有达到样本门槛的季节，继续沿用当前订正输入。'
            result = {'status': 'insufficient', 'inputId': active['id'], 'sourceInputId': base_id,
                      'pendingInputId': base_id if base_id != active['id'] else None,
                      'updatedFactors': 0, 'reusedFactors': 0, 'insufficientStationSeasons': [],
                      'message': reason, 'diagnostics': diagnostics}
            db.set_setting(STATUS_SETTING, {'at': now(), **result})
            db.log('inputs.precipitation-correction-insufficient', result)
            return result

        from .land_precipitation import register_product
        provenance = {'method': 'automatic-warm-rain-reestimate-v1',
                      'estimatedAt': now(), 'sourceInputId': base_id,
                      'observationSnapshotSHA256': diagnostics['observationSnapshotSHA256'],
                      'usableStationSeasons': fresh_count, 'reusedFactors': reused,
                      'rule': {'minimumCoverageFraction': MIN_COVERAGE,
                               'minimumWarmRainDays': MIN_WET_DAYS, 'minimumEraMm': MIN_ERA_MM,
                               'temperatureThresholdC': 5.0, 'radiusKm': 100.0}}
        manifest = register_product(db, base_id, profile, lake_warm_rain=True,
                                    update_provenance=provenance)
        candidate = model_product(db, manifest['id'])
        verify_product(db, candidate)
        changed = manifest['id'] != active['id']
        result = {'status': 'updated' if changed else 'unchanged', 'inputId': manifest['id'], 'sourceInputId': base_id,
                  'pendingInputId': None, 'updatedFactors': fresh_count, 'reusedFactors': reused,
                  'insufficientStationSeasons': [
                      {'station': station, 'season': season} for station, row in diagnostics['stationSeasons'].items()
                      for season in SEASONS if not profile['stations'][station]['seasonFactors'].get(season)
                  ], 'message': ('站点雨量订正已按现行暖季规则更新。' if changed else
                                  '没有新增已确认的有效样本，继续使用当前订正输入。'),
                  'diagnostics': diagnostics}
        if activate:
            with db.connect() as connection:
                current_row = connection.execute('SELECT value FROM settings WHERE key=?', (CURRENT_SETTING,)).fetchone()
                current_value = json.loads(current_row['value']) if current_row else {}
                if changed:
                    connection.execute("DELETE FROM settings WHERE key LIKE 'calibration-configuration:%'")
                connection.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                                   (CURRENT_SETTING, canonical({'inputId': manifest['id'],
                                                                'precipitationUpdatedAt': now()})))
                connection.execute('DELETE FROM settings WHERE key=?', (PENDING_SETTING,))
                connection.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                                   (STATUS_SETTING, canonical({'at': now(), **result})))
                connection.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                                   (now(), 'inputs.precipitation-correction-updated', canonical(result)))
        else:
            db.set_setting(STATUS_SETTING, {'at': now(), **result})
        return result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = {'status': 'insufficient', 'inputId': active['id'], 'sourceInputId': base_id,
                  'pendingInputId': base_id if base_id != active['id'] else None,
                  'updatedFactors': 0, 'reusedFactors': 0,
                  'insufficientStationSeasons': [],
                  'message': f'站点订正资料尚不满足估计条件，继续沿用当前输入：{str(exc)[:240]}'}
        if base_id != active['id']:
            db.set_setting(PENDING_SETTING, {'inputId': base_id, 'message': result['message'], 'at': now()})
        db.set_setting(STATUS_SETTING, {'at': now(), **result})
        db.log('inputs.precipitation-correction-insufficient', result)
        return result


def activate(db, input_id, task_id=None):
    """Atomically select an already verified registered forcing input."""
    from .model_forcing import CURRENT_SETTING, _product, verify_product
    candidate = _product(db, input_id)
    verify_product(db, candidate)
    with db.connect() as connection:
        current_row = connection.execute('SELECT value FROM settings WHERE key=?', (CURRENT_SETTING,)).fetchone()
        current_value = json.loads(current_row['value']) if current_row else {}
        if current_value.get('inputId') != input_id:
            connection.execute("DELETE FROM settings WHERE key LIKE 'calibration-configuration:%'")
        connection.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                           (CURRENT_SETTING, canonical({'inputId': input_id, 'taskId': task_id})))
        connection.execute('DELETE FROM settings WHERE key=?', (PENDING_SETTING,))
    return candidate['manifest']
