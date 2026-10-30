"""只读核验本轮已完成任务的逐时工件、杜加汇总与水量守恒，不运行模型。"""
import argparse
import json
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, WORKSPACE, now, read_json, sha_file
from scripts.核验湖位联合成果 import _aggregation_audit, _audit_hourly, _frame, _water_budget


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True,
                        choices=['JOB-6536f36e07cc46868255', 'JOB-05c3f62282ce4239aed1'])
    args = parser.parse_args()
    database = WORKSPACE / 'database/hydrology.sqlite3'
    with sqlite3.connect('file:' + database.as_posix() + '?mode=ro', uri=True, timeout=30) as connection:
        status = connection.execute('SELECT status FROM tasks WHERE id=?', (args.task,)).fetchone()
    if status != ('succeeded',):
        raise SystemExit('该正式任务尚未成功，暂不核验最终工件。')
    folder = WORKSPACE / 'tasks' / args.task
    result = read_json(folder / 'result.json')
    request = read_json(folder / 'request.json')
    if result['taskId'] != args.task or result['inputId'] != request['inputId']:
        raise ValueError('请求与成果身份不一致')
    frame = _frame(result)
    hourly = _audit_hourly(folder, frame, result)
    aggregation = _aggregation_audit(result, hourly)
    budget = _water_budget(frame, result['initialization'])
    total = budget['total']
    balance_error = total['lakeDeltaVolumeM3'] - (
        total['lakeInflowM3'] + total['lakePrecipitationM3'] - total['lakeEvaporationM3'])
    if abs(balance_error) > .05:
        raise ValueError('完整日湖泊水量账不闭合：' + str(balance_error))
    parity = result['calibrationWorkflow']['selection']['formalParity']
    if any(value > (1e-8 if key == 'lakeLevel' else 1e-7) for key, value in parity.items()):
        raise ValueError('最终参数快速与完整计算超过约定容差')
    diagnostics = {key: value for key, value in result['diagnostics'].items() if key != 'partitions'}
    report = {'createdAt': now(), 'taskId': args.task, 'inputId': result['inputId'],
              'scope': '对完成成果只读核验；不新增数值模拟或改写工件。',
              'requestSHA256': sha_file(folder / 'request.json'),
              'resultSHA256': sha_file(folder / 'result.json'),
              'hourly': hourly, 'aggregation': aggregation, 'formalParity': parity,
              'diagnostics': diagnostics, 'completeDayBalanceErrorM3': balance_error,
              'waterBudget': budget}
    path = ROOT / 'docs/evidence' / ('双驱动逐时成果核验-' + args.task + '-' + uuid.uuid4().hex[:8] + '.json')
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'path': str(path), 'sha256': sha_file(path), 'taskId': args.task,
                      'files': hourly['manifestFiles'], 'hours': hourly['hourlyHours'],
                      'riverSumDifferenceM3s': hourly['maximumTotalFlowSummationDifferenceM3s'],
                      'dujaIncludedOnceVolumeErrorM3': aggregation['dujaIncludedOnceVolumeErrorM3'],
                      'completeDayBalanceErrorM3': balance_error}, ensure_ascii=False))


if __name__ == '__main__':
    main()
