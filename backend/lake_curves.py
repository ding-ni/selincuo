"""Versioned lake hypsometry and fixed-inflow lake-balance comparisons."""
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

from .common import write_json
from . import operational_store as store

COLLECTION = 'lake-curves'
DEFAULT_ID = 'CURVE-DEMO-SELINCO'


def default_curve():
    return {'id': DEFAULT_ID, 'name': '色林错水位库容关系（演示）', 'type': 'quadratic',
            'coefficients': {'a': .066557, 'b': 2.283942, 'c': 34.7485, 'referenceLevel': 4540.},
            'minLevel': 4537., 'maxLevel': 4545., 'offsetM': 0., 'provisional': True,
            'source': '用户提供的二次关系；待正式水位库容资料确认', 'reference': '待核实高程基准',
            'sourceImage': 'docs/图件/色林错水位面积库容关系.png',
            'rSquared': .9901,
            'areaComparison': {'type': 'quadratic',
                               'coefficients': {'a': -4.228727, 'b': 62.931490, 'c': 2245.603203, 'referenceLevel': 4540.},
                               'rSquared': .9629, 'unit': 'km²', 'role': 'comparison-only',
                               'source': '用户提供的水位面积库容关系图独立面积拟合'},
            'storageUnit': 'km³', 'areaMethod': 'dV/dH', 'status': 'demonstration'}


def validate(value):
    curve = deepcopy(value)
    curve['name'] = str(curve.get('name', '')).strip()
    if not curve['name']:
        raise ValueError('请填写库容曲线名称')
    for key in ('minLevel', 'maxLevel', 'offsetM'):
        curve[key] = float(curve.get(key, 0.))
        if not np.isfinite(curve[key]):
            raise ValueError('曲线高程范围和基准偏移须为有限数值')
    if curve['minLevel'] >= curve['maxLevel']:
        raise ValueError('库容曲线最低水位须低于最高水位')
    if curve.get('type', 'quadratic') == 'quadratic':
        curve['type'] = 'quadratic'
        supplied = curve.get('coefficients') or {}
        coefficients = {k: float(supplied[k]) for k in ('a', 'b', 'c', 'referenceLevel')}
        if not np.isfinite(list(coefficients.values())).all():
            raise ValueError('库容曲线系数须为有限数值')
        curve['coefficients'] = coefficients
        slopes = [2 * coefficients['a'] * (h - coefficients['referenceLevel']) + coefficients['b']
                  for h in (curve['minLevel'], curve['maxLevel'])]
        if min(slopes) <= 0:
            raise ValueError('库容曲线在整个适用水位内必须严格递增且面积为正')
    elif curve['type'] == 'table':
        points = sorted((float(p['level']), float(p['storageKm3'])) for p in curve.get('points', []))
        if len(points) < 2 or not np.isfinite(points).all():
            raise ValueError('水位库容表至少需要两个有效数据点')
        if np.any(np.diff(np.array(points), axis=0) <= 0):
            raise ValueError('水位与库容均须严格递增，不能重复')
        if points[0][0] > curve['minLevel'] or points[-1][0] < curve['maxLevel']:
            raise ValueError('水位库容表未覆盖适用水位范围')
        curve['points'] = [{'level': h, 'storageKm3': v} for h, v in points]
    else:
        raise ValueError('库容曲线仅支持二次关系或水位库容表')
    comparison = curve.get('areaComparison')
    if comparison is not None:
        supplied = comparison.get('coefficients') or {}
        coefficients = {k: float(supplied[k]) for k in ('a', 'b', 'c', 'referenceLevel')}
        if not np.isfinite(list(coefficients.values())).all():
            raise ValueError('独立面积拟合系数须为有限数值')
        low, high = (curve[key] - coefficients['referenceLevel'] for key in ('minLevel', 'maxLevel'))
        xs = [low, high]
        if coefficients['a'] > 0:
            xs.append(np.clip(-coefficients['b'] / (2 * coefficients['a']), low, high))
        if min(coefficients['a'] * x * x + coefficients['b'] * x + coefficients['c'] for x in xs) <= 0:
            raise ValueError('独立面积拟合在适用水位范围内须为正')
        comparison.update(type='quadratic', coefficients=coefficients, unit='km²', role='comparison-only')
        if comparison.get('rSquared') is not None:
            comparison['rSquared'] = float(comparison['rSquared'])
            if not 0 <= comparison['rSquared'] <= 1:
                raise ValueError('独立面积拟合R²须在0—1之间')
    curve.update(storageUnit='km³', areaMethod='dV/dH')
    return curve


def catalog(db):
    items = store.list_records(db, COLLECTION)
    return {'defaultCurveId': DEFAULT_ID, 'items': [default_curve(), *items]}


def get_curve(db, identifier=None):
    return default_curve() if not identifier or identifier == DEFAULT_ID else store.get(db, COLLECTION, identifier)


def save_curve(db, payload):
    curve = validate(payload)
    curve.pop('id', None)
    if not str(curve.get('source', '')).strip() or not str(curve.get('reference', '')).strip():
        raise ValueError('请登记曲线资料来源与高程基准')
    return store.save(db, COLLECTION, curve, prefix='CURVE-')


def _height(h, curve):
    value = np.asarray(h, dtype=float) + curve.get('offsetM', 0.)
    if not np.isfinite(value).all() or np.any(value < curve['minLevel'] - 1e-9) or np.any(value > curve['maxLevel'] + 1e-9):
        raise ValueError('湖位超出库容曲线适用范围')
    return np.clip(value, curve['minLevel'], curve['maxLevel'])


def _number(value):
    return float(value) if np.ndim(value) == 0 else value


def storage(h, curve):
    z = _height(h, curve)
    if curve['type'] == 'quadratic':
        p = curve['coefficients']; x = z - p['referenceLevel']
        v = (p['a'] * x * x + p['b'] * x + p['c']) * 1e9
    else:
        v = np.interp(z, [p['level'] for p in curve['points']], [p['storageKm3'] for p in curve['points']]) * 1e9
    return _number(v)


def level(volume, curve):
    v = np.asarray(volume, dtype=float)
    offset = curve.get('offsetM', 0.)
    low, high = curve['minLevel'] - offset, curve['maxLevel'] - offset
    if not np.isfinite(v).all() or np.any(v < storage(low, curve) - 1.) or np.any(v > storage(high, curve) + 1.):
        raise ValueError('蓄水量超出库容曲线适用范围')
    if curve['type'] == 'table':
        h = np.interp(v / 1e9, [p['storageKm3'] for p in curve['points']], [p['level'] for p in curve['points']])
    else:
        p = curve['coefficients']; target = v / 1e9 - p['c']
        if abs(p['a']) < 1e-14:
            x = target / p['b']
        else:
            root = np.sqrt(p['b'] * p['b'] + 4 * p['a'] * target)
            # This root is continuous with the linear limit and avoids cancellation.
            x = 2 * target / (p['b'] + root) if p['b'] >= 0 else (-p['b'] + root) / (2 * p['a'])
        h = p['referenceLevel'] + x
    return _number(np.clip(h - offset, low, high))


def area(h, curve):
    z = _height(h, curve)
    if curve['type'] == 'quadratic':
        p = curve['coefficients']; a = (2 * p['a'] * (z - p['referenceLevel']) + p['b']) * 1e9
    else:
        heights = np.array([p['level'] for p in curve['points']]); volumes = np.array([p['storageKm3'] for p in curve['points']])
        indexes = np.clip(np.searchsorted(heights, z, side='right') - 1, 0, len(heights) - 2)
        a = (np.diff(volumes) / np.diff(heights))[indexes] * 1e9
    return _number(a)


def comparison_area(h, curve):
    """Independent fitted area in m² for comparison with the storage derivative."""
    comparison = curve.get('areaComparison')
    if comparison is None:
        return None
    p = comparison['coefficients']; x = _height(h, curve) - p['referenceLevel']
    return _number((p['a'] * x * x + p['b'] * x + p['c']) * 1e6)


def hourly_balance(inflow, rain_mm, evaporation_mm, initial_level, curve, outlet=None):
    q, rain, evap = map(lambda v: np.asarray(v, dtype=float), (inflow, rain_mm, evaporation_mm))
    if q.ndim != 1 or rain.shape != q.shape or evap.shape != q.shape or not np.isfinite([q, rain, evap]).all():
        raise ValueError('逐时湖泊来水、降水和蒸发须完整且长度一致')
    if np.any(q < 0) or np.any(rain < 0) or np.any(evap < 0):
        raise ValueError('湖泊来水、降水及蒸发不能为负')
    h = float(initial_level); volume = storage(h, curve)
    levels, volumes, areas, fluxes, outflows = [], [], [], [], []
    for i in range(len(q)):
        surface = area(h, curve)
        out = float(outlet(h) if callable(outlet) else (outlet[i] if outlet is not None else 0.))
        if not np.isfinite(out) or out < 0: raise ValueError('湖泊外流须为非负有效流量')
        net = (q[i] - out) * 3600 + (rain[i] - evap[i]) / 1000 * surface
        volume += net; h = level(volume, curve)
        levels.append(h); volumes.append(volume); areas.append(surface); fluxes.append(net); outflows.append(out)
    return {'lakeLevel': np.array(levels), 'storageM3': np.array(volumes), 'areaM2': np.array(areas),
            'netVolumeM3': np.array(fluxes), 'outflow': np.array(outflows)}


def run_comparison(db, request, folder=None, progress=None, cancelled=None):
    from .result_query import source
    from .grid_simulation import prepare
    from .forcing import lake_precipitation_series
    from .simulation import RIVERS
    curve = get_curve(db, request.get('curveId'))
    task, hourly, state = source(db, request['sourceResultId'])
    result = task.get('result') or task
    times = pd.DatetimeIndex(np.asarray(hourly['time'][:]).astype('datetime64[ns]'))
    old_levels = np.asarray(hourly['lakeLevel'][:], dtype=float)
    mask = np.isfinite(old_levels)
    if request.get('start'): mask &= times >= pd.Timestamp(request['start'])
    if request.get('end'): mask &= times <= pd.Timestamp(request['end'])
    ids = np.flatnonzero(mask)
    if len(ids) < 2 or not np.all(np.diff(ids) == 1):
        raise ValueError('对照需要有初始湖位的连续逐时成果')
    forcing = prepare(db, result['inputId'], result['spatialVersion'])
    positions = forcing['times'].get_indexer(times[ids])
    if np.any(positions < 0): raise ValueError('原成果的冻结气象未完整安装')
    rain = lake_precipitation_series(forcing['manifest'], forcing['spatial'], forcing['arrays'], forcing['times'], positions)
    evap = forcing['arrays']['evaporation'][positions] @ np.asarray(forcing['spatial']['lakeWeights']) * result['parameters']['lakeCoefficient']
    q = np.asarray(hourly['flow'][ids[0]:ids[-1] + 1])[:, result.get('rivers', RIVERS).index('total')]
    if cancelled and cancelled(): raise InterruptedError('已停止湖位库容对照')
    replay = hourly_balance(q[1:], rain[1:], evap[1:], old_levels[ids[0]], curve)
    curve_levels = np.r_[old_levels[ids[0]], replay['lakeLevel']]
    value = {'name': request.get('name', '湖位库容固定来水对照'), 'sourceResultId': request['sourceResultId'],
             'curveId': curve['id'], 'lakeCurve': curve, 'method': 'fixed-inflow-variable-area-water-balance',
             'hydrologyRecalculated': False, 'times': [t.isoformat() for t in times[ids]],
             'fixedAreaLevel': old_levels[ids].tolist(), 'curveLevel': curve_levels.tolist(),
             'initialLakeLevel': float(old_levels[ids[0]]), 'inflow': q.tolist(),
             'maximumLevelDifferenceM': float(np.max(np.abs(curve_levels - old_levels[ids]))),
             'provisional': bool(curve.get('provisional', False)),
             'summary': {'hours': len(ids), 'start': times[ids[0]].isoformat(), 'end': times[ids[-1]].isoformat()}}
    saved = store.save(db, 'lake-curve-comparisons', value, prefix='LCOMP-')
    if folder: write_json(Path(folder) / '湖位库容对照.json', saved)
    if progress: progress(100, '原入湖过程不变，湖位库容对照已完成')
    return saved
