# coding: utf-8
"""Frozen-land routing experiment; selection sees 2024 only."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import read_json, write_json, model_identity, now
from backend.database import Database
from backend.simulation import run_simulation, observation_snapshot
from backend.calibration import calibrate
from backend.calibration_config import flow_objective


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--budget', type=int, default=120)
    args = parser.parse_args()
    db = Database()
    prior = read_json(ROOT/'docs/model-improvement-v0.5.0.json')
    root = ROOT/'build/routing-experiment-v0.6.0'
    root.mkdir(parents=True, exist_ok=True)
    request = {'inputId': 'INPUT-967613234ca58f1c6cfb44e2', 'start': '2023-01-01T00:00:00',
               'end': '2026-01-01T00:00:00', 'routingMode': 'source-separated', 'seed': 42,
               'objectiveProfile': 'balanced', '_observations': observation_snapshot(db), '_modelIdentity': model_identity()}
    report = {'createdAt': now(), 'selectionYear': 2024, 'excludedYears': [2025, 2026],
              'purpose': 'Frozen-land routing diagnosis; no automatic reference replacement', 'cases': {}}
    for key, old in [('reference', 'sourceControl'), ('flow', 'sourceLake')]:
        base_request = {**request, 'parameters': prior[old]['parameters']}
        print('BASELINE '+key, flush=True)
        baseline = run_simulation(db, base_request, root/(key+'-baseline'))
        for y in ('2024', '2025'):
            for variable in ('flow', 'lake'):
                assert abs(baseline['metrics'][y][variable]['rmse']-prior[old]['metrics'][y][variable]['rmse']) < 1e-8
        folder = root/(key+'-routing'); folder.mkdir(exist_ok=True)
        def progress(value, message):
            print(f'{key} {value:.1f} '+message, flush=True)
        tuned = calibrate(db, {**base_request, 'stage': 'routing', 'maxEvaluations': args.budget}, folder, progress)
        fixed = [k for k in baseline['parameters'] if k not in tuned['calibration']['searchParameters']]
        assert all(tuned['parameters'][k] == baseline['parameters'][k] for k in fixed)
        report['cases'][key] = {'baseline': {k: baseline[k] for k in ('parameters', 'metrics', 'modelDiagnostics', 'identity')},
                               'routing': {k: tuned[k] for k in ('parameters', 'metrics', 'modelDiagnostics', 'identity', 'calibration')},
                               'fixedParametersUnchanged': True,
                               'objectiveBefore': flow_objective(baseline['metrics']['2024']['flow'])[0],
                               'objectiveAfter': tuned['calibration']['bestObjective']}
        write_json(ROOT/'docs/routing-partition-v0.6.0.json', report)
        print(str({key: {name: r['metrics'] for name, r in [('baseline',baseline),('routing',tuned)]}}), flush=True)
    report.update(completedAt=now(), status='complete')
    write_json(ROOT/'docs/routing-partition-v0.6.0.json', report)


if __name__ == '__main__':
    main()
