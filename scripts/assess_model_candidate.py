"""Read-only source data -> bounded repeated calibration -> one frozen candidate.

Does not submit service jobs, edit the installed database, select on 2025, or
activate/package a model. All research output stays under the requested folder.
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import digest, model_identity, now, read_json, write_json
from backend.database import Database
from backend.model_core import initial_states
from backend.simulation import prepare, observation_snapshot, run_simulation
from backend.calibration import calibrate
from backend.calibration_config import flow_objective


class ReadOnlyData:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.path = self.workspace/'database/hydrology.sqlite3'

    def rows(self, sql, args=()):
        with sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(r) for r in connection.execute(sql, args)]

    observations = Database.observations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=ROOT/'workspace')
    parser.add_argument('--task-workspace', type=Path, default=ROOT/'运行程序/workspace')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seeds', type=int, nargs='+', default=[42, 142, 242])
    parser.add_argument('--runoff-budget', type=int, default=240)
    parser.add_argument('--routing-budget', type=int, default=120)
    parser.add_argument('--lake-budget', type=int, default=40)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    db = ReadOnlyData(args.workspace)
    spatial = read_json(ROOT/'data/derived/spatial/manifest.json')
    products = [json.loads(r['manifest']) for r in db.rows('SELECT manifest FROM input_products')]
    available = [m for m in products if m['spatialVersion'] == spatial['version'] and
                 m['start'] <= '2023-01-01T00:00:00' and m['end'] >= '2026-01-01T00:00:00']
    if not available:
        raise ValueError('Requires complete registered 2023-2025 input; no synthetic fallback')
    product = sorted(available, key=lambda m: m['id'])[0]
    snapshot = observation_snapshot(db)
    identity = model_identity()
    source = prepare(db, product['id'])
    reference = read_json(ROOT/'docs/model-improvement-v0.5.0.json')['sourceControl']['parameters']
    report = {
        'createdAt': now(), 'status': 'running', 'sourceReadOnly': True, 'modelImplementation': identity,
        'plan': {
            'seeds': args.seeds, 'runoffEvaluations': args.runoff_budget,
            'routingEvaluations': args.routing_budget, 'lakeEvaluations': args.lake_budget,
            'searchStart': '2023-01-01T00:00:00', 'searchEnd': '2025-01-01T00:00:00',
            'warmupYear': 2023, 'selectionYear': 2024,
            'selectionCriterion': '2024 balanced Duja flow objective only; lake coefficient fitted within each path using 2024 lake RMSE',
            'withheldYearUsedForSelection': False, 'sameStartingParametersForEverySeed': True,
            'iceSnowPhaseElevationParametersSearched': False,
            'finalAssessmentEnd': '2026-01-01T00:00:00',
            'initialWaterFractions': [.25, .5, .75],
            'note': '2025 has been inspected in previous project diagnostics; it is excluded from this search but is not an untouched final external test.',
        },
        'input': {k: product[k] for k in ('id', 'dataSHA256', 'spatialVersion', 'start', 'end', 'hours')},
        'observationSHA256': digest(snapshot), 'existingCalibrations': [], 'paths': [],
    }
    if (args.task_workspace/'database/hydrology.sqlite3').is_file():
        task_db = ReadOnlyData(args.task_workspace)
        for row in task_db.rows("SELECT id,name,result FROM tasks WHERE kind='calibration' AND status='succeeded' ORDER BY created_at"):
            r = json.loads(row['result'])
            c = r.get('calibration', {})
            report['existingCalibrations'].append({
                'id': row['id'], 'name': row['name'], **{k: c.get(k) for k in
                ('stage', 'seed', 'searchParameters', 'evaluations', 'converged', 'termination')},
            })
    write_json(args.output/'assessment.json', report)  # Plan saved before fitting.
    request = {'inputId': product['id'], 'start': report['plan']['searchStart'],
               'end': report['plan']['searchEnd'], 'parameters': reference,
               'routingMode': 'source-separated', 'objectiveProfile': 'balanced',
               '_observations': snapshot, '_modelIdentity': identity}
    for seed in args.seeds:
        path = {'seed': seed, 'stages': {}}
        params = dict(reference)
        for stage, budget in [('runoff', args.runoff_budget), ('routing', args.routing_budget), ('lake', args.lake_budget)]:
            folder = args.output/f'seed-{seed}'/stage
            folder.mkdir(parents=True)
            def progress(value, message):
                step = int(value)//20
                if step != progress.last:
                    print(json.dumps({'seed': seed, 'stage': stage, 'progress': round(value, 1)}, ensure_ascii=True), flush=True)
                    progress.last = step
            progress.last = -1
            tick = time.perf_counter()
            r = calibrate(db, {**request, 'parameters': params, 'seed': seed,
                              'stage': stage, 'maxEvaluations': budget,
                              'calibrationPartition': 'uniform'}, folder, progress)
            params = r['parameters']
            path['stages'][stage] = {
                'seconds': time.perf_counter()-tick, 'parameters': params,
                'metrics2024': r['metrics']['2024'],
                'search': {k: v for k, v in r['calibration'].items() if k != 'trace'},
            }
            write_json(args.output/f'seed-{seed}'/'summary.json', path)
        path['objective2024'] = flow_objective(r['metrics']['2024']['flow'])[0]
        report['paths'].append(path)
        write_json(args.output/'assessment.json', report)
    chosen = min(report['paths'], key=lambda p: p['objective2024'])
    # Selection is committed before any withheld simulation is evaluated here.
    report['selection'] = {'seed': chosen['seed'], 'objective2024': chosen['objective2024'],
                           'parameters': chosen['stages']['lake']['parameters'],
                           'criterion': report['plan']['selectionCriterion']}
    write_json(args.output/'selection-before-validation.json', report['selection'])
    write_json(args.output/'assessment.json', report)
    full_request = {**request, 'end': report['plan']['finalAssessmentEnd']}
    for name, params in [('reference', reference), ('selected', report['selection']['parameters'])]:
        print('ASSESS '+name, flush=True)
        result = run_simulation(db, {**full_request, 'parameters': params},
                                folder=args.output/(name+'-full'), prepared=source)
        report[name] = {k: result[k] for k in ('parameters', 'metrics', 'identity', 'diagnostics', 'modelDiagnostics')}
        if name == 'selected':
            cases = []
            for fraction in (.25, .75):
                def initialize(units, p):
                    state = initial_states(units, p)
                    state[:, 2:5] *= fraction/.5
                    return state
                with patch('backend.simulation.initial_states', side_effect=initialize):
                    v = run_simulation(db, {**full_request, 'parameters': params},
                                       prepared=source)
                cases.append({'fraction': fraction, 'metrics': v['metrics'],
                              'cryosphere': v['modelDiagnostics']['cryosphere'],
                              'annualStorage': v['modelDiagnostics']['monthly']})
            report['initialStateSensitivity'] = cases
        write_json(args.output/'assessment.json', report)
    bundle = {
        'schemaVersion': 1, 'projectId': 'selinco', 'name': '色林错历史模型候选',
        'status': 'research-candidate', 'activateByDefault': False,
        'parameters': report['selection']['parameters'], 'routingMode': 'source-separated',
        'identity': report['selected']['identity'], 'searchPlan': report['plan'],
        'metrics': report['selected']['metrics'], 'initialization': {
            'tensionWaterFraction': .5, 'snowAndRouting': 'empty at 2023-01-01',
            'lake': 'first reliable observation per window'},
        'assessment': 'assessment.json',
    }
    bundle['bundleSHA256'] = digest(bundle)
    write_json(args.output/'frozen-candidate.json', bundle)
    report.update(status='complete', completedAt=now(), activated=False)
    write_json(args.output/'assessment.json', report)
    print(json.dumps({'status': 'complete', 'selectedSeed': chosen['seed'], 'output': str(args.output)}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
