"""Run the registered real historical chain through the desktop service."""
import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import now, read_json, write_json


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--url', default='http://127.0.0.1:8879')
    p.add_argument('--runoff-budget', type=int, default=120)
    p.add_argument('--lake-budget', type=int, default=30)
    p.add_argument('--report', type=Path, default=ROOT / 'docs/historical-results-v0.4.0.json')
    args = p.parse_args()
    session = requests.Session()
    session.trust_env = False

    def api(path, payload=None):
        url = args.url + '/api/model/' + path
        response = session.get(url, timeout=120) if payload is None else session.post(url, json=payload, timeout=120)
        response.raise_for_status()
        return response.json()

    boot = api('bootstrap')
    assert boot['projectId'] == 'selinco' and boot['annualValidation']['ready']
    source = next(i for i in boot['inputs'] if i.get('compatible', True) and
                  i['manifest']['start'] <= '2023-01-01T00:00:00' and
                  i['manifest']['end'] >= '2026-01-01T00:00:00')
    report = read_json(args.report, {'createdAt': now(), 'inputId': source['id'], 'tasks': {}})
    if report['inputId'] != source['id']:
        raise ValueError('Existing report belongs to another input; choose a new report path.')
    request = {'inputId': source['id'], 'start': '2023-01-01T00:00:00',
               'end': '2026-01-01T00:00:00', 'seed': 42, 'calibrationYear': 2024}

    def execute(stage, payload):
        identifier = report['tasks'].get(stage)
        task = api('tasks/' + identifier) if identifier else api('tasks', payload)
        report['tasks'][stage] = task['id']
        write_json(args.report, report)
        last = None
        while task['status'] in ('queued', 'running', 'stopping'):
            message = (task['status'], task.get('message', ''))
            if message != last:
                print(json.dumps({'stage': stage, 'id': task['id'], 'status': task['status'],
                                  'progress': task.get('progress'), 'message': message[1]}, ensure_ascii=True), flush=True)
                last = message
            time.sleep(3)
            task = api('tasks/' + task['id'])
        if task['status'] != 'succeeded':
            raise RuntimeError(task.get('message', task['status']))
        result = task['result']
        assert result['inputId'] == source['id']
        assert len(result['daily']) == 1096 and all(r['forcingHours'] == 24 for r in result['daily'])
        assert set(result['metrics']) == {'2023', '2024', '2025'}
        report[stage] = {k: result[k] for k in ('metrics', 'parameters', 'identity', 'diagnostics')}
        if result.get('calibration'):
            report[stage]['search'] = {k: v for k, v in result['calibration'].items() if k != 'trace'}
        write_json(args.report, report)
        api('tasks/' + task['id'] + '/metadata', {'collection': '完整历史', 'favorite': True})
        print(json.dumps({'stage': stage, 'status': 'succeeded', 'id': task['id'], 'metrics': result['metrics']}, ensure_ascii=True), flush=True)
        return task

    baseline = execute('baseline', {**request, 'kind': 'simulation', 'name': '完整历史 · 未率定基线 · 2023预热／2024／2025'})
    runoff = execute('runoff', {**request, 'kind': 'calibration', 'stage': 'runoff',
                             'maxEvaluations': args.runoff_budget, 'name': '完整历史 · 2024杜加产汇流率定（初轮）'})
    lake = execute('lake', {**request, 'kind': 'calibration', 'stage': 'lake', 'baseTaskId': runoff['id'],
                         'maxEvaluations': args.lake_budget, 'name': '完整历史 · 分步率定与2025独立验证（初轮）'})
    a, b = runoff['result']['parameters'], lake['result']['parameters']
    assert all(a[k] == b[k] for k in a if k != 'lakeCoefficient')
    report['parameterVersion'] = api('parameters', {'name': '2024分步率定冻结参数 · 初轮',
          'payload': {'parameters': b, 'sourceTaskId': lake['id'], 'calibrationYear': 2024,
                      'validationYearsExcludedFromSearch': [2025, 2026]}})['id']
    report['scheme'] = api('schemes', {'name': '完整历史 · 冻结参数复算（2023预热—2025验证）',
          'payload': {**request, 'parameters': b, 'name': '完整历史冻结参数复算'}})['id']
    report.update(completedAt=now(), status='complete', dayCount=1096,
                  futureForcingAvailable=False, calibrationAndValidationSeparated=True,
                  note='First bounded search; numerical completion does not imply operational predictive skill.')
    write_json(args.report, report)
    print(json.dumps({'status': 'complete', 'tasks': report['tasks'], 'report': str(args.report)}, ensure_ascii=True), flush=True)
    session.close()


if __name__ == '__main__':
    main()
