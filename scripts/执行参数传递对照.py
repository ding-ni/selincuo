"""Separate API-backed parameter-transfer experiments; never replace adoption.

Use ``submit`` once, then ``collect`` after the tasks finish. Existing receipts
are reused, not overwritten. All alternatives use the same space/forcing and
2021 anchor. A1E is an explicit evaporation-prior ablation, not an optimization.
"""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from urllib.request import Request, urlopen

os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
from backend.common import digest, read_json, sha_file
from backend.model_core import parameters
from backend.simulation import metrics
from backend.grid_storage import open_arrays

BASE = 'http://127.0.0.1:8882'
OUT = ROOT / 'build/第三轮整改-20261028/参数传递对照'
CONFIG = ROOT / 'config/operational/continuous-builtin-v1.json'


def api(path, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = Request(BASE + '/api/model/' + path, data=data,
                      headers={'Content-Type': 'application/json', 'Origin': BASE})
    with urlopen(request, timeout=180) as response:
        return json.load(response)


def exclusive(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def alternatives(config):
    original = deepcopy(config)
    for key in ('id', 'resultId', 'configurationSHA256', 'familyId', 'revision',
                'adoptedParameterId', 'calibrationInitialParameters', 'calibrationInitialPartitionParameters'):
        original.pop(key, None)
    original.update(target='duja', origin='user', initialization={'mode': 'automatic'})
    original['periods'] = {'warmupStart': '2020-01-01T00:00:00',
                           'trainingStart': '2024-01-01T00:00:00', 'trainingEnd': '2025-01-01T00:00:00',
                           'evaluationStart': '2025-01-01T00:00:00', 'evaluationEnd': '2026-10-21T00:00:00'}
    original['search']['scope'] = 'duja'
    original['parameterTransfer'] = {'version': 1, 'mode': 'legacy', 'donor': 'duja', 'areaExponent': .5}
    original['name'] = '原参数对照方案'
    shared = deepcopy(original)
    shared['name'] = '杜加参数共享方案'
    shared['sourceTransferSnapshot'] = {'method': 'explicit-duja-lift',
        'sourceConfigurationSHA256': sha_file(CONFIG),
        'parameters': deepcopy(original['parameters']),
        'partitionParameters': deepcopy(original['partitionParameters']),
        'verifiedCalibration': False,
        'basis': 'Historical 2024-flow-constrained donor; not independently validated elsewhere.'}
    shared['parameters'] = parameters({**original['parameters'], **original['partitionParameters'].get('duja', {})})
    shared['partitionParameters'] = {}
    shared['parameterTransfer']['mode'] = 'duja-shared'
    regional = deepcopy(shared)
    regional['name'] = '面积尺度参数对照方案'
    regional['parameterTransfer']['mode'] = 'duja-area-routing'
    proxy = deepcopy(shared)
    proxy['name'] = '杜加共享与固定湖面蒸发方案'
    proxy['parameters']['lakeCoefficient'] = 1.
    proxy['lakeParameterSource'] = {'method': 'fixed-potential-open-water-proxy',
        'coefficient': 1., 'calibratedToLakeLevel': False, 'directLakeEvaporationObserved': False}
    return {'A0': original, 'A1': shared, 'A2': regional, 'A1E': proxy}


def submit():
    OUT.mkdir(parents=True, exist_ok=True)
    planned = OUT / '试验计划.json'
    if not planned.exists():
        exclusive(planned, {'sourceConfigSHA256': sha_file(CONFIG), 'sourceResultId': read_json(CONFIG)['resultId'],
            'cases': alternatives(read_json(CONFIG)),
            'controlledFactors': ['model implementation', 'spatial version', 'forcing', 'warmup', 'lake anchor'],
            'selectionUsesLakeLevel': False, 'evaluationYearsPreviouslyInspected': [2025, 2026],
            'interpretation': 'A0/A1/A2 differ only in parameter application; lake coefficient 1.5 is a legacy H-fitted value frozen for that contrast. A1E changes only this coefficient to a unit potential-open-water proxy; it is not direct evaporation validation.',
            'adoptionChanged': False})
    plan = read_json(planned)
    for case, payload in plan['cases'].items():
        receipt = OUT / (case + '-提交.json')
        if receipt.exists():
            print(json.dumps({'case': case, 'existingTask': read_json(receipt)['taskId']}), flush=True)
            continue
        row = api('schemes', {'payload': payload})
        task = api('tasks', {'kind': 'simulation', 'researchSchemeId': row['id'],
            'name': payload['name'] + ' · 2021—2026', 'start': '2021-04-23T00:00:00',
            'end': '2026-10-21T00:00:00'})
        exclusive(receipt, {'case': case, 'scheme': row, 'taskId': task['id'], 'adoptionChanged': False})
        print(json.dumps({'case': case, 'taskId': task['id'], 'status': task['status']}, ensure_ascii=False), flush=True)


def period(frame, start, end):
    selected = frame.loc[(frame.index >= pd.Timestamp(start)) & (frame.index < pd.Timestamp(end))]
    output = {'flow': metrics(selected.observedFlow, selected.duja),
              'lake': metrics(selected.observedLevel, selected.lakeLevel)}
    flow_pair = selected[['observedFlow', 'duja', 'forcingHours']].dropna()
    if not flow_pair.forcingHours.eq(24).all():
        raise AssertionError('径流量比较只能使用有完整24小时模拟的实测配对日')
    observed_volume = float(flow_pair.observedFlow.sum() * 86400)
    simulated_volume = float(flow_pair.duja.sum() * 86400)
    output['flow'].update(observedVolumeM3=observed_volume,
        simulatedVolumeM3OnObservedDays=simulated_volume,
        volumeErrorM3=simulated_volume-observed_volume,
        volumeErrorPercent=100*(simulated_volume-observed_volume)/observed_volume if observed_volume else None,
        volumeCoverage='paired observed days only; not complete annual runoff')
    pair = selected[['observedLevel', 'lakeLevel']].dropna()
    if len(pair) > 1:
        years = (pair.index - pair.index[0]).total_seconds().to_numpy() / (86400 * 365.25)
        output['lake'].update(observedTrendMPerYear=float(np.polyfit(years, pair.observedLevel, 1)[0]),
                             simulatedTrendMPerYear=float(np.polyfit(years, pair.lakeLevel, 1)[0]))
    return output


def collect():
    plan = read_json(OUT / '试验计划.json')
    settings = read_json(ROOT / 'config/active-workspace.json')
    workspace = (ROOT / settings['workspace']).resolve()
    records, tables = {}, {}
    for case in plan['cases']:
        receipt = read_json(OUT / (case + '-提交.json'))
        task = api('tasks/' + receipt['taskId'])
        if task['status'] != 'succeeded':
            raise ValueError(case + '任务尚未成功：' + task['status'] + ' ' + str(task.get('error', '')))
        folder = workspace / 'tasks' / task['id']
        result = read_json(folder / 'result.json')
        frame = pd.DataFrame(result['daily'])
        frame.index = pd.to_datetime(frame['date'])
        tables[case] = frame
        arrays = open_arrays(folder)
        times = arrays['time'][:]
        flow = arrays['flow'][:]
        at = pd.Timestamp(result['initialization']['time']).value
        integrated = float(np.sum(arrays['lakeFlux'][:][times > at]))
        ending = result['checkpoint']['lake']['volumeChange']
        partition_checks = {}
        final_stores = arrays['routingStorage'][-1]
        for index, name in enumerate(result['partitions']):
            totals = result['diagnostics']['partitions'][name]
            residual = totals['generatedM3'] - totals['routedM3'] - float(final_stores[index])
            if abs(residual) > max(1e-4, totals['generatedM3'] * 1e-10):
                raise AssertionError((case, name, 'routing mass', residual))
            partition_checks[name] = residual
        composed = float(np.max(np.abs(flow[:, 6] - flow[:, 1:6].sum(axis=1))))
        if composed > 1e-8 or abs(ending - integrated) > max(1e-4, abs(integrated)*1e-10):
            raise AssertionError((case, 'composition/lake mass', composed, ending-integrated))
        hourly = read_json(folder / 'hourly-manifest.json')
        for name, sha in hourly['files'].items():
            if sha_file(folder / 'hourly.zarr' / name) != sha:
                raise AssertionError((case, 'artifact SHA mismatch', name))
        records[case] = {'taskId': task['id'], 'schemeId': receipt['scheme']['id'],
            'sourceRequestSHA256': sha_file(folder/'request.json'), 'resultSHA256': sha_file(folder/'result.json'),
            'identity': result['identity'], 'initialization': result['initialization'],
            'calculationCoverage': result.get('calculationCoverage'), 'displayCoverage': result.get('displayCoverage'),
            'parameters': result['parameters'], 'partitionParameters': result['partitionParameters'],
            'parameterTransfer': result['parameterTransfer'], 'parameterTransferAudit': result['parameterTransferAudit'],
            'training2024': period(frame, '2024-01-01', '2025-01-01'),
            'evaluation2025_2026': period(frame, '2025-01-01', '2026-10-21'),
            'annual': result['metrics'], 'diagnostics': result['diagnostics'],
            'inflowComplete': result['inflowComplete'],
            'lakeCumulativeBalanceErrorM3': ending - integrated,
            'partitionCumulativeBalanceErrorsM3': partition_checks,
            'compositionMaxErrorM3s': composed,
            'maximumDailyLakeBalanceErrorM3': float(frame.lakeBalanceErrorM3.abs().max()),
            'hourlyFilesVerified': len(hourly['files'])}
        print(json.dumps({'case': case, 'taskId': task['id'], 'trainingFlowNSE': records[case]['training2024']['flow']['nse'],
                          'evaluationFlowNSE': records[case]['evaluation2025_2026']['flow']['nse'],
                          'evaluationLakeRMSE': records[case]['evaluation2025_2026']['lake']['rmse']}, ensure_ascii=False), flush=True)
    comparisons = {}
    for case in ('A1', 'A2', 'A1E'):
        frame = tables[case]
        anchor = tables['A0']
        comparisons[case] = {'maximumDujaDeltaM3s': float((frame.duja-anchor.duja).abs().max()),
                             'meanTotalDeltaM3s': float((frame.total-anchor.total).mean()),
                             'maximumLakeDeltaM': float((frame.lakeLevel-anchor.lakeLevel).abs().max())}
    evidence = {'plan': {k:v for k,v in plan.items() if k!='cases'}, 'results': records,
                'comparisonToA0': comparisons, 'adoptionChanged': False,
                'sourceConfigUnchanged': sha_file(CONFIG) == plan['sourceConfigSHA256']}
    if not evidence['sourceConfigUnchanged']:
        raise AssertionError('原采用配置在试验中发生变化')
    exclusive(ROOT/'docs/evidence/杜加参数传递真实对照-20261028.json', evidence)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['submit', 'collect'])
    args = parser.parse_args()
    (submit if args.action == 'submit' else collect)()
