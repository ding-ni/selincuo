"""Compare completed frozen outcomes, without running or adopting any model.

Default invocation prints a compact status. --write saves a uniquely named
evidence file; incomplete tasks are recorded as missing, never as results.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, WORKSPACE, digest, now, read_json, sha_file
from backend.fast_calibration import array_metrics
from backend.lake_calibration import assess, freeze_window, joint_assess

TASKS = {
    'previous': 'JOB-88b35b44b27d4e15840c',
    'baseline_recalibrated': 'JOB-6536f36e07cc46868255',
    'warm_rain_recalibrated': 'JOB-05c3f62282ce4239aed1',
}


def task_statuses():
    path = WORKSPACE / 'database/hydrology.sqlite3'
    with sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True, timeout=30) as db:
        db.row_factory = sqlite3.Row
        return {row['id']: dict(row) for row in db.execute(
            'SELECT id,status,progress,message,updated_at FROM tasks WHERE id IN (?,?,?)',
            tuple(TASKS.values()))}


def finished_task(task_id, statuses):
    status = statuses.get(task_id)
    if not status:
        return {'taskId': task_id, 'status': 'missing', 'missing': '数据库无该任务'}, None
    entry = {'taskId': task_id, **{k: status[k] for k in ('status', 'progress', 'message', 'updated_at')}}
    if status['status'] != 'succeeded':
        entry['missing'] = '任务尚未成功，不读取尚在写入的结果'
        return entry, None
    folder = WORKSPACE / 'tasks' / task_id
    result_path, request_path = folder / 'result.json', folder / 'request.json'
    if not result_path.is_file() or not request_path.is_file():
        entry['missing'] = '任务已成功但结果或请求文件缺失'
        return entry, None
    result, request = read_json(result_path), read_json(request_path)
    if not isinstance(result, dict) or result.get('taskId') != task_id:
        raise ValueError(task_id + ' 的结果身份不符')
    if not isinstance(request, dict) or request.get('inputId') != result.get('inputId'):
        raise ValueError(task_id + ' 的请求与结果输入身份不符')
    config_sha = request.get('calibrationConfigurationSHA256')
    config = {'sha256': config_sha}
    if config_sha:
        if len(config_sha) != 64 or any(c not in '0123456789abcdef' for c in config_sha):
            raise ValueError(task_id + ' 的率定配置摘要格式无效')
        config_path = WORKSPACE / 'calibration-configurations' / (config_sha + '.json')
        config['path'] = config_path.relative_to(ROOT).as_posix()
        if config_path.is_file():
            content = read_json(config_path)['content']
            config.update(contentDigestMatches=digest(content) == config_sha,
                          fileSHA256=sha_file(config_path),
                          schemeRevision=request.get('schemeSnapshot', {}).get('revision'),
                          schemeConfigurationSHA256=request.get('schemeSnapshot', {}).get('configurationSHA256'))
        else:
            config['missing'] = '任务声明的冻结配置文件不存在'
    else:
        config['missing'] = '历史任务未绑定可执行率定配置摘要'
    frame = pd.DataFrame(result['daily'])
    frame.index = pd.to_datetime(frame['date'])
    if frame.index.has_duplicates:
        raise ValueError(task_id + ' 含重复日')
    for key in ('duja', 'observedFlow', 'lakeLevel', 'observedLevel'):
        frame[key] = pd.to_numeric(frame[key], errors='coerce')
    entry.update(resultPath=result_path.relative_to(ROOT).as_posix(), resultSHA256=sha_file(result_path),
        requestPath=request_path.relative_to(ROOT).as_posix(), requestSHA256=sha_file(request_path),
        inputId=result['inputId'], spatialVersion=result['spatialVersion'],
        start=result['start'], end=result['end'], initialization=result['initialization'],
        initialHydrology=result.get('initialHydrology'), initialParameters=request['parameters'],
        formalParameters=result['parameters'],
        executableConfiguration=config,
        observationSnapshotSHA256=digest(request.get('_observations', {})),
        information=workflow(result, request, folder),
        warmup=warmup(result), waterBudget=budget(frame, result['initialization']))
    return entry, frame


def workflow(result, request, folder):
    workflow_data = result.get('calibrationWorkflow') or {}
    selection = workflow_data.get('selection') or {}
    opts = request.get('twoStage') or {}
    phases = []
    for row in selection.get('phases', []):
        refinement = row.get('refinement') or {}
        phases.append({key: row.get(key) for key in ('phase', 'label', 'candidateId', 'generated',
            'uniqueGenerated', 'selectedForBasin', 'basinCandidatesEvaluated',
            'jointObjective', 'flowObjective')} | {'refinement': {
                key: refinement.get(key) for key in ('evaluations', 'converged', 'termination')} | {
                    'starts': [{key: start.get(key) for key in ('seed', 'budget', 'evaluations',
                        'converged', 'termination', 'generations')} for start in refinement.get('starts', [])]},
                'fastParity': row.get('fastParity') or {'missing': '该阶段未记录快速与完整计算核对'}})
    generated = []
    for fit in selection.get('generationFits', []):
        stages = []
        for stage in fit.get('stages', []):
            stages.append({key: stage.get(key) for key in ('name', 'cycle', 'keys', 'budget', 'used',
                'bestObjective', 'converged')} | {'starts': [
                    {key: start.get(key) for key in ('seed', 'budget', 'evaluations',
                        'converged', 'termination', 'generations', 'bestObjective',
                        'populationFile', 'populationSHA256')} for start in stage.get('starts', [])]})
        generated.append({key: fit.get(key) for key in ('fit', 'algorithm', 'evaluations', 'extended',
            'converged', 'termination', 'nearBoundaryParameters', 'trainingPeriod',
            'validationPeriod', 'traceFile', 'traceSHA256')} | {'stages': stages})
    fits = []
    for fit in workflow_data.get('flowFits', []):
        fits.append({key: fit.get(key) for key in ('fit', 'algorithm', 'trainingPeriod', 'validationPeriod',
            'training', 'validation', 'evaluations', 'hydrologyEvaluations', 'jointEvaluations',
            'converged', 'termination', 'validationExcludedFromSearch')})
    prior = [{key: row.get(key) for key in ('fit', 'trainingPeriod', 'validationPeriod', 'training', 'validation')}
             for row in selection.get('priorFlowSplitChecks', [])]
    lake = workflow_data.get('lake') or {}
    lake_fits = {}
    for label in ('split', 'final'):
        record = lake.get(label) or {}
        lake_fits[label] = {key: record.get(key) for key in ('value', 'bounds', 'trainingPeriod',
            'validationPeriod', 'training', 'validation', 'jointScore', 'converged', 'termination')}
    traces = []
    for path in (folder / 'joint').rglob('search-trace.jsonl'):
        if path.is_file():
            traces.append({'path': path.relative_to(ROOT).as_posix(), 'sha256': sha_file(path)})
    bounds = request.get('search', {}).get('bounds') or {}
    final_values = result.get('parameters') or {}
    near_boundary, missing_boundary = [], []
    for name in [*(request.get('search', {}).get('keys') or []), 'lakeCoefficient']:
        if name not in bounds or name not in final_values:
            missing_boundary.append(name)
            continue
        lower, upper = bounds[name]
        value = final_values[name]
        if not upper > lower:
            missing_boundary.append(name)
            continue
        low_fraction = (value-lower)/(upper-lower)
        high_fraction = (upper-value)/(upper-lower)
        if min(low_fraction, high_fraction) <= .01:
            near_boundary.append({'parameter': name, 'value': value, 'bounds': [lower, upper],
                'nearestBoundary': 'lower' if low_fraction <= high_fraction else 'upper',
                'fractionOfRange': min(low_fraction, high_fraction)})
    return {'selectionMode': selection.get('selectionMode'), 'algorithm': selection.get('algorithm'),
        'searchParameters': request.get('search', {}).get('keys'),
        'bounds': request.get('search', {}).get('bounds'), 'periods': request.get('periods'),
        'lakePeriods': opts.get('lakePeriods'), 'objective': opts.get('objective'),
        'flowThresholdsApplied': selection.get('flowThresholdsApplied'),
        'targetBasedStopping': selection.get('targetBasedStopping'),
        'seeds': opts.get('seeds'), 'budgetsPerFit': opts.get('budgets'),
        'allowExtension': opts.get('allowExtension'), 'candidateLimitPerFit': opts.get('candidateLimit'),
        'jointBudgetPerFit': opts.get('jointBudget'),
        'actual': {key: selection.get(key) for key in ('totalGenerated', 'uniqueGenerated',
            'totalRescored', 'eligibleRescored', 'selectedForBasin', 'basinCandidatesEvaluated')},
        'refinement': {key: (selection.get('refinement') or {}).get(key)
                       for key in ('evaluations', 'converged', 'termination')},
        'phases': phases, 'generationFits': generated or {'missing': '历史任务未生成本轮分阶段搜索结果'},
        'flowFits': fits, 'historicalFlowSplitChecks': prior, 'lakeFits': lake_fits,
        'finalParametersNearBoundary': {'rule': '最终联合参数距上下限不超过范围宽度1%',
            'parameters': near_boundary, 'missing': missing_boundary},
        'formalParity': selection.get('formalParity') or {'missing': '最终参数快速与完整计算核对未记录'},
        'historicFastParity': selection.get('fastParity') if not phases else None,
        'finalFlowMetricsStored': selection.get('flowMetrics'),
        'lakeAllDataMetricsStored': selection.get('allDataEvaluation'),
        'trainingUsesValidationLake': selection.get('trainingUsesValidationLake'),
        'trainingUsesValidationFlow': selection.get('trainingUsesValidationFlow'),
        'finalUsesAllLakeObservations': selection.get('finalUsesAllLakeObservations'),
        'traceFiles': traces,
        'interpretation': '分期训练及检验取当时分期参数；正式参数的分年结果是全资料定参后的回代，不作为独立检验。'}


def warmup(result):
    data = result.get('warmupDiagnostics') or {}
    if not data:
        return {'missing': '结果没有预热核验'}
    summary = {key: data.get(key) for key in ('referenceStart', 'baselineCase', 'parametersSHA256',
        'comparisons', 'recentComparisonStart', 'recentComparisonPass',
        'initialConditionPass', 'allComparisonsPass', 'missingComparisons', 'basis')}
    summary['anchorStates'] = data.get('anchorStates') or {'missing': '无湖泊起算前后状态'}
    return summary


def budget(frame, anchor):
    data = frame.loc[frame.index > pd.Timestamp(anchor['time']).normalize()].copy()
    columns = ['lakeInflowM3', 'lakePrecipitationM3', 'lakeEvaporationM3',
               'lakeDeltaVolumeM3', 'lakeBalanceErrorM3']
    for column in columns:
        data[column] = pd.to_numeric(data[column], errors='coerce')
    def groups(freq, fmt):
        result = []
        for date, group in data.resample(freq):
            if group.empty:
                continue
            row = {'period': date.strftime(fmt), 'days': int(len(group))}
            row.update({name: float(group[name].sum(min_count=1))
                        if group[name].notna().any() else None for name in columns})
            result.append(row)
        return result
    monthly, annual = groups('MS', '%Y-%m'), groups('YS', '%Y')
    totals = {name: float(data[name].sum(min_count=1)) if data[name].notna().any() else None
              for name in columns}
    return {'basis': '北京时间完整日，自湖位起算日次日起；首尾月或年可能不完整',
        'cumulative': totals, 'monthly': monthly, 'annual': annual}


def common_pairs(frames, obs, sim, anchor_days=()):
    columns = []
    for label, frame in frames.items():
        columns.append(frame[[obs, sim]].rename(columns={obs: 'obs_'+label, sim: 'sim_'+label}))
    joined = pd.concat(columns, axis=1, join='inner').replace([np.inf, -np.inf], np.nan).dropna()
    joined = joined.loc[~joined.index.isin(anchor_days)]
    obs_columns = ['obs_'+label for label in frames]
    difference = (joined[obs_columns].max(axis=1)-joined[obs_columns].min(axis=1)).abs()
    different = int((difference > 1.e-8).sum())
    return joined.loc[difference <= 1.e-8].copy(), different


def flow_report(joined, label):
    obs = joined['obs_'+label]
    sim = joined['sim_'+label]
    result = {}
    for key, selected in [('all', joined), *[(str(year), joined[joined.index.year == year])
                                              for year in range(2021, 2027)]]:
        o, s = selected['obs_'+label].to_numpy(float), selected['sim_'+label].to_numpy(float)
        result[key] = array_metrics(o, s) if len(o) else {'count': 0, 'nse': None, 'kge': None,
            'rmse': None, 'pbias': None, 'pairedVolumeErrorM3': None}
    return result


def lake_report(joined, label, anchor):
    f = pd.DataFrame(index=joined.index)
    f['observedLevel'] = joined['obs_'+label]
    f['baseLevel'] = joined['sim_'+label]
    f['slope'] = 0.
    result = {}
    for key, selected in [('all', f), *[(str(year), f[f.index.year == year])
                                        for year in range(2021, 2027)]]:
        if selected.empty:
            result[key] = {'count': 0, 'rmse': None, 'seasonMetricsUnavailable': '无共同观测'}
            continue
        start, end = selected.index.min(), selected.index.max() + pd.Timedelta(days=1)
        row = assess(selected, 0., 0., start, end, anchor)
        observations = {date.isoformat(): float(value) for date, value in selected.observedLevel.items()}
        try:
            window = freeze_window(observations, start, end, anchor)
            row.update(joint_assess(selected, 0., 0., window))
        except ValueError as error:
            row['seasonMetricsUnavailable'] = str(error)
        result[key] = row
    return result


def _contract_field(request, path):
    value = request
    for item in path.split('.'):
        if not isinstance(value, dict) or item not in value:
            return {'missing': path}
        value = value[item]
    return value


def contract_comparison():
    # Requests were saved before launch and stay immutable on resume. This is
    # a read-only settings check, never a claim that the runs have finished.
    paths = {label: WORKSPACE / 'tasks' / task_id / 'request.json'
             for label, task_id in TASKS.items() if label != 'previous'}
    if not all(path.is_file() for path in paths.values()):
        return {'missing': '两套正式任务请求尚未完整保存'}
    requests = {name: read_json(path) for name, path in paths.items()}
    keys = ('start', 'end', 'spatialVersion', 'parameters', 'partitionParameters',
            'initialHydrology', '_fixedInitialization', 'parameterTransfer', 'routingMode',
            'periods', 'search', 'twoStage.mode', 'twoStage.finalRefit', 'twoStage.seeds',
            'twoStage.budgets', 'twoStage.allowExtension', 'twoStage.selectionMode',
            'twoStage.candidateLimit', 'twoStage.jointBudget', 'twoStage.lakePeriods',
            'twoStage.objective', '_modelIdentity', '_observations')
    entries = []
    for key in keys:
        values = {label: _contract_field(request, key) for label, request in requests.items()}
        entries.append({'field': key, 'equal': len({digest(v) for v in values.values()}) == 1,
                        'valueSHA256': {label: digest(value) for label, value in values.items()}})
    return {'requests': {label: {'path': path.relative_to(ROOT).as_posix(), 'sha256': sha_file(path),
                'inputId': requests[label].get('inputId'),
                'executableConfigurationSHA256': requests[label].get('calibrationConfigurationSHA256')}
                for label, path in paths.items()},
            'sameExceptInput': all(row['equal'] for row in entries), 'fields': entries}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--write', action='store_true', help='独占创建新的对比证据文件')
    args = parser.parse_args()
    statuses = task_statuses()
    entries, frames = {}, {}
    for label, task_id in TASKS.items():
        entry, frame = finished_task(task_id, statuses)
        entries[label] = entry
        if frame is not None:
            frames[label] = frame
    complete = len(frames) == len(TASKS)
    report = {'createdAt': now(), 'complete': complete,
        'basis': ('三个已完成成果的共同有效观测日' if complete else '目前已完成成果的共同有效观测日；两套新结果尚未全部完成'),
        'limitations': ['正式参数回代的分年成绩不是未参与定参的独立检验。',
            '两套新驱动与既有成果分别按其冻结输入计算；不能只以拟合精度断言降水产品优劣。'],
        'tasks': entries, 'pairedRequestContract': contract_comparison()}
    if frames:
        anchors = {label: pd.Timestamp(entries[label]['initialization']['time']).normalize() for label in frames}
        flow, flow_conflicts = common_pairs(frames, 'observedFlow', 'duja')
        lake, lake_conflicts = common_pairs(frames, 'observedLevel', 'lakeLevel', set(anchors.values()))
        report['comparison'] = {'allResultsPresent': complete,
            'anchorsSameDay': len(set(anchors.values())) == 1,
            'commonFlowDays': int(len(flow)), 'differentFlowObservationDatesExcluded': flow_conflicts,
            'commonLakeDays': int(len(lake)), 'differentLakeObservationDatesExcluded': lake_conflicts,
            'flow': {label: flow_report(flow, label) for label in frames},
            'lake': {label: lake_report(lake, label, entries[label]['initialization']) for label in frames}}
    if args.write:
        destination = ROOT / 'docs/evidence' / ('双驱动完整率定对比-20261030-' + uuid.uuid4().hex[:12] + '.json')
        with destination.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        report_path = destination.relative_to(ROOT).as_posix()
    else:
        report_path = None
    print(json.dumps({'complete': complete, 'statuses': {label: x['status'] for label, x in entries.items()},
        'missing': {label: x['missing'] for label, x in entries.items() if 'missing' in x},
        'commonFlowDays': report.get('comparison', {}).get('commonFlowDays'),
        'commonLakeDays': report.get('comparison', {}).get('commonLakeDays'),
        'lakeRmse': {label: report['comparison']['lake'][label]['all']['rmse'] for label in frames},
        'report': report_path, 'reportSHA256': sha_file(ROOT / report_path) if report_path else None},
        ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
