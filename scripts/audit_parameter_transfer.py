"""Controlled partition-composition experiment, no change to runtime defaults.

Current units and routing partitions do not exchange water upstream. Therefore
substituting only the saved Duja partition hydrograph is exactly the downstream
lake inflow of a Duja-only parameter change, with every other partition frozen.
This tests transfer policy; it is not a uniformly-parameterized runnable package.
"""
import argparse
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import read_json, write_json, digest
from backend.simulation import prepare, observation_snapshot, lake_balance, metrics
from scripts.assess_model_candidate import ReadOnlyData


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--assessment', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, default=ROOT/'workspace')
    args = parser.parse_args()
    study = read_json(args.assessment/'assessment.json')
    source = prepare(ReadOnlyData(args.workspace), study['input']['id'])
    snapshot = observation_snapshot(ReadOnlyData(args.workspace))
    if digest(snapshot) != study['observationSHA256']:
        raise ValueError('Observation snapshot changed')
    with np.load(args.assessment/'reference-full/hourly.npz') as z:
        times = pd.DatetimeIndex(z['time'].astype('datetime64[ns]'))
        reference = z['flow'].copy()
    with np.load(args.assessment/'selected-full/hourly.npz') as z:
        if not np.array_equal(z['time'], times.values.astype('int64')):
            raise ValueError('Timestamp mismatch')
        selected = z['flow'].copy()
    positions = source['times'].get_indexer(times)
    windows = [{**w, 'start': pd.Timestamp(w['start']), 'end': pd.Timestamp(w['end'])} for w in snapshot['windows']]
    observed = pd.Series(list(snapshot['level'].values()), index=pd.to_datetime(list(snapshot['level'])), dtype=float)
    base_coefficient = study['reference']['parameters']['lakeCoefficient']
    selected_coefficient = study['selected']['parameters']['lakeCoefficient']
    if base_coefficient != selected_coefficient:
        raise ValueError('Transfer ablation requires identical lake evaporation coefficient')
    # Columns: duja, zajia, zagen, boqu, ali, fringe, total.
    composed = reference.copy()
    composed[:, 0] = selected[:, 0]
    composed[:, 1] = reference[:, 1]-reference[:, 0]+selected[:, 0]
    composed[:, 6] = composed[:, 1:6].sum(axis=1)
    np.testing.assert_allclose(composed[:, 6], reference[:, 6]-reference[:, 0]+selected[:, 0], atol=1e-10)
    h, _, _, _, _ = lake_balance(times, composed[:, 6], source['lakeRain'][positions],
                                 source['lakeEvaporation'][positions], source['spatial']['lakeAreaKm2']*1e6,
                                 base_coefficient, windows)
    groups = pd.Series(h, index=times).resample('D', closed='right', label='left')
    lake = groups.mean().where(groups.count() >= 12)
    for w in windows:
        lake.loc[lake.index == w['start'].normalize()] = np.nan
    report = {
        'kind': 'controlled-parameter-transfer-ablation', 'runtimeChanged': False,
        'dujaParameters': study['selected']['parameters'],
        'otherPartitionParameters': study['reference']['parameters'],
        'lakeCoefficient': base_coefficient, 'metrics': {},
        'identity': {'inputId': study['input']['id'], 'observationSHA256': digest(snapshot)},
        'limitations': ['Frozen unobserved partitions retain an unvalidated reference parameter set.',
                        'This composed diagnostic is not exported as a deployable uniform-parameter model.'],
    }
    # Declare the policy before presenting holdout metrics.
    write_json(args.assessment/'transfer-policy.json', {
        'policy': 'Only Duja changes; all unobserved partitions and lake coefficient remain frozen',
        'selectionUses2025': False, 'deployed': False})
    for y in ('2024', '2025'):
        mask = observed.index.year == int(y)
        report['metrics'][y] = {
            'flow': study['selected']['metrics'][y]['flow'],
            'lake': metrics(observed.loc[mask], lake.reindex(observed.index[mask])),
            'referenceLakeRmse': study['reference']['metrics'][y]['lake']['rmse'],
            'globalTransferLakeRmse': study['selected']['metrics'][y]['lake']['rmse'],
        }
    write_json(args.assessment/'parameter-transfer-audit.json', report)
    print({y: {'dujaOnlyLakeRmse': r['lake']['rmse'], 'globalTransferLakeRmse': r['globalTransferLakeRmse']} for y, r in report['metrics'].items()})


if __name__ == '__main__':
    main()
