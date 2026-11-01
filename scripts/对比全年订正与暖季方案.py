"""Read-only, common-date comparison of monthly-total and warm-season runs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import uuid

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend.common import ROOT, WORKSPACE, digest, now, read_json, sha_file
from 对比双驱动完整率定 import budget, common_pairs, flow_report, lake_report, warmup


RUNS = {
    'warmSeason': 'JOB-05c3f62282ce4239aed1',
    'monthlyTotal': 'JOB-d3a61eeca6a34a228897',
}


def _compact_fit(value):
    if not isinstance(value, dict):
        return value
    return {key: value.get(key) for key in (
        'count', 'nse', 'kge', 'rmse', 'pbias', 'bias', 'score',
        'trendErrorM', 'amplitudeErrorM', 'peakTimingErrorDays',
        'observedTrendMPerYear', 'simulatedTrendMPerYear') if key in value}


def _contract(a, b):
    ignored = {'inputId', 'parameters', 'twoStage', '_modelIdentity',
               'calibrationConfigurationSHA256', 'researchSchemeId'}
    fields = ('start', 'end', 'spatialVersion', 'partitionParameters',
              'initialHydrology', '_fixedInitialization', 'parameterTransfer',
              'routingMode', 'periods', 'search', '_observations')
    checks = {key: digest(a.get(key)) == digest(b.get(key)) for key in fields}
    a_parameters, b_parameters = a.get('parameters') or {}, b.get('parameters') or {}
    hydro_keys = sorted(set(a_parameters) | set(b_parameters))
    checks['hydrologyInitialParameters'] = all(
        digest(a_parameters.get(key)) == digest(b_parameters.get(key))
        for key in hydro_keys if key != 'lakeCoefficient')
    ta, tb = dict(a.get('twoStage') or {}), dict(b.get('twoStage') or {})
    ta.pop('baselineTaskId', None)
    tb.pop('baselineTaskId', None)
    checks['twoStageSettingsExceptBaselineReference'] = digest(ta) == digest(tb)
    return {
        'sameScientificSettingsExceptForcingAndLakeStageSeed': all(checks.values()),
        'checks': checks,
        'differences': {
            'inputId': [a.get('inputId'), b.get('inputId')],
            'lakeCoefficientInitialValue': [a_parameters.get('lakeCoefficient'), b_parameters.get('lakeCoefficient')],
            'baselineTaskId': [(a.get('twoStage') or {}).get('baselineTaskId'),
                               (b.get('twoStage') or {}).get('baselineTaskId')],
            'modelIdentity': [a.get('_modelIdentity'), b.get('_modelIdentity')],
        },
        'hydrologyInitialParameterKeysCompared': len([key for key in hydro_keys if key != 'lakeCoefficient']),
    }


def _run(task_id, db):
    status_row = db.execute('SELECT id,kind,status,progress,message,updated_at,request,result FROM tasks WHERE id=?',
                            (task_id,)).fetchone()
    if not status_row:
        raise ValueError('率定任务不存在：' + task_id)
    status = dict(status_row)
    if status['status'] != 'succeeded' or status['kind'] != 'two-stage-calibration':
        raise ValueError('率定任务尚未成功完成：' + task_id)
    folder = WORKSPACE / 'tasks' / task_id
    request_path, result_path = folder / 'request.json', folder / 'result.json'
    request, result = read_json(request_path), read_json(result_path)
    if not request or not result or result.get('taskId') != task_id:
        raise ValueError('任务请求或成果文件缺失/身份不符：' + task_id)
    if request.get('inputId') != result.get('inputId'):
        raise ValueError('任务请求与结果使用的气象输入不一致：' + task_id)
    if json.loads(status['request']) != request or json.loads(status['result']) != result:
        raise ValueError('SQLite记录与任务冻结文件不一致：' + task_id)
    frame = pd.DataFrame(result['daily'])
    frame.index = pd.to_datetime(frame['date'])
    if frame.index.has_duplicates:
        raise ValueError('任务日过程存在重复日期：' + task_id)
    for key in ('duja', 'observedFlow', 'lakeLevel', 'observedLevel'):
        frame[key] = pd.to_numeric(frame[key], errors='coerce')
    manifest_path = WORKSPACE / 'inputs/products' / request['inputId'] / 'manifest.json'
    manifest = read_json(manifest_path)
    if not manifest:
        raise ValueError('任务引用的气象输入清单缺失：' + task_id)
    workflow = result.get('calibrationWorkflow') or {}
    selection = workflow.get('selection') or {}
    lake = workflow.get('lake') or {}
    fit_rows = []
    for fit in workflow.get('flowFits') or []:
        fit_rows.append({key: fit.get(key) for key in (
            'fit', 'trainingPeriod', 'validationPeriod', 'evaluations',
            'hydrologyEvaluations', 'jointEvaluations', 'converged', 'termination')
            if key in fit} | {
                'training': _compact_fit(fit.get('training')),
                'validation': _compact_fit(fit.get('validation'))})
    return {
        'taskId': task_id, 'status': {key: status[key] for key in
            ('status', 'progress', 'message', 'updated_at')},
        'requestPath': request_path.relative_to(ROOT).as_posix(),
        'requestSHA256': sha_file(request_path),
        'resultPath': result_path.relative_to(ROOT).as_posix(),
        'resultSHA256': sha_file(result_path),
        'scheme': {key: result.get('scheme', {}).get(key) for key in
                   ('id', 'name', 'purpose', 'familyId', 'revision')},
        'input': {'id': manifest['id'], 'dataSHA256': manifest['dataSHA256'],
                  'landCorrection': manifest.get('landPrecipitationCorrection'),
                  'lakeCorrection': manifest.get('lakePrecipitationCorrection')},
        'calibration': result.get('calibration'),
        'search': {key: selection.get(key) for key in (
            'selectionMode', 'algorithm', 'totalGenerated', 'uniqueGenerated',
            'totalRescored', 'selectedForBasin', 'basinCandidatesEvaluated',
            'refinement', 'flowMetrics', 'allDataEvaluation',
            'trainingUsesValidationLake', 'trainingUsesValidationFlow',
            'finalUsesAllLakeObservations', 'formalParity')},
        'flowFits': fit_rows,
        'lakeFits': {
            label: {key: fit.get(key) for key, value in (lake.get(label) or {}).items()
                    if key in ('algorithm', 'parameter', 'initial', 'bounds', 'value',
                               'unconstrainedValue', 'boundary', 'converged', 'termination',
                               'trainingPeriod', 'validationPeriod', 'jointScore')}
                    | {'training': _compact_fit((lake.get(label) or {}).get('training')),
                       'validation': _compact_fit((lake.get(label) or {}).get('validation'))}
            for label in ('split', 'final') if isinstance(lake.get(label), dict)},
        'parameters': result['parameters'],
        'annualStoredMetrics': {year: result['metrics'].get(year) for year in
                                ('2024', '2025', '2026')},
        'warmup': warmup(result),
        'waterBudget': budget(frame, result['initialization']),
        'initialization': result['initialization'],
        'spatialVersion': result['spatialVersion'],
        'daily': frame,
        'request': request,
    }


def build_report(workspace=WORKSPACE):
    global WORKSPACE
    WORKSPACE = Path(workspace).resolve()
    db_path = WORKSPACE / 'database/hydrology.sqlite3'
    with sqlite3.connect('file:' + db_path.as_posix() + '?mode=ro', uri=True, timeout=30) as db:
        db.row_factory = sqlite3.Row
        runs = {label: _run(task_id, db) for label, task_id in RUNS.items()}
        adoption_row = db.execute("SELECT value FROM settings WHERE key='business-adopted-scheme-v1'").fetchone()
        adoption = json.loads(adoption_row['value']) if adoption_row else None
    frames = {label: run['daily'] for label, run in runs.items()}
    anchors = {label: pd.Timestamp(run['initialization']['time']).normalize()
               for label, run in runs.items()}
    flow, flow_date_conflicts = common_pairs(frames, 'observedFlow', 'duja')
    lake, lake_date_conflicts = common_pairs(frames, 'observedLevel', 'lakeLevel', set(anchors.values()))
    if len(set(anchors.values())) != 1:
        raise ValueError('两次计算的湖位起算日期不同')
    comparison = {
        'sameLakeAnchorDay': True,
        'commonFlowDays': int(len(flow)),
        'differentFlowObservationDatesExcluded': flow_date_conflicts,
        'commonLakeDays': int(len(lake)),
        'differentLakeObservationDatesExcluded': lake_date_conflicts,
        'flow': {label: flow_report(flow, label) for label in frames},
        'lake': {label: lake_report(lake, label, runs[label]['initialization']) for label in frames},
    }
    objective = {label: {
        'bestObjective': (run['calibration'] or {}).get('bestObjective'),
        'objectiveProfile': (run['calibration'] or {}).get('objectiveProfile'),
        'converged': (run['calibration'] or {}).get('converged'),
        'termination': (run['calibration'] or {}).get('termination'),
        'lakeAllDataScore': ((run['search'] or {}).get('allDataEvaluation') or {}).get('score'),
    } for label, run in runs.items()}
    warm = runs['warmSeason']
    monthly = runs['monthlyTotal']
    request_contract = _contract(warm['request'], monthly['request'])
    if not request_contract['sameScientificSettingsExceptForcingAndLakeStageSeed']:
        raise ValueError('两次任务的主要训练/验证设置不同，不能宣称只比较了降水输入')
    adopted_is_warm = bool(adoption and adoption.get('resultId') == RUNS['warmSeason']
                           and adoption.get('familyId') == warm['scheme']['familyId'])
    report = {
        'createdAt': now(),
        'comparisonType': 'sameDates-two-completed-full-recalibrations',
        'basis': '相同440个杜加实测流量日与1304个湖位实测日；同空间、同起算时刻、同搜索范围/目标/预算；只读读取已完成任务。',
        'requestContract': request_contract,
        'tasks': {label: {key: value for key, value in run.items()
                          if key not in ('daily', 'request')}
                  for label, run in runs.items()},
        'adoption': {'current': adoption, 'warmSeasonStillAdopted': adopted_is_warm,
                     'adoptedResultMatchesWarmTask': bool(adoption and adoption.get('resultId') == RUNS['warmSeason']),
                     'adoptedSchemeFamilyMatchesWarmTask': bool(adoption and adoption.get('familyId') == warm['scheme']['familyId']),
                     'taskSchemeIdIsRevision': warm['scheme']['id'] != (adoption or {}).get('schemeId'),
                     'monthlyCandidateAutomaticallyAdopted': bool(adoption and adoption.get('resultId') == RUNS['monthlyTotal'])},
        'objectiveComparison': objective,
        'comparison': comparison,
        'decision': {
            'adoptedResultId': adoption.get('resultId') if adoption else None,
            'candidateResultId': RUNS['monthlyTotal'],
            'adoptMonthlyTotal': False,
            'reason': [
                '月总量订正的杜加流量NSE、KGE下降且RMSE上升。',
                '共同湖位日RMSE上升，全年方案的预热/初始状态核验未通过。',
                '全年方案四项湖位综合分略优，但差异很小，不能抵消流量、湖位RMSE和预热表现的退步。',
                '两方案联合搜索均用尽预算且未收敛；本比较不代表外部独立验证。',
            ],
        },
        'limitations': [
            '2025资料参与率定时间检验，2025—2026资料参与订正、开发和方案筛选，不称独立验证。',
            '全年月订正仅在有充分站点资料的月份应用；其余月份保留ERA原值。仪器类型未知，本轮按总降水水当量假设试验。',
            '总流域空间仍按现行研究计算范围，未解决的湖周只作背景。',
        ],
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=WORKSPACE)
    parser.add_argument('--write', action='store_true', help='独占创建比较证据JSON')
    args = parser.parse_args()
    report = build_report(args.workspace)
    path = None
    if args.write:
        path = ROOT / 'docs/evidence' / ('全年订正与暖季方案公平对比-20261101-' + uuid.uuid4().hex[:10] + '.json')
        with path.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
    result = {'report': str(path.relative_to(ROOT)) if path else None,
        'allTasksSucceeded': all(row['status']['status'] == 'succeeded' for row in report['tasks'].values()),
        'adoptedResultId': report['adoption']['current']['resultId'],
        'warmSeasonStillAdopted': report['adoption']['warmSeasonStillAdopted'],
        'commonFlowDays': report['comparison']['commonFlowDays'],
        'commonLakeDays': report['comparison']['commonLakeDays'],
        'flow': {label: values['all'] for label, values in report['comparison']['flow'].items()},
        'lake': {label: {key: values['all'].get(key) for key in
                         ('count', 'rmse', 'nse', 'kge', 'score')}
                 for label, values in report['comparison']['lake'].items()},
        'warmupPass': {label: row['warmup'].get('allComparisonsPass')
                       for label, row in report['tasks'].items()}}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
