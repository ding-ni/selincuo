"""Packaged historical scheme: parameters, routing and initialization together.

The fixed configuration is usable without calibration or database registration.
Its scientific limitations remain in the configuration and technical report.
"""
from copy import deepcopy
import pandas as pd
from .common import ROOT, CONTEXT_KEY, digest, read_json
from .model_core import parameters
from .parameter_regions import resolve_regions

SCHEME_ID = 'selinco-historical-standard-v1'


def definition():
    scheme = read_json(ROOT / 'config/operational/historical-standard-v1.json')
    if not scheme or scheme['id'] != SCHEME_ID or scheme['projectId'] != 'selinco':
        raise ValueError('内置色林错方案配置缺失或身份不一致')
    scheme = deepcopy(scheme)
    scheme['parameters'] = parameters(scheme['parameters'])
    spatial = read_json(ROOT / 'data/derived/spatial/manifest.json')
    if not spatial or spatial['version'] != scheme['spatialVersion']:
        raise ValueError('当前空间资料与内置方案不一致，请在模型维护中更新方案')
    scheme['partitionParameters'], _ = resolve_regions(spatial['units'], scheme['parameters'], scheme['partitionParameters'])
    scheme['configurationSHA256'] = digest(scheme)
    return scheme


def catalog(db):
    scheme = definition()
    products = [r for r in db.items('input_products') if
                r['manifest']['spatialVersion'] == scheme['spatialVersion'] and
                r['manifest']['start'] <= scheme['initialization']['origin'] and
                r['manifest']['end'] >= '2025-01-01T00:00:00' and input_matches(scheme, r['manifest'])]
    products.sort(key=lambda r: (r['manifest']['end'], r['manifest']['hours'], r.get('created_at',''), r['id']), reverse=True)
    selected = products[0] if products else None
    return [{**scheme, 'available': bool(selected), 'inputId': selected['id'] if selected else '',
             'start': '2024-01-01T00:00:00',
             'end': selected['manifest']['end'][:10]+'T00:00:00' if selected else '',
             'message': '' if selected else '请接入覆盖2023预热及历史评价期的气象资料'}]


def input_matches(scheme, manifest):
    required = scheme.get('inputRequirement', {}).get('transformationSHA256')
    return required is None or manifest.get('transformationSHA256') == required


def prepare_request(db, request):
    scheme = definition()
    if request.get('schemeId') != scheme['id']:
        raise ValueError('内置计算方案不存在')
    if request.get('schemeRevision', scheme['revision']) != scheme['revision']:
        raise ValueError('方案版本已更新，请刷新后重新运行')
    choices = catalog(db)
    input_id = request.get('inputId') or choices[0]['inputId']
    product = next((r for r in db.items('input_products') if r['id'] == input_id), None)
    if not product:
        raise ValueError('缺少方案所需的真实气象输入')
    m = product['manifest']
    if not input_matches(scheme, m):
        raise ValueError('所选驱动不符合内置方案的湖面降水转换版本，请采用标准驱动')
    if m['spatialVersion'] != scheme['spatialVersion'] or m['start'] > scheme['initialization']['origin']:
        raise ValueError('所选资料不能覆盖内置方案的统一预热起点')
    start = pd.Timestamp(request.get('start') or choices[0]['start'])
    end = pd.Timestamp(request.get('end') or choices[0]['end'])
    if start.tzinfo or end.tzinfo or start != start.normalize() or end != end.normalize():
        raise ValueError('历史业务方案按完整北京时间日计算')
    if start < pd.Timestamp('2024-01-01') or start >= end or end > pd.Timestamp(m['end']):
        raise ValueError('请选择2024年起、真实资料覆盖范围内的完整历史时段')
    snapshot = {k: scheme[k] for k in ('id', 'name', 'revision', 'configurationSHA256', 'initialization', 'spatialVersion')}
    return {**request, 'projectId': 'selinco', 'contextKey': CONTEXT_KEY,
            'inputId': input_id, 'start': scheme['initialization']['origin'], 'end': end.isoformat(),
            'evaluationStart': start.isoformat(), 'evaluationEnd': end.isoformat(),
            'parameters': deepcopy(scheme['parameters']), 'partitionParameters': deepcopy(scheme['partitionParameters']),
            'routingMode': scheme['routingMode'], 'schemeSnapshot': snapshot,
            'schemeRevision': scheme['revision'], 'name': request.get('name') or f'色林错历史模拟 · {start:%Y-%m-%d}—{end-pd.Timedelta(days=1):%Y-%m-%d}'}
