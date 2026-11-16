"""Lake-model revision on the adopted V(H) curve with the 2023 Siling Co breach.

Hydrology is not recomputed: hourly inflow, lake precipitation and raw lake
evaporation come from the adopted continuous result.  Stage 1 fits the lake
evaporation coefficient before the overflow (2021-04-23 → 2023-09-20).  Stage 2
keeps that coefficient and fits the breach outflow after the 2023-09-21 onset.
"""
from math import sqrt
from pathlib import Path

import numpy as np
import pandas as pd

from .common import now, write_json
from . import operational_store as store
from . import lake_curves

COLLECTION = 'lake-models'
DEFAULT_SOURCE = 'JOB-05c3f62282ce4239aed1'
ONSET = pd.Timestamp('2023-09-21T00:00:00')
STAGE1_END = pd.Timestamp('2023-09-20T23:00:00')
G = 9.80665
WEIR_COEFFICIENT = 0.577   # broad-crested weir: (2/3)·C·√(2g) ≈ 1.705
INITIAL_WIDTH_M = 60.      # 2023-09-23 field width (Science Bulletin 2024)
LITERATURE = [
    {'title': 'Overflow of Siling Co on the central Tibetan Plateau and its environmental impacts', 'journal': 'Science Bulletin', 'year': 2024},
    {'title': 'Recent large-inland-lake outbursts on the Tibetan Plateau: processes, causes, and mechanisms', 'journal': 'Natural Hazards and Earth System Sciences 25, 1187', 'year': 2025},
]


def weir_flow(head_m, width_m, coefficient=WEIR_COEFFICIENT):
    return 0. if head_m <= 0 else (2. / 3.) * coefficient * width_m * sqrt(2. * G) * head_m ** 1.5


def breach_state(hours_since_onset, crest0, outlet):
    """Crest lowers and the channel widens linearly over the erosion period."""
    if hours_since_onset < 0:
        return None
    fraction = min(1., hours_since_onset / max(1., outlet['erosionDays'] * 24.))
    crest = crest0 - (crest0 - outlet['crestM']) * fraction
    width = INITIAL_WIDTH_M + (outlet['widthM'] - INITIAL_WIDTH_M) * fraction
    return crest, width


def simulate(q, rain, evap, coefficient, initial_level, curve, onset_index=None, crest0=None, outlet=None):
    """Fast hourly V(H) balance; returns hourly levels and outflow (m³/s)."""
    p = curve['coefficients']; a, b, c, ref = p['a'], p['b'], p['c'], p['referenceLevel']
    x = float(initial_level) - ref
    v = a * x * x + b * x + c
    n = len(q); levels = np.empty(n); outflow = np.zeros(n)
    for i in range(n):
        area = (2. * a * x + b) * 1e9
        net = q[i] * 3600. + (rain[i] - coefficient * evap[i]) / 1000. * area
        if outlet is not None and onset_index is not None and i >= onset_index:
            crest, width = breach_state(i - onset_index, crest0, outlet)
            o = weir_flow(ref + x - crest, width)
            if o > 0:
                xc = crest - ref
                above = (v - (a * xc * xc + b * xc + c)) * 1e9
                o = min(o, max(0., above + net) / 3600.)
            outflow[i] = o; net -= o * 3600.
        v += net / 1e9
        x = (-b + sqrt(max(0., b * b - 4. * a * (c - v)))) / (2. * a)
        levels[i] = ref + x
    return levels, outflow


def load_source(db, source_id):
    from .result_query import source
    from .grid_simulation import prepare
    from .forcing import lake_precipitation_series
    from .simulation import RIVERS
    from .published_results import result_file
    task, hourly, _ = source(db, source_id)
    result = task.get('result') or task
    times = pd.DatetimeIndex(np.asarray(hourly['time'][:]).astype('datetime64[ns]'))
    formal = np.asarray(hourly['lakeLevel'][:], dtype=float)
    ids = np.flatnonzero(np.isfinite(formal))
    if len(ids) < 48 or not np.all(np.diff(ids) == 1):
        raise ValueError('来源成果缺少连续逐时湖位')
    forcing = prepare(db, result['inputId'], result['spatialVersion'])
    positions = forcing['times'].get_indexer(times[ids])
    if np.any(positions < 0):
        raise ValueError('来源成果的冻结气象未完整安装')
    rain = np.asarray(lake_precipitation_series(forcing['manifest'], forcing['spatial'], forcing['arrays'], forcing['times'], positions), dtype=float)
    evap = np.asarray(forcing['arrays']['evaporation'][positions] @ np.asarray(forcing['spatial']['lakeWeights']), dtype=float)
    q = np.asarray(hourly['flow'][ids[0]:ids[-1] + 1], dtype=float)[:, result.get('rivers', RIVERS).index('total')]
    daily = pd.read_csv(result_file(source_id, 'daily.csv'), encoding='utf-8-sig', parse_dates=['date']).set_index('date')
    observed = daily['observedLevel'].dropna()
    return {'times': times[ids], 'q': q, 'rain': rain, 'evap': evap, 'formal': formal[ids],
            'observed': observed, 'formalCoefficient': float(result['parameters']['lakeCoefficient']),
            'result': result}


def daily_mean(times, levels):
    series = pd.Series(levels, index=times)
    grouped = series.resample('D', closed='right', label='left')
    return grouped.mean().where(grouped.count() == 24)


def score(times, levels, observed, start=None, end=None):
    sim = daily_mean(times, levels)
    joined = pd.concat([sim.rename('sim'), observed.rename('obs')], axis=1).dropna()
    if start is not None: joined = joined[joined.index >= start.normalize()]
    if end is not None: joined = joined[joined.index <= end.normalize()]
    if joined.empty:
        return {'days': 0, 'rmseM': None, 'biasM': None, 'maeM': None}
    error = joined['sim'] - joined['obs']
    return {'days': int(len(error)), 'rmseM': float(np.sqrt(np.mean(error ** 2))), 'biasM': float(error.mean()),
            'maeM': float(error.abs().mean())}


def fit(data, curve, progress=None, cancelled=None):
    from scipy.optimize import differential_evolution, minimize_scalar
    times, q, rain, evap, observed = (data[k] for k in ('times', 'q', 'rain', 'evap', 'observed'))
    h0 = float(data['formal'][0])
    stage1 = times <= STAGE1_END
    n1 = int(stage1.sum())
    t1 = times[:n1]

    def objective1(k):
        if cancelled and cancelled(): raise InterruptedError('已停止湖泊方案修订')
        levels, _ = simulate(q[1:n1], rain[1:n1], evap[1:n1], k, h0, curve)
        return score(t1, np.r_[h0, levels], observed)['rmseM'] or 1e9

    found = minimize_scalar(objective1, bounds=(.2, 2.), method='bounded', options={'xatol': 1e-4})
    coefficient = float(found.x)
    if progress: progress(30, f'漫溢前湖面系数 {coefficient:.4f}')

    onset_index = int(times.get_indexer([ONSET])[0])
    if onset_index < 0: raise ValueError('来源成果未覆盖2023年9月21日起溢时刻')
    # Stage 2 starts from the observed level at onset (daily mean of 2023-09-21).
    crest0 = float(observed.loc[ONSET.normalize()]) if ONSET.normalize() in observed.index else None
    if crest0 is None: raise ValueError('缺少2023年9月21日主站观测湖位')
    t2 = times[onset_index:]
    tail = slice(onset_index + 1, None)
    evaluations = {'count': 0}

    def run2(params):
        outlet = {'crestM': params[0], 'widthM': params[1], 'erosionDays': params[2]}
        levels, outflow = simulate(q[tail], rain[tail], evap[tail], coefficient, crest0, curve, 0, crest0, outlet)
        return np.r_[crest0, levels], np.r_[0., outflow]

    def objective2(params):
        if cancelled and cancelled(): raise InterruptedError('已停止湖泊方案修订')
        evaluations['count'] += 1
        if progress and evaluations['count'] % 40 == 0:
            progress(min(90, 30 + evaluations['count'] / 12), f'溃口出流反演 {evaluations["count"]} 次')
        levels, _ = run2(params)
        return score(t2, levels, observed, start=ONSET)['rmseM'] or 1e9

    bounds = [(crest0 - 2.0, crest0), (INITIAL_WIDTH_M, 600.), (2., 400.)]
    found2 = differential_evolution(objective2, bounds, seed=20230921, popsize=8, maxiter=30, tol=1e-6,
                                    polish=False, updating='immediate')
    crest, width, days = map(float, found2.x)
    return coefficient, crest0, {'crestM': crest, 'widthM': width, 'erosionDays': days,
                                 'coefficient': WEIR_COEFFICIENT, 'initialWidthM': INITIAL_WIDTH_M}, evaluations['count']


def run(db, request, folder=None, progress=None, cancelled=None):
    source_id = request.get('sourceResultId') or DEFAULT_SOURCE
    curve = lake_curves.get_curve(db, request.get('curveId'))
    if progress: progress(3, '读取正式成果来水与湖面气象')
    data = load_source(db, source_id)
    coefficient, crest0, outlet, count = fit(data, curve, progress, cancelled)
    times, q, rain, evap, observed = (data[k] for k in ('times', 'q', 'rain', 'evap', 'observed'))
    h0 = float(data['formal'][0])
    onset = int(times.get_indexer([ONSET])[0])
    pre, _ = simulate(q[1:onset + 1], rain[1:onset + 1], evap[1:onset + 1], coefficient, h0, curve)
    post, outflow = simulate(q[onset + 1:], rain[onset + 1:], evap[onset + 1:], coefficient, crest0, curve, 0, crest0, outlet)
    levels = np.r_[h0, pre[:-1], crest0, post]
    flows = np.r_[np.zeros(onset + 1), outflow]
    no_outflow, _ = simulate(q[onset + 1:], rain[onset + 1:], evap[onset + 1:], coefficient, crest0, curve)
    windows = {'stage1': (None, STAGE1_END), 'stage2': (ONSET, None), 'all': (None, None)}
    metrics = {}
    for name, (start, end) in windows.items():
        metrics[name] = {'revised': score(times, levels, observed, start, end),
                         'formal': score(times, data['formal'], observed, start, end)}
    daily_out = pd.Series(flows, index=times).resample('D', closed='right', label='left').mean()
    volume = pd.Series(flows * 3600., index=times)
    event = volume[(volume.index > ONSET) & (volume.index <= pd.Timestamp('2024-01-01'))].sum()
    total = volume[volume.index > ONSET].sum()
    end_level = float(levels[-1])
    value = {'name': '色林错湖泊方案（V(H)+溃口出流）', 'status': 'active', 'sourceResultId': source_id,
             'curveId': curve['id'], 'lakeCurve': curve, 'lakeCoefficient': coefficient,
             'formalLakeCoefficient': data['formalCoefficient'], 'hydrologyRecalculated': False,
             'onset': ONSET.isoformat(), 'onsetLevelM': crest0,
             'outlet': {**outlet, 'name': '2023年色林错—班戈错溃口', 'currentCrestM': outlet['crestM'],
                        'currentWidthM': outlet['widthM']},
             'fit': {'stage1': {'start': times[0].isoformat(), 'end': STAGE1_END.isoformat(), 'parameter': 'lakeCoefficient', 'bounds': [.2, 2.]},
                     'stage2': {'start': ONSET.isoformat(), 'end': times[-1].isoformat(), 'parameters': ['crestM', 'widthM', 'erosionDays'],
                                'method': 'differential-evolution', 'evaluations': count}},
             'metrics': metrics,
             'outflow': {'eventVolumeKm3': float(event / 1e9), 'eventPeriod': [ONSET.isoformat(), '2023-12-31'],
                         'totalVolumeKm3': float(total / 1e9), 'peakM3s': float(flows.max()),
                         'peakTime': times[int(flows.argmax())].isoformat(),
                         'literatureEventVolumeKm3': .3, 'currentM3s': float(flows[-1])},
             'endState': {'time': times[-1].isoformat(), 'levelM': end_level,
                          'storageM3': lake_curves.storage(end_level, curve)},
             'noOutflowEndLevelM': float(no_outflow[-1]),
             'daily': [{'date': d.date().isoformat(), 'level': None if not np.isfinite(l) else float(l),
                        'formal': None if not np.isfinite(f) else float(f),
                        'observed': None if d not in observed.index else float(observed.loc[d]),
                        'outflow': None if not np.isfinite(o) else float(o)}
                       for d, l, f, o in zip(daily_mean(times, levels).index, daily_mean(times, levels).values,
                                             daily_mean(times, data['formal']).reindex(daily_mean(times, levels).index).values,
                                             daily_out.reindex(daily_mean(times, levels).index).values)],
             'literature': LITERATURE, 'createdAt': now()}
    for record in store.list_records(db, COLLECTION):
        if record.get('status') == 'active':
            record['status'] = 'superseded'
            store.save(db, COLLECTION, record, identifier=record['id'])
    saved = store.save(db, COLLECTION, value, prefix='LAKE-')
    if folder:
        write_json(Path(folder) / '湖泊方案修订.json', saved)
        np.savez_compressed(Path(folder) / '逐时湖位与外泄.npz', time=times.asi8, level=levels, outflow=flows)
    if progress: progress(100, '湖泊方案修订完成，正式水文成果保持不变')
    return saved


def active(db):
    """Most recent adopted lake model, or None."""
    if not hasattr(db, 'connect'):
        return None
    records = [r for r in store.list_records(db, COLLECTION) if r.get('status') == 'active']
    return max(records, key=lambda r: r.get('createdAt', '')) if records else None


def outlet_function(model, curve):
    """Constant post-erosion breach outflow (m³/s) for forecasts and scenarios."""
    outlet = model['outlet']
    def flow(level):
        return weir_flow(float(level) - outlet['currentCrestM'], outlet['currentWidthM'], outlet.get('coefficient', WEIR_COEFFICIENT))
    return flow
