from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .common import ROOT, CONTEXT_KEY, digest, model_identity, read_json, write_json
from .forcing import load_input
from .model_core import (DEFAULT_PARAMETERS, DIAGNOSTIC_NAMES, PARAMETER_NAMES, STATE_NAMES,
                         initial_states, parameters, route_series, run_kernel)
from .observations import daily_series

PARTITIONS = ['duja', 'zajia_rest', 'zagen', 'boqu', 'ali', 'fringe']
RIVERS = ['duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total']
_prepared_cache = {}


def observation_snapshot(db):
    flow = daily_series(db, 'DUJA', 'flow')
    level = daily_series(db, 'SLC', 'level')
    windows = lake_windows(db)
    return {'sources': db.rows('SELECT id,sha256 FROM sources ORDER BY id'),
            'flow': {t.isoformat(): float(v) for t, v in flow.dropna().items()},
            'level': {t.isoformat(): float(v) for t, v in level.dropna().items()},
            'windows': [{**w, 'start': w['start'].isoformat(), 'end': w['end'].isoformat()} for w in windows]}


def snapshot_series(snapshot, key):
    values = snapshot[key]
    return pd.Series(list(values.values()), index=pd.to_datetime(list(values)), dtype=float).sort_index()


def metrics(observed, simulated):
    pair = pd.concat([observed.rename('obs'), simulated.rename('sim')], axis=1).dropna()
    if not len(pair):
        return {'count': 0, 'nse': None, 'kge': None, 'rmse': None, 'mae': None, 'bias': None, 'pbias': None}
    o, s = pair.obs.values, pair.sim.values
    error = s - o
    variance = np.sum((o - o.mean()) ** 2)
    nse = 1 - np.sum(error ** 2) / variance if variance > 1e-12 else None
    kge = None
    if len(o) > 1 and np.std(o) > 1e-10 and np.std(s) > 1e-10 and abs(o.mean()) > 1e-10:
        r = np.corrcoef(o, s)[0, 1]
        kge = 1 - math.sqrt((r - 1) ** 2 + (np.std(s) / np.std(o) - 1) ** 2 + (s.mean() / o.mean() - 1) ** 2)
    return {'count': len(pair), 'nse': float(nse) if nse is not None else None,
            'kge': float(kge) if kge is not None else None, 'rmse': float(np.sqrt(np.mean(error ** 2))),
            'mae': float(np.mean(np.abs(error))), 'bias': float(error.mean()),
            'pbias': float(100 * error.sum() / o.sum()) if abs(o.sum()) > 1e-10 else None,
            'observedMean': float(o.mean()), 'simulatedMean': float(s.mean())}


def lake_windows(db):
    series = daily_series(db, 'SLC', 'level')
    windows = []
    if not len(series):
        return windows
    group = []
    for time, value in series.dropna().items():
        if group and (time - group[-1][0] > pd.Timedelta(days=7) or time.year != group[-1][0].year):
            windows.append(group)
            group = []
        group.append((time, value))
    if group:
        windows.append(group)
    result = []
    for group in windows:
        first, final = group[0][0], group[-1][0]
        raw = db.observations('SLC', 'level', 'irregular', first.isoformat(), (first + pd.Timedelta(days=1)).isoformat())
        valid = [r for r in raw if r['value'] is not None and r['quality'] == 'valid']
        if valid:
            observed_at = pd.Timestamp(valid[0]['time'])
            start = observed_at.ceil('h')
            h0 = valid[0]['value']
            method = 'first-reliable-subdaily-level'
        else:
            start, h0, method = first + pd.Timedelta(hours=12), group[0][1], 'source-daily-mean-anchored-at-noon'
            observed_at = start
        result.append({'id': start.isoformat(), 'start': start, 'end': final + pd.Timedelta(hours=23),
                       'level': float(h0), 'method': method, 'observationTime': observed_at.isoformat(),
                       'firstHourFraction': float((start - observed_at) / pd.Timedelta(hours=1))})
    return result


def lake_balance(times, inflow, rain, e0, area_m2, coefficient, windows, parent=None):
    levels = np.full(len(times), np.nan)
    volumes = np.full(len(times), np.nan)
    flux = inflow * 3600 + (rain - e0 * coefficient) / 1000 * area_m2
    current = dict(parent or {})
    starts = []
    for i, time in enumerate(times):
        window = next((w for w in windows if w['start'] <= time <= w['end']), None)
        if window is None:
            current = {}
            continue
        if current.get('windowId') != window['id']:
            # A simulation beginning midway through a window cannot borrow an
            # unobserved initial lake level. The caller must replay from anchor.
            if time != window['start']:
                continue
            partial = float(flux[i] * window.get('firstHourFraction', 0))
            current = {'windowId': window['id'], 'level': window['level'] + partial / area_m2,
                       'volumeChange': partial, 'validAt': time.isoformat()}
            starts.append(time.normalize())
        else:
            if pd.Timestamp(current['validAt']) + pd.Timedelta(hours=1) != time:
                raise ValueError('湖泊状态与下一个输入时次不连续')
            current['volumeChange'] += float(flux[i])
            current['level'] += float(flux[i] / area_m2)
            current['validAt'] = time.isoformat()
        levels[i], volumes[i] = current['level'], current['volumeChange']
    return levels, volumes, flux, current, starts


def prepare(db, input_id):
    spatial = read_json(ROOT / 'data/derived/spatial/manifest.json')
    cache_key = (str(db.workspace), input_id, spatial['version'])
    if cache_key not in _prepared_cache:
        manifest, forcing = load_input(db, input_id, spatial)
        weights = np.asarray(spatial['weights'], dtype=float)
        lake_weights = np.asarray(spatial['lakeWeights'], dtype=float)
        unit_temperature = forcing['temperature'] @ weights.T
        grid_z = np.array([g['referenceElevation'] for g in spatial['grids']])
        reference_z = weights @ grid_z
        _prepared_cache.clear()
        _prepared_cache[cache_key] = {'spatial': spatial, 'manifest': manifest, 'times': forcing['time'],
                                    'precipitation': forcing['precipitation'] @ weights.T,
                                    'temperature': unit_temperature,
                                    'evaporation': forcing['evaporation'] @ weights.T,
                                    'lakeRain': forcing['precipitation'] @ lake_weights,
                                    'lakeEvaporation': forcing['evaporation'] @ lake_weights,
                                    'deltaElevation': np.array([u['elevation'] for u in spatial['units']]) - reference_z}
    return _prepared_cache[cache_key]


def run_simulation(db, request, folder=None, progress=None, cancelled=None, prepared=None):
    if request.get('contextKey', CONTEXT_KEY) != CONTEXT_KEY or request.get('projectId', 'selinco') != 'selinco':
        raise ValueError('计算上下文必须属于色林错独立项目')
    p = parameters(request.get('parameters'))
    source = prepared or prepare(db, request['inputId'])
    observation = request.get('_observations') or observation_snapshot(db)
    windows = [{**w, 'start': pd.Timestamp(w['start']), 'end': pd.Timestamp(w['end'])} for w in observation['windows']]
    spatial, input_manifest = source['spatial'], source['manifest']
    all_times = source['times']
    start = pd.Timestamp(request.get('start') or all_times[0])
    end = pd.Timestamp(request.get('end') or all_times[-1])
    if start.minute or start.second or end.minute or end.second or start.tzinfo or end.tzinfo:
        raise ValueError('计算时段须为北京时间整小时')
    if start > end or start < all_times[0] or end > all_times[-1]:
        raise ValueError(f'计算时段超出输入覆盖：{all_times[0]} 至 {all_times[-1]}')
    select = np.where((all_times >= start) & (all_times <= end))[0]
    if not len(select) or all_times[select[0]] != start or all_times[select[-1]] != end:
        raise ValueError('输入中没有完整的指定起止时次')
    times = all_times[select]
    units = spatial['units']
    identity = {'projectId': 'selinco', 'contextKey': CONTEXT_KEY, 'spatialVersion': spatial['version'],
                'inputId': input_manifest['id'], 'inputSHA256': input_manifest['dataSHA256'],
                'parameterSHA256': digest(p), 'observationSHA256': digest(observation),
                'modelImplementation': request.get('_modelIdentity') or model_identity()}
    parent = request.get('_parentState')
    if parent:
        if parent['identity'] != identity:
            raise ValueError('父状态与输入、参数、空间或模型版本不一致')
        if pd.Timestamp(parent['validAt']) + pd.Timedelta(hours=1) != start:
            raise ValueError('续算必须从父状态的下一小时开始')
        state = np.array(parent['unitStates'], dtype=float)
        if state.shape != (len(units), 8) or not np.isfinite(state).all() or np.min(state) < -1e-8:
            raise ValueError('父状态形状或数值无效')
    else:
        state = initial_states(units, p)
    glaciers = np.array([u['kind'] == 'glacier' for u in units])
    vector = np.array([p[k] for k in PARAMETER_NAMES], dtype=float)
    diagnostics, history = [], []
    for offset in range(0, len(select), 744):
        if cancelled and cancelled():
            raise InterruptedError('用户停止计算')
        ids = select[offset:offset + 744]
        rain = np.ascontiguousarray(source['precipitation'][ids])
        temp = np.ascontiguousarray(source['temperature'][ids] + source['deltaElevation'][None, :] * p['lapseRate'])
        evap = np.ascontiguousarray(source['evaporation'][ids])
        d, h, state = run_kernel(rain, temp, evap, glaciers, state, vector)
        diagnostics.append(d)
        history.append(h)
        if progress:
            progress(5 + 65 * min(offset + len(ids), len(select)) / len(select), f'逐小时冰雪产汇流 {times[min(offset + len(ids) - 1, len(times) - 1)]}')
    diagnostics, history = np.concatenate(diagnostics), np.concatenate(history)
    if np.max(np.abs(diagnostics[:, :, 7])) > 1e-6:
        raise ValueError('产流内核水量平衡未通过，成果不登记')
    areas = np.array([u['areaKm2'] for u in units])
    runoff = diagnostics[:, :, :4].sum(axis=2) * areas[None, :] * 1000
    routed, routing_final, routing_history, routing_errors = {}, {}, {}, {}
    for partition in PARTITIONS:
        indexes = [i for i, u in enumerate(units) if u['partition'] == partition]
        inflow = runoff[:, indexes].sum(axis=1)
        old = (parent or {}).get('routing', {}).get(partition, {})
        storage = float(old.get('storage', 0.))
        queue = np.array(old.get('queue', [0.] * (int(p['lagHours']) + 2)), dtype=float)
        if len(queue) != int(p['lagHours']) + 2:
            raise ValueError('传播状态长度与滞时参数不一致')
        old_total = storage + queue.sum()
        out, stored, final_storage, queue = route_series(inflow, storage, queue, p['tauHours'], p['lagHours'])
        error = float(inflow.sum() - out.sum() - (stored[-1] - old_total))
        if abs(error) > max(1e-4, inflow.sum() * 1e-10):
            raise ValueError(f'{partition}汇流水量不闭合')
        routed[partition] = out / 3600
        routing_history[partition] = stored
        routing_errors[partition] = error
        routing_final[partition] = {'storage': float(final_storage), 'queue': queue.tolist()}
    q = {'duja': routed['duja'], 'zajia': routed['duja'] + routed['zajia_rest'],
         **{k: routed[k] for k in ('zagen', 'boqu', 'ali', 'fringe')}}
    q['total'] = sum(q[k] for k in ('zajia', 'zagen', 'boqu', 'ali', 'fringe'))
    levels, volumes, lake_flux, lake_state, starts = lake_balance(
        times, q['total'], source['lakeRain'][select], source['lakeEvaporation'][select],
        spatial['lakeAreaKm2'] * 1e6, p['lakeCoefficient'], windows, (parent or {}).get('lake'))
    hourly = pd.DataFrame(q, index=times)
    daily = hourly.resample('D', closed='right', label='left').mean()
    counts = hourly.total.resample('D', closed='right', label='left').count()
    daily.loc[counts < 24, list(q)] = np.nan
    h_series = pd.Series(levels, index=times)
    daily['lakeLevel'] = h_series.resample('D').mean().where(h_series.resample('D').count() >= 12)
    daily['volumeChange'] = pd.Series(volumes, index=times).resample('D', closed='right', label='left').last()
    daily['lakeRainMm'] = pd.Series(source['lakeRain'][select], index=times).resample('D', closed='right', label='left').sum()
    daily['lakeEvaporationMm'] = pd.Series(source['lakeEvaporation'][select] * p['lakeCoefficient'], index=times).resample('D', closed='right', label='left').sum()
    anchor_times = {w['start']: w.get('firstHourFraction', 0) for w in windows}
    available = np.isfinite(levels)
    applied = available.astype(float) * np.array([anchor_times.get(t, 1.) for t in times])
    active_hours = pd.Series(available.astype(int), index=times).resample('D', closed='right', label='left').sum()
    daily['lakeActiveHours'] = active_hours
    for key, values in {'lakeInflowM3': q['total'] * 3600,
                        'lakePrecipitationM3': source['lakeRain'][select] / 1000 * spatial['lakeAreaKm2'] * 1e6,
                        'lakeEvaporationM3': source['lakeEvaporation'][select] * p['lakeCoefficient'] / 1000 * spatial['lakeAreaKm2'] * 1e6,
                        'lakeDeltaVolumeM3': lake_flux}.items():
        daily[key] = pd.Series(applied * values, index=times).resample('D', closed='right', label='left').sum().where(active_hours > 0)
    daily['lakeBalanceErrorM3'] = daily.lakeDeltaVolumeM3 - daily.lakeInflowM3 - daily.lakePrecipitationM3 + daily.lakeEvaporationM3
    for k, col in [('snowmelt', 4), ('icemelt', 5), ('et', 6)]:
        daily[k + 'Mm'] = pd.Series((diagnostics[:, :, col] * areas[None, :]).sum(axis=1) / areas.sum(), index=times).resample('D', closed='right', label='left').sum()
    for k, col in [('RS', 0), ('RI', 1), ('RG', 2), ('glacierRunoff', 3)]:
        daily[k + 'M3'] = pd.Series((diagnostics[:, :, col] * areas[None, :] * 1000).sum(axis=1), index=times).resample('D', closed='right', label='left').sum()
    flow_obs = snapshot_series(observation, 'flow')
    lake_obs = snapshot_series(observation, 'level')
    daily['observedFlow'] = flow_obs.reindex(daily.index)
    daily['observedLevel'] = lake_obs.reindex(daily.index)
    # Initialization observations never score their own initialized result.
    scored_lake = daily.lakeLevel.copy()
    for window in windows:
        scored_lake.loc[scored_lake.index == window['start'].normalize()] = np.nan
    yearly = {}
    for year in sorted(set(daily.index.year)):
        mask = daily.index.year == year
        flow_metrics = metrics(daily.observedFlow[mask], daily.duja[mask])
        paired = mask & daily.observedFlow.notna() & daily.duja.notna()
        flow_metrics['observedVolumeM3'] = float(daily.observedFlow[paired].sum() * 86400) if paired.any() else None
        flow_metrics['simulatedVolumeM3OnObservedDays'] = float(daily.duja[paired].sum() * 86400) if paired.any() else None
        yearly[str(year)] = {'purpose': {2023: '预热', 2024: '率定', 2025: '独立验证', 2026: '延伸检验'}.get(year, '历史回算'),
                             'flow': flow_metrics, 'lake': metrics(daily.observedLevel[mask], scored_lake[mask])}
    checkpoint = {'schemaVersion': 1, 'identity': identity, 'validAt': times[-1].isoformat(), 'parameters': p,
                  'unitStates': state.tolist(), 'routing': routing_final, 'lake': lake_state}
    result = {'projectId': 'selinco', 'resultSource': 'model-backend', 'identity': identity,
              'start': times[0].isoformat(), 'end': times[-1].isoformat(), 'hours': len(times),
              'parameters': p, 'inputId': input_manifest['id'], 'spatialVersion': spatial['version'],
              'units': units, 'rivers': RIVERS, 'metrics': yearly,
              'daily': json.loads(daily.reset_index(names='date').to_json(orient='records', date_format='iso')),
              'diagnostics': {'landGlacierMaxBalanceErrorMm': float(np.max(np.abs(diagnostics[:, :, 7]))),
                              'routingBalanceErrorM3': routing_errors,
                              'lakeAreaKm2': spatial['lakeAreaKm2'], 'glacierAreaKm2': spatial['glacierAreaKm2'],
                              'lakeWindows': [{'id': w['id'], 'start': w['start'].isoformat(), 'end': w['end'].isoformat(), 'initialization': w['method'],
                                               'observationTime': w.get('observationTime'), 'firstHourFraction': w.get('firstHourFraction', 0)} for w in windows],
                              'unvalidatedRivers': ['zagen', 'boqu', 'ali', 'fringe'],
                              'assumptions': ['定面积湖泊，仅保存相对蓄水变化', '地下水净交换和外流暂按零处理',
                                              '冰川面积固定；长年冰体消融为显式外部水源', '无冰期起点使用实测湖位，其后独立演算',
                                              '未率定初值为通用模型初值，不代表已通过业务验证']},
              'checkpoint': checkpoint}
    if folder:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(folder / 'hourly.npz', time=times.values.astype('datetime64[ns]').astype('int64'),
                            flow=np.stack([q[k] for k in RIVERS], axis=1), lakeLevel=levels, volumeChange=volumes,
                            unitStates=history, unitDiagnostics=diagnostics,
                            routingStorage=np.stack([routing_history[k] for k in PARTITIONS], axis=1),
                            lakeFlux=lake_flux)
        daily.to_csv(folder / 'daily.csv', encoding='utf-8-sig', index_label='date')
        write_json(folder / 'checkpoint.json', checkpoint)
        write_json(folder / 'result.json', result)
    if progress:
        progress(100, '历史计算完成，已保存逐时状态和日评价')
    return result
