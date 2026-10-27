"""Exact linear-response diagnosis of a fixed-area lake, using 2024 only.

The unconstrained optimum is a diagnosis, never an instruction to widen bounds.
This consumes saved inflow; it cannot change upstream snow/ice/runoff parameters.
"""
import argparse
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import read_json, write_json
from backend.simulation import lake_balance, metrics, prepare, observation_snapshot
from scripts.assess_model_candidate import ReadOnlyData


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--assessment', type=Path, required=True)
    p.add_argument('--workspace', type=Path, default=ROOT/'workspace')
    args = p.parse_args()
    study = read_json(args.assessment/'assessment.json')
    selected = study['selected']
    db = ReadOnlyData(args.workspace)
    source = prepare(db, study['input']['id'])
    observations = observation_snapshot(db)
    if selected['identity']['observationSHA256'] != study['observationSHA256']:
        raise ValueError('Selected result observation identity differs from assessment')
    with np.load(args.assessment/'selected-full/hourly.npz') as z:
        times = pd.DatetimeIndex(z['time'].astype('datetime64[ns]'))
        ids = source['times'].get_indexer(times)
        if (ids < 0).any():
            raise ValueError('Saved result timestamps outside forcing')
        flow = z['flow'][:, 6].copy()
    windows = [{**w, 'start': pd.Timestamp(w['start']), 'end': pd.Timestamp(w['end'])}
               for w in observations['windows']]
    observed = pd.Series(list(observations['level'].values()),
                         index=pd.to_datetime(list(observations['level'])), dtype=float)
    observed = observed.loc[observed.index.year == 2024]
    area = source['spatial']['lakeAreaKm2']*1e6

    def series(coefficient):
        h, *_ = lake_balance(times, flow, source['lakeRain'][ids], source['lakeEvaporation'][ids],
                             area, coefficient, windows)
        values = pd.Series(h, index=times).resample('D', closed='right', label='left')
        daily = values.mean().where(values.count() >= 12)
        for w in windows:
            daily.loc[daily.index == w['start'].normalize()] = np.nan
        return daily.reindex(observed.index)

    lo, hi = .2, 1.5
    low, high = series(lo), series(hi)
    paired = pd.concat([observed.rename('obs'), low.rename('lo'), high.rename('hi')], axis=1).dropna()
    slope = (paired.hi-paired.lo)/(hi-lo)
    unconstrained = float(lo + np.dot(slope, paired.obs-paired.lo)/np.dot(slope, slope))
    bounded = float(np.clip(unconstrained, lo, hi))
    rows = []
    for coefficient in sorted({lo, .5, .8, 1., 1.25, hi, bounded}):
        values = series(coefficient)
        metric = metrics(observed, values)
        affine = paired.lo + (coefficient-lo)*slope
        error = float(np.max(np.abs(values.reindex(paired.index)-affine)))
        rows.append({'coefficient': coefficient, 'metrics2024': metric, 'linearResponseMaxErrorM': error})
    result = {'selectionYear': 2024, 'pairedDays': len(paired),
              'boundedOptimum': bounded, 'unconstrainedLeastSquaresOptimum': unconstrained,
              'declaredBounds': [lo, hi], 'boundActive': unconstrained < lo or unconstrained > hi,
              'rows': rows, 'appliedToModel': False,
              'interpretation': 'Fixed upstream inflow; a bound-active fit indicates compensation pressure, not evidence that a larger evaporation coefficient is physical.'}
    write_json(args.assessment/'lake-coefficient-audit.json', result)
    print(result['boundedOptimum'], result['unconstrainedLeastSquaresOptimum'], result['pairedDays'])


if __name__ == '__main__':
    main()
