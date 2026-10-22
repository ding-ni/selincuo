# coding: utf-8
"""Reproducible same-budget routing controls; selection uses 2024 only."""
import argparse
import json
import sys
import time
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import now, read_json, write_json
from backend.calibration_config import flow_objective


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--url', default='http://127.0.0.1:8879')
    p.add_argument('--report', type=Path, default=ROOT/'docs/model-improvement-v0.5.0.json')
    p.add_argument('--budget', type=int, default=240)
    args = p.parse_args()
    session = requests.Session()
    session.trust_env = False

    def api(path, payload=None):
        url = args.url+'/api/model/'+path
        r = session.get(url, timeout=120) if payload is None else session.post(url, json=payload, timeout=120)
        r.raise_for_status()
        return r.json()

    boot = api('bootstrap')
    old = read_json(ROOT/'docs/historical-results-v0.4.0.json')['lake']
    base = {**boot['defaultParameters'], **old['parameters']}
    request = {'inputId': 'INPUT-967613234ca58f1c6cfb44e2', 'start': '2023-01-01T00:00:00',
               'end': '2026-01-01T00:00:00', 'seed': 42, 'calibrationYear': 2024,
               'objectiveProfile': 'balanced', 'parameters': base}
    report = read_json(args.report, {'createdAt': now(), 'tasks': {}, 'request': request,
                         'selectionYear': 2024, 'validationYearsExcludedFromSelection': [2025, 2026]})

    def execute(key, payload, collection='结构对照'):
        identifier = report['tasks'].get(key)
        task = api('tasks/'+identifier) if identifier else api('tasks', payload)
        report['tasks'][key] = task['id']
        write_json(args.report, report)
        tick = -1
        while task['status'] in ('queued', 'running', 'stopping'):
            step = int(task.get('progress', 0)//10)
            if tick != step:
                print(json.dumps({'stage': key, 'id': task['id'], 'progress': task.get('progress'), 'message': task.get('message')}, ensure_ascii=True), flush=True)
                tick = step
            time.sleep(3)
            task = api('tasks/'+task['id'])
        if task['status'] != 'succeeded':
            raise RuntimeError(task.get('message', task['status']))
        r = task['result']
        assert len(r['daily']) == 1096 and all(d['forcingHours'] == 24 for d in r['daily'])
        report[key] = {k: r[k] for k in ('parameters', 'metrics', 'identity', 'diagnostics', 'routingMode', 'modelDiagnostics')}
        if r.get('calibration'):
            report[key]['search'] = {k: v for k, v in r['calibration'].items() if k != 'trace'}
        if r.get('sensitivity'):
            report[key]['sensitivity'] = r['sensitivity']
        write_json(args.report, report)
        api('tasks/'+task['id']+'/metadata', {'collection': collection, 'favorite': False})
        print(json.dumps({'stage': key, 'status': 'succeeded', 'id': task['id'], 'metrics': r['metrics']}, ensure_ascii=True), flush=True)
        return task

    control = execute('legacyControl', {**request, 'kind': 'simulation', 'routingMode': 'lumped', 'name': '结构对照 · 原汇流与v0.4冻结参数'})
    for year in ('2024', '2025'):
        for variable in ('flow', 'lake'):
            for metric in ('nse', 'rmse', 'pbias'):
                a, b = old['metrics'][year][variable].get(metric), control['result']['metrics'][year][variable].get(metric)
                assert a is None and b is None or a is not None and b is not None and abs(a-b) < 1e-8, (year, variable, metric, a, b)
    report['legacyMetricsReproduced'] = True
    execute('sourceControl', {**request, 'kind': 'simulation', 'routingMode': 'source-separated', 'name': '结构对照 · 同参数分水源调蓄'})
    candidates = {}
    for mode, prefix in (('lumped', 'legacy'), ('source-separated', 'source')):
        runoff = execute(prefix+'Runoff', {**request, 'kind': 'calibration', 'stage': 'runoff', 'routingMode': mode,
                         'maxEvaluations': args.budget, 'name': '结构对照 · '+mode+' · 2024平衡目标率定'})
        lake = execute(prefix+'Lake', {**request, 'kind': 'calibration', 'stage': 'lake', 'baseTaskId': runoff['id'],
                         'routingMode': mode, 'maxEvaluations': 60, 'name': '结构对照 · '+mode+' · 冻结流域后湖泊率定'})
        candidates[prefix] = (flow_objective(runoff['result']['metrics']['2024']['flow'], 'balanced')[0], lake)
    chosen = min(candidates, key=lambda key: candidates[key][0])
    chosen_task = candidates[chosen][1]
    report['selection'] = {'criterion': '2024 balanced flow objective only; then lake evaporation calibrated independently',
                           'scores2024': {k: v[0] for k, v in candidates.items()}, 'chosen': chosen, 'taskId': chosen_task['id'],
                           'interpretation': 'Runoff experiment winner only; does not automatically replace the lake reference. Bounded exploratory comparison, not unique parameters or operational prediction skill.'}
    write_json(args.report, report)
    r = chosen_task['result']
    sensitivity = execute('sensitivity', {**request, 'parameters': r['parameters'], 'routingMode': r['routingMode'],
                             'kind': 'sensitivity', 'name': '模型诊断 · 冻结参数逐项敏感性（2024）'}, '模型诊断')
    source_task = candidates['source'][1]
    source_result = source_task['result']
    execute('sourceSensitivity', {**request, 'parameters': source_result['parameters'], 'routingMode': 'source-separated',
                 'kind': 'sensitivity', 'name': '模型诊断 · 分水源调蓄参数敏感性（2024）'}, '模型诊断')
    if 'parameterVersion' not in report:
        report['parameterVersion'] = api('parameters', {'name': 'v0.5结构对照 · 2024冻结参数', 'payload': {
                     'parameters': r['parameters'], 'sourceTaskId': chosen_task['id'], 'routingMode': r['routingMode'],
                     'calibrationYear': 2024, 'validationYearsExcludedFromSearch': [2025, 2026]}})['id']
        write_json(args.report, report)
    if 'scheme' not in report:
        report['scheme'] = api('schemes', {'name': 'v0.5完整历史 · 冻结条件复算', 'payload': {
                     **request, 'parameters': r['parameters'], 'routingMode': r['routingMode'], 'name': 'v0.5冻结参数复算'}})['id']
    for prefix in ('legacy', 'source'):
        api('tasks/'+candidates[prefix][1]['id']+'/metadata', {'collection': '结构对照', 'favorite': False,
            'notes': '产汇流目标试验；湖位效果并未自动改善，保留作诊断，不直接替换湖位参考成果。'})
    reference_keys = ('legacyControl', 'sourceControl', 'legacyLake', 'sourceLake')
    reference_key = min(reference_keys, key=lambda key: report[key]['metrics']['2024']['lake']['rmse'])
    reference_task = api('tasks/'+report['tasks'][reference_key])
    reference = reference_task['result']
    report['lakeReference'] = {'criterion': 'Lowest 2024 conditional lake RMSE among the four declared controls/calibrated candidates; 2025 excluded',
              'candidateScores2024': {k: report[k]['metrics']['2024']['lake']['rmse'] for k in reference_keys},
              'chosen': reference_key, 'taskId': reference_task['id'], 'operationallyValidated': False,
              'note': 'Exploratory reference only; negative log-flow NSE, boundary evaporation and unvalidated basin transfer remain unresolved.'}
    api('tasks/'+reference_task['id']+'/metadata', {'collection': '完整历史', 'favorite': True,
        'name': '完整历史 · 湖位参考方案（2024比较，非业务定型）',
        'notes': '仅按2024条件湖位RMSE选择对照参考；不表示流量、水量收支或未来预报均已达标。2025留出评价，旧成果均保留。'})
    for key, title, task in (('referenceParameterVersion', 'v0.5湖位参考 · 沿用初轮陆面冰雪参数', reference_task),
                             ('sourceParameterVersion', 'v0.5分水源流量试验 · 2024冻结参数', source_task)):
        if key not in report:
            report[key] = api('parameters', {'name': title, 'payload': {'parameters': task['result']['parameters'],
                   'sourceTaskId': task['id'], 'routingMode': task['result']['routingMode'], 'calibrationYear': 2024,
                   'validationYearsExcludedFromSearch': [2025, 2026]}})['id']
            write_json(args.report, report)
    if 'referenceScheme' not in report:
        report['referenceScheme'] = api('schemes', {'name': 'v0.5湖位参考 · 完整历史复算', 'payload': {
                 **request, 'parameters': reference['parameters'], 'routingMode': reference['routingMode'],
                 'name': 'v0.5湖位参考复算'}})['id']
    report.update(status='complete', completedAt=now(), modelYear2026Available=False)
    write_json(args.report, report)
    print(json.dumps({'status': 'complete', 'selection': report['selection'], 'tasks': report['tasks']}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
