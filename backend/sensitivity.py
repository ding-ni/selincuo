"""Bounded local experiments; rankings use 2024 only and never replace parameters."""
from pathlib import Path
from .calibration_config import public_config, flow_objective
from .model_core import parameters, PARAMETER_DEFINITIONS
from .simulation import prepare, run_simulation
from .common import write_json


def analyze(db, request, folder, progress=None, cancelled=None):
    source = prepare(db, request['inputId'])
    base = parameters(request.get('parameters'))
    regions = request.get('partitionParameters') or {}
    definitions = {d['key']: d for d in PARAMETER_DEFINITIONS}
    keys = public_config()['sensitivity']['keys']
    profile = request.get('objectiveProfile', 'balanced')
    baseline = run_simulation(db, {**request, 'parameters': base}, prepared=source, folder=folder, cancelled=cancelled)
    metric = baseline['metrics'].get('2024', {})
    if metric.get('flow', {}).get('count', 0) < 30 or metric.get('lake', {}).get('count', 0) < 30:
        raise ValueError('敏感性比较需要2024年可靠流量与湖位至少各30个匹配日')
    objective, _ = flow_objective(metric['flow'], profile)
    rows, skipped = [], []
    for index, key in enumerate(keys):
        d = definitions[key]
        target = 'duja' if regions and key != 'lakeCoefficient' else None
        local = parameters({**base, **regions.get(target, {})}) if target else base
        delta = max(abs(local[key])*.2, (d['max']-d['min'])*.01)
        for direction in (-1, 1):
            if cancelled and cancelled():
                raise InterruptedError('用户停止敏感性实验')
            value = min(d['max'], max(d['min'], local[key]+direction*delta))
            if abs(value-local[key]) < 1e-10:
                continue
            candidate = {**local, key: value}
            try:
                parameters(candidate)
            except ValueError as exc:
                skipped.append({'parameter': key, 'value': value, 'reason': str(exc)})
                continue
            changes = {'parameters': base, 'partitionParameters': {**regions, target: {**regions.get(target, {}), key: value}}} if target else {'parameters': candidate}
            result = run_simulation(db, {**request, **changes, '_skipDiagnostics': True}, prepared=source, cancelled=cancelled)
            m = result['metrics']['2024']
            score, _ = flow_objective(m['flow'], profile)
            rows.append({'parameter': key, 'label': d['label'], 'baselineValue': local[key], 'value': value,
                         'parameterScope': target or 'basin',
                         'relativeChange': (value-local[key])/abs(local[key]) if local[key] else None,
                         'flowObjectiveChange': score-objective, 'flowNseChange': m['flow']['nse']-metric['flow']['nse'],
                         'flowBiasChange': m['flow']['pbias']-metric['flow']['pbias'],
                         'lakeRmseChangeM': m['lake']['rmse']-metric['lake']['rmse'], 'metrics2024': m})
        if progress:
            progress((index+1)/len(keys)*95, '局部敏感性 '+d['label'])
    rows.sort(key=lambda row: (row['parameter'], row['value']))
    baseline['sensitivity'] = {'method': 'one-at-a-time', 'fraction': .2, 'evaluationYear': 2024,
            'excludedYears': [2025, 2026], 'objectiveProfile': profile, 'baselineMetrics': metric, 'rows': rows,
            'parametersApplied': False, 'modelEvaluations': len(rows)+1, 'skipped': skipped,
            'limitations': ['仅为当前参数附近的逐参数扰动，未刻画参数交互；不能据此证明参数唯一或最优。',
                           '排序用于诊断当前目标的响应，2025与2026不参与排序。',
                           '修改不会自动采用，需在人工试调中明确运行并比较。']}
    write_json(Path(folder)/'sensitivity.json', baseline['sensitivity'])
    return baseline
