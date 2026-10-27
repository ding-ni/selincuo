"""Replay the Duja-only candidate with real forcing and verify its saved state.

Read-only access to source SQLite. No installed tasks, defaults or seed changed.
The output is a reproducible research candidate, not an approved business model.
"""
import argparse
import sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import read_json, write_json, digest, model_identity, now
from backend.simulation import prepare, observation_snapshot, run_simulation
from scripts.assess_model_candidate import ReadOnlyData


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--assessment', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, default=ROOT/'workspace')
    parser.add_argument('--output-name', default='duja-region-full')
    args = parser.parse_args()
    folder = args.assessment/args.output_name
    folder.mkdir(exist_ok=False)
    study = read_json(args.assessment/'assessment.json')
    db = ReadOnlyData(args.workspace)
    source = prepare(db, study['input']['id'])
    obs = observation_snapshot(db)
    if digest(obs) != study['observationSHA256']:
        raise ValueError('Observation snapshot changed')
    if source['manifest']['dataSHA256'] != study['input']['dataSHA256']:
        raise ValueError('Forcing changed')
    identity = model_identity()
    request = {'inputId': study['input']['id'], 'start': '2023-01-01T00:00:00',
               'end': '2026-01-01T00:00:00', 'routingMode': 'source-separated',
               'parameters': study['reference']['parameters'],
               'partitionParameters': {'duja': {k: v for k, v in study['selected']['parameters'].items()
                                               if k != 'lakeCoefficient'}},
               '_observations': obs, '_modelIdentity': identity}
    write_json(folder/'request.json', request)
    result = run_simulation(db, request, folder, prepared=source)
    with np.load(folder/'hourly.npz') as actual, np.load(args.assessment/'reference-full/hourly.npz') as reference, np.load(args.assessment/'selected-full/hourly.npz') as selected:
        np.testing.assert_array_equal(actual['time'], reference['time'])
        np.testing.assert_array_equal(actual['time'], selected['time'])
        np.testing.assert_allclose(actual['flow'][:, 0], selected['flow'][:, 0], atol=1e-10, rtol=1e-12)
        np.testing.assert_allclose(actual['flow'][:, 2:6], reference['flow'][:, 2:6], atol=1e-10, rtol=1e-12)
        composed = reference['flow'][:, 6]-reference['flow'][:, 0]+selected['flow'][:, 0]
        max_error = float(np.max(np.abs(actual['flow'][:, 6]-composed)))
        np.testing.assert_allclose(actual['flow'][:, 6], composed, atol=1e-10, rtol=1e-12)
    print('Three-year regional replay matches independent partition composition.', flush=True)
    head = run_simulation(db, {**request, 'end': '2024-12-31T17:00:00', '_skipDiagnostics': True},
                          folder/'resume-head', prepared=source)
    tail = run_simulation(db, {**request, 'start': '2024-12-31T18:00:00', '_parentState': head['checkpoint'],
                              '_skipDiagnostics': True}, folder/'resume-tail', prepared=source)
    if result['checkpoint'] != tail['checkpoint']:
        raise AssertionError('Final checkpoint differs after resume')
    resume_errors = {}
    with np.load(folder/'hourly.npz') as full, np.load(folder/'resume-head/hourly.npz') as first, np.load(folder/'resume-tail/hourly.npz') as last:
        for key in ('flow', 'unitStates', 'unitDiagnostics', 'routingStorage', 'sourceRoutingStorage',
                    'continuousLakeVolumeChange', 'lakeLevel', 'volumeChange'):
            combined = np.concatenate([first[key], last[key]])
            np.testing.assert_allclose(full[key], combined, atol=1e-8, rtol=1e-12, equal_nan=True)
            finite = np.isfinite(full[key])
            resume_errors[key] = float(np.max(np.abs(full[key][finite]-combined[finite]))) if finite.any() else 0.
    audit = result['modelDiagnostics']
    if audit['catchmentBalanceMaxErrorM3'] >= .001 or audit['cryosphere']['snowTransferMaxErrorMm'] >= 1e-9:
        raise AssertionError('Water accounting failed')
    candidate = {'createdAt': now(), 'decision': 'research-candidate', 'businessReady': False,
                 'request': {k: v for k, v in request.items() if not k.startswith('_')},
                 'identity': result['identity'], 'observationSHA256': digest(obs),
                 'metrics': result['metrics'], 'compositionMaxFlowErrorM3s': max_error,
                 'resumeMaxErrors': resume_errors, 'catchmentBalanceMaxErrorM3': audit['catchmentBalanceMaxErrorM3'],
                 'cryosphere': audit['cryosphere'],
                 'scope': {'duja': '2024 flow-constrained regional candidate',
                           'otherPartitions': 'Unvalidated reference parameters frozen',
                           'lake': 'Reference evaporation coefficient frozen'},
                 'unresolved': ['Initial soil-water sensitivity', 'Multiple boundary parameters and nonconverged searches',
                                'No independent snow/glacier observations', 'Lake evaporation and ungauged inflow uncertainty'],
                 'activated': False}
    write_json(folder/'verified-candidate.json', candidate)
    print({y: {'flowNse': m['flow'].get('nse'), 'flowPbias': m['flow'].get('pbias'),
               'lakeRmse': m['lake'].get('rmse')} for y, m in result['metrics'].items()}, flush=True)
    print({'resumeMaxErrors': resume_errors, 'compositionMaxFlowErrorM3s': max_error}, flush=True)


if __name__ == '__main__':
    main()
