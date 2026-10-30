"""Bounded lake evaporation fit conditional on frozen hourly basin inflow."""
from copy import deepcopy
import numpy as np
import pandas as pd

from .common import digest
from .simulation import metrics


def response_slope(source, start, end, anchor):
    """Daily response to one unit of lake evaporation coefficient."""
    times=source['times']
    ids=np.flatnonzero((times>=pd.Timestamp(start)) & (times<=pd.Timestamp(end)))
    times=times[ids]
    if not anchor:
        raise ValueError('湖泊率定需要起算水位')
    evaporation=source['arrays']['evaporation'][ids]@np.asarray(source['spatial']['lakeWeights'])/1000
    evaporation[times<=pd.Timestamp(anchor['time'])]=0.
    derivative=-np.cumsum(evaporation)
    derivative[times<pd.Timestamp(anchor['time'])]=np.nan
    return pd.Series(derivative,index=times).resample('D',closed='right',label='left').mean()


def response(source, request, baseline):
    anchor=baseline.get('initialization')
    slopes=response_slope(source, baseline['start'], baseline['end'], anchor)
    frame=pd.DataFrame(baseline['daily'])
    frame.index=pd.to_datetime(frame.date)
    frame['slope']=slopes.reindex(frame.index)
    frame['baseLevel']=frame['lakeLevel']
    return frame


def assess(frame,coefficient,baseline_coefficient,start,end,anchor):
    selected=frame.loc[(frame.index>=pd.Timestamp(start)) & (frame.index<pd.Timestamp(end))].copy()
    selected=selected.loc[selected.index!=pd.Timestamp(anchor['time']).normalize()]
    simulated=selected.baseLevel+(coefficient-baseline_coefficient)*selected.slope
    report=metrics(selected.observedLevel,simulated)
    pair=pd.DataFrame({'observed':selected.observedLevel,'simulated':simulated}).dropna()
    if len(pair)>1:
        x=(pair.index-pair.index[0]).days.to_numpy()/365.2425
        report['observedTrendMPerYear']=float(np.polyfit(x,pair.observed,1)[0])
        report['simulatedTrendMPerYear']=float(np.polyfit(x,pair.simulated,1)[0])
        report['firstToLastErrorChangeM']=float((pair.simulated-pair.observed).iloc[-1]-(pair.simulated-pair.observed).iloc[0])
    report['countByYear']={str(y):int(n) for y,n in pair.groupby(pair.index.year).size().items()}
    return report


def fit(frame,baseline_coefficient,bounds,start,end,anchor):
    pairs=frame.loc[(frame.index>=pd.Timestamp(start)) & (frame.index<pd.Timestamp(end))]
    pairs=pairs.loc[pairs.index!=pd.Timestamp(anchor['time']).normalize()]
    pairs=pairs[['observedLevel','baseLevel','slope']].dropna()
    if len(pairs)<30 or float(np.sum(pairs.slope**2))<1e-12:
        raise ValueError('所选湖位率定期不足30个有效日或蒸发响应不足')
    unconstrained=baseline_coefficient+float(np.sum(pairs.slope*(pairs.observedLevel-pairs.baseLevel))/np.sum(pairs.slope**2))
    coefficient=float(np.clip(unconstrained,*bounds))
    observations=[{'date':t.isoformat(),'observed':float(v)} for t,v in pairs.observedLevel.items()]
    return {'algorithm':'bounded-linear-least-squares-on-frozen-inflow-v1',
        'parameter':'lakeCoefficient','initial':baseline_coefficient,'bounds':list(bounds),
        'value':coefficient,'unconstrainedValue':unconstrained,'converged':True,
        'termination':'bounded-optimum' if coefficient!=unconstrained else 'analytic-optimum',
        'boundary':coefficient in bounds,'trainingPeriod':{'start':start,'end':end},
        'training':assess(frame,coefficient,baseline_coefficient,start,end,anchor),
        'fixedTrainingDates':[r['date'] for r in observations],
        'fixedTrainingObservationSHA256':digest(observations),
        'trace':[{'value':v,'metrics':assess(frame,v,baseline_coefficient,start,end,anchor)}
                 for v in sorted(set([float(bounds[0]),float(baseline_coefficient),coefficient,float(bounds[1])]))]}


# The observation dates and the qualifying seasonal windows are established
# before any candidate is evaluated.  In particular, a candidate cannot get a
# better score by making an inconvenient day missing.
JOINT_SCALES = {'rmseM': .1, 'trendErrorM': .1,
                'amplitudeErrorM': .1, 'peakTimingErrorDays': 14.}
SEASON_MONTHS = (5, 6, 7, 8, 9, 10)


def freeze_window(observations, start, end, anchor):
    """Freeze real paired dates and adequately sampled May--October years."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    anchor_day = pd.Timestamp(anchor['time']).normalize()
    pairs = sorted((pd.Timestamp(t), float(v)) for t, v in observations.items()
                   if start <= pd.Timestamp(t) < end and pd.Timestamp(t) != anchor_day
                   and np.isfinite(float(v)))
    if len(pairs) < 30:
        raise ValueError('湖位评分至少需要30个有效实测日')
    dates = pd.DatetimeIndex([row[0] for row in pairs])
    if dates.has_duplicates:
        raise ValueError('湖位观测日期重复')
    years = []
    for year in sorted(set(dates.year)):
        sub = dates[(dates.year == year) & dates.month.isin(SEASON_MONTHS)]
        if len(sub) >= 90 and all(int((sub.month == month).sum()) >= 10 for month in SEASON_MONTHS):
            years.append(int(year))
    if not years:
        raise ValueError('湖位评分需要至少一年5—10月各月不少于10天且总计不少于90天的观测')
    items = [{'date': t.isoformat(), 'observed': v} for t, v in pairs]
    return {'start': start.isoformat(), 'end': end.isoformat(),
            'dates': [r['date'] for r in items], 'observed': [r['observed'] for r in items],
            'observationSHA256': digest(items), 'seasonYears': years,
            'seasonMonths': list(SEASON_MONTHS), 'minimumSeasonDays': 90,
            'minimumDaysPerMonth': 10, 'anchorDayExcluded': anchor_day.isoformat()}


def _joint_scores(frame, coefficients, baseline_coefficient, window):
    dates = pd.DatetimeIndex(pd.to_datetime(window['dates']))
    selected = frame.reindex(dates)
    base = selected['baseLevel'].to_numpy(dtype=float)
    slope = selected['slope'].to_numpy(dtype=float)
    observed = np.asarray(window['observed'], dtype=float)
    if not np.isfinite(base).all() or not np.isfinite(slope).all():
        raise ValueError('候选湖位未覆盖固定实测日期')
    coeff = np.atleast_1d(np.asarray(coefficients, dtype=float))
    simulated = base[None, :] + (coeff[:, None] - baseline_coefficient) * slope[None, :]
    error = simulated - observed[None, :]
    rmse = np.sqrt(np.mean(error * error, axis=1))
    years = (dates - dates[0]).days.to_numpy(dtype=float) / 365.2425
    centered = years - years.mean()
    denominator = float(np.dot(centered, centered))
    if denominator <= 0:
        raise ValueError('湖位日期不足以评价趋势')
    observed_trend = float(np.dot(centered, observed - observed.mean()) / denominator)
    simulated_trend = (simulated - simulated.mean(axis=1)[:, None]) @ centered / denominator
    trend_error = np.abs(simulated_trend - observed_trend) * (years[-1] - years[0])
    amplitudes, timings = [], []
    for year in window['seasonYears']:
        ids = np.flatnonzero((dates.year == year) & dates.month.isin(SEASON_MONTHS))
        obs, sim = observed[ids], simulated[:, ids]
        amplitudes.append(np.abs(np.ptp(sim, axis=1) - float(np.ptp(obs))))
        obs_peak = int(np.argmax(obs))
        sim_peaks = np.argmax(sim, axis=1)
        day_numbers = dates[ids].asi8 // (24 * 3600 * 10**9)
        timings.append(np.abs(day_numbers[sim_peaks] - day_numbers[obs_peak]))
    amplitude = np.mean(np.vstack(amplitudes), axis=0)
    peak = np.mean(np.vstack(timings), axis=0)
    objective = window.get('objective') or {}
    scales = objective.get('lakeScales', JOINT_SCALES)
    weights = objective.get('lakeWeights', {'rmse': .25, 'trend': .25, 'amplitude': .25, 'peakTiming': .25})
    if not objective:
        # Preserve the operation order of archived four-criterion fits.
        score = (rmse / JOINT_SCALES['rmseM'] + trend_error / JOINT_SCALES['trendErrorM'] +
                 amplitude / JOINT_SCALES['amplitudeErrorM'] + peak / JOINT_SCALES['peakTimingErrorDays']) / 4.
    else:
        score = (weights['rmse'] * rmse / scales['rmseM'] +
                 weights['trend'] * trend_error / scales['trendErrorM'] +
                 weights['amplitude'] * amplitude / scales['amplitudeErrorM'] +
                 weights['peakTiming'] * peak / scales['peakTimingErrorDays'])
    return {'score': score, 'rmse': rmse, 'trendErrorM': trend_error,
            'amplitudeErrorM': amplitude, 'peakTimingErrorDays': peak,
            'observedTrendMPerYear': observed_trend, 'simulatedTrendMPerYear': simulated_trend,
            'bias': np.mean(error, axis=1)}


def joint_assess(frame, coefficient, baseline_coefficient, window):
    """Four equally weighted criteria, all on the same frozen observations."""
    raw = _joint_scores(frame, [coefficient], baseline_coefficient, window)
    result = {k: float(v[0]) if isinstance(v, np.ndarray) else float(v)
              for k, v in raw.items()}
    scales = (window.get('objective') or {}).get('lakeScales', JOINT_SCALES)
    result.update(count=len(window['dates']), seasonYears=list(window['seasonYears']),
                  fixedObservationSHA256=window['observationSHA256'], scales=dict(scales))
    result['scoreComponents'] = {'rmse': result['rmse']/scales['rmseM'],
        'trend': result['trendErrorM']/scales['trendErrorM'],
        'amplitude': result['amplitudeErrorM']/scales['amplitudeErrorM'],
        'peakTiming': result['peakTimingErrorDays']/scales['peakTimingErrorDays']}
    if window.get('objective'):
        result['weights'] = deepcopy(window['objective']['lakeWeights'])
    return result


def joint_fit(frame, baseline_coefficient, bounds, window, anchor):
    """Deterministic bounded 1-D fit, with ordinary RMSE fit as a comparison."""
    lo, hi = map(float, bounds)
    if not lo < hi:
        raise ValueError('湖面蒸发系数范围无效')
    grid = np.unique(np.r_[np.linspace(lo, hi, 131), float(np.clip(baseline_coefficient, lo, hi))])
    scores = _joint_scores(frame, grid, baseline_coefficient, window)['score']
    first = int(np.argmin(scores))
    step = (hi - lo)/130.
    refined = np.linspace(max(lo, grid[first]-step), min(hi, grid[first]+step), 21)
    all_values = np.unique(np.r_[grid, refined])
    all_scores = _joint_scores(frame, all_values, baseline_coefficient, window)['score']
    best = int(np.argmin(all_scores))
    coefficient = float(all_values[best])
    ordinary = fit(frame, baseline_coefficient, bounds, window['start'], window['end'], anchor)
    score = joint_assess(frame, coefficient, baseline_coefficient, window)
    conventional = assess(frame, coefficient, baseline_coefficient,
                          window['start'], window['end'], anchor)
    # Keep the conventional fields consumed by existing result pages.
    return {'algorithm': 'bounded-four-criterion-grid-v1', 'parameter': 'lakeCoefficient',
            'initial': float(baseline_coefficient), 'bounds': [lo, hi], 'value': coefficient,
            'boundary': coefficient in (lo, hi), 'trainingPeriod': {'start': window['start'], 'end': window['end']},
            'training': {**conventional, **score}, 'jointScore': score['score'],
            'rmseOnlyFit': ordinary, 'fixedTrainingDates': list(window['dates']),
            'fixedTrainingObservationSHA256': window['observationSHA256'],
            'converged': True, 'termination': 'bounded-grid-completed',
            'continuousOptimumGuaranteed': False, 'coarseGridSize': 131, 'localGridSize': 21,
            'trace': [{'value': float(v), 'score': float(s)} for v, s in zip(all_values, all_scores)]}
