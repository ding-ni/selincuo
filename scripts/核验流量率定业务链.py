"""Run one real, flow-only staged calibration and persist its new business chain.

The operational configuration is never edited. Commands are resumable through
exclusive stage receipts and task request IDs. Existing model results are read.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import digest, read_json, sha_file
from scripts.执行参数传递对照 import api, exclusive, period
import pandas as pd

OUT = ROOT/'build/第三轮整改-20261028/流量分阶段率定'
WORKSPACE = (ROOT/read_json(ROOT/'config/active-workspace.json')['workspace']).resolve()
CONFIG = ROOT/'config/operational/continuous-builtin-v1.json'


def submit():
    receipt = OUT/'率定提交.json'
    if receipt.exists():
        print(json.dumps(read_json(receipt), ensure_ascii=False)); return
    value = api('research-template')
    value['name'] = '杜加流量分阶段率定方案'
    value['search']['keys'] = ['KC', 'WM', 'KI', 'lagHours']
    value['search']['seed'] = 42
    value['search']['seeds'] = [42, 142]
    value['search']['budgets'].update(runoff=120, routing=60)
    value['periods'] = {'warmupStart': '2020-01-01T00:00:00',
        'trainingStart': '2024-01-01T00:00:00', 'trainingEnd': '2025-01-01T00:00:00',
        'evaluationStart': '2025-01-01T00:00:00', 'evaluationEnd': '2026-10-21T00:00:00'}
    plan = OUT/'率定计划.json'
    if not plan.exists():
        exclusive(plan, {'sourceConfigSHA256': sha_file(CONFIG), 'adoptedResultId': read_json(CONFIG)['resultId'],
            'schemePayload': value, 'objectiveUsesLakeLevel': False,
            'selectionBasis': 'Real 2024 fixed-date local response and correlation; IM remains fixed without independent impervious-area evidence; snow/ice and lake parameters not optimized.',
            'searchLimit': 'Finite multistart chain verification, not globally optimal or converged by declaration.',
            'auditSourceSHA256': sha_file(ROOT/'docs/evidence/第三轮参数作用与率定审计-20261028.json')})
    row = api('schemes', {'payload': value})
    task = api('tasks', {'kind': 'calibration', 'researchSchemeId': row['id'],
        'clientRequestId': 'round3-flow-audit-20261028-calibration-v1', 'name': '杜加流量分阶段率定'})
    exclusive(receipt, {'taskId': task['id'], 'schemeId': row['id'],
                         'familyId': row['payload']['familyId'], 'configurationSHA256': row['payload']['configurationSHA256']})
    print(json.dumps(read_json(receipt), ensure_ascii=False), flush=True)


def finalize():
    receipt = read_json(OUT/'率定提交.json')
    task = api('tasks/'+receipt['taskId'])
    if task['status'] != 'succeeded':
        raise ValueError('率定尚未成功：'+task['status']+' '+task.get('message', ''))
    result = task['result']
    calibration = result['calibration']
    if calibration['targets']['lake']['usedForObjective']:
        raise AssertionError('湖位不得进入本次流量率定')
    if result['parameters']['lakeCoefficient'] != 1. or result['partitionParameters']:
        raise AssertionError('湖面固定值/单一参数来源被改变')
    saved_receipt = OUT/'参数保存采用.json'
    if saved_receipt.exists():
        saved = read_json(saved_receipt)
    else:
        parameters = api('parameters/from-task', {'taskId': task['id'], 'name': '杜加流量率定参数 · 2024'})
        adopted = api('schemes/'+receipt['schemeId']+'/adopt-parameters', {'parameterId': parameters['id']})
        reopened = next(row for row in api('schemes')['schemes'] if row['id'] == adopted['id'])
        assert reopened['payload']['parameters'] == result['parameters']
        assert reopened['payload']['partitionParameters'] == {}
        assert reopened['payload']['parameterTransfer'] == result['parameterTransfer']
        assert reopened['payload']['adoptedParameterId'] == parameters['id']
        saved = {'parameterId': parameters['id'], 'schemeId': adopted['id'],
                 'parameterSHA256': parameters['payload']['parameterSHA256'],
                 'effectiveParameterSHA256': parameters['payload']['effectiveParameterSHA256'],
                 'reopenedConfigurationSHA256': reopened['payload']['configurationSHA256']}
        exclusive(saved_receipt, saved)
        exclusive(ROOT/'config/model-candidates/杜加流量参数-20261028.json',
            {'status': 'research-flow-constrained', 'operationalAdoptionChanged': False,
             'schemeId': saved['schemeId'], 'parameterId': parameters['id'], 'sourceTaskId': task['id'],
             'parameters': result['parameters'], 'partitionParameters': {},
             'parameterTransfer': result['parameterTransfer'], 'identity': result['identity'],
             'calibration': {key:calibration[key] for key in ('algorithm','seeds','stages','targets','assessments','periods',
                 'evaluations','converged','boundaryParameters','nearBoundaryParameters','bestObjective')},
             'limitations': ['2025/26 previously used in development; not untouched external validation.',
                 'Lake evaporation is a fixed potential-water proxy, not a measured actual lake flux.',
                 'Single-station transfer, initial states and unresolved drainage remain uncertain.']})
    simulation_receipt = OUT/'模拟提交.json'
    if not simulation_receipt.exists():
        job = api('tasks', {'kind': 'simulation', 'researchSchemeId': saved['schemeId'],
            'start': '2021-04-23T00:00:00', 'end': '2026-10-21T00:00:00',
            'clientRequestId': 'round3-flow-audit-20261028-simulation-v1', 'name': '杜加流量率定后历史模拟 · 2021—2026'})
        exclusive(simulation_receipt, {'taskId': job['id'], 'schemeId': saved['schemeId']})
    print(json.dumps({'calibration': task['id'], 'saved': saved,
        'simulation': read_json(simulation_receipt)}, ensure_ascii=False), flush=True)


def continuation():
    simulation = read_json(OUT/'模拟提交.json')
    task = api('tasks/'+simulation['taskId'])
    if task['status'] != 'succeeded':
        raise ValueError('模拟尚未成功：'+task['status'])
    receipt = OUT/'续算提交.json'
    if not receipt.exists():
        with sqlite3.connect((WORKSPACE/'database/hydrology.sqlite3').as_uri()+'?mode=ro', uri=True) as con:
            rows = con.execute('SELECT id FROM states WHERE task_id=?', (task['id'],)).fetchall()
        if len(rows) != 1:
            raise AssertionError('成功模型任务应有唯一完整末状态')
        job = api('tasks', {'kind': 'continuation', 'stateId': rows[0][0],
            'start': '2026-10-21T01:00:00', 'end': '2026-10-21T07:00:00',
            'clientRequestId': 'round3-flow-audit-20261028-continuation-v1', 'name': '完整状态续算核验'})
        exclusive(receipt, {'taskId': job['id'], 'parentTaskId': task['id'], 'parentStateId': rows[0][0],
            'note': 'Seven available hours only; this is not a complete daily result or a new complete driver library.'})
    print(json.dumps(read_json(receipt), ensure_ascii=False), flush=True)


def verify():
    from backend.grid_simulation import run, model_identity
    from backend.grid_storage import open_arrays
    from scripts.assess_model_candidate import ReadOnlyData
    import numpy as np
    calibrated = read_json(OUT/'率定提交.json')
    simulated = read_json(OUT/'模拟提交.json')
    extended = read_json(OUT/'续算提交.json')
    results = {}
    for label, stage in [('calibration', calibrated), ('simulation', simulated), ('continuation', extended)]:
        task = api('tasks/'+stage['taskId'])
        if task['status'] != 'succeeded':
            raise ValueError(label+'尚未成功：'+task['status'])
        folder = WORKSPACE/'tasks'/task['id']
        result = read_json(folder/'result.json')
        result['request'] = read_json(folder/'request.json')
        results[label] = result
    simulation, extension = results['simulation'], results['continuation']
    request = deepcopy(simulation['request'])
    request.update(end='2026-10-21T07:00:00', _metricsOnly=True)
    if request['_modelIdentity'] != model_identity():
        raise AssertionError('数值实现身份改变，禁止跨版本假比较')
    full = run(ReadOnlyData(WORKSPACE), request)
    a, b = np.asarray(full['checkpoint']['unitStates']), np.asarray(extension['checkpoint']['unitStates'])
    np.testing.assert_allclose(a, b, rtol=0., atol=1e-10)
    np.testing.assert_allclose(full['checkpoint']['lake']['level'], extension['checkpoint']['lake']['level'], rtol=0., atol=1e-9)
    for partition in full['checkpoint']['routing']:
        left, right = full['checkpoint']['routing'][partition], extension['checkpoint']['routing'][partition]
        np.testing.assert_allclose(left['channel']['queue'], right['channel']['queue'], rtol=0., atol=1e-7)
        np.testing.assert_allclose(left['channel']['storage'], right['channel']['storage'], rtol=0., atol=1e-6)
        for key in left['sources']:
            np.testing.assert_allclose(left['sources'][key], right['sources'][key], rtol=0., atol=1e-6)
    frame = pd.DataFrame(simulation['daily']);frame.index=pd.to_datetime(frame.date)
    pairs = pd.DatetimeIndex(results['calibration']['calibration']['fixedTrainingDates'])
    observed = results['calibration']['request']['_observations']['flow']
    assert all(pd.Timestamp('2024-01-01') <= day < pd.Timestamp('2025-01-01') and day.isoformat() in observed for day in pairs)
    downloaded = {}
    for name in ('daily.csv', 'checkpoint.json', 'result.json'):
        with urlopen('http://127.0.0.1:8882/api/model/export?taskId='+simulated['taskId']+'&format='+name, timeout=180) as response:
            downloaded[name] = hashlib.sha256(response.read()).hexdigest()
        assert downloaded[name] == sha_file(WORKSPACE/'tasks'/simulated['taskId']/name)
    arrays = open_arrays(WORKSPACE/'tasks'/simulated['taskId'])
    q = arrays['flow'][:]
    composition = float(np.max(np.abs(q[:,6]-q[:,1:6].sum(axis=1))))
    if composition > 1e-8:
        raise AssertionError('入湖组成不一致')
    anchor = pd.Timestamp(simulation['initialization']['time']).value
    integrated = float(np.sum(arrays['lakeFlux'][:][arrays['time'][:] > anchor]))
    balance_error = simulation['checkpoint']['lake']['volumeChange'] - integrated
    if abs(balance_error) > max(1e-4, abs(integrated)*1e-10):
        raise AssertionError('累计湖泊水量不守恒')
    verified_artifacts = {}
    for label, stage in [('simulation', simulated), ('continuation', extended)]:
        folder = WORKSPACE/'tasks'/stage['taskId']
        manifest = read_json(folder/'hourly-manifest.json')
        for name, sha in manifest['files'].items():
            if sha_file(folder/'hourly.zarr'/name) != sha:
                raise AssertionError(label+'逐时工件摘要不一致：'+name)
        verified_artifacts[label] = len(manifest['files'])
    objective_config = {'profile': results['calibration']['calibration']['objectiveProfile'],
                        'weights': results['calibration']['calibration']['objectiveWeights']}
    with urlopen('http://127.0.0.1:8882/api/health', timeout=30) as response:
        verification_health = json.load(response)
    plan = read_json(OUT/'率定计划.json')
    assert sha_file(CONFIG) == plan['sourceConfigSHA256']
    receipt = {'calibration': calibrated, 'savedAndAdoptedIntoNewScheme': read_json(OUT/'参数保存采用.json'),
        'simulation': simulated, 'continuation': extended,
        'numericalIdentity': simulation['identity'], 'parameterTransfer': simulation['parameterTransfer'],
        'trainingDateCount': len(pairs), 'trainingDatesSHA256': digest([d.isoformat() for d in pairs]),
        'calibrationSummary': {k:v for k,v in results['calibration']['calibration'].items() if k!='trace'},
        'traceSHA256': digest(results['calibration']['calibration']['trace']),
        'optimizerImplementationSHA256': results['calibration']['calibration']['calibrationImplementationSHA256'],
        'objectiveConfiguration': objective_config,
        'objectiveConfigurationSHA256': digest(objective_config),
        'verificationBackendSourceSHA256': verification_health['backendSourceSHA256'],
        'training2024': period(frame,'2024-01-01','2025-01-01'),
        'evaluation2025_2026': period(frame,'2025-01-01','2026-10-21'),
        'continuationUnitStateMaxError': float(np.max(np.abs(a-b))),
        'continuationLakeLevelErrorM': full['checkpoint']['lake']['level']-extension['checkpoint']['lake']['level'],
        'compositionMaxErrorM3s': composition, 'downloadSHA256': downloaded,
        'lakeCumulativeBalanceErrorM3': balance_error,
        'maximumDailyLakeBalanceErrorM3': float(frame.lakeBalanceErrorM3.abs().max()),
        'hourlyFilesVerified': verified_artifacts,
        'originalOperationalConfigurationUnchanged': True, 'operationalAdoptionChanged': False,
        'displayCoverage': simulation['displayCoverage'], 'diagnostics': simulation['diagnostics']}
    exclusive(ROOT/'docs/evidence/流量率定参数采用与续算-20261028.json',receipt)
    print(json.dumps({'evidence': 'docs/evidence/流量率定参数采用与续算-20261028.json',
        'simulation':simulated['taskId'],'stateError':receipt['continuationUnitStateMaxError'],
        'lakeError':receipt['continuationLakeLevelErrorM']},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser();parser.add_argument('action', choices=['submit','finalize','continuation','verify'])
    args=parser.parse_args()
    {'submit':submit,'finalize':finalize,'continuation':continuation,'verify':verify}[args.action]()
