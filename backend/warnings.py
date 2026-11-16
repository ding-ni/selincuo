"""Versioned thresholds and local review/publication records."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

import numpy as np

from .common import now
from . import operational_store as store


COLORS = ('blue', 'yellow', 'orange', 'red')
LABELS = {'blue': '蓝色', 'yellow': '黄色', 'orange': '橙色', 'red': '红色'}

GRADES = {'blue': 'IV级', 'yellow': 'III级', 'orange': 'II级', 'red': 'I级'}
MEANINGS = {'blue': '湖位接近2023年溃决水位，加密监测', 'yellow': '湖位或外泄明显上升，做好应急准备',
            'orange': '接近溃决水位或外泄接近2024年峰值，启动应急响应', 'red': '达到或超过2023年溃决水位，或外泄超过历史峰值'}
# Grades are anchored on the 2023 event: the observed level at the 2023-09-21 overflow onset
# and the 2024 peak breach outflow (about 61 m³/s, lake-model reconstruction).
LEVEL_OFFSETS = {'blue': -.35, 'yellow': -.20, 'orange': -.10, 'red': 0.}
OUTFLOW_LIMITS = {'blue': 20., 'yellow': 40., 'orange': 60., 'red': 80.}
EXCEEDANCE = .3


def default_rules(db=None):
    """Platform grading of overflow risk, built from the adopted lake model."""
    reference = 4544.65
    if db is not None:
        from .lake_revision import active
        model = active(db)
        if model:
            reference = float(model['onsetLevelM'])
    return {'name': '色林错漫溢风险分级', 'mode': 'scenario', 'referenceLevelM': reference,
            'source': '以2023年9月21日起溢水位和2024年溃口外泄峰值为基准',
            'levels': [{'color': c, 'levelM': round(reference + LEVEL_OFFSETS[c], 2), 'outflowM3s': OUTFLOW_LIMITS[c],
                        'probability': EXCEEDANCE, 'grade': GRADES[c], 'meaning': MEANINGS[c]} for c in COLORS],
            '_isDefault': True}


def active_rules(db, identifier=None):
    if identifier:
        return store.get(db, 'warning-rules', identifier)
    saved = store.list_records(db, 'warning-rules')
    return saved[0] if saved else default_rules(db)


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
        q = item.get('outflowM3s')
        q = None if q in (None, '') else float(q)
        if not math.isfinite(h) or not 4000 <= h <= 6500 or not math.isfinite(p) or not 0 <= p <= 1 or (q is not None and (not math.isfinite(q) or q < 0)):
            raise ValueError('预警水位、外泄流量或超限成员比例无效')
        rows.append({'color': color, 'levelM': h, 'outflowM3s': q, 'probability': p,
                     'grade': GRADES[color], 'meaning': str(item.get('meaning') or MEANINGS[color])})
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


def evaluate_rules(times, member_levels, rules, member_outflows=None):
    values = np.asarray(member_levels, dtype=float)
    if values.ndim == 1:
        values = values[None, :]
    if values.ndim != 2 or values.shape[1] != len(times) or not np.isfinite(values).all():
        raise ValueError('集合湖位或有效时段不完整')
    flows = None
    if member_outflows is not None:
        flows = np.asarray(member_outflows, dtype=float)
        flows = flows[None, :] if flows.ndim == 1 else flows
        if flows.shape != values.shape or not np.isfinite(flows).all():
            flows = None
    matches = []
    for row in rules['levels']:
        hit = values >= row['levelM']
        basis = 'level'
        if flows is not None and row.get('outflowM3s') is not None:
            hit = hit | (flows >= row['outflowM3s'])
        ratios = np.mean(hit, axis=0)
        ids = np.flatnonzero((ratios > 0) & (ratios >= row['probability']))
        if len(ids):
            first = int(ids[0])
            by_flow = flows is not None and row.get('outflowM3s') is not None and bool(np.any(flows[:, first] >= row['outflowM3s']))
            by_level = bool(np.any(values[:, first] >= row['levelM']))
            basis = 'level+outflow' if by_flow and by_level else 'outflow' if by_flow else 'level'
            matches.append({**row, 'firstTime': str(times[first]), 'basis': basis,
                'maximumProportion': float(ratios.max()),
                'firstProportion': float(ratios[first]), 'memberCount': int(values.shape[0])})
    selected = max(matches, key=lambda r: COLORS.index(r['color'])) if matches else None
    median = np.median(values, axis=0)
    peak = int(np.argmax(median))
    result = {'selected': selected, 'matches': matches, 'memberCount': int(values.shape[0]),
              'peakLevelM': float(values.max()), 'peakMedianLevelM': float(median[peak]), 'peakMedianTime': str(times[peak])}
    if flows is not None:
        fm = np.median(flows, axis=0)
        result.update(peakOutflowM3s=float(fm.max()), peakOutflowTime=str(times[int(np.argmax(fm))]))
    return result


def assess(db, forecast_id, rule_id=None):
    from .forecasts import detail
    forecast = detail(db, forecast_id)
    forecast = forecast.get('result', forecast)
    rules = active_rules(db, rule_id)
    value = evaluate_rules(forecast['times'], _member_levels(db, forecast), rules, _member_outflows(db, forecast))
    return {**value, 'forecastId': forecast['id'], 'rules': rules, 'issueTime': forecast.get('issueTime'),
            'end': forecast.get('end')}


def _member_outflows(db, forecast):
    artifact = forecast.get('memberArtifact') or {}
    if artifact.get('path'):
        from .common import within
        try:
            with np.load(within(db.workspace, artifact['path']), allow_pickle=False) as arrays:
                if 'outflow' in arrays:
                    return arrays['outflow'].copy()
        except (OSError, ValueError):
            pass
    control = forecast.get('control') or {}
    return [control['outflow']] if control.get('outflow') and forecast.get('memberCount', 1) == 1 else None


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
    rules = active_rules(db, payload.get('ruleSetId'))
    assessed = evaluate_rules(forecast['times'], _member_levels(db, forecast), rules, _member_outflows(db, forecast))
    if assessed['selected'] is None:
        raise ValueError('本次预报未达到任何预警等级')
    curve = forecast.get('lakeCurve') or {}
    provisional = bool(forecast.get('provisional', curve.get('provisional', True)) or curve.get('provisional', False))
    mode = 'business' if rules['mode'] == 'business' and not provisional else 'scenario'
    default_end = datetime.now(timezone.utc)+timedelta(days=1)
    valid_until = payload.get('validUntil') or default_end.isoformat(timespec='seconds')
    _instant(valid_until)
    value = {'name': '色林错漫溢风险'+GRADES[assessed['selected']['color']]+LABELS[assessed['selected']['color']]+'预警',
        'forecastId': forecast['id'], 'scenarioId': payload.get('scenarioId'),
        'ruleSetId': rules.get('id', 'DEFAULT'), 'ruleSnapshot': deepcopy(rules), 'assessment': assessed,
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
