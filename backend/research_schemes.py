"""Business schemes and parameter groups backed by immutable run identities."""
from copy import deepcopy
import uuid

import numpy as np
import pandas as pd

from .common import ROOT, digest, read_json
from .model_core import parameters, PARAMETER_DEFINITIONS, PARAMETER_NAMES
from .calibration_config import RUNOFF_KEYS, ROUTING_KEYS
from .operational_scheme import definition, catalog
from .parameter_regions import resolve_regions
from . import grid_spatial


BUILTIN_PATH = ROOT / 'config/operational/continuous-builtin-v1.json'
SUPPORTED_UNITS = {'grid-partition-elevation-surface-v2',
                   'partition-elevation-surface-area-weighted-v1'}


def builtin(db):
    """Return the configured scheme without silently creating a database row."""
    config = read_json(BUILTIN_PATH)
    if not config:
        return None
    value = deepcopy(config)
    identifier = value.pop('id')
    value['builtin'] = True
    value['origin'] = 'builtin'
    value['configurationSHA256'] = digest({k: v for k, v in value.items() if k != 'resultId'})
    return {'id': identifier, 'name': value['name'], 'payload': value, 'builtin': True}


def template(db):
    standard = builtin(db)
    if standard:
        value = deepcopy(standard['payload'])
        for key in ('builtin', 'configurationSHA256', 'resultId'):
            value.pop(key, None)
        value['name'] = '我的色林错方案'
        value['origin'] = 'user'
        return value
    fixed = definition()
    spatial = grid_spatial.current()
    choice = catalog(db)[0]
    return {'schemaVersion': 2, 'name': '我的色林错方案', 'modelFamily': 'grid-continuous-v1',
            'spatialVersion': spatial['version'] if spatial else '', 'inputId': choice['inputId'],
            'parameters': deepcopy(fixed['parameters']), 'partitionParameters': deepcopy(fixed['partitionParameters']),
            'routingMode': fixed['routingMode'], 'target': 'duja',
            'periods': {'warmupStart': '2023-01-01T00:00:00', 'trainingStart': '2024-01-01T00:00:00',
                        'trainingEnd': '2025-01-01T00:00:00', 'evaluationStart': '2025-01-01T00:00:00',
                        'evaluationEnd': '2026-01-01T00:00:00'},
            'inputRequirement': deepcopy(fixed.get('inputRequirement', {})),
            'search': {'scope': 'duja', 'keys': RUNOFF_KEYS + ROUTING_KEYS,
                       'bounds': {d['key']: [d['min'], d['max']] for d in PARAMETER_DEFINITIONS},
                       'budgets': {'runoff': 120, 'routing': 120, 'lake': 30}, 'seed': 42,
                       'objectiveProfile': 'balanced'}, 'origin': 'user'}


def _family(row):
    return row['payload'].get('familyId') or row['id']


def list_schemes(db, include_trashed=False):
    """Expose one latest entry per user scheme while retaining every revision."""
    latest = {}
    for row in db.items('schemes'):
        if row['payload'].get('schemaVersion') != 2:
            continue
        family = _family(row)
        old = latest.get(family)
        if old is None or (row['payload'].get('revision', 1), row.get('created_at', '')) > (
                old['payload'].get('revision', 1), old.get('created_at', '')):
            latest[family] = row
    result = []
    standard = builtin(db)
    if standard:
        result.append(standard)
    for row in latest.values():
        row['trashed'] = bool(db.setting('scheme-hidden:' + _family(row), False))
        if include_trashed or not row['trashed']:
            result.append(row)
    return result


def _scheme(db, identifier):
    standard = builtin(db)
    if standard and standard['id'] == identifier:
        return standard
    row = next((r for r in db.items('schemes') if r['id'] == identifier), None)
    if not row or row['payload'].get('schemaVersion') != 2:
        raise ValueError('方案不存在')
    return row


def scheme_metadata(db, identifier, trashed):
    row = _scheme(db, identifier)
    if row.get('builtin'):
        raise ValueError('内置方案不能移入回收站')
    family = _family(row)
    if trashed:
        ids = [r['id'] for r in db.items('schemes') if _family(r) == family]
        for active in db.rows("SELECT id,request FROM tasks WHERE status IN ('queued','running','stopping')"):
            import json
            if json.loads(active['request']).get('researchSchemeId') in ids:
                raise ValueError('方案正在计算，请先等待任务完成')
    db.set_setting('scheme-hidden:' + family, bool(trashed))
    db.log('schemes.visibility', {'id': identifier, 'familyId': family, 'trashed': bool(trashed)})
    return {'id': identifier, 'trashed': bool(trashed)}


def _compatible_input(value, product, spatial, end):
    manifest = product['manifest']
    try:
        if (pd.Timestamp(manifest['start']) > pd.Timestamp(value['periods']['warmupStart']) or
                pd.Timestamp(manifest['end']) < pd.Timestamp(end)):
            return False
        requirements = value.get('inputRequirement', {})
        if 'transformationSHA256' in requirements and manifest.get('transformationSHA256') != requirements['transformationSHA256']:
            return False
        input_spatial = grid_spatial.load(manifest['spatialVersion'])
        return digest(input_spatial['grids']) == spatial.get('forcingGridVersion', digest(spatial['grids']))
    except (KeyError, TypeError, ValueError, FileNotFoundError):
        return False


def _input(db, value, spatial, end, identifier=None):
    products = db.items('input_products')
    if identifier:
        product = next((r for r in products if r['id'] == identifier), None)
        if not product:
            raise ValueError('所选气象资料不存在，请更新或选择已有资料')
        if not _compatible_input(value, product, spatial, end):
            raise ValueError('所选气象资料未覆盖预热起点至计算结束，或格网、湖面降水处理与方案不兼容')
        return product
    compatible = [r for r in products if _compatible_input(value, r, spatial, end)]
    if not compatible:
        raise ValueError('没有兼容气象资料完整覆盖预热起点至计算结束，请补充资料')
    return max(compatible, key=lambda r: (r['manifest']['end'], r.get('created_at', ''), r['id']))


def validate(db, payload, coverage_end=None):
    value = deepcopy(payload)
    if value.get('schemaVersion') != 2 or value.get('modelFamily') != 'grid-continuous-v1':
        raise ValueError('请选择有效的连续计算方案')
    if not str(value.get('name', '')).strip() or len(value['name']) > 120:
        raise ValueError('请填写1—120字方案名称')
    spatial = grid_spatial.load(value['spatialVersion'])
    if spatial.get('unitStrategy') not in SUPPORTED_UNITS:
        raise ValueError('方案空间资料不受支持')
    value['parameters'] = parameters(value.get('parameters'))
    value['partitionParameters'], _ = resolve_regions(spatial['units'], value['parameters'], value.get('partitionParameters'))
    if 'calibrationInitialParameters' in value or 'calibrationInitialPartitionParameters' in value:
        initial = parameters(value.get('calibrationInitialParameters'))
        initial_regions, _ = resolve_regions(spatial['units'], initial, value.get('calibrationInitialPartitionParameters'))
        value['calibrationInitialParameters'] = initial
        value['calibrationInitialPartitionParameters'] = initial_regions
    periods = value['periods']
    dates = {key: pd.Timestamp(periods[key]) for key in
             ('warmupStart', 'trainingStart', 'trainingEnd', 'evaluationStart', 'evaluationEnd')}
    if any(pd.isna(t) or t.tzinfo or t != t.normalize() for t in dates.values()):
        raise ValueError('方案时段使用北京时间完整日边界')
    if not dates['warmupStart'] < dates['trainingStart'] < dates['trainingEnd'] <= dates['evaluationStart'] < dates['evaluationEnd']:
        raise ValueError('预热、率定、评价须按顺序且不重叠')
    if dates['trainingStart'] - dates['warmupStart'] < pd.Timedelta(days=365):
        raise ValueError('率定前至少保留365天统一预热')
    _input(db, value, spatial, coverage_end or periods['evaluationEnd'], value.get('inputId'))
    search = value['search']
    defs = {d['key']: d for d in PARAMETER_DEFINITIONS}
    keys = search['keys']
    if not keys or len(keys) != len(set(keys)) or set(keys) - set(defs):
        raise ValueError('参与搜索参数无效或重复')
    if search.get('scope') not in ('duja', 'background'):
        raise ValueError('参数区域无效')
    if value.get('target') not in ('duja', 'lake'):
        raise ValueError('率定对象无效')
    if value['target'] == 'duja' and search['scope'] != 'duja':
        raise ValueError('杜加站流量率定须调整杜加控制区参数')
    if value['target'] == 'lake' and search['scope'] != 'background':
        raise ValueError('湖泊率定须使用全湖参数')
    if value['target'] == 'lake' and keys != ['lakeCoefficient']:
        raise ValueError('湖泊对象仅搜索全湖蒸发系数')
    if value['target'] == 'duja' and 'lakeCoefficient' in keys:
        raise ValueError('杜加产汇流不同时搜索湖泊系数')
    initial = value.get('calibrationInitialParameters', value['parameters'])
    initial_regions = value.get('calibrationInitialPartitionParameters', value['partitionParameters'])
    baseline = {**initial, **(initial_regions.get('duja', {}) if search['scope'] == 'duja' else {})}
    for key in keys:
        bounds = search['bounds'].get(key)
        if not isinstance(bounds, list) or len(bounds) != 2 or any(type(v) not in (int, float) or not np.isfinite(v) for v in bounds):
            raise ValueError('请填写有效上下界：' + key)
        if not defs[key]['min'] <= bounds[0] < bounds[1] <= defs[key]['max']:
            raise ValueError('搜索上下界超过物理范围或顺序错误：' + key)
        if not bounds[0] <= baseline[key] <= bounds[1]:
            raise ValueError('率定初值必须位于本次搜索范围内：' + key)
    for stage in ('runoff', 'routing', 'lake'):
        budget = search['budgets'][stage]
        if type(budget) != int or not 1 <= budget <= 2000:
            raise ValueError('每阶段预算须为1—2000的整数')
    if type(search.get('seed')) != int or not 0 <= search['seed'] < 2 ** 32:
        raise ValueError('随机种子无效')
    if search.get('objectiveProfile') not in ('balanced', 'peaks'):
        raise ValueError('率定目标无效')
    if value.get('routingMode') != 'source-separated':
        raise ValueError('连续方案采用分水源调蓄与河道汇流')
    return value


def save(db, payload, parent_id=None):
    value = validate(db, payload)
    for key in ('configurationSHA256', 'revision', 'familyId', 'parentRevisionId', 'id', 'created_at', 'builtin', 'resultId'):
        value.pop(key, None)
    parent = _scheme(db, parent_id) if parent_id else None
    if parent and not parent.get('builtin') and db.setting('scheme-hidden:' + _family(parent), False):
        raise ValueError('方案已移入回收站，请先恢复')
    if parent and not parent.get('builtin'):
        latest_revision = max((r['payload'].get('revision', 1) for r in db.items('schemes')
                               if _family(r) == _family(parent)), default=1)
        if parent['payload'].get('revision', 1) != latest_revision:
            raise ValueError('方案已有更新，请刷新后再保存，或复制为新方案')
    if value.get('adoptedParameterId'):
        try:
            saved = _verified_parameter(db, value['adoptedParameterId'], value)
            if saved['parameters'] != value['parameters'] or saved['partitionParameters'] != value['partitionParameters']:
                value.pop('adoptedParameterId')
        except (ValueError, FileNotFoundError):
            value.pop('adoptedParameterId')
    # Editing the built-in scheme starts a user-owned copy.
    if parent and parent.get('builtin'):
        parent = None
        parent_id = None
    value['origin'] = 'user'
    value['revision'] = int(parent['payload'].get('revision', 1)) + 1 if parent else 1
    value['parentRevisionId'] = parent_id
    value['familyId'] = _family(parent) if parent else 'FAMILY-' + uuid.uuid4().hex[:16]
    value['configurationSHA256'] = digest(value)
    identifier = 'SCHEME-' + uuid.uuid4().hex[:20]
    db.save_item('schemes', identifier, value['name'], value)
    db.log('schemes.revision', {'id': identifier, 'parentId': parent_id, 'sha256': value['configurationSHA256']})
    return {'id': identifier, 'name': value['name'], 'payload': value}


def _result_parameters(db, task_id, allow_trashed=False):
    from .business_context import result_task
    task = result_task(db, task_id)
    result = task.get('result') or {}
    if task.get('status') != 'succeeded' or not result or (task.get('trashed') and not allow_trashed):
        raise ValueError('请选择未移入回收站的成功成果')
    raw = result.get('parameters')
    if not isinstance(raw, dict) or set(raw) != set(PARAMETER_NAMES):
        raise ValueError('成果未记录完整实际参数，不能保存为参数组')
    checked = parameters(raw)
    spatial_version = result.get('spatialVersion')
    spatial = grid_spatial.load(spatial_version)
    regions, _ = resolve_regions(spatial['units'], checked, result.get('partitionParameters', {}))
    if regions != result.get('partitionParameters', {}):
        raise ValueError('成果分区参数不完整或无法核验')
    return {'sourceTaskId': task_id, 'sourceTaskKind': task.get('kind'),
            'sourceSchemeId': (task.get('request') or {}).get('researchSchemeId'),
            'modelFamily': result.get('modelFamily'), 'spatialVersion': spatial_version,
            'routingMode': result.get('routingMode'), 'parameters': deepcopy(checked),
            'partitionParameters': deepcopy(regions),
            'resultIdentitySHA256': digest(result.get('identity') or {}),
            'parameterSHA256': digest({'parameters': checked, 'partitionParameters': regions})}


def save_parameters_from_task(db, task_id, name):
    if not isinstance(task_id, str) or not task_id:
        raise ValueError('请选择成功成果')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
        raise ValueError('参数名称须为1—120个字符')
    value = _result_parameters(db, task_id)
    identifier = 'PARAM-' + uuid.uuid4().hex[:20]
    db.save_item('parameter_versions', identifier, name.strip(), value)
    db.log('parameters.save-from-task', {'id': identifier, 'sourceTaskId': task_id, 'sha256': value['parameterSHA256']})
    return {'id': identifier, 'name': name.strip(), 'payload': value}


def _verified_parameter(db, identifier, scheme):
    row = next((r for r in db.items('parameter_versions') if r['id'] == identifier), None)
    if not row or not row['payload'].get('sourceTaskId'):
        raise ValueError('参数组不存在或没有成功成果来源')
    saved = row['payload']
    actual = _result_parameters(db, saved['sourceTaskId'], allow_trashed=True)
    for field in ('parameters', 'partitionParameters'):
        if saved.get(field, {}) != actual[field]:
            raise ValueError('参数组与来源成果不一致')
    if actual['modelFamily'] != scheme['modelFamily'] or actual['spatialVersion'] != scheme['spatialVersion'] or actual['routingMode'] != scheme['routingMode']:
        raise ValueError('参数组与方案的模型、空间或汇流方式不兼容')
    return actual


def adopt_parameters(db, scheme_id, parameter_id):
    row = _scheme(db, scheme_id)
    value = deepcopy(row['payload'])
    chosen = _verified_parameter(db, parameter_id, value)
    # Adoption never changes the independent starting point used by calibration.
    value.setdefault('calibrationInitialParameters', deepcopy(value['parameters']))
    value.setdefault('calibrationInitialPartitionParameters', deepcopy(value['partitionParameters']))
    value['parameters'] = deepcopy(chosen['parameters'])
    value['partitionParameters'] = deepcopy(chosen['partitionParameters'])
    value['adoptedParameterId'] = parameter_id
    return save(db, value, scheme_id)


def prepare_request(db, request, kind='simulation'):
    identifier = request.get('researchSchemeId')
    row = _scheme(db, identifier)
    if not row.get('builtin') and db.setting('scheme-hidden:' + _family(row), False):
        raise ValueError('方案已移入回收站，请先恢复')
    value = deepcopy(row['payload'])
    p = value['periods']
    requested_end = p['evaluationEnd'] if kind == 'calibration' else request.get('end') or p['evaluationEnd']
    spatial = grid_spatial.load(value['spatialVersion'])
    if 'inputRequirement' not in value:
        original = next((r for r in db.items('input_products') if r['id'] == value.get('inputId')), None)
        if original:
            value['inputRequirement'] = {'transformationSHA256': original['manifest'].get('transformationSHA256')}
    product = _input(db, value, spatial, requested_end, request.get('inputId'))
    value['inputId'] = product['id']
    value = validate(db, value, requested_end)
    start = pd.Timestamp(request.get('start') or p['trainingStart'])
    end = pd.Timestamp(requested_end)
    warmup = pd.Timestamp(p['warmupStart'])
    if any(pd.isna(t) or t.tzinfo or t != t.normalize() for t in (start, end)) or start < warmup or start >= end:
        raise ValueError('开始和结束须为预热起点之后的完整北京时间日，且结束晚于开始')
    if kind == 'calibration':
        run_parameters = deepcopy(value.get('calibrationInitialParameters', value['parameters']))
        run_regions = deepcopy(value.get('calibrationInitialPartitionParameters', value['partitionParameters']))
    else:
        run_parameters = deepcopy(value['parameters'])
        run_regions = deepcopy(value['partitionParameters'])
    if request.get('candidateParameterId'):
        chosen = _verified_parameter(db, request['candidateParameterId'], value)
        run_parameters, run_regions = deepcopy(chosen['parameters']), deepcopy(chosen['partitionParameters'])
    initial = deepcopy(value.get('initialization') or {})
    for key in ('initialLakeLevel', 'initialLakeTime', 'initialLakeSource'):
        if key in request:
            initial[key] = request[key]
    earliest_lake_day = None
    if initial.get('initialLakeLevel') is not None and initial.get('initialLakeTime'):
        anchor_time = pd.Timestamp(initial['initialLakeTime'])
        earliest_lake_day = (anchor_time.normalize() +
                             (pd.Timedelta(days=1) if anchor_time != anchor_time.normalize() else pd.Timedelta(0)))
        if end <= earliest_lake_day:
            raise ValueError(f'该方案湖位初始化为{anchor_time:%Y-%m-%d %H:%M}，完整湖位从{earliest_lake_day:%Y-%m-%d}起，请调整结束日期或计算设置')
    return {**request, **{k: v for k, v in initial.items() if k in ('initialLakeLevel', 'initialLakeTime', 'initialLakeSource')},
            'modelFamily': 'grid-continuous-v1', 'spatialVersion': value['spatialVersion'],
            'inputId': product['id'], 'parameters': run_parameters, 'partitionParameters': run_regions,
            'routingMode': value['routingMode'], 'start': warmup.isoformat(), 'end': end.isoformat(),
            'evaluationStart': start.isoformat(), 'evaluationEnd': end.isoformat(),
            'earliestCompleteLakeDay': earliest_lake_day.date().isoformat() if earliest_lake_day is not None else None,
            'schemeSnapshot': deepcopy({'id': identifier, **row['payload']}), 'researchConfiguration': value,
            'target': value['target'], 'periods': deepcopy(p), 'search': deepcopy(value['search']), 'origin': 'user-research'}
