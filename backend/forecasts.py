"""Hourly forecast branches from verified frozen or successful model states."""
from copy import deepcopy
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from .common import read_json, within, write_json
from . import operational_store as store
from . import lake_curves

COLLECTION = 'forecasts'
FROZEN_WARM_RAIN_HANDOFF = (
    'add1e581be4154dc22df6ae1a9f6765e2fff8bffd4f6075dfa61107d3bf8c549',
    '9c9e4369876dfd1daf9b3a5a3f989958d1a6a459fc115fdec8dbc752793f5695')


def source_context(db, identifier):
    from .published_results import catalog as published_catalog, detail as published_detail, result_file
    from .grid_simulation import model_identity, parameter_configuration, PARTITIONS
    from .grid_spatial import load
    published = next((r for r in published_catalog() if r['id'] == identifier), None)
    if published:
        task = published_detail(identifier)
        state = read_json(result_file(identifier, 'final-state.json'))
    else:
        task = db.task(identifier)
        if task['status'] != 'succeeded': raise ValueError('初始状态必须来自成功成果')
        state = read_json(within(db.workspace / 'tasks', identifier) / 'checkpoint.json')
    result = task.get('result') or task
    if not state or state.get('schemaVersion') != 3 or state['identity'].get('modelFamily') != 'grid-continuous-v1':
        raise ValueError('请选择带完整连续模型状态的成果')
    source_implementation = state['identity'].get('modelImplementation')
    implementation = model_identity()
    frozen_handoff = (bool(published) and identifier == 'JOB-05c3f62282ce4239aed1' and
                      (source_implementation, implementation) == FROZEN_WARM_RAIN_HANDOFF)
    if source_implementation != implementation and not frozen_handoff:
        raise ValueError('初始成果的数值模型与当前实现不同，请先以相同配置完成历史状态计算')
    handoff = {'sourceModelImplementation': source_implementation, 'forecastModelImplementation': implementation,
               'method': 'verified-frozen-warm-rain-state' if frozen_handoff else 'same-implementation',
               'basis': 'unchanged snow/ice, runoff, routing and warm-rain numerical branches' if frozen_handoff else 'identical model identity'}
    spatial = load(state['identity']['spatialVersion'])
    if result.get('spatialVersion') != spatial['version'] or state.get('unitIds') != [u['id'] for u in spatial['units']]:
        raise ValueError('成果空间或初始状态单元顺序不一致')
    source_request = {key: state.get(key) for key in ('parameters', 'partitionParameters', 'parameterTransfer')}
    _, _, params, regions, audit, identities = parameter_configuration(spatial, source_request)
    if any(state['identity'].get(key) != value for key, value in identities.items()):
        raise ValueError('初始成果的实际参数或参数移植关系不一致')
    values = np.asarray(state['unitStates'], dtype=float)
    if values.shape != (len(spatial['units']), 8) or not np.isfinite(values).all() or np.any(values < -1e-8):
        raise ValueError('初始雪冰土壤水状态不完整')
    if set(state.get('routing', {})) != set(PARTITIONS): raise ValueError('初始成果缺少完整分区汇流状态')
    for partition in PARTITIONS:
        routing = state['routing'][partition]; channel = routing.get('channel', {}); sources = routing.get('sources', {})
        p = regions.get(partition, params)
        if set(sources) != {'interflow', 'baseflow'} or len(channel.get('queue', [])) != int(p['lagHours']) + 2:
            raise ValueError('初始成果缺少水源蓄水或传播队列')
        stored = np.asarray([channel['storage'], *channel['queue'], *sources.values()])
        if not np.isfinite(stored).all() or np.any(stored < -1e-8): raise ValueError('初始汇流蓄水无效')
    if result.get('parameters') != state.get('parameters') or result.get('parameterTransfer') != state.get('parameterTransfer'):
        raise ValueError('成果与检查点的参数不一致')
    from . import lake_revision
    model = lake_revision.active(db)
    lake_model = model if model and model.get('sourceResultId') == identifier and model['endState']['time'] == state['validAt'] else None
    if lake_model:
        # Lake-only revision: same land/routing state, V(H) lake level and the fitted lake coefficient.
        params = {**params, 'lakeCoefficient': lake_model['lakeCoefficient']}
        state = deepcopy(state)
        state['lake'] = {**(state.get('lake') or {}), 'level': lake_model['endState']['levelM'], 'lakeModelId': lake_model['id']}
    return {'task': task, 'result': result, 'state': state, 'spatial': spatial, 'parameters': params,
            'regions': regions, 'transferAudit': audit, 'sourceResultId': identifier, 'published': bool(published),
            'stateHandoff': handoff, 'lakeModel': lake_model}


def _lake_setup(db, context, curve_id=None):
    '''Curve and breach outflow used by this forecast's lake balance.'''
    from . import lake_revision
    model = context.get('lakeModel')
    curve = lake_curves.get_curve(db, model['curveId'] if model else curve_id)
    return curve, (lake_revision.outlet_function(model, curve) if model else None)


def catalog(db):
    return {'items': store.list_records(db, COLLECTION)}


def _input(db, identifier):
    from .forecast_inputs import COLLECTION as INPUT_COLLECTION
    return store.get(db, INPUT_COLLECTION, identifier)


def _history(db, context, issue):
    from .grid_simulation import prepare, verify_parent_forcing
    from .model_forcing import current_product
    state_time = pd.Timestamp(context['state']['validAt'])
    if issue == state_time: return None
    if issue < state_time: raise ValueError('起报时刻早于所选末状态，请选择该时刻之前的成果')
    choices = [context['state']['inputId']]
    try:
        identifier = current_product(db)['id']
        if identifier not in choices: choices.insert(0, identifier)
    except (OSError, ValueError, KeyError): pass
    for identifier in choices:
        manifest = read_json(db.workspace / 'inputs/products' / identifier / 'manifest.json')
        if not manifest or pd.Timestamp(manifest['end']) < issue: continue
        source = prepare(db, identifier, context['spatial']['version'])
        original_identity = context['state']['identity']
        if (source['manifest'].get('transformationSHA256') != original_identity.get('forcingTransformationSHA256') or
                source['manifest'].get('landTransformationSHA256') != original_identity.get('landForcingTransformationSHA256')):
            raise ValueError('状态推进资料的降水订正规则与原成果不同，请先重算历史状态')
        verify_parent_forcing(db, source, context['state'])
        expected = pd.date_range(state_time + pd.Timedelta(hours=1), issue, freq='h')
        indexes = source['times'].get_indexer(expected)
        if np.any(indexes < 0): raise ValueError('末状态至起报时刻之间存在逐时气象缺口')
        return source, indexes, expected
    raise ValueError(f'实际气象未覆盖末状态{state_time:%Y-%m-%d %H:%M}至起报{issue:%Y-%m-%d %H:%M}的状态推进；请更新资料或选择资料覆盖内的起报时刻')


def preflight(db, payload):
    from .forecast_inputs import hour
    deficiencies = []
    try:
        context = source_context(db, payload['sourceResultId'])
        weather = _input(db, payload['inputId'])
        issue = hour(payload.get('issueTime') or weather['issueTime'], '起报时刻')
        if issue != pd.Timestamp(weather['issueTime']): raise ValueError('起报时刻与未来气象起点不一致')
        if weather['sourceResultId'] != payload['sourceResultId'] or weather['spatialVersion'] != context['spatial']['version'] or weather['unitIds'] != context['state']['unitIds']:
            raise ValueError('未来气象与初始成果的空间单元不一致')
        _history(db, context, issue)
        curve, _ = _lake_setup(db, context, payload.get('curveId'))
        initial = payload.get('initialLakeLevel')
        if initial is None: initial = context['state'].get('lake', {}).get('level')
        if initial is None: raise ValueError('初始成果没有湖位，请提供有来源的起报湖位')
        lake_curves.storage(initial, curve)
        if payload.get('initialLakeLevel') is not None and not str(payload.get('initialLakeSource', '')).strip():
            raise ValueError('手填初始湖位须填写来源')
        if payload.get('useLatestMainStation'): _latest_main(db, issue)
        return {'ready': True, 'deficiencies': [], 'issueTime': issue.isoformat(), 'stateTime': context['state']['validAt'],
                'gapHours': int((issue - pd.Timestamp(context['state']['validAt'])) / pd.Timedelta(hours=1)),
                'weather': {k: weather[k] for k in ('id', 'name', 'start', 'end', 'hours', 'memberCount')},
                'curve': curve, 'stateSource': 'frozen-result' if context['published'] else 'successful-task'}
    except (ValueError, FileNotFoundError, KeyError, OSError) as exc:
        deficiencies.append(str(exc))
    return {'ready': False, 'deficiencies': deficiencies}


def _latest_main(db, issue):
    from .observation_query import SeriesView
    stations = db.rows("SELECT metadata FROM stations WHERE id='SLC'")
    metadata = json.loads(stations[0]['metadata']) if stations else {}
    datum = metadata.get('verticalDatum') or {}
    if not metadata.get('datumVerified') and datum.get('role') != 'working-reference':
        raise ValueError('主站水位基准尚未确认为当前模型工作基准')
    rows = SeriesView(db).observations('SLC', 'level', start=(issue - pd.Timedelta(hours=6)).isoformat(), end=issue.isoformat())
    eligible = [r for r in rows if r['step'] != 'day' and r['quality'] in ('valid', 'qualified') and r['value'] is not None]
    if not eligible: raise ValueError('起报前6小时没有同基准的主站合格瞬时水位')
    row = eligible[-1]
    sources = db.rows('SELECT s.path,s.metadata FROM observations o JOIN sources s ON s.id=o.source_id WHERE o.id=?',
                      (row['id'],))
    if sources and sources[0]['path'].startswith('exchange/connection/'):
        source_metadata = json.loads(sources[0]['metadata']).get('sourceMetadata') or {}
        level_datum = source_metadata.get('levelDatum') or {}
        if level_datum.get('status') != 'verified' or not level_datum.get('name') or not level_datum.get('basis'):
            raise ValueError('最新主站水位的现场来源尚未确认与模型同基准，请在连接映射中核实后重新同步')
    return {'level': float(row['value']), 'time': row['time'], 'source': 'SLC合格瞬时观测', 'observationId': row['id'],
            'hoursBeforeIssue': float((issue - pd.Timestamp(row['time'])) / pd.Timedelta(hours=1))}


def _hydrology(context, parent, times, rain, temperature, evaporation):
    from .parameter_regions import run_region_kernel
    from .routing import route_partition
    from .grid_simulation import PARTITIONS
    from .simulation import RIVERS
    units = context['spatial']['units']; p = context['parameters']; regions = context['regions']
    expected = (len(times), len(units))
    if any(np.shape(a) != expected or not np.isfinite(a).all() for a in (rain, temperature, evaporation)):
        raise ValueError('预报气象与模型单元、小时轴不一致')
    state = np.asarray(parent['unitStates'], dtype=float).copy()
    diagnostic, _, state = run_region_kernel(np.ascontiguousarray(rain), np.ascontiguousarray(temperature),
        np.ascontiguousarray(evaporation), np.array([u['kind'] == 'glacier' for u in units]), state, units, p, regions)
    balance = float(np.max(np.abs(diagnostic[:, :, 7])))
    if balance > 1e-6: raise ValueError('预报产流水量平衡不闭合')
    volumes = diagnostic[:, :, :4] * np.array([u['areaKm2'] for u in units])[None, :, None] * 1000
    old_routing = deepcopy(parent['routing']); routed = {}; routing_error = 0.
    for partition in PARTITIONS:
        indexes = [i for i, u in enumerate(units) if u['partition'] == partition]
        volume = volumes[:, indexes].sum(axis=1) if indexes else np.zeros((len(times), 4))
        flow, _, final, _, errors = route_partition(volume, old_routing[partition], regions.get(partition, p), parent['identity']['routingMode'])
        routed[partition] = flow; old_routing[partition] = final
        routing_error = max(routing_error, max(map(abs, errors.values())))
    q = {'duja': routed['duja'], 'zajia': routed['duja'] + routed['zajia_rest'],
         **{k: routed[k] for k in ('zagen', 'boqu', 'ali', 'fringe')}}
    q['total'] = sum(q[k] for k in ('zajia', 'zagen', 'boqu', 'ali', 'fringe'))
    final = deepcopy(parent); final.update(unitStates=state.tolist(), routing=old_routing, validAt=times[-1].isoformat())
    return q, final, {'landBalanceMaximumMm': balance, 'routingBalanceMaximumM3': routing_error,
                      'excludedVolumeM3': float(((routed['external'] + routed['unresolved']) * 3600).sum())}


def _advance(db, context, issue, curve, progress=None, cancelled=None, outlet=None):
    from .grid_simulation import unit_forcing
    from .land_precipitation import validate_manifest
    from .forcing import lake_precipitation_series
    history = _history(db, context, issue); parent = deepcopy(context['state'])
    if history is None: return parent, {'hours': 0, 'inputId': parent['inputId'], 'recentForcing': []}
    source, indexes, times = history; p = context['parameters']; regions = context['regions']; spatial = context['spatial']
    if not parent.get('lake') or parent['lake'].get('level') is None: raise ValueError('状态推进缺少连续湖位')
    recent = []
    for start in range(0, len(times), 168):
        if cancelled and cancelled(): raise InterruptedError('已停止实际气象状态推进')
        ids = indexes[start:start + 168]; block_times = times[start:start + 168]
        rain, temp, evap = unit_forcing(spatial, source['arrays'], ids, p, regions, validate_manifest(source['manifest']), source['times'])
        q, final, _ = _hydrology(context, parent, block_times, rain, temp, evap)
        lake_rain = lake_precipitation_series(source['manifest'], spatial, source['arrays'], source['times'], ids)
        lake_evap = source['arrays']['evaporation'][ids] @ np.asarray(spatial['lakeWeights']) * p['lakeCoefficient']
        recent.extend({'time': time.isoformat(), 'inflow': float(flow), 'rainMm': float(r), 'evaporationMm': float(e)}
                      for time, flow, r, e in zip(block_times, q['total'], lake_rain, lake_evap) if time > issue - pd.Timedelta(hours=7))
        lake = lake_curves.hourly_balance(q['total'], lake_rain, lake_evap, parent['lake']['level'], curve, outlet)
        final['lake'] = {**parent['lake'], 'level': float(lake['lakeLevel'][-1]), 'validAt': block_times[-1].isoformat()}
        parent = final
        if progress: progress(15 * (start + len(ids)) / len(times), f'实际气象推进至{block_times[-1]}')
    return parent, {'hours': len(times), 'inputId': source['manifest']['id'], 'start': times[0].isoformat(),
                    'end': times[-1].isoformat(), 'recentForcing': recent}


def _reanchor(db, context, observation, issue, curve, advancement, outlet=None):
    """Carry a recent observed lake level to issue time with actual hourly fluxes."""
    observed_at = pd.Timestamp(observation['time'])
    if observed_at == issue: return float(observation['level'])
    required = pd.date_range(observed_at.floor('h') + pd.Timedelta(hours=1), issue, freq='h')
    values = {pd.Timestamp(row['time']): row for row in advancement.get('recentForcing', [])}
    missing = [time for time in required if time not in values]
    if missing:
        from .result_query import source
        from .grid_simulation import prepare
        from .forcing import lake_precipitation_series
        from .simulation import RIVERS
        task, hourly, _ = source(db, context['sourceResultId'])
        result = task.get('result') or task
        frozen_times = pd.DatetimeIndex(np.asarray(hourly['time'][:]).astype('datetime64[ns]'))
        positions = frozen_times.get_indexer(missing)
        if np.any(positions < 0): raise ValueError('主站观测至起报时刻缺少实际来水过程，不能直接将旧观测当作起报湖位')
        forcing = prepare(db, context['state']['inputId'], context['spatial']['version'])
        ids = forcing['times'].get_indexer(missing)
        if np.any(ids < 0): raise ValueError('主站观测至起报时刻缺少实际湖面气象')
        rain = lake_precipitation_series(forcing['manifest'], context['spatial'], forcing['arrays'], forcing['times'], ids)
        evap = forcing['arrays']['evaporation'][ids] @ np.asarray(context['spatial']['lakeWeights']) * context['parameters']['lakeCoefficient']
        for time, index, r, e in zip(missing, positions, rain, evap):
            flow = np.asarray(hourly['flow'][int(index)])[result.get('rivers', RIVERS).index('total')]
            values[time] = {'inflow': float(flow), 'rainMm': float(r), 'evaporationMm': float(e)}
    fractions = np.ones(len(required)); fractions[0] = float((required[0] - observed_at) / pd.Timedelta(hours=1))
    q = [values[t]['inflow'] * f for t, f in zip(required, fractions)]
    rain = [values[t]['rainMm'] * f for t, f in zip(required, fractions)]
    evap = [values[t]['evaporationMm'] * f for t, f in zip(required, fractions)]
    return float(lake_curves.hourly_balance(q, rain, evap, observation['level'], curve, outlet)['lakeLevel'][-1])


def run(db, request, folder=None, progress=None, cancelled=None):
    from .forecast_inputs import hour
    from .simulation import RIVERS
    check = preflight(db, request)
    if not check['ready']: raise ValueError('；'.join(check['deficiencies']))
    context = source_context(db, request['sourceResultId']); weather = _input(db, request['inputId'])
    issue = hour(check['issueTime']); curve, outlet = _lake_setup(db, context, request.get('curveId'))
    parent, advancement = _advance(db, context, issue, curve, progress, cancelled, outlet)
    initial_level = float(parent.get('lake', {}).get('level'))
    level_source = {'method': 'model-state', 'time': issue.isoformat(), 'level': initial_level}
    if request.get('useLatestMainStation'):
        observed = _latest_main(db, issue)
        initial_level = _reanchor(db, context, observed, issue, curve, advancement, outlet)
        level_source = {**observed, 'method': 'main-station-then-actual-flux-to-issue', 'issueLevel': initial_level}
    if request.get('initialLakeLevel') is not None:
        initial_level = float(request['initialLakeLevel']); level_source = {'method': 'user-known-level', 'level': initial_level,
               'time': issue.isoformat(), 'source': request['initialLakeSource']}
    lake_curves.storage(initial_level, curve)
    identifier = 'FORECAST-' + uuid.uuid4().hex[:20]
    output = Path(folder) if folder else db.workspace / '水文预报' / identifier
    output.mkdir(parents=True, exist_ok=True)
    with np.load(within(db.workspace, weather['dataPath']), allow_pickle=False) as archive:
        times = pd.DatetimeIndex(archive['time'].astype('datetime64[ns]'))
        expected = pd.date_range(issue + pd.Timedelta(hours=1), periods=weather['hours'], freq='h')
        if not times.equals(expected): raise ValueError('未来气象小时轴与起报时刻不连续')
        flows = []; levels = []; outflows = []; diagnostics = []; index = []; rain_lake = archive['lakePrecipitation'].copy()
        evap_lake = archive['lakeEvaporation'].copy() * context['parameters']['lakeCoefficient']
        for m, member in enumerate(weather['memberIds']):
            if cancelled and cancelled(): raise InterruptedError('已停止水文集合预报')
            q, final, diagnostic = _hydrology(context, parent, times, archive['precipitation'][m], archive['temperature'][m], archive['evaporation'][m])
            lake = lake_curves.hourly_balance(q['total'], rain_lake[m], evap_lake[m], initial_level, curve, outlet)
            flows.append(np.stack([q[k] for k in RIVERS], axis=1)); levels.append(lake['lakeLevel']); outflows.append(lake['outflow'])
            diagnostics.append({'memberId': member, **diagnostic})
            final['lake'] = {'level': float(lake['lakeLevel'][-1]), 'storageM3': float(lake['storageM3'][-1]),
                             'validAt': times[-1].isoformat(), 'curveId': curve['id']}
            final.update(forecastInputId=weather['id'], memberId=member, initialLakeSource=level_source)
            checkpoint_path = output / ('成员末状态-' + member + '.json'); write_json(checkpoint_path, final)
            index.append({'memberId': member, 'validAt': times[-1].isoformat(),
                          'path': checkpoint_path.relative_to(db.workspace).as_posix()})
            if progress: progress(15 + 80 * (m + 1) / weather['memberCount'], f'水文预报成员 {m + 1}/{weather["memberCount"]}')
    flows = np.asarray(flows); levels = np.asarray(levels); outflows = np.asarray(outflows)
    inflow = flows[:, :, RIVERS.index('total')]; duja = flows[:, :, RIVERS.index('duja')]
    path = output / '逐时集合预报.npz'
    np.savez_compressed(path, time=times.asi8, flow=flows, inflow=inflow, lakeLevel=levels, outflow=outflows,
                        lakeRainMm=rain_lake, lakeEvaporationMm=evap_lake)
    control_index = weather['memberIds'].index('control') if 'control' in weather['memberIds'] else 0
    def member_values(i):
        return {'inflow': inflow[i].tolist(), 'lakeLevel': levels[i].tolist(), 'dujaFlow': duja[i].tolist(),
                'outflow': outflows[i].tolist(),
                'rivers': {k: flows[i, :, RIVERS.index(k)].tolist() for k in ('zajia', 'zagen', 'boqu', 'ali', 'fringe')},
                'lakeRainMm': rain_lake[i].tolist(), 'lakeEvaporationMm': evap_lake[i].tolist()}
    quantiles = {}
    for label, percentile in [('p10', 10), ('p50', 50), ('p90', 90)]:
        quantiles[label] = {key: np.percentile(value, percentile, axis=0).tolist() for key, value in
                           [('inflow', inflow), ('lakeLevel', levels), ('dujaFlow', duja), ('outflow', outflows),
                            ('lakeRainMm', rain_lake), ('lakeEvaporationMm', evap_lake)]}
    exceedance = []
    for threshold in request.get('thresholds', []):
        value = float(threshold['level'])
        if not np.isfinite(value): raise ValueError('预警水位须为有效数值')
        exceedance.append({**threshold, 'level': value, 'hourlyFraction': np.mean(levels >= value, axis=0).tolist(),
                           'anyTimeFraction': float(np.mean(np.any(levels >= value, axis=1))),
                           'memberCount': weather['memberCount']})
    result = {'name': request.get('name') or '色林错水文预报', 'sourceResultId': request['sourceResultId'], 'inputId': weather['id'],
              'issueTime': issue.isoformat(), 'times': [t.isoformat() for t in times], 'start': times[0].isoformat(),
              'end': times[-1].isoformat(), 'hours': len(times), 'memberIds': weather['memberIds'], 'memberCount': weather['memberCount'],
              'initialLakeLevel': initial_level, 'initialLakeSource': level_source, 'stateAdvancement': advancement,
              'stateHandoff': context.get('stateHandoff'),
              'lakeCurve': curve, 'curveId': curve['id'], 'controlMember': weather['memberIds'][control_index],
              'control': member_values(control_index), 'quantiles': quantiles, 'exceedance': exceedance,
              'memberLakeLevels': levels.tolist(), 'memberCheckpointIndex': index,
              'memberArtifact': {'path': path.relative_to(db.workspace).as_posix(), 'shape': list(levels.shape),
                                 'arrays': {'lakeLevel': 'lakeLevel', 'inflow': 'inflow', 'flow': 'flow', 'lakeRainMm': 'lakeRainMm', 'lakeEvaporationMm': 'lakeEvaporationMm'}},
              'modelFamily': 'forecast-grid-continuous-v1', 'hydrologyModelFamily': 'grid-continuous-v1',
              'spatialVersion': context['spatial']['version'],
              'parameters': deepcopy(context['state']['parameters']), 'diagnostics': diagnostics,
              'weatherMetadata': weather['metadata'], 'probabilityMeaning': '未校准的集合成员超限比例',
              'provisional': bool(curve.get('provisional', False)),
              'lakeModelId': (context.get('lakeModel') or {}).get('id'),
              'outlet': (context.get('lakeModel') or {}).get('outlet'),
              'summary': {'peakInflowM3s': float(np.max(inflow)), 'maximumMedianLevel': float(np.max(np.median(levels, axis=0))),
                          'maximumMedianLevelTime': times[int(np.argmax(np.median(levels, axis=0)))].isoformat(),
                          'maximumControlLevel': float(np.max(levels[control_index])),
                          'finalMedianLevel': float(np.median(levels[:, -1])),
                          'peakDujaM3s': float(np.max(np.median(duja, axis=0))),
                          'peakDujaTime': times[int(np.argmax(np.median(duja, axis=0)))].isoformat(),
                          'outflowVolumeM3': float(np.median(outflows.sum(axis=1)) * 3600.),
                          'forecastDays': len(times) / 24}}
    saved = store.save(db, COLLECTION, result, identifier=identifier)
    write_json(output / '预报结果.json', saved)
    if progress: progress(100, '水文预报已保存，原始成果与状态保留')
    return saved


def detail(db, identifier):
    return store.get(db, COLLECTION, identifier)
