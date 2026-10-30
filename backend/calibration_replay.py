"""Reproduce the 2026-10-29 lake selection with its original implementation.

The historical request is never re-prepared from current schemes or observations.
The old numerical package runs in a separate interpreter; this module only
archives inputs, launches it, and compares its outputs with the saved result.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.metadata
import io
import json
import math
import os
import platform
import queue
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path

from .common import ROOT, digest, read_json, sha_file, within, write_json

SOURCE_ID = 'JOB-88b35b44b27d4e15840c'
BASELINE_ID = 'JOB-4934a5d35c9344a59353'
INPUT_ID = 'INPUT-c00df956d638351ebeb4e96e'
SPATIAL_ID = 'AGG-SPATIAL-90fbdd73db960fdda4a6'
PARENT_SPATIAL_ID = 'GRID-SPATIAL-0dbe04d1764277525f6a'
INPUT_SPATIAL_ID = 'SPATIAL-1e6a229e51f0da4f1ffd'
REVISION = '9cbed4b58ab3ded5982bbb55dd9850f7d58ddda5'
BACKEND_SHA = 'd209aa6b038b278e913aeffce300494de3c9556f76037c6115f191837b2bfd5f'
MODEL_SHA = '6abbc3e802baa94c00c3f4489e72847086eac3123ba1ead576d8379cdee2ee7d'
REQUEST_SHA = 'b3acc5a7ae1d80ce0725d86389c62edb3ba13138e1b473703f1cc921bfea8581'
RESULT_SHA = '4af2c70536f3a6f8e6f056190a92427ac4167d5dcf3ce44cba64a50a80cd78ec'
BASE_REQUEST_SHA = '7d61a07c122148774acc1aa72b2c07db67989c84685654140086c686cc77f966'
BASE_RESULT_SHA = '35acc88c3a97f2f68a35032ec63a43eb83cc1e52786867c151f24f734bcf2f50'
TRACE_SHA = 'd12fa8d700d24b8fd5fda0d7dbce54922463959e0dc6807eb81e014cc3a2a563'
INPUT_SHA = 'ec25016799f8d2585f1569d5710b56c0ae40d5017a743660cb6ed258ec1f0574'
MODEL_FILES = ('model_core.py', 'routing.py', 'parameter_regions.py', 'parameter_transfer.py',
               'grid_simulation.py', 'forcing.py', 'grid_spatial.py', 'simulation.py')
WORKER = Path(__file__).with_name('calibration_replay_worker.py')
# The only permitted change to an existing replay is this audited control
# reader repair. Historical numerical code and every other plan field remain
# tied to the original frozen plan.
WORKER_CONTROL_MIGRATIONS = frozenset({(
    '667311bafa6ea8a77b6578c88a85f2470518fac5cd1277f5ded44eeb7bce6702',
    '4c26f751f038598865944487049fddbe51989ad28cef9beee47af9db0e96d341',
)})
WINDOWS_PLATFORM_MIGRATIONS = frozenset({
    ('Windows-10-10.0.26100-SP0', 'Windows-10-10.0.26200-SP0')})


def _git_bytes(*args):
    try:
        return subprocess.run(['git', '-C', str(ROOT), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError('无法读取历史代码提交，请检查本地Git历史') from error


def _archive():
    raw = _git_bytes('archive', '--format=tar', REVISION, 'backend')
    files = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
        for item in archive:
            if not item.isfile():
                continue
            name = item.name.replace('\\', '/')
            parts = Path(name).parts
            if not name.startswith('backend/') or '..' in parts or not name.endswith('.py'):
                raise ValueError('历史代码归档包含无法核验的文件')
            files[name] = archive.extractfile(item).read()
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
    numerical = digest({'format': 'grid-continuous-v1',
                        'files': {name: hashes['backend/' + name] for name in MODEL_FILES}})
    if len(files) != 77 or digest(hashes) != BACKEND_SHA or numerical != MODEL_SHA:
        raise ValueError('历史代码摘要与原成果不一致')
    return files, hashes


def _environment():
    lock = _git_bytes('show', REVISION + ':requirements-lock.windows.txt').decode('utf-8')
    expected = dict(line.split('==', 1) for line in lock.splitlines() if '==' in line)
    names = sorted(set(expected) | {'llvmlite'})
    values = {}
    for name in names:
        try:
            values[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            values[name] = None
    mismatches = {key: {'expected': val, 'actual': values[key]}
                  for key, val in expected.items() if values[key] != val}
    return {'python': platform.python_version(), 'pythonVersion': sys.version,
            'executable': str(Path(sys.executable).resolve()), 'platform': platform.platform(),
            'packages': values, 'lockMismatches': mismatches,
            'lockReference': REVISION + ':requirements-lock.windows.txt',
            'historicalRuntimeRecorded': False}


def _asset(name, path, expected=None):
    if not path.is_file():
        raise ValueError(name + '缺失')
    actual = sha_file(path)
    if expected and actual != expected:
        raise ValueError(name + '摘要与历史记录不一致')
    return {'name': name, 'sha256': actual, 'bytes': path.stat().st_size}


def _source_paths(db):
    root = Path(db.workspace)
    source = within(root / 'tasks', SOURCE_ID)
    baseline = within(root / 'tasks', BASELINE_ID)
    product = within(root / 'inputs/products', INPUT_ID)
    spatial = ROOT / 'data/derived/spatial/versions'
    return {'sourceRequest': source / 'request.json', 'sourceResult': source / 'result.json',
            'sourcePool': source / 'joint/candidate-pool.json',
            'baselineRequest': baseline / 'request.json', 'baselineResult': baseline / 'result.json',
            'sourceTrace': baseline / 'all-data/search-trace.jsonl',
            'sourceProgress': baseline / 'all-data/search-progress.json',
            'inputManifest': product / 'manifest.json', 'forcing': product / 'forcing.npz',
            'spatial': spatial / SPATIAL_ID / 'manifest.json',
            'parentSpatial': spatial / PARENT_SPATIAL_ID / 'manifest.json',
            'inputSpatial': spatial / INPUT_SPATIAL_ID / 'manifest.json'}


def _lock_requirements():
    # Only imported packages needed for this workflow; absent optional GIS and
    # deployment packages do not make the numerical replay unusable.
    return ('numpy', 'pandas', 'numba', 'llvmlite', 'scipy', 'zarr', 'numcodecs')


def _daily_compact(rows):
    keys = ('date', 'duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total', 'lakeLevel')
    return [{key: row.get(key) for key in keys} for row in rows]


def replay_plan(db, identifier):
    if identifier != SOURCE_ID:
        raise ValueError('当前只支持重放已保存的2026年10月29日湖位联合率定')
    plan = {'sourceTaskId': identifier, 'planSHA256': None, 'available': False,
            'missing': [], 'configuration': {}, 'counts': {}, 'assets': [], 'environment': {}}
    paths = _source_paths(db)
    expected = {'sourceRequest': REQUEST_SHA, 'sourceResult': RESULT_SHA,
                'baselineRequest': BASE_REQUEST_SHA, 'baselineResult': BASE_RESULT_SHA,
                'sourceTrace': TRACE_SHA, 'forcing': INPUT_SHA}
    for name, path in paths.items():
        try:
            plan['assets'].append(_asset(name, path, expected.get(name)))
        except (OSError, ValueError) as error:
            plan['missing'].append(str(error))
    try:
        _, hashes = _archive()
        plan['assets'].append({'name': 'historicalBackend', 'sha256': digest(hashes), 'files': len(hashes),
                               'revision': REVISION})
        plan['assets'].append(_asset('replayWorker', WORKER))
        plan['environment'] = _environment()
        absent = [name for name in _lock_requirements() if plan['environment']['packages'].get(name) is None]
        if absent:
            plan['missing'].append('历史重放缺少运行依赖：' + '、'.join(absent))
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        plan['missing'].append(str(error))
    if plan['missing']:
        return plan
    try:
        request = read_json(paths['sourceRequest'])
        result = read_json(paths['sourceResult'])
        base_request = read_json(paths['baselineRequest'])
        baseline = read_json(paths['baselineResult'])
        source_row, base_row = db.task(SOURCE_ID), db.task(BASELINE_ID)
        for row, frozen_request, frozen_result in ((source_row, request, result),
                                                    (base_row, base_request, baseline)):
            if row['kind'] != 'two-stage-calibration' or row['status'] != 'succeeded' or \
                    row.get('request') != frozen_request or row.get('result') != frozen_result:
                raise ValueError('历史率定的数据库记录与冻结文件不一致')
        manifest = read_json(paths['inputManifest'])
        spatial = read_json(paths['spatial'])
        parent = read_json(paths['parentSpatial'])
        input_spatial = read_json(paths['inputSpatial'])
        if (request['inputId'] != INPUT_ID or request['spatialVersion'] != SPATIAL_ID or
                request['_modelIdentity'] != MODEL_SHA or request['_candidateSourceTraceSHA256'] != TRACE_SHA or
                request['twoStage']['candidateSourceTaskId'] != BASELINE_ID or
                request['twoStage']['baselineTaskId'] != BASELINE_ID or
                manifest['id'] != INPUT_ID or manifest['data'] != 'forcing.npz' or
                manifest['dataSHA256'] != INPUT_SHA or manifest['spatialVersion'] != INPUT_SPATIAL_ID or
                digest(manifest['lakePrecipitationCorrection']) != manifest['transformationSHA256'] or
                manifest['transformationSHA256'] != result['identity']['forcingTransformationSHA256'] or
                spatial['version'] != SPATIAL_ID or
                SPATIAL_ID != 'AGG-SPATIAL-' + digest({k: v for k, v in spatial.items() if k != 'version'})[:20] or
                spatial['parentSpatialVersion'] != PARENT_SPATIAL_ID or
                digest(parent) != spatial['parentManifestSHA256'] or
                input_spatial['version'] != INPUT_SPATIAL_ID or
                digest(input_spatial['grids']) != spatial['forcingGridVersion'] or
                spatial['forcingGridVersion'] != result['identity']['forcingGridSHA256'] or
                digest(request['_observations']) != result['identity']['observationSnapshotSHA256'] or
                digest(request['_fixedInitialization']) != result['identity']['initializationSHA256']):
            raise ValueError('历史请求、观测、气象或空间身份不一致')
        selection = result['calibrationWorkflow']['selection']
        search = request['search']
        options = request['twoStage']
        plan['configuration'] = {'parameters': deepcopy(request['parameters']),
            'results': deepcopy(result['parameters']), 'bounds': deepcopy(search['bounds']),
            'selectedKeys': list(search['keys']), 'periods': deepcopy(request['periods']),
            'twoStage': deepcopy(options), 'initialHydrology': deepcopy(request.get('initialHydrology')),
            'initialization': deepcopy(request['_fixedInitialization']), 'inputId': INPUT_ID,
            'spatialVersion': SPATIAL_ID, 'start': request['start'], 'end': request['end'],
            'objectiveProfile': search['objectiveProfile'],
            'objectiveSpec': {'flowWeights': {'nse': .5, 'logNse': .25, 'volume': .25},
                'lakeWeights': {'rmse': .25, 'trend': .25, 'amplitude': .25, 'peakTiming': .25},
                'lakeScales': {'rmseM': .1, 'trendErrorM': .1, 'amplitudeErrorM': .1, 'peakTimingErrorDays': 14.}},
            'observationCounts': {'level': len(request['_observations']['level']),
                                  'flow': len(request['_observations']['flow'])},
            'sourceRequestSHA256': REQUEST_SHA, 'modelIdentity': MODEL_SHA}
        plan['counts'] = {'rescored': selection['totalRescored'], 'eligible': selection['eligibleRescored'],
                          'selectedForBasin': selection['selectedForBasin'],
                          'jointProposals': selection['refinement']['evaluations'],
                          'basinCandidates': selection['basinCandidatesEvaluated']}
        comparison_fingerprint = {'daily': _daily_compact(result['daily']), 'parameters': result['parameters'],
            'flowMetrics': selection['flowMetrics'], 'allDataEvaluation': selection['allDataEvaluation'],
            'training': selection['trainingFit']['training'], 'validation': selection['trainingFit']['validation'],
            'yearly': result['metrics'], 'candidatePool': read_json(paths['sourcePool'])['candidateIds']}
        plan['comparison'] = {'sourceTaskId': SOURCE_ID, 'fingerprint': digest(comparison_fingerprint),
            'flowMetrics': deepcopy(selection['flowMetrics']),
            'lakeMetrics': {'training': deepcopy(selection['trainingFit']['training']),
                            'timeCheck': deepcopy(selection['trainingFit']['validation']),
                            'allData': deepcopy(selection['allDataEvaluation'])}}
        plan['available'] = True
        plan['planSHA256'] = digest({k: v for k, v in plan.items() if k != 'planSHA256'})
    except (OSError, ValueError, KeyError, TypeError) as error:
        plan['missing'].append(str(error))
    return plan


def prepare_request(db, payload):
    allowed = {'sourceTaskId', 'planSHA256', 'clientRequestId', 'projectId', 'contextKey'}
    if set(payload) - allowed:
        raise ValueError('历史重放只接受预览标识和请求编号')
    plan = replay_plan(db, payload.get('sourceTaskId'))
    if not plan['available']:
        raise ValueError('历史重放资料不可用：' + '；'.join(plan['missing']))
    if payload.get('planSHA256') != plan['planSHA256']:
        raise ValueError('历史重放预览已变化，请重新查看')
    request = deepcopy(read_json(_source_paths(db)['sourceRequest']))
    request['replay'] = {'sourceTaskId': SOURCE_ID, 'planSHA256': plan['planSHA256'],
                         'sourceRequestSHA256': REQUEST_SHA, 'automaticAdoption': False}
    request['name'] = '正式率定复现'
    request['purpose'] = 'development'
    if payload.get('clientRequestId'):
        request['clientRequestId'] = payload['clientRequestId']
    return request, plan


def _exclusive_copy(source, destination, expected):
    if destination.is_file():
        if sha_file(destination) != expected:
            raise ValueError('历史重放已保存资料发生变化：' + destination.name)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as old, destination.open('xb') as new:
        for block in iter(lambda: old.read(1024 * 1024), b''):
            new.write(block)
    if sha_file(destination) != expected:
        raise ValueError('复制历史重放资料时摘要不一致')


def freeze(db, folder, plan):
    """Create immutable small snapshots in the new task, without touching originals."""
    directory = folder / 'replay-snapshot'
    directory.mkdir(parents=True, exist_ok=True)
    by_name = {a['name']: a for a in plan['assets']}
    for name in ('sourceRequest', 'sourceResult', 'baselineRequest', 'baselineResult'):
        _exclusive_copy(_source_paths(db)[name], directory / (name + '.json'), by_name[name]['sha256'])
    write_json(directory / 'plan.json', plan)


def _verify_snapshot(folder, plan):
    assets = {row['name']: row for row in plan['assets']}
    for name in ('sourceRequest', 'sourceResult', 'baselineRequest', 'baselineResult'):
        _asset('冻结' + name, folder / 'replay-snapshot' / (name + '.json'), assets[name]['sha256'])


def _worker_migration(frozen, current):
    """Permit only an explicitly reviewed worker-only control change."""
    if frozen == current:
        return None
    before = deepcopy(frozen)
    after = deepcopy(current)
    before.pop('planSHA256', None)
    after.pop('planSHA256', None)
    old = [row for row in before.get('assets', []) if row.get('name') == 'replayWorker']
    new = [row for row in after.get('assets', []) if row.get('name') == 'replayWorker']
    if len(old) != 1 or len(new) != 1:
        raise ValueError('历史重放启动器身份不唯一')
    old_asset, new_asset = deepcopy(old[0]), deepcopy(new[0])
    if (old_asset.get('sha256'), new_asset.get('sha256')) not in WORKER_CONTROL_MIGRATIONS:
        raise ValueError('历史重放启动器变化不在已核验修复范围')
    old_platform = before.get('environment', {}).get('platform')
    new_platform = after.get('environment', {}).get('platform')
    environment_change = None
    if old_platform != new_platform:
        if (old_platform, new_platform) not in WINDOWS_PLATFORM_MIGRATIONS:
            raise ValueError('历史重放运行环境变化不在已登记范围')
        environment_change = {'field': 'platform', 'previous': old_platform, 'current': new_platform,
                              'note': '两次进程的Windows build报告不同，最终须完成数值容差核验。'}
        before['environment']['platform'] = after['environment']['platform'] = 'recorded-windows-build-change'
    for asset in (old[0], new[0]):
        asset['sha256'], asset['bytes'] = 'reviewed-control-repair', 0
    if before != after:
        raise ValueError('历史资料、数值代码或运行环境已变化，不能按启动器修复接续')
    return {'reason': '短暂控制文件读取异常不再误判为停止',
            'frozenPlanSHA256': frozen['planSHA256'], 'currentPlanSHA256': current['planSHA256'],
            'previousWorker': old_asset, 'currentWorker': new_asset, 'environmentChange': environment_change}


def _write_immutable_record(path, record):
    try:
        with path.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if read_json(path) != record:
            raise ValueError('历史重放迁移记录不一致')


def _record_worker_migration(folder, frozen, current):
    change = _worker_migration(frozen, current)
    if change is None:
        return None
    record = {'schemaVersion': 1, **change, 'currentPlan': current}
    path = folder / 'replay-snapshot' / ('worker-migration-' + change['currentWorker']['sha256'][:12] +
                                         '-' + change['currentPlanSHA256'][:12] + '.json')
    _write_immutable_record(path, record)
    return {**change, 'recordFile': str(path.relative_to(folder)).replace('\\', '/')}


def _runtime_environment_migration(previous, current, worker_migration):
    if previous == current:
        return None
    change = (worker_migration or {}).get('environmentChange')
    before, after = deepcopy(previous), deepcopy(current)
    old_platform, new_platform = before.pop('platform', None), after.pop('platform', None)
    if (not change or (old_platform, new_platform) not in WINDOWS_PLATFORM_MIGRATIONS or
            change['previous'] != old_platform or change['current'] != new_platform or before != after):
        raise ValueError('运行环境与已保存的重放环境不同，不能接续')
    return {'schemaVersion': 1, 'change': change, 'previousEnvironment': previous,
            'currentEnvironment': current}


def verify_inputs(db, request, folder):
    plan = read_json(folder / 'replay-snapshot/plan.json')
    current = replay_plan(db, SOURCE_ID)
    if (not plan or not current['available'] or not plan.get('available') or
            plan.get('planSHA256') != request['replay']['planSHA256'] or
            any(value.get('planSHA256') != digest({k: v for k, v in value.items() if k != 'planSHA256'})
                for value in (plan, current))):
        raise ValueError('历史资料、源码或运行环境已变化，不能重放或接续')
    _worker_migration(plan, current)
    _verify_snapshot(folder, plan)
    return current


def _runtime(db, folder, plan):
    files, hashes = _archive()
    app = folder / 'replay-runtime/app'
    app.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = within(app, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if sha_file(path) != hashes[name]:
                raise ValueError('已归档历史代码发生变化')
        else:
            with path.open('xb') as stream:
                stream.write(data)
    names = {'spatial': SPATIAL_ID, 'parentSpatial': PARENT_SPATIAL_ID, 'inputSpatial': INPUT_SPATIAL_ID}
    paths = _source_paths(db)
    assets = {a['name']: a for a in plan['assets']}
    for key, version in names.items():
        _exclusive_copy(paths[key], app / 'data/derived/spatial/versions' / version / 'manifest.json',
                        assets[key]['sha256'])
    cache = folder / 'replay-runtime/cache'
    cache.mkdir(parents=True, exist_ok=True)
    return app, cache


def _comparison(folder, original, replay):
    old = original['calibrationWorkflow']['selection']
    new = replay['calibrationWorkflow']['selection']
    old_pool = read_json(_source_paths_from_folder(folder)['sourcePool'])
    new_pool = read_json(folder / 'joint/candidate-pool.json')
    keys = ('duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total', 'lakeLevel')
    old_days = {row['date']: row for row in original.get('daily', [])}
    new_days = {row['date']: row for row in replay.get('daily', [])}
    differences = {}
    for key in keys:
        pairs = [(old_days[d].get(key), new_days[d].get(key)) for d in old_days.keys() & new_days.keys()]
        valid = [(float(a), float(b)) for a, b in pairs if a is not None and b is not None]
        differences[key] = {'paired': len(valid), 'missingPatternEqual': all((a is None) == (b is None) for a, b in pairs),
                            'maxAbsoluteDifference': max((abs(a-b) for a, b in valid), default=None)}
    parameter_delta = {key: abs(float(value) - float(replay['parameters'][key]))
                       for key, value in original['parameters'].items()}
    def metric_difference(left, right):
        if isinstance(left, dict) and isinstance(right, dict):
            if left.keys() != right.keys(): return False, 0.
            children = [metric_difference(left[key], right[key]) for key in left]
        elif isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right): return False, 0.
            children = [metric_difference(a, b) for a, b in zip(left, right)]
        elif isinstance(left, (int, float)) and not isinstance(left, bool) and \
                isinstance(right, (int, float)) and not isinstance(right, bool):
            if not math.isfinite(left) or not math.isfinite(right): return False, 0.
            return True, abs(float(left) - float(right))
        else:
            return left == right, 0.
        return all(v[0] for v in children), max((v[1] for v in children), default=0.)
    def indicators(selection, result):
        fit = selection.get('trainingFit', {})
        return {'flow': selection['flowMetrics'], 'lakeAll': selection['allDataEvaluation'],
                'lakeTraining': fit.get('training'), 'lakeTimeCheck': fit.get('validation'),
                'yearly': result.get('metrics')}
    metrics_ok, metrics_delta = metric_difference(indicators(old, original), indicators(new, replay))
    report = {'sourceTaskId': SOURCE_ID, 'candidateOrderEqual': old_pool['candidateIds'] == new_pool['candidateIds'],
            'trainingCandidateEqual': old['trainingCandidateId'] == new['trainingCandidateId'],
            'finalCandidateEqual': old['finalCandidateId'] == new['finalCandidateId'],
            'counts': {key: {'original': old[key], 'replayed': new[key]} for key in
                       ('totalRescored', 'eligibleRescored', 'selectedForBasin', 'basinCandidatesEvaluated')},
            'jointProposals': {'original': old['refinement']['evaluations'], 'replayed': new['refinement']['evaluations']},
            'datesEqual': old_days.keys() == new_days.keys(), 'dailyDifferences': differences,
            'parameterMaximumDifference': max(parameter_delta.values(), default=0.),
            'tolerances': {'flowM3s': 1e-7, 'lakeM': 1e-8, 'metrics': 1e-8, 'parameters': 1e-12},
            'metricsStructureEqual': metrics_ok, 'metricsMaxAbsoluteDifference': metrics_delta,
            'flowMetrics': {'original': old['flowMetrics'], 'replayed': new['flowMetrics']},
            'lakeMetrics': {'original': old['allDataEvaluation'], 'replayed': new['allDataEvaluation']},
            'periodMetrics': {'original': indicators(old, original), 'replayed': indicators(new, replay)}}
    flow_values = [differences[key]['maxAbsoluteDifference'] for key in keys if key != 'lakeLevel']
    report['flowMaxAbsoluteM3s'] = max((v for v in flow_values if v is not None), default=None)
    report['lakeMaxAbsoluteM'] = differences['lakeLevel']['maxAbsoluteDifference']
    report['passes'] = bool(report['candidateOrderEqual'] and report['trainingCandidateEqual'] and
        report['finalCandidateEqual'] and report['datesEqual'] and
        all(value['original'] == value['replayed'] for value in report['counts'].values()) and
        report['jointProposals']['original'] == report['jointProposals']['replayed'] and
        report['parameterMaximumDifference'] <= report['tolerances']['parameters'] and
        metrics_ok and metrics_delta <= report['tolerances']['metrics'] and
        all(value['paired'] and value['missingPatternEqual'] and value['maxAbsoluteDifference'] is not None and
            value['maxAbsoluteDifference'] <= report['tolerances']['lakeM' if key == 'lakeLevel' else 'flowM3s']
            for key, value in differences.items()))
    return report


def _source_paths_from_folder(folder):
    return {'sourcePool': folder.parent / SOURCE_ID / 'joint/candidate-pool.json'}


def run(db, request, folder, progress, cancelled):
    plan = verify_inputs(db, request, folder)
    frozen_plan = read_json(folder / 'replay-snapshot/plan.json')
    worker_migration = _record_worker_migration(folder, frozen_plan, plan)
    app, cache = _runtime(db, folder, plan)
    distributions = sorted([d.metadata.get('Name', ''), d.version] for d in importlib.metadata.distributions())
    environment = {**plan['environment'], 'allDistributions': distributions}
    environment_path = folder / 'replay-runtime/environment.json'
    previous = read_json(environment_path)
    environment_migration = (_runtime_environment_migration(previous, environment, worker_migration)
                             if previous is not None else None)
    if previous is None:
        write_json(environment_path, environment)
    elif environment_migration is not None:
        name = ('environment-migration-' + worker_migration['currentWorker']['sha256'][:12] + '-' +
                worker_migration['currentPlanSHA256'][:12] + '.json')
        _write_immutable_record(folder / 'replay-runtime' / name, environment_migration)
    control = folder / 'replay-runtime/control.json'
    token = uuid.uuid4().hex
    expected_environment = folder / 'replay-runtime' / ('worker-expected-environment-' + token + '.json')
    _write_immutable_record(expected_environment, plan['environment'])
    env = os.environ.copy()
    env.update(SELINCO_APP_ROOT=str(app), SELINCO_WORKSPACE=str(cache), XAJ_RUNTIME_DIR=str(cache),
               PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
    write_json(control, {'token': token, 'heartbeat': time.time(), 'cancel': False})
    log_path = folder / 'replay-runtime/worker.log'
    messages = queue.Queue()
    with log_path.open('a', encoding='utf-8') as log:
        process = subprocess.Popen([sys.executable, '-B', str(WORKER), str(folder), str(db.workspace), token],
                                   cwd=str(app), env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=log, text=True, encoding='utf-8', errors='replace',
                                   bufsize=1, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def read_messages():
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass
        reader = threading.Thread(target=read_messages, daemon=True)
        reader.start()
        last = 0.
        while process.poll() is None:
            if time.monotonic() - last >= 1.:
                write_json(control, {'token': token, 'heartbeat': time.time(), 'cancel': bool(cancelled())})
                last = time.monotonic()
            try:
                message = messages.get(timeout=.3)
                if message.get('type') == 'progress' and not cancelled():
                    try:
                        progress(message['value'], message['message'])
                    except InterruptedError:
                        write_json(control, {'token': token, 'heartbeat': time.time(), 'cancel': True})
            except queue.Empty:
                pass
        reader.join(timeout=2)
    if cancelled():
        raise InterruptedError('历史重放已停止，已完成搜索状态保留，可继续')
    if process.returncode == 130:
        reason_path = folder / 'replay-runtime' / ('control-reason-' + token + '.json')
        reason = read_json(reason_path, {}).get('reason')
        if reason == 'explicit-cancel':
            raise InterruptedError('历史重放已停止，已完成搜索状态保留，可继续')
        descriptions = {'heartbeat-stale': '控制心跳持续中断', 'control-unreadable': '控制文件持续无法读取',
                        'token-changed': '控制身份发生变化'}
        raise ValueError('历史重放中断：' + descriptions.get(reason, '工作进程未记录停止原因') +
                         '；已完成搜索状态保留，可继续')
    if process.returncode != 0:
        raise ValueError('历史重放工作进程未完成，请查看任务运行记录')
    finished_plan = replay_plan(db, SOURCE_ID)
    if not finished_plan['available'] or finished_plan['planSHA256'] != plan['planSHA256']:
        raise ValueError('重放期间历史资料或环境发生变化，请查看任务记录')
    _verify_snapshot(folder, plan)
    result = read_json(folder / 'result.json')
    if not result:
        raise ValueError('历史重放未生成完整成果')
    original = read_json(folder / 'replay-snapshot/sourceResult.json')
    comparison = _comparison(folder, original, result)
    result['replay'] = {'sourceTaskId': SOURCE_ID, 'planSHA256': plan['planSHA256'],
                        'frozenPlanSHA256': frozen_plan['planSHA256'], 'workerMigration': worker_migration,
                        'backendSourceSHA256': BACKEND_SHA, 'historicalRevision': REVISION,
                        'automaticAdoption': False, 'comparison': comparison,
                        'environment': plan['environment']}
    child_environment = folder / 'replay-runtime' / ('worker-environment-' + token + '.json')
    if not child_environment.is_file():
        raise ValueError('重放工作进程缺少实际运行环境记录')
    result['replay']['workerEnvironment'] = {'file': str(child_environment.relative_to(folder)).replace('\\', '/'),
                                            'sha256': sha_file(child_environment)}
    comparison_file = folder / 'replay-comparison.json'
    write_json(comparison_file, comparison)
    from urllib.parse import urlencode
    stat = comparison_file.stat()
    result['artifacts'] = [{'name': comparison_file.name, 'filename': comparison_file.name,
        'storage': 'task', 'ownerTaskId': folder.name, 'bytes': stat.st_size,
        'sha256': sha_file(comparison_file), 'mtimeNs': stat.st_mtime_ns, 'complete': True,
        'downloadUrl': '/api/model/task-artifact?' + urlencode({'taskId': folder.name, 'name': comparison_file.name})}]
    return result
