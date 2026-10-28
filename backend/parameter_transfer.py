"""Resolve explicit, auditable parameter-transfer research alternatives.

The legacy path preserves the existing global and regional parameter semantics.
The two transfer modes use one shared input vector.  The area-routing mode is
an equal-shape/equal-velocity sensitivity assumption, not a surveyed law.
"""
from collections.abc import Mapping
from copy import deepcopy
import math

from .model_core import PARAMETER_DEFINITIONS, PARAMETER_NAMES, parameters
from .parameter_regions import resolve_regions


MODES = ('legacy', 'duja-shared', 'duja-area-routing')
SCALED_PARTITIONS = frozenset(('duja', 'zajia_rest', 'zagen', 'boqu', 'ali'))
REGIONAL_KEYS = tuple(key for key in PARAMETER_NAMES if key != 'lakeCoefficient')
ROUTING_KEYS = ('tauHours', 'lagHours')
BOUNDS = {item['key']: (item['min'], item['max']) for item in PARAMETER_DEFINITIONS}


def normalize_transfer_policy(policy=None):
    """Validate the policy without claiming that a declared source is verified."""
    if policy is None:
        policy = {}
    if not isinstance(policy, Mapping):
        raise ValueError('参数迁移设置必须为对象')
    allowed = {'version', 'mode', 'donor', 'areaExponent', 'sourceTaskId'}
    unknown = set(policy) - allowed
    if unknown:
        raise ValueError('未知参数迁移设置：' + ', '.join(sorted(map(str, unknown))))
    version = policy.get('version', 1)
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError('不支持的参数迁移设置版本')
    mode = policy.get('mode', 'legacy')
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError('未知参数迁移方式')
    donor = policy.get('donor', 'duja')
    if donor != 'duja':
        raise ValueError('当前参数迁移仅支持杜加作为参考区')
    exponent = policy.get('areaExponent', .5)
    if (isinstance(exponent, bool) or not isinstance(exponent, (int, float)) or
            not math.isfinite(exponent) or exponent != .5):
        raise ValueError('当前面积缩放仅支持平方根指数 0.5')
    result = {'version': 1, 'mode': mode, 'donor': 'duja', 'areaExponent': .5}
    if 'sourceTaskId' in policy:
        source = policy['sourceTaskId']
        if not isinstance(source, str) or not source.strip():
            raise ValueError('参数来源任务编号必须是非空文字')
        result['sourceTaskId'] = source
    return result


def _region_areas(units, strict):
    """Sum the actual land and glacier HRU areas; never substitute lake area."""
    pieces = {}
    invalid = set()
    for unit in units:
        partition = unit['partition']
        pieces.setdefault(partition, {'land': [], 'glacier': []})
        kind = unit.get('kind')
        value = unit.get('areaKm2')
        good = (kind in ('land', 'glacier') and not isinstance(value, bool) and
                isinstance(value, (int, float)) and math.isfinite(value) and value > 0)
        if not good:
            if strict:
                raise ValueError('参数迁移需要每个陆地或冰川单元的有效正面积')
            invalid.add(partition)
            continue
        pieces[partition][kind].append(value)
    result = {}
    for partition, kinds in pieces.items():
        if partition in invalid:
            result[partition] = {'areaKm2': None, 'landAreaKm2': None, 'glacierAreaKm2': None}
            continue
        try:
            land = math.fsum(kinds['land'])
            glacier = math.fsum(kinds['glacier'])
            total = math.fsum((land, glacier))
        except OverflowError as exc:
            if strict:
                raise ValueError('参数迁移的分区面积无效') from exc
            result[partition] = {'areaKm2': None, 'landAreaKm2': None, 'glacierAreaKm2': None}
            continue
        if not math.isfinite(total):
            if strict:
                raise ValueError('参数迁移的分区面积无效')
            result[partition] = {'areaKm2': None, 'landAreaKm2': None, 'glacierAreaKm2': None}
            continue
        result[partition] = {'areaKm2': total, 'landAreaKm2': land, 'glacierAreaKm2': glacier}
    return result


def _scale_basis(partition, mode):
    if mode != 'duja-area-routing':
        return 'not-requested'
    if partition in SCALED_PARTITIONS:
        return 'named-model-partition-assumption'
    if partition == 'fringe':
        return 'distributed-direct-lake-inflow'
    if partition in ('unresolved', 'external'):
        return 'not-a-verified-catchment'
    return 'partition-not-in-scaling-scope'


def resolve_transfer(units, base, overrides=None, policy=None):
    """Return (global parameters, normalized region patches, provenance audit).

    New policies require an empty input patch map.  A caller migrating a Duja
    patch must explicitly create its shared base beforehand, and retain that
    step's source as a declaration instead of silently choosing between inputs.
    The returned materialized patches are effective values, never raw inputs.
    """
    rule = normalize_transfer_policy(policy)
    raw_base = deepcopy(base)
    raw_overrides = deepcopy(overrides)
    p = parameters(base)
    supplied = {} if overrides is None else overrides
    if not isinstance(supplied, Mapping):
        raise ValueError('分区参数必须为对象')
    mode = rule['mode']
    if mode == 'legacy':
        patches, regions = resolve_regions(units, p, supplied)
        areas = _region_areas(units, strict=False)
    else:
        if supplied:
            raise ValueError('参数迁移模式须先迁移到共享基准并清除覆盖')
        areas = _region_areas(units, strict=True)
        if 'duja' not in areas or not areas['duja']['areaKm2']:
            raise ValueError('参数迁移需要杜加参考区有效面积')
        patches, regions = {}, {}

    raw_mapping = base if isinstance(base, Mapping) else {}
    rows = {}
    for partition, details in areas.items():
        scale_basis = _scale_basis(partition, mode)
        apply_scale = mode == 'duja-area-routing' and partition in SCALED_PARTITIONS
        factor = 1.
        if apply_scale:
            factor = math.sqrt(details['areaKm2'] / areas['duja']['areaKm2'])
            if not math.isfinite(factor):
                raise ValueError('参数迁移的面积缩放无效')
        if mode == 'legacy':
            regional = regions.get(partition, p)
            regional_patch = patches.get(partition, {})
        else:
            regional = dict(p)
            regional_patch = {}
        sources = {}
        routing = {}
        for key in PARAMETER_NAMES:
            source = 'global-base' if key in raw_mapping else 'model-default'
            if mode == 'legacy' and key in regional_patch:
                source = 'regional-override'
            if key in ROUTING_KEYS:
                value = regional[key]
                unbounded = value * factor if apply_scale else value
                if not math.isfinite(unbounded):
                    raise ValueError('参数迁移的汇流时间缩放无效')
                lower, upper = BOUNDS[key]
                bounded = max(lower, min(upper, unbounded))
                if apply_scale:
                    regional[key] = bounded
                    source = 'area-scaled:' + source
                routing[key] = {'donorValue': p[key] if mode != 'legacy' else None,
                                'unclampedValue': unbounded, 'value': regional[key],
                                'min': lower, 'max': upper,
                                'clamped': apply_scale and bounded != unbounded}
            sources[key] = source
        if mode != 'legacy':
            # Lake evaporation is global and must never be placed in a region patch.
            regional_patch = {key: regional[key] for key in sorted(REGIONAL_KEYS)}
            patches[partition] = regional_patch
        rows[partition] = {'parameterGroup': 'partition:' + str(partition), **details,
                           'parameters': dict(regional), 'sources': sources,
                           'routingScale': factor, 'routingScaleApplied': apply_scale,
                           'routingScaleBasis': scale_basis, 'routing': routing}
    if mode != 'legacy':
        # Validate the final values through the exact same contract as normal runs.
        patches, _ = resolve_regions(units, p, patches)

    audit_units = []
    for index, unit in enumerate(units):
        identifier = unit.get('id')
        fallback = identifier is None or identifier == ''
        audit_units.append({'index': index,
                            'id': 'unit-index:' + str(index) if fallback else identifier,
                            'idSource': 'index-fallback' if fallback else 'unit',
                            'partition': unit['partition'],
                            'parameterGroup': 'partition:' + str(unit['partition'])})
    audit = {
        'schemaVersion': 1,
        'policy': rule,
        'raw': {'parameters': raw_base, 'partitionParameters': raw_overrides,
                'parameterTransfer': deepcopy(policy)},
        'effective': {'base': deepcopy(p), 'partitionParameters': deepcopy(patches), 'regions': rows},
        'units': audit_units,
        'structuralNotes': [
            {'condition': 'routingMode=lumped', 'inactiveKeys': ['interflowTauHours', 'baseflowTauHours'],
             'scope': 'routing', 'explanation': '合并汇流结构不使用壤中和地下独立退水时间。'},
            {'condition': 'unit.kind=glacier', 'inactiveKeys': ['KC', 'WM', 'B', 'C', 'SM', 'EX', 'KI', 'KG', 'IM'],
             'scope': 'unit-kernel', 'explanation': '冰川单元不使用陆面产流参数。'},
            {'condition': 'unit.kind=land', 'inactiveKeys': ['iceDdf', 'glacierRecessionDay', 'iceSublimationFraction', 'iceSublimationMaxMmHour'],
             'scope': 'unit-kernel', 'explanation': '陆地单元不使用冰川专用参数。'},
            {'condition': 'all-units', 'inactiveKeys': ['lakeCoefficient', 'tauHours', 'lagHours', 'interflowTauHours', 'baseflowTauHours', 'lapseRate'],
             'scope': 'unit-kernel-only', 'explanation': '这些参数分别作用于湖面、分区汇流或高程气温处理，不在单元产流核内直接使用。'},
        ],
        'assumptions': [
            '参数来源任务编号仅为调用方声明，未据此核验参数或任务血缘。',
            '空间单元保留原有气象格网、高程及陆地/冰川类型，不另加自由系数。',
            '面积缩放为等形、等流速的敏感性假设，不证明土壤相似或实测河道连通。',
            '湖周、未核实及外排区不按单一流域面积缩放；未核实和外排来水不计入湖。',
            '杜加站至湖口的传播与实际分区河道关系仍需独立资料。',
        ],
    }
    return p, patches, audit
