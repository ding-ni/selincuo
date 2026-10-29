"""Submit/resume the approved lake-constrained workflow through the live service."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import time

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, read_json, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8882')
    parser.add_argument('--record', default='build/湖位联合率定-20261029/启动记录.json')
    parser.add_argument('--warmup-start', default='2019-01-01')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    record_path = ROOT / args.record
    record = read_json(record_path)
    session = requests.Session()

    def api(route, payload=None):
        url = args.url.rstrip('/') + '/api/model/' + route
        response = session.get(url, timeout=120) if payload is None else session.post(url, json=payload, timeout=120)
        if not response.ok:
            raise RuntimeError(f'{response.status_code}: {response.text[:1200]}')
        return response.json()

    if record is None:
        value = api('research-template')
        value.update(name='色林错流量与湖位联合优选', purpose='business', target='duja')
        value['periods']['warmupStart'] = args.warmup_start + 'T00:00:00'
        value['initialization'] = {'mode': 'automatic'}
        value['partitionParameters'] = {}
        value['calibrationInitialPartitionParameters'] = {}
        value['calibrationInitialParameters'] = deepcopy(value['parameters'])
        value['twoStage'].update(mode='both', selectionMode='flow-constrained-lake',
            candidateSourceTaskId='JOB-4934a5d35c9344a59353', candidateLimit=256, jointBudget=2000,
            flowConstraints={'minNse': .70, 'maxAbsPbias': 5.0}, seeds=[42, 142, 242])
        for key in ('id', 'familyId', 'revision', 'parentRevisionId', 'configurationSHA256', 'adoptedParameterId', 'resultId', 'builtin'):
            value.pop(key, None)
        scheme = api('schemes', {'payload': value})
        record = {'schemeId': scheme['id'], 'warmupStart': value['periods']['warmupStart'],
                  'candidateSourceTaskId': value['twoStage']['candidateSourceTaskId']}
        write_json(record_path, record)
    if args.prepare_only:
        print(json.dumps(record, ensure_ascii=True), flush=True)
        return
    if record.get('taskId'):
        task = api('tasks/' + record['taskId'])
        if task['status'] in ('failed', 'cancelled', 'interrupted'):
            task = api('tasks/' + task['id'] + '/resume', {})
    else:
        task = api('tasks', {'kind': 'two-stage-calibration', 'researchSchemeId': record['schemeId'],
            'name': '色林错湖位联合优选 · ' + record['warmupStart'][:4] + '预热 / 2021—2026连续模拟',
            'adoptOnSuccess': True, 'clientRequestId': 'approved-flow-constrained-lake-20261029-v1'})
        record['taskId'] = task['id']
        write_json(record_path, record)
    marker = None
    while True:
        state = (task['status'], task.get('message'))
        if state != marker:
            print(task['id'], task['status'], round(task.get('progress', 0), 2), task.get('message'), flush=True)
            marker = state
        if task['status'] in ('succeeded', 'failed', 'cancelled', 'interrupted'):
            break
        time.sleep(10)
        task = api('tasks/' + task['id'])
    # Completion and automatic adoption are two consecutive persisted writes.
    deadline = time.monotonic() + 120
    while task['status'] == 'succeeded' and task['request'].get('adoptOnSuccess') and not any(
            key in (task.get('result') or {}) for key in ('adoption', 'adoptionSkipped', 'adoptionError')):
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
        task = api('tasks/' + task['id'])
    record.update(status=task['status'], message=task.get('message'))
    if task.get('result'):
        result = task['result']
        record.update(parameters=result.get('parameters'), metrics=result.get('metrics'),
                      calibrationWorkflow=result.get('calibrationWorkflow'), adoption=result.get('adoption'),
                      adoptionSkipped=result.get('adoptionSkipped'), adoptionError=result.get('adoptionError'))
    write_json(record_path, record)
    print(json.dumps({'taskId': record['taskId'], 'status': record['status'],
                      'adoption': record.get('adoption'), 'adoptionSkipped': record.get('adoptionSkipped')}, ensure_ascii=True), flush=True)
    if task['status'] != 'succeeded':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
