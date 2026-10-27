"""2024 gauge-constrained lake rain; freeze before inspecting 2025 results.

Upstream snow/ice/runoff and lake area/initial levels remain unchanged. Saved
hourly inflow is replayed exactly; only lake precipitation and its bounded
evaporation coefficient are considered. No gauge is spread over the catchment.
"""
import argparse
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import digest, now, read_json, write_json
from backend.forcing import load_input
from backend.observations import daily_series
from backend.simulation import prepare, observation_snapshot, snapshot_series, lake_balance, metrics
from scripts.assess_model_candidate import ReadOnlyData


def fit_coefficient(observed, low, high, year=2024, bounds=(.2, 1.5)):
    paired = pd.concat([observed.rename('obs'), low.rename('low'), high.rename('high')], axis=1).dropna()
    paired = paired.loc[paired.index.year == year]
    if len(paired) < 60:
        raise ValueError('Insufficient development-period lake observations')
    slope = (paired.high-paired.low)/(bounds[1]-bounds[0])
    denominator = float(slope @ slope)
    if denominator <= 1e-15:
        raise ValueError('Lake evaporation response is unidentifiable')
    optimum = bounds[0] + float(slope @ (paired.obs-paired.low))/denominator
    return float(np.clip(optimum, *bounds)), optimum, len(paired)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, default=ROOT/'workspace')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    db = ReadOnlyData(args.workspace)
    result = read_json(args.result/'result.json')
    source = prepare(db, result['inputId'])
    manifest, raw = load_input(db, result['inputId'], source['spatial'])
    if manifest.get('lakePrecipitationCorrection'):
        raise ValueError('Use uncorrected baseline input')
    station = db.rows("SELECT * FROM stations WHERE id='SLC'")[0]
    grids = source['spatial']['grids']
    index = int(np.argmin([(g['lon']-station['lon'])**2*np.cos(np.radians(station['lat']))**2+
                          (g['lat']-station['lat'])**2 for g in grids]))
    grid = pd.Series(raw['precipitation'][:, index], index=raw['time']).resample('D', closed='right', label='left').agg(['sum','count'])
    gauge = daily_series(db, 'SLC', 'precipitation')
    pair = pd.concat([gauge.rename('gauge'), grid['sum'].where(grid['count']==24).rename('grid')], axis=1).dropna()
    development = pair.loc[pair.index.year==2024]
    if len(development) != 366 or development.grid.sum() <= 0:
        raise ValueError('Requires complete labelled 2024 SLC daily rainfall')
    factor = float(development.gauge.sum()/development.grid.sum())
    if not 0 < factor <= 2:
        raise ValueError('Gauge ratio outside declared diagnostic range')
    correction = {'method':'station-point-annual-ratio-v1', 'scope':'lake-only', 'factor':factor,
                  'station':'SLC', 'gridId':grids[index]['id'], 'developmentYear':2024,
                  'pairedDays':len(development), 'gaugeMm':float(development.gauge.sum()),
                  'gridMm':float(development.grid.sum()),
                  'gaugeSeriesSHA256':digest({t.isoformat():float(v) for t,v in development.gauge.items()}),
                  'limitations':['One shore gauge represents the lake only as a provisional spatial transfer.',
                                 'Source daily labels used; station reporting day boundary not independently verified.',
                                 'Gauge catch bias and snowfall undercatch not corrected.']}
    with np.load(args.result/'hourly.npz') as saved:
        times = pd.DatetimeIndex(saved['time'].astype('datetime64[ns]'))
        inflow = saved['flow'][:,6].copy()
    ids = source['times'].get_indexer(times)
    if (ids<0).any():
        raise ValueError('Saved flow outside baseline forcing')
    observations = observation_snapshot(db)
    if result['identity']['observationSHA256'] != digest(observations):
        raise ValueError('Observation snapshot differs from saved result')
    windows = [{**w,'start':pd.Timestamp(w['start']),'end':pd.Timestamp(w['end'])} for w in observations['windows']]
    observed = snapshot_series(observations, 'level')
    area = source['spatial']['lakeAreaKm2']*1e6

    def simulate(factor, coefficient):
        h, *_ = lake_balance(times, inflow, source['lakeRain'][ids]*factor,
                            source['lakeEvaporation'][ids], area, coefficient, windows)
        aggregation = pd.Series(h,index=times).resample('D', closed='right',label='left')
        daily = aggregation.mean().where(aggregation.count()>=12)
        for window in windows:
            daily.loc[daily.index==window['start'].normalize()] = np.nan
        return daily

    candidate_lo, candidate_hi = simulate(factor,.2), simulate(factor,1.5)
    coefficient, unconstrained, count = fit_coefficient(observed, candidate_lo, candidate_hi)
    candidate = simulate(factor,coefficient)
    baseline = simulate(1.,result['parameters']['lakeCoefficient'])
    dev_observed = observed.loc[observed.index.year==2024]
    baseline_dev, candidate_dev = metrics(dev_observed,baseline), metrics(dev_observed,candidate)
    accepted = candidate_dev['rmse'] < baseline_dev['rmse']*.85 and abs(candidate_dev['bias']) < abs(baseline_dev['bias'])
    freeze = {'createdAt':now(), 'selectionYear':2024, 'futureObservationsUsedForSelection':False,
              'baselineIdentity':result['identity'], 'correction':correction, 'lakeCoefficient':coefficient,
              'unconstrainedCoefficient':unconstrained, 'coefficientBounds':[.2,1.5], 'pairedLakeDays':count,
              'baseline2024':baseline_dev,'candidate2024':candidate_dev,'developmentAccepted':accepted,
              'upstreamParametersChanged':False,'areaOrInitialLevelFitted':False}
    freeze['freezeSHA256'] = digest(freeze)
    write_json(args.output/'frozen-development.json',freeze)
    # The held-out year is evaluated only AFTER the explicit development freeze.
    report = {**freeze,'evaluation2025':{'baseline':metrics(observed.loc[observed.index.year==2025],baseline),
                                       'candidate':metrics(observed.loc[observed.index.year==2025],candidate)},
              'monthly':[],'gaugeMonthly2024':[]}
    for month, block in development.groupby(development.index.to_period('M')):
        report['gaugeMonthly2024'].append({'month':str(month),'days':len(block),
            'gaugeMm':float(block.gauge.sum()),'pointGridMm':float(block.grid.sum())})
    for month in sorted(set(observed.index.to_period('M'))):
        o = observed.loc[observed.index.to_period('M')==month]
        report['monthly'].append({'month':str(month),'baseline':metrics(o,baseline),'candidate':metrics(o,candidate)})
    daily = pd.concat([observed.rename('observedLevel'),baseline.rename('baselineLevel'),candidate.rename('candidateLevel')],axis=1)
    daily.to_csv(args.output/'lake-comparison.csv',index_label='date',encoding='utf-8-sig')
    write_json(args.output/'assessment.json',report)
    print({k:report[k] for k in ('lakeCoefficient','developmentAccepted','baseline2024','candidate2024','evaluation2025')},flush=True)


if __name__=='__main__':
    main()
