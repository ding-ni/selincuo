"""用同一真实冻结输入重复小预算搜索，不连接任务队列、不改生产库。"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import platform
import sys
import time
from types import SimpleNamespace
import uuid

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import WORKSPACE, digest, read_json, sha_file
from backend.dual_calibration import run_selection
from backend.grid_simulation import model_identity, prepare
from backend.lake_constrained_calibration import _read_series, _rows

TASK = 'JOB-05c3f62282ce4239aed1'
SOURCE = WORKSPACE / 'tasks' / TASK / 'request.json'
EVIDENCE = ROOT / 'docs/evidence/连续双目标数值复现核验-20261030.json'


def finite_difference(a, b):
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if a.shape != b.shape:
        raise AssertionError('逐日数组形状不一致')
    mask_a, mask_b = np.isfinite(a), np.isfinite(b)
    if not np.array_equal(mask_a, mask_b):
        raise AssertionError('逐日过程有效值掩码不一致')
    return float(np.max(np.abs(a[mask_a] - b[mask_a]))) if mask_a.any() else 0.


def numeric_leaves(value, prefix=''):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from numeric_leaves(child, prefix + '/' + str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from numeric_leaves(child, prefix + '/' + str(index))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield prefix, float(value)


def numeric_difference(a, b):
    left, right = dict(numeric_leaves(a)), dict(numeric_leaves(b))
    if left.keys() != right.keys():
        raise AssertionError('指标字段不一致')
    return max((abs(left[k] - right[k]) for k in left), default=0.)


def save_exclusive(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write('\n')


def main():
    if EVIDENCE.exists():
        raise FileExistsError('已有核验记录，不覆盖')
    started = time.monotonic()
    original = read_json(SOURCE)
    request = deepcopy(original)
    request['twoStage'].update(budgets={'runoff': 18, 'snow': 18, 'routing': 12, 'joint': 12},
        allowExtension=False, candidateLimit=8, jointBudget=6)
    actual_changes = {key for key in original['twoStage'] if original['twoStage'][key] != request['twoStage'][key]}
    assert actual_changes == {'budgets', 'allowExtension', 'candidateLimit', 'jointBudget'}
    assert request['_modelIdentity'] == model_identity()
    folder = ROOT / 'build' / ('真实输入缩额率定复现-' + uuid.uuid4().hex[:12])
    folder.mkdir()
    save_exclusive(folder / '缩额请求.json', request)
    db = SimpleNamespace(workspace=WORKSPACE)
    source = prepare(db, request['inputId'], request['spatialVersion'])
    selections, roots = [], []
    for index in (1, 2):
        joint = folder / ('第%d次' % index) / 'joint'
        print('开始第%d次' % index, flush=True)
        last = [0.]
        def progress(percent, message):
            now = time.monotonic()
            if now - last[0] > 30:
                print('%s %.1f%% %s' % (datetime.now().isoformat(timespec='seconds'), percent, message), flush=True)
                last[0] = now
        selections.append(run_selection(db, deepcopy(request), source, joint, progress, lambda: False))
        roots.append(joint)
    first, second = selections
    candidate_checks = {}
    for phase in ('training', 'all-data'):
        path = Path(phase) / 'joint'
        pools = [read_json(root / path / 'candidate-pool.json')['candidateIds'] for root in roots]
        basins = [[row['candidateId'] for row in _rows(root / path / 'lake-candidates.jsonl')] for root in roots]
        refine = [[row['candidateId'] for row in _rows(root / path / 'search-trace.jsonl')] for root in roots]
        flow = [[(row['evaluation'], row['parameters'], row['objective'])
                 for row in _rows(root / phase / 'flow' / 'search-trace.jsonl')] for root in roots]
        candidate_checks[phase] = {'initialBasinCandidates': len(pools[0]),
            'basinCandidateOrderEqual': pools[0] == pools[1],
            'computedBasinOrderEqual': basins[0] == basins[1],
            'jointProposalOrderEqual': refine[0] == refine[1],
            'waterSearchTraceEqual': flow[0] == flow[1]}
    series = {}
    for phase, key in [('training', 'trainingSeries'), ('all-data', 'finalSeries')]:
        frames = [_read_series(root, selection[key], request['_observations']['level'])
                  for root, selection in zip(roots, selections)]
        if not frames[0].index.equals(frames[1].index):
            raise AssertionError('逐日日期不一致')
        deltas = {name: finite_difference(frames[0][name], frames[1][name])
                  for name in ('duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total')}
        lakes = [frame.baseLevel + (selection['parameters']['lakeCoefficient'] - request['parameters']['lakeCoefficient']) * frame.slope
                 for frame, selection in zip(frames, selections)]
        # Split selection has its own fitted coefficient, distinct from final.
        if phase == 'training':
            lakes = [frame.baseLevel + (selection['trainingParameters']['lakeCoefficient'] - request['parameters']['lakeCoefficient']) * frame.slope
                     for frame, selection in zip(frames, selections)]
        series[phase] = {'days': len(frames[0]),
                         'validFlowDays': int(np.isfinite(frames[0]['duja']).sum()),
                         'validLakeDays': int(np.isfinite(lakes[0]).sum()),
                         'flowMaxAbsoluteDifferenceM3s': deltas,
                         'lakeMaxAbsoluteDifferenceM': finite_difference(*lakes)}
    metrics = numeric_difference({'flow': first['flowMetrics'], 'lake': first['allDataEvaluation'],
        'fits': [{'training': x['training'], 'validation': x['validation']} for x in first['flowFits']]},
        {'flow': second['flowMetrics'], 'lake': second['allDataEvaluation'],
         'fits': [{'training': x['training'], 'validation': x['validation']} for x in second['flowFits']]})
    parameter_diff = numeric_difference(first['parameters'], second['parameters'])
    counts = lambda s: {'generated': s['totalGenerated'], 'basinCandidates': s['basinCandidatesEvaluated'],
        'jointEvaluations': s['refinement']['evaluations'], 'selectedForBasin': s['selectedForBasin'],
        'byPhase': [{k: row[k] for k in ('phase', 'generated', 'selectedForBasin', 'basinCandidatesEvaluated')}
                    | {'jointEvaluations': row['refinement']['evaluations']} for row in s['phases']]}
    checks = {'parametersEqual': parameter_diff == 0, 'selectedIdsEqual':
        (first['trainingCandidateId'], first['finalCandidateId']) == (second['trainingCandidateId'], second['finalCandidateId']),
        'countsEqual': counts(first) == counts(second), 'metricMaxAbsoluteDifference': metrics,
        'parameterMaxAbsoluteDifference': parameter_diff, 'candidateOrder': candidate_checks, 'dailySeries': series}
    passed = (checks['parametersEqual'] and checks['selectedIdsEqual'] and checks['countsEqual'] and metrics <= 1e-8
        and all(all(value for key, value in case.items() if key.endswith('Equal')) for case in candidate_checks.values())
        and all(max(case['flowMaxAbsoluteDifferenceM3s'].values()) <= 1e-7 and case['lakeMaxAbsoluteDifferenceM'] <= 1e-8
                for case in series.values()))
    report = {'scope': '真实冻结暖雨输入，两次独立重复两期小预算数值选择；没有重复正式18000次预算、生成逐时工件或重跑预热诊断。',
        'passed': passed, 'startedFromTask': TASK, 'sourceRequestSHA256': sha_file(SOURCE),
        'testRequest': str((folder / '缩额请求.json').relative_to(ROOT)).replace('\\', '/'),
        'testRequestSHA256': sha_file(folder / '缩额请求.json'),
        'unchangedExcept': sorted(actual_changes), 'inputId': request['inputId'],
        'inputDataSHA256': source['manifest']['dataSHA256'],
        'inputTransformationSHA256': source['manifest']['transformationSHA256'],
        'spatialVersion': request['spatialVersion'], 'modelImplementation': request['_modelIdentity'],
        'sourceCodeSHA256': sha_file(ROOT / 'backend/dual_calibration.py'),
        'observationSHA256': digest(request['_observations']),
        'seeds': request['twoStage']['seeds'], 'budgetsPerPhase': request['twoStage']['budgets'],
        'candidateLimitPerPhase': 8, 'jointBudgetPerPhase': 6, 'counts': counts(first),
        'checks': checks, 'python': platform.python_version(), 'numpy': np.__version__,
        'pandas': pd.__version__, 'elapsedSeconds': round(time.monotonic() - started, 2)}
    save_exclusive(EVIDENCE, report)
    print(json.dumps({'evidence': str(EVIDENCE), 'passed': passed, 'elapsedSeconds': report['elapsedSeconds'],
                      'counts': report['counts'], 'checks': checks}, ensure_ascii=False), flush=True)
    if not passed:
        raise AssertionError('缩额率定复现核验未通过')


if __name__ == '__main__':
    main()
