"""导出已完成的两套双驱动率定设置与参数；不运行模型、不改变采用关系。"""
import argparse
import json
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, WORKSPACE, digest, now, read_json, sha_file
from backend.model_core import PARAMETER_DEFINITIONS, PARAMETER_NAMES

TASKS = {
    '原驱动': 'JOB-6536f36e07cc46868255',
    '暖雨订正驱动': 'JOB-05c3f62282ce4239aed1',
}


def file_identity(path):
    if not path.is_file():
        raise ValueError('所需文件缺失：' + str(path))
    return {'path': path.relative_to(ROOT).as_posix(), 'sha256': sha_file(path)}


def compact_fit(fit):
    fields = ('fit', 'algorithm', 'trainingPeriod', 'validationPeriod', 'evaluations',
              'extended', 'converged', 'termination', 'nearBoundaryParameters', 'traceFile', 'traceSHA256')
    result = {key: fit.get(key) for key in fields}
    result['stages'] = []
    for stage in fit.get('stages', []):
        item = {key: stage.get(key) for key in ('name', 'cycle', 'keys', 'budget', 'used', 'converged')}
        item['starts'] = [{key: start.get(key) for key in ('seed', 'budget', 'evaluations', 'converged',
                           'termination', 'populationFile', 'populationSHA256')} for start in stage.get('starts', [])]
        result['stages'].append(item)
    return result


def export_task(task_id):
    folder = WORKSPACE / 'tasks' / task_id
    request_path, result_path = folder / 'request.json', folder / 'result.json'
    request_info, result_info = file_identity(request_path), file_identity(result_path)
    request, result = read_json(request_path), read_json(result_path)
    if result.get('taskId') != task_id or result.get('inputId') != request.get('inputId'):
        raise ValueError(task_id + ' 的结果、请求身份不一致')
    identifier = request.get('calibrationConfigurationSHA256', '')
    if len(identifier) != 64 or any(c not in '0123456789abcdef' for c in identifier):
        raise ValueError(task_id + ' 缺少有效可执行配置摘要')
    recipe_path = WORKSPACE / 'calibration-configurations' / (identifier + '.json')
    recipe_info = file_identity(recipe_path)
    recipe = read_json(recipe_path)['content']
    if digest(recipe) != identifier:
        raise ValueError(task_id + ' 的冻结配置摘要不符')
    if recipe['request']['parameters'] != request['parameters'] or recipe['request']['_observations'] != request['_observations']:
        raise ValueError(task_id + ' 的初值或观测快照与冻结配置不符')
    definitions = {row['key']: row for row in PARAMETER_DEFINITIONS}
    if set(definitions) != set(PARAMETER_NAMES) or set(request['parameters']) != set(PARAMETER_NAMES) or set(result['parameters']) != set(PARAMETER_NAMES):
        raise ValueError(task_id + ' 的参数清单不完整')
    bounds, selected = request['search']['bounds'], request['search']['keys']
    if len(selected) != 22 or len(set(selected)) != 22:
        raise ValueError(task_id + ' 的参选水文参数不是22项')
    rows = []
    for key in PARAMETER_NAMES:
        definition = definitions[key]
        rows.append({'key': key, 'meaning': definition['label'], 'unit': definition['unit'],
                     'group': definition['group'], 'stage': definition['calibrationRole'],
                     'selectedForFlow': key in selected, 'initial': request['parameters'][key],
                     'bounds': bounds.get(key), 'final': result['parameters'][key]})
    workflow = result.get('calibrationWorkflow') or {}
    selection = workflow.get('selection') or {}
    phases = []
    for phase in selection.get('phases', []):
        refinement = phase.get('refinement') or {}
        phases.append({key: phase.get(key) for key in ('phase', 'label', 'candidateId', 'generated',
            'uniqueGenerated', 'selectedForBasin', 'basinCandidatesEvaluated')} | {'refinement': {
            key: refinement.get(key) for key in ('evaluations', 'converged', 'termination', 'starts')}})
    if not phases or not selection.get('generationFits'):
        raise ValueError(task_id + ' 缺少完整分期搜索记录')
    options = request['twoStage']
    manifest_path = WORKSPACE / 'inputs/products' / request['inputId'] / 'manifest.json'
    manifest = read_json(manifest_path)
    manifest_info = file_identity(manifest_path)
    if digest(manifest) != recipe['inputManifestSHA256']:
        raise ValueError(task_id + ' 输入清单与冻结配置不符')
    return {'taskId': task_id, 'schemeId': request['researchSchemeId'],
        'request': request_info, 'result': result_info,
        'executableConfiguration': {**recipe_info, 'contentSHA256': identifier},
        'implementation': recipe['implementation'], 'resultIdentity': result.get('identity'),
        'input': {'id': request['inputId'], 'manifest': manifest_info,
                  'manifestContentSHA256': recipe['inputManifestSHA256'],
                  'dataSHA256': manifest.get('dataSHA256'),
                  'transformationSHA256': manifest.get('transformationSHA256')},
        'spatialVersion': request['spatialVersion'], 'observationSnapshotSHA256': digest(request['_observations']),
        'initialHydrology': request.get('initialHydrology'), 'lakeInitialization': request.get('_fixedInitialization'),
        'parameterTransfer': request.get('parameterTransfer'), 'routingMode': request.get('routingMode'),
        'periods': request['periods'], 'lakePeriods': options['lakePeriods'],
        'simulation': {'start': request['start'], 'end': request['end']},
        'search': {'selected': selected, 'objective': options['objective'], 'seeds': options['seeds'],
                   'budgetsPerFit': options['budgets'], 'allowExtension': options['allowExtension'],
                   'candidateLimitPerFit': options['candidateLimit'], 'jointBudgetPerFit': options['jointBudget'],
                   'flowThresholdsApplied': selection.get('flowThresholdsApplied'),
                   'targetBasedStopping': selection.get('targetBasedStopping')},
        'parameters': rows, 'generationFits': [compact_fit(fit) for fit in selection['generationFits']],
        'phases': phases, 'actual': {key: selection.get(key) for key in ('totalGenerated',
            'uniqueGenerated', 'totalRescored', 'selectedForBasin', 'basinCandidatesEvaluated')},
        'lakeFit': {key: (workflow.get('lake') or {}).get('final', {}).get(key) for key in
                    ('value', 'bounds', 'algorithm', 'converged', 'termination', 'trainingPeriod')}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selected-task', required=True, choices=tuple(TASKS.values()),
                        help='比较后确定的正式成果任务编号')
    args = parser.parse_args()
    database = WORKSPACE / 'database/hydrology.sqlite3'
    with sqlite3.connect('file:' + database.as_posix() + '?mode=ro', uri=True, timeout=30) as conn:
        rows = dict(conn.execute('SELECT id,status FROM tasks WHERE id IN (?,?)', tuple(TASKS.values())))
    incomplete = {task_id: rows.get(task_id, 'missing') for task_id in TASKS.values() if rows.get(task_id) != 'succeeded'}
    if incomplete:
        raise SystemExit('两套任务必须均成功后才能导出：' + json.dumps(incomplete, ensure_ascii=False))
    data = {'schemaVersion': 1, 'createdAt': now(), 'selectedTaskId': args.selected_task,
            'scope': '两套任务的冻结配置与实际结果；选择身份由调用者提供，导出不会改变采用关系。',
            'tasks': {name: export_task(identifier) for name, identifier in TASKS.items()}}
    path = ROOT / 'config/operational' / ('双驱动完整率定参数-20261030-' + uuid.uuid4().hex[:12] + '.json')
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'path': str(path), 'sha256': sha_file(path), 'selectedTaskId': args.selected_task}, ensure_ascii=False))


if __name__ == '__main__':
    main()
