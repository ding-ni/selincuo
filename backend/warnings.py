"""Versioned thresholds and local review/publication records."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

import numpy as np

from .common import now
from . import operational_store as store


COLORS = ('blue', 'yellow', 'orange', 'red')
LABELS = {'blue': '蓝色', 'yellow': '黄色', 'orange': '橙色', 'red': '红色'}

# Default suggested thresholds based on historical high-water and 2023 spill event.
# These are reference values only: mode is 'scenario', not 'business'.
# Platform operators must review and replace with confirmed survey values.
DEFAULT_LEVELS = [
    {'color': 'blue',   'levelM': 4543.00, 'probability': 0.30,
     'description': '接近历史高水位，注意监视'},
    {'color': 'yellow', 'levelM': 4543.50, 'probability': 0.30,
     'description': '超历史实测最高，启动预警'},
    {'color': 'orange', 'levelM': 4544.00, 'probability': 0.25,
     'description': '接近东侧低洼溢口高程，准备应急'},
    {'color': 'red',    'levelM': 4544.50, 'probability': 0.20,
     'description': '漫溢高风险，立即启动应急预案'},
]


def default_rules():
    """Return suggested threshold values when no rules have been saved yet.

    These are based on 2023 spill-event reference data and published literature.
    They are marked 'scenario' mode and must be confirmed before business use.
    """
    return {
        'name': '色林错湖位情景判据（内置参考）',
        'mode': 'scenario',
        'source': '平台内置参考值（基于2023年漫溢事件），须由主管人员根据实测资料确认后替换',
        'levels': [dict(color=r['color'], levelM=r['levelM'],
                        probability=r['probability']) for r in DEFAULT_LEVELS],
        'descriptions': {r['color']: r['description'] for r in DEFAULT_LEVELS},
        '_isDefault': True,
    }


def catalog(db):
    expire(db)
    return {'rules': store.list_records(db, 'warning-rules'),
            'warnings': store.list_records(db, 'warnings'), 'colors': LABELS}


def save_rules(db, payload, identifier=None):
    rows = []
    seen = set()
    for item in payload.get('levels') or []:
        color = item.get('color')
        if color not in COLORS or color in seen:
            raise ValueError('预警颜色须为不重复的蓝、黄、橙、红')
        h = float(item['levelM'])
        p = float(item.get('probability', .5))
        if not math.isfinite(h) or not 4000 <= h <= 6500 or not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError('预警水位或超限成员比例无效')
        rows.append({'color': color, 'levelM': h, 'probability': p})
        seen.add(color)
    if not rows:
        raise ValueError('请至少配置一个水位及对应超限比例')
    rows.sort(key=lambda r: COLORS.index(r['color']))
    if any(a['levelM'] > b['levelM'] for a, b in zip(rows, rows[1:])):
        raise ValueError('预警水位须按蓝、黄、橙、红逐级不降低')
    mode = payload.get('mode', 'scenario')
    if mode not in ('scenario', 'business'):
        raise ValueError('判据类别无效')
    source = str(payload.get('source') or '').strip()
    if mode == 'business' and not source:
        raise ValueError('业务判据须登记来源或批准文件')
    return store.save(db, 'warning-rules', {'name': str(payload.get('name') or '湖位判据'),
        'levels': rows, 'mode': mode, 'source': source,
        'description': str(payload.get('description') or '')}, identifier=identifier, prefix='RULE-')


def evaluate_rules(times, member_levels, rules):
    values = np.asarray(member_levels, dtype=float)
    if values.ndim == 1:
        values = values[None, :]
    if values.ndim != 2 or values.shape[1] != len(times) or not np.isfinite(values).all():
        raise ValueError('集合湖位或有效时段不完整')
    matches = []
    for row in rules['levels']:
        ratios = np.mean(values >= row['levelM'], axis=0)
        ids = np.flatnonzero((ratios > 0) & (ratios >= row['probability']))
        if len(ids):
            first = int(ids[0])
            matches.append({**row, 'firstTime': str(times[first]),
                'maximumProportion': float(ratios.max()),
                'firstProportion': float(ratios[first]), 'memberCount': int(values.shape[0])})
    selected = max(matches, key=lambda r: COLORS.index(r['color'])) if matches else None
    return {'selected': selected, 'matches': matches, 'memberCount': int(values.shape[0]),
            'peakLevelM': float(values.max())}


def _member_levels(db, forecast):
    value = forecast.get('memberLakeLevels') or forecast.get('memberLevels')
    if value is not None:
        return value
    artifact = forecast.get('memberArtifact') or forecast.get('memberDataPath')
    if artifact:
        from .common import within
        with np.load(within(db.workspace, artifact), allow_pickle=False) as arrays:
            for key in ('lakeLevel', 'memberLakeLevels', 'levels'):
                if key in arrays:
                    return arrays[key].copy()
    members = forecast.get('members')
    if members and isinstance(members[0], dict) and 'lakeLevel' in members[0]:
        return [member['lakeLevel'] for member in members]
    if forecast.get('memberCount', 1) == 1:
        return [forecast['control']['lakeLevel']]
    raise ValueError('预测缺少逐成员湖位，不能用分位数替代超限成员比例')


def draft(db, payload):
    from .forecasts import detail
    forecast = detail(db, payload['forecastId'])
    forecast = forecast.get('result', forecast)
    rules = store.get(db, 'warning-rules', payload['ruleSetId'])
    assessed = evaluate_rules(forecast['times'], _member_levels(db, forecast), rules)
    if assessed['selected'] is None:
        raise ValueError('当前预测未达到所选判据，无需生成分级提示')
    curve = forecast.get('lakeCurve') or {}
    provisional = bool(forecast.get('provisional', curve.get('provisional', True)) or curve.get('provisional', False))
    mode = 'business' if rules['mode'] == 'business' and not provisional else 'scenario'
    default_end = datetime.now(timezone.utc)+timedelta(days=1)
    valid_until = payload.get('validUntil') or default_end.isoformat(timespec='seconds')
    _instant(valid_until)
    value = {'name': ('湖位预警' if mode == 'business' else '情景风险提示')+' · '+LABELS[assessed['selected']['color']],
        'forecastId': forecast['id'], 'scenarioId': payload.get('scenarioId'),
        'ruleSetId': rules['id'], 'ruleSnapshot': deepcopy(rules), 'assessment': assessed,
        'color': assessed['selected']['color'], 'mode': mode, 'status': 'draft',
        'validUntil': valid_until, 'createdAt': now(),
        'notes': str(payload.get('notes') or ''),
        'history': [{'action': 'draft', 'at': now(), 'operator': str(payload.get('operator') or '本机操作员')}]}
    return store.save(db, 'warnings', value, prefix='WARNING-')


def _instant(value):
    instant = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone(timedelta(hours=8)))
    return instant.astimezone(timezone.utc)


def publish(db, identifier, payload):
    value = store.get(db, 'warnings', identifier)
    if value['status'] != 'draft':
        raise ValueError('只有草稿可确认发布')
    if _instant(value['validUntil']) <= datetime.now(timezone.utc):
        raise ValueError('有效期已过，请更新草稿')
    operator = str(payload.get('operator') or '').strip()
    if not operator:
        raise ValueError('请填写确认发布人')
    value.update(status='published', publishedAt=now(), operator=operator)
    value['history'].append({'action': 'published', 'at': now(), 'operator': operator})
    return store.save(db, 'warnings', value, identifier=identifier)


def withdraw(db, identifier, payload):
    value = store.get(db, 'warnings', identifier)
    if value['status'] != 'published':
        raise ValueError('只有已发布记录可撤回')
    operator = str(payload.get('operator') or '').strip()
    reason = str(payload.get('reason') or '').strip()
    if not operator or not reason:
        raise ValueError('撤回须填写操作人和原因')
    value['status'] = 'withdrawn'
    value['history'].append({'action': 'withdrawn', 'at': now(), 'operator': operator, 'reason': reason})
    return store.save(db, 'warnings', value, identifier=identifier)


def expire(db):
    changed = []
    instant = datetime.now(timezone.utc)
    for value in store.list_records(db, 'warnings'):
        if value.get('status') == 'published' and _instant(value['validUntil']) <= instant:
            value['status'] = 'expired'
            value['history'].append({'action': 'expired', 'at': now(), 'operator': '系统'})
            changed.append(store.save(db, 'warnings', value, identifier=value['id']))
    return changed
