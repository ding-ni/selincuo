"""Business schemes and parameter groups backed by immutable run identities."""
from copy import deepcopy
import uuid

import numpy as np
import pandas as pd

from .common import ROOT, canonical, digest, now, read_json
from .model_core import parameters, PARAMETER_DEFINITIONS, PARAMETER_NAMES, normalize_initial_hydrology
from .calibration_config import RUNOFF_KEYS, ROUTING_KEYS, public_config
from .operational_scheme import definition, catalog
from .parameter_regions import resolve_regions
from .parameter_transfer import normalize_transfer_policy, resolve_transfer
from . import grid_spatial


BUILTIN_PATH = ROOT / 'config/operational/continuous-builtin-v1.json'
SUPPORTED_UNITS = {'grid-partition-elevation-surface-v2',
                   'partition-elevation-surface-area-weighted-v1'}
INTEGRATED_KEYS = RUNOFF_KEYS + ROUTING_KEYS + ['lakeCoefficient']
# The loss is selected before looking at any candidate. Each observation
# series supplies its own training-period scale and dates.
INTEGRATED_OBJECTIVE = {'method': 'rmse-over-training-std-v1',
                        'weights': {'flow': .5, 'lake': .5},
                        'minimumPairs': 30, 'minimumScale': 1e-10}


def _current_workflow_defaults(db, value):
    """Only new drafts opt into lake selection; saved revisions stay intact."""
    from .two_stage_calibration import defaults
    from .model_forcing import current_summary
    options = {**defaults(), **deepcopy(value.get('twoStage') or {})}
    options.update(selectionMode='flow-constrained-lake', candidateLimit=256, jointBudget=2000,
                   flowConstraints={'minNse': .70, 'maxAbsPbias': 5.0})
    options.setdefault('candidateSourceTaskId', 'JOB-4934a5d35c9344a59353')
    value['twoStage'] = options
    coverage = current_summary(db) or {}
    if (coverage.get('firstCompleteDay') or '9999') <= '2019-01-01':
        value['periods']['warmupStart'] = min(value['periods']['warmupStart'], '2019-01-01T00:00:00')
    return value


def _new_scheme_defaults(value):
    """New drafts use one explicit donor vector; saved schemes retain history."""
    original = {'parameters': deepcopy(value['parameters']),
                'partitionParameters': deepcopy(value.get('partitionParameters', {}))}
    donor = parameters({**value['parameters'], **value.get('partitionParameters', {}).get('duja', {})})
    # ERA5-Land potential open-water evaporation is a proxy, not a measured
    # lake flux. A unit conversion coefficient avoids inheriting a coefficient
    # previously fitted to H into a new flow-only workflow.
    donor['lakeCoefficient'] = 1.
    value['parameters'] = donor
    value['partitionParameters'] = {}
    value.pop('calibrationInitialParameters', None)
    value.pop('calibrationInitialPartitionParameters', None)
    value.pop('adoptedParameterId', None)
    value['parameterTransfer'] = {'version': 1, 'mode': 'duja-shared', 'donor': 'duja', 'areaExponent': .5}
    value['sourceTransferSnapshot'] = {**original, 'method': 'explicit-new-draft-duja-lift',
                                       'source': 'continuous-builtin-v1.json', 'verifiedCalibration': False}
    value['lakeParameterSource'] = {'method': 'fixed-potential-open-water-proxy', 'coefficient': 1.,
                                    'calibratedToLakeLevel': False, 'directLakeEvaporationObserved': False}
    value['initialization'] = {'mode': 'automatic'}
    value['target'] = 'duja'
    search = value['search']
    search['scope'] = 'duja'
    search['keys'] = list(public_config()['flowSelectableKeys'])
    search.setdefault('budgets', {}).setdefault('snow', 120)
    search.setdefault('seeds', [search.get('seed', 42)])
    search.pop('joint', None)
    value['periods']['warmupStart'] = '2020-01-01T00:00:00'
    from .two_stage_calibration import defaults
    value['twoStage']=defaults()
    value['purpose']='business'
    return value


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
    # Visibility is business metadata, not part of the immutable configuration.
    family = value.get('familyId') or identifier
    return {'id': identifier, 'name': value['name'], 'payload': value, 'builtin': True,
            'trashed': bool(db.setting('scheme-hidden:' + family, False))}


def template(db):
    from .business_selection import current
    selection=current(db)
    if selection.get('schemeId'):
        value=deepcopy(_scheme(db,selection['schemeId'])['payload'])
        for key in ('builtin','configurationSHA256','resultId','adoptedParameterId','id','familyId','revision','parentRevisionId'):
            value.pop(key,None)
        value['name']='我的色林错方案'
        value['purpose']='business'
        value['calibrationInitialParameters']=deepcopy(value['parameters'])
        value['calibrationInitialPartitionParameters']={}
        source=db.task(selection['resultId']).get('result') or {}
        value['parameterSources']={**deepcopy(source.get('parameterSources',{})),
                                   'sourceTaskId':selection['resultId']}
        return _current_workflow_defaults(db, value)
    standard = builtin(db)
    if standard:
        value = deepcopy(standard['payload'])
        for key in ('builtin', 'configurationSHA256', 'resultId'):
            value.pop(key, None)
        value['name'] = '我的色林错方案'
        value['origin'] = 'user'
        return _current_workflow_defaults(db, _new_scheme_defaults(value))
    fixed = definition()
    spatial = grid_spatial.current()
    choice = catalog(db)[0]
    return _current_workflow_defaults(db, _new_scheme_defaults({'schemaVersion': 2, 'name': '我的色林错方案', 'modelFamily': 'grid-continuous-v1',
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
                       'objectiveProfile': 'balanced'}, 'origin': 'user'}))


def _family(row):
    return row['payload'].get('familyId') or row['id']


def list_schemes(db, include_trashed=False, include_experiments=False):
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
    from .business_selection import purpose,current
    chosen=current(db)
    standard = builtin(db)
    if standard and not chosen and (include_trashed or not standard.get('trashed',False)):
        result.append(standard)
    adopted=next((r for r in db.items('schemes') if r['id']==chosen.get('schemeId')),None)
    if adopted and latest.get(_family(adopted),{}).get('id')!=adopted['id']:
        # A newly saved revision is a draft until a completed result is adopted.
        # Keep the exact adopted revision selectable alongside that draft.
        result.append({**adopted,'trashed':False})
    for row in latest.values():
        if not include_experiments and purpose(db,row)=='development':
            continue
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
    family = _family(row)
    from .business_selection import current
    if trashed and current(db).get('familyId')==family:
        raise ValueError('该方案正在采用，请先采用其他方案')
    if trashed:
        ids = [r['id'] for r in db.items('schemes') if _family(r) == family]
        if row.get('builtin'):
            ids.append(row['id'])
        for active in db.rows("SELECT id,request FROM tasks WHERE status IN ('queued','running','stopping')"):
            import json
            if json.loads(active['request']).get('researchSchemeId') in ids:
                raise ValueError('方案正在计算，请先等待任务完成')
    db.set_setting('scheme-hidden:' + family, bool(trashed))
    db.log('schemes.visibility', {'id': identifier, 'familyId': family, 'trashed': bool(trashed)})
    return {'id': identifier, 'trashed': bool(trashed)}


def list_parameters(db, include_trashed=False):
    """Keep saved parameter bytes and expose their recoverable visibility."""
    result = []
    for row in db.items('parameter_versions'):
        row['trashed'] = bool(db.setting('parameter-hidden:' + row['id'], False))
        if include_trashed or not row['trashed']:
            result.append(row)
    return result


def parameter_metadata(db, identifier, trashed):
    if not isinstance(identifier, str) or not identifier or not isinstance(trashed, bool):
        raise ValueError('请选择参数并指定是否移入回收站')
    from .business_selection import current
    if trashed and current(db).get('parameterId')==identifier:
        raise ValueError('该参数正在采用，请先采用其他参数')
    # Visibility and audit are committed together; parameter/result bytes and
    # already captured task requests are left unchanged.
    with db.connect() as connection:
        if not connection.execute('SELECT 1 FROM parameter_versions WHERE id=?', (identifier,)).fetchone():
            raise ValueError('参数组不存在')
        connection.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                           ('parameter-hidden:' + identifier, canonical(trashed)))
        connection.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                           (now(), 'parameters.visibility', canonical({'id': identifier, 'trashed': trashed})))
    return {'id': identifier, 'trashed': trashed}


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
    from .model_forcing import resolve
    product, _ = resolve(db, value['periods']['warmupStart'], end, spatial)
    if identifier and identifier != product['id']:
        raise ValueError('气象资料已更新，请重新检查所选时期')
    required = value.get('inputRequirement', {}).get('transformationSHA256')
    if required and required != product['manifest'].get('transformationSHA256'):
        raise ValueError('方案与当前气象资料的湖面降水处理不兼容')
    return product


def validate(db, payload, coverage_end=None):
    value = deepcopy(payload)
    if 'twoStage' in value:
        from .two_stage_calibration import validate_options
        value['twoStage']=validate_options(value['twoStage'])
    if value.get('schemaVersion') != 2 or value.get('modelFamily') != 'grid-continuous-v1':
        raise ValueError('请选择有效的连续计算方案')
    if not str(value.get('name', '')).strip() or len(value['name']) > 120:
        raise ValueError('请填写1—120字方案名称')
    spatial = grid_spatial.load(value['spatialVersion'])
    if spatial.get('unitStrategy') not in SUPPORTED_UNITS:
        raise ValueError('方案空间资料不受支持')
    value['parameters'] = parameters(value.get('parameters'))
    if 'initialHydrology' in value:
        value['initialHydrology'] = normalize_initial_hydrology(value['initialHydrology'])
    resolve_transfer(spatial['units'], value['parameters'], value.get('partitionParameters'), value.get('parameterTransfer'))
    value['partitionParameters'], _ = resolve_regions(spatial['units'], value['parameters'], value.get('partitionParameters'))
    if 'parameterTransfer' in value:
        value['parameterTransfer'] = normalize_transfer_policy(value['parameterTransfer'])
    # Resolve both adopted and calibration-initial vectors now. A new shared
    # strategy must never retain a hidden independent Duja override.
    resolve_transfer(spatial['units'], value['parameters'], value['partitionParameters'], value.get('parameterTransfer'))
    if 'calibrationInitialParameters' in value or 'calibrationInitialPartitionParameters' in value:
        initial = parameters(value.get('calibrationInitialParameters'))
        resolve_transfer(spatial['units'], initial, value.get('calibrationInitialPartitionParameters'), value.get('parameterTransfer'))
        initial_regions, _ = resolve_regions(spatial['units'], initial, value.get('calibrationInitialPartitionParameters'))
        value['calibrationInitialParameters'] = initial
        value['calibrationInitialPartitionParameters'] = initial_regions
        resolve_transfer(spatial['units'], initial, initial_regions, value.get('parameterTransfer'))
    periods = value['periods']
    dates = {key: pd.Timestamp(periods[key]) for key in
             ('warmupStart', 'trainingStart', 'trainingEnd', 'evaluationStart', 'evaluationEnd')}
    if any(pd.isna(t) or t.tzinfo or t != t.normalize() for t in dates.values()):
        raise ValueError('方案时段使用北京时间完整日边界')
    if not dates['warmupStart'] < dates['trainingStart'] < dates['trainingEnd'] <= dates['evaluationStart'] < dates['evaluationEnd']:
        raise ValueError('预热、率定、评价须按顺序且不重叠')
    if dates['trainingStart'] - dates['warmupStart'] < pd.Timedelta(days=365):
        raise ValueError('率定前至少保留365天统一预热')
    _input(db, value, spatial, coverage_end or periods['evaluationEnd'])
    search = value['search']
    defs = {d['key']: d for d in PARAMETER_DEFINITIONS}
    keys = search['keys']
    lake_only=value.get('twoStage',{}).get('mode')=='lake'
    if (not keys and not lake_only) or len(keys) != len(set(keys)) or set(keys) - set(defs):
        raise ValueError('参与搜索参数无效或重复')
    if search.get('scope') not in ('duja', 'background'):
        raise ValueError('参数区域无效')
    if value.get('target') not in ('duja', 'lake', 'integrated'):
        raise ValueError('率定对象无效')
    if value['target'] == 'duja' and search['scope'] != 'duja':
        raise ValueError('杜加站流量率定须调整杜加控制区参数')
    if value['target'] == 'lake' and search['scope'] != 'background':
        raise ValueError('湖泊率定须使用全湖参数')
    if value['target'] == 'lake' and keys != ['lakeCoefficient']:
        raise ValueError('湖泊对象仅搜索全湖蒸发系数')
    if value['target'] == 'duja' and 'lakeCoefficient' in keys:
        raise ValueError('杜加产汇流不同时搜索湖泊系数')
    if value['target'] == 'duja' and set(keys) - set(public_config()['flowSelectableKeys']):
        raise ValueError('所选参数不受杜加流量约束，请使用对应过程资料或湖泊研究阶段')
    if value['target'] == 'integrated':
        if value.get('parameterTransfer', {}).get('mode', 'legacy') != 'legacy':
            raise ValueError('共享移植方案先用杜加流量率定、湖位评价；联合研究请另建保留分区的方案')
        if search['scope'] != 'duja' or set(keys) - set(INTEGRATED_KEYS):
            raise ValueError('综合率定仅调整杜加上游产汇流参数与全湖蒸发系数')
        if search.get('joint', INTEGRATED_OBJECTIVE) != INTEGRATED_OBJECTIVE:
            raise ValueError('综合率定评价方法与权重须使用已确定的配置')
        search['joint'] = deepcopy(INTEGRATED_OBJECTIVE)
        search['budgets'].setdefault('integrated', 120)
    initial = value.get('calibrationInitialParameters', value['parameters'])
    initial_regions = value.get('calibrationInitialPartitionParameters', value['partitionParameters'])
    baseline = {**initial, **(initial_regions.get('duja', {}) if search['scope'] == 'duja' else {})}
    bound_keys=list(keys)
    if value.get('twoStage',{}).get('mode') in ('both','lake') and 'lakeCoefficient' not in bound_keys:
        bound_keys.append('lakeCoefficient')
    for key in bound_keys:
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
    if 'snow' in search['budgets']:
        budget = search['budgets']['snow']
        if type(budget) != int or not 1 <= budget <= 2000:
            raise ValueError('雪冰阶段预算须为1—2000的整数')
    if 'seeds' in search:
        seeds = search['seeds']
        if (not isinstance(seeds, list) or not 1 <= len(seeds) <= 4 or
                any(type(seed) != int or not 0 <= seed < 2 ** 32 for seed in seeds) or
                len(set(seeds)) != len(seeds)):
            raise ValueError('多起点须提供1—4个不同的有效整数种子')
    if value['target'] == 'integrated':
        budget = search['budgets']['integrated']
        if type(budget) != int or not 1 <= budget <= 2000:
            raise ValueError('综合率定次数须为1—2000的整数')
    if type(search.get('seed')) != int or not 0 <= search['seed'] < 2 ** 32:
        raise ValueError('随机种子无效')
    if search.get('objectiveProfile') not in ('balanced', 'peaks'):
        raise ValueError('率定目标无效')
    if value.get('routingMode') != 'source-separated':
        raise ValueError('连续方案采用分水源调蓄与河道汇流')
    return value


def save(db, payload, parent_id=None):
    parent = _scheme(db, parent_id) if parent_id else None
    if parent and db.setting('scheme-hidden:' + _family(parent), False):
        raise ValueError('方案已移入回收站，请先恢复')
    value = validate(db, payload)
    for key in ('configurationSHA256', 'revision', 'familyId', 'parentRevisionId', 'id', 'created_at', 'builtin', 'resultId'):
        value.pop(key, None)
    # Input identity belongs to a run, not to a mutable user scheme.
    value.pop('inputId', None)
    if parent and not parent.get('builtin'):
        latest_revision = max((r['payload'].get('revision', 1) for r in db.items('schemes')
                               if _family(r) == _family(parent)), default=1)
        if parent['payload'].get('revision', 1) != latest_revision:
            raise ValueError('方案已有更新，请刷新后再保存，或复制为新方案')
    if value.get('adoptedParameterId'):
        try:
            saved = _verified_parameter(db, value['adoptedParameterId'], value)
            if (saved['parameters'] != value['parameters'] or saved['partitionParameters'] != value['partitionParameters'] or
                    saved.get('parameterTransfer') != normalize_transfer_policy(value.get('parameterTransfer'))):
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
    policy = normalize_transfer_policy(result.get('parameterTransfer'))
    _, _, actual_audit = resolve_transfer(spatial['units'], checked, regions, policy)
    effective = actual_audit['effective']
    if result.get('parameterTransferAudit') and result['parameterTransferAudit']['effective'] != effective:
        raise ValueError('成果有效分区参数与策略不一致')
    return {'sourceTaskId': task_id, 'sourceTaskKind': task.get('kind'),
            'sourceSchemeId': (task.get('request') or {}).get('researchSchemeId'),
            'modelFamily': result.get('modelFamily'), 'spatialVersion': spatial_version,
            'routingMode': result.get('routingMode'), 'parameters': deepcopy(checked),
            'partitionParameters': deepcopy(regions),
            'parameterTransfer': policy,
            'effectiveParameterSHA256': digest({'parameters': effective['base'],
                                               'partitionParameters': effective['partitionParameters']}),
            'effectiveAuditSHA256': digest(effective),
            'resultIdentitySHA256': digest(result.get('identity') or {}),
            'parameterSHA256': digest({'parameters': checked, 'partitionParameters': regions, 'parameterTransfer': policy})}


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
    if normalize_transfer_policy(saved.get('parameterTransfer')) != actual['parameterTransfer']:
        raise ValueError('参数组的传递策略与来源成果不一致')
    if saved.get('resultIdentitySHA256') is not None and saved['resultIdentitySHA256'] != actual['resultIdentitySHA256']:
        raise ValueError('参数组的来源成果身份已变化，请核对原始运行记录')
    if actual['modelFamily'] != scheme['modelFamily'] or actual['spatialVersion'] != scheme['spatialVersion'] or actual['routingMode'] != scheme['routingMode']:
        raise ValueError('参数组与方案的模型、空间或汇流方式不兼容')
    if normalize_transfer_policy(scheme.get('parameterTransfer')) != actual['parameterTransfer']:
        raise ValueError('参数组与方案的传递策略不兼容，请先显式建立同策略的新方案')
    return actual


def adopt_parameters(db, scheme_id, parameter_id):
    row = _scheme(db, scheme_id)
    value = deepcopy(row['payload'])
    if db.setting('parameter-hidden:' + str(parameter_id), False):
        raise ValueError('参数已移入回收站，请先恢复')
    chosen = _verified_parameter(db, parameter_id, value)
    # Adoption never changes the independent starting point used by calibration.
    value.setdefault('calibrationInitialParameters', deepcopy(value['parameters']))
    value.setdefault('calibrationInitialPartitionParameters', deepcopy(value['partitionParameters']))
    value['parameters'] = deepcopy(chosen['parameters'])
    value['partitionParameters'] = deepcopy(chosen['partitionParameters'])
    value['parameterTransfer'] = deepcopy(chosen['parameterTransfer'])
    value['adoptedParameterId'] = parameter_id
    return save(db, value, scheme_id)


def prepare_request(db, request, kind='simulation'):
    identifier = request.get('researchSchemeId')
    row = _scheme(db, identifier)
    if db.setting('scheme-hidden:' + _family(row), False):
        raise ValueError('方案已移入回收站，请先恢复')
    value = deepcopy(row['payload'])
    hydrology_initial = normalize_initial_hydrology(value.get('initialHydrology'))
    if ('initialHydrology' in request and
            normalize_initial_hydrology(request['initialHydrology']) != hydrology_initial):
        raise ValueError('陆域初始蓄水条件与方案不一致，请先保存方案')
    if ('parameterTransfer' in request and
            normalize_transfer_policy(request['parameterTransfer']) != normalize_transfer_policy(value.get('parameterTransfer'))):
        raise ValueError('请求的参数传递策略与已保存方案不一致，请先保存新的方案')
    p = value['periods']
    requested_end = p['evaluationEnd'] if kind == 'calibration' else request.get('end') or p['evaluationEnd']
    start = pd.Timestamp(request.get('start') or p['trainingStart'])
    end = pd.Timestamp(requested_end)
    if any(pd.isna(t) or t.tzinfo or t != t.normalize() for t in (start, end)) or start >= end:
        raise ValueError('开始和结束须为完整北京时间日，且结束晚于开始')
    configured_warmup = pd.Timestamp(p['warmupStart'])
    initial = deepcopy(value.get('initialization') or {})
    for key in ('initialLakeLevel', 'initialLakeTime', 'initialLakeSource'):
        if key in request:
            initial[key] = request[key]
    mode = initial.get('mode', 'legacy')
    if mode not in ('automatic', 'manual', 'legacy'):
        raise ValueError('湖位起算方式无效')
    anchor_time = pd.Timestamp(initial['initialLakeTime']) if initial.get('initialLakeTime') else None
    if mode == 'manual' and any(initial.get(k) in (None, '') for k in ('initialLakeLevel', 'initialLakeTime', 'initialLakeSource')):
        raise ValueError('自定湖位起算需要时刻、水位及依据来源')
    # Earlier requested periods need a full year of forcing before the requested
    # start. An explicitly fixed earlier lake anchor also needs the same land
    # prehistory for every output window, not an origin chosen by each window.
    warmup = min(configured_warmup, start - pd.Timedelta(days=365)) if kind == 'simulation' else configured_warmup
    if mode in ('manual', 'legacy') and anchor_time is not None:
        warmup = min(warmup, pd.Timestamp(year=anchor_time.year - 1, month=1, day=1))
    p['warmupStart'] = warmup.isoformat()
    spatial = grid_spatial.load(value['spatialVersion'])
    if 'inputRequirement' not in value:
        original = next((r for r in db.items('input_products') if r['id'] == value.get('inputId')), None)
        if original:
            value['inputRequirement'] = {'transformationSHA256': original['manifest'].get('transformationSHA256')}
    product = _input(db, value, spatial, requested_end, request.get('inputId'))
    value['inputId'] = product['id']
    value = validate(db, value, requested_end)
    if start < warmup:
        raise ValueError('开始日期早于可用预热起点')
    if kind == 'calibration':
        run_parameters = deepcopy(value.get('calibrationInitialParameters', value['parameters']))
        run_regions = deepcopy(value.get('calibrationInitialPartitionParameters', value['partitionParameters']))
    else:
        run_parameters = deepcopy(value['parameters'])
        run_regions = deepcopy(value['partitionParameters'])
    if request.get('candidateParameterId'):
        if db.setting('parameter-hidden:' + str(request['candidateParameterId']), False):
            raise ValueError('参数已移入回收站，请先恢复')
        chosen = _verified_parameter(db, request['candidateParameterId'], value)
        run_parameters, run_regions = deepcopy(chosen['parameters']), deepcopy(chosen['partitionParameters'])
    anchor_limit = pd.Timestamp(p['trainingEnd']) if kind == 'calibration' else end
    no_automatic_anchor = mode in ('manual', 'legacy') and anchor_time is not None and anchor_time >= anchor_limit
    # A known anchor remains the same even when the selected output begins
    # before it. If it falls after this calculation, leave lake level absent;
    # never switch to a different observation just because the window changed.
    if mode == 'automatic' or no_automatic_anchor:
        for key in ('initialLakeLevel', 'initialLakeTime', 'initialLakeSource'):
            initial.pop(key, None)
    earliest_lake_day = None
    if initial.get('initialLakeLevel') is not None and initial.get('initialLakeTime'):
        anchor_time = pd.Timestamp(initial['initialLakeTime'])
        earliest_lake_day = (anchor_time.normalize() +
                             (pd.Timedelta(days=1) if anchor_time != anchor_time.normalize() else pd.Timedelta(0)))
        if end <= earliest_lake_day:
            earliest_lake_day = None
    # An automatic/legacy fallback must not retain manual values from the
    # request after the effective initialization has been cleared above.
    fields = ('initialLakeLevel', 'initialLakeTime', 'initialLakeSource')
    base_request = {k: v for k, v in request.items() if k not in fields}
    return {**base_request, **{k: v for k, v in initial.items() if k in fields},
            'modelFamily': 'grid-continuous-v1', 'spatialVersion': value['spatialVersion'],
            'inputId': product['id'], 'parameters': run_parameters, 'partitionParameters': run_regions,
            'initialHydrology': hydrology_initial,
            'parameterTransfer': normalize_transfer_policy(value.get('parameterTransfer')),
            'routingMode': value['routingMode'], 'start': warmup.isoformat(), 'end': end.isoformat(),
            'evaluationStart': start.isoformat(), 'evaluationEnd': end.isoformat(),
            '_noAutomaticLakeAnchor': no_automatic_anchor,
            'earliestCompleteLakeDay': earliest_lake_day.date().isoformat() if earliest_lake_day is not None else None,
            'schemeSnapshot': deepcopy({'id': identifier, **row['payload']}), 'researchConfiguration': value,
            'target': value['target'], 'periods': deepcopy(p), 'search': deepcopy(value['search']), 'origin': 'user-research'}


def resolved_initialization(request, observation, kind='simulation'):
    """Use only information available before the end of training for calibration."""
    from .grid_simulation import anchor
    if request.get('_noAutomaticLakeAnchor'):
        return None, None
    end = pd.Timestamp(request['periods']['trainingEnd']) if kind == 'calibration' else pd.Timestamp(request['end'])
    available = observation
    if kind == 'calibration':
        available = {**observation, 'level': {t: v for t, v in observation.get('level', {}).items()
                                               if pd.Timestamp(t) < end}}
    initial = anchor(request, available, pd.Timestamp(request['start']), end)
    if initial is None:
        return None, None
    at = pd.Timestamp(initial['time'])
    first_day = at.normalize() + (pd.Timedelta(days=1) if at != at.normalize() else pd.Timedelta(0))
    if first_day >= pd.Timestamp(request['end']):
        return initial, None
    if kind == 'calibration' and at >= end:
        raise ValueError('率定不能使用评价期的湖位作为起算依据')
    return initial, first_day.date().isoformat()


def plan_token(request, initialization, observation):
    return digest({'scheme': request['schemeSnapshot'], 'inputId': request['inputId'],
                   'start': request['start'], 'end': request['end'],
                   'evaluationStart': request['evaluationStart'],
                   'parameters': request['parameters'], 'partitionParameters': request['partitionParameters'],
                   'parameterTransfer': normalize_transfer_policy(request.get('parameterTransfer')),
                   'initialHydrology': normalize_initial_hydrology(request.get('initialHydrology')),
                   'initialization': initialization, 'observation': digest(observation)})
