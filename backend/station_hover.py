"""Compact hover summaries for map stations and weather cells."""
from datetime import datetime, timedelta

VALID = ('valid', 'qualified')
LABELS = {'level': '水位', 'flow': '流量', 'precipitation': '降水', 'temperature': '气温'}
UNITS = {'level': 'm', 'flow': 'm³/s', 'precipitation': 'mm', 'temperature': '℃'}
KINDS = {'lake': '湖泊水位站', 'river': '水文站', 'rain': '雨量站'}
ORDER = ('level', 'flow', 'precipitation', 'temperature')


def _instant(text):
    return datetime.fromisoformat(str(text)[:19])


def _daily(records, variable, end, days):
    """Daily means (sums for rain) from the bounded recent window only."""
    dates = [(end - timedelta(days=days - 1 - i)).date().isoformat() for i in range(days)]
    by_date = {}
    for r in records:
        by_date.setdefault(r['time'][:10], []).append(r)
    rows = []
    for d in dates:
        items = by_date.get(d, [])
        daily = [r['value'] for r in items if r['step'] == 'day']
        other = [r['value'] for r in items if r['step'] != 'day']
        if variable == 'precipitation':
            value = daily[-1] if daily else (round(sum(other), 2) if other else 0.)
        else:
            value = daily[-1] if daily else (sum(other) / len(other) if other else None)
        rows.append({'date': d, 'value': value})
    return rows


def station_summary(db, station_id):
    from .observation_query import selected_sql
    stations = db.rows('SELECT * FROM stations WHERE id=?', (station_id,))
    if not stations:
        raise FileNotFoundError('站点不存在')
    station = stations[0]
    found = [r['variable'] for r in db.rows('SELECT DISTINCT variable FROM observations WHERE station_id=?', (station_id,))]
    groups = []
    for variable in sorted(found, key=lambda v: ORDER.index(v) if v in ORDER else 99):
        last = db.rows("SELECT MAX(time) t FROM observations WHERE station_id=? AND variable=? "
                       "AND quality IN ('valid','qualified') AND value IS NOT NULL", (station_id, variable))[0]['t']
        group = {'variable': variable, 'label': LABELS.get(variable, variable), 'unit': UNITS.get(variable, ''),
                 'latest': None, 'change24h': None, 'daily': [], 'sum7d': None}
        if not last:
            groups.append(group)
            continue
        end = _instant(last)
        sql, args = selected_sql(station_id, variable, start=(end - timedelta(days=14)).isoformat(timespec='seconds'),
                                 end=end.isoformat(timespec='seconds'), lean=True)
        window = [r for r in db.rows(sql + ' ORDER BY time,id', args)
                  if r['quality'] in VALID and r['value'] is not None]
        recent = [r for r in window if _instant(r['time']) >= end - timedelta(days=2)]
        if not recent:
            groups.append(group)
            continue
        latest = max(recent, key=lambda r: (r['time'], r['step'] != 'day'))
        group['daily'] = _daily(window, variable, end, 14)
        if variable == 'precipitation':
            # Rain gauges report events; the business value is the daily total.
            totals = [d for d in group['daily'] if d['value'] is not None]
            if totals:
                group['latest'] = {'value': totals[-1]['value'], 'time': totals[-1]['date'], 'kind': 'daily-total'}
                group['sum7d'] = round(sum(d['value'] for d in totals if d['date'] > (end - timedelta(days=7)).date().isoformat()), 2)
            else:
                group['latest'] = {'value': latest['value'], 'time': latest['time'], 'kind': 'event'}
        else:
            group['latest'] = {'value': latest['value'], 'time': latest['time'], 'kind': 'instant'}
            target = _instant(latest['time']) - timedelta(hours=24)
            same_step = [r for r in recent if r['step'] == latest['step'] and _instant(r['time']) <= target + timedelta(hours=4)]
            if same_step:
                previous = max(same_step, key=lambda r: r['time'])
                if abs((_instant(previous['time']) - target).total_seconds()) <= 8 * 3600:
                    group['change24h'] = latest['value'] - previous['value']
        groups.append(group)
    times = [g['latest']['time'] for g in groups if g['latest'] and g['latest'].get('kind') != 'daily-total']
    return {'title': station['name'], 'kind': station['kind'], 'kindLabel': KINDS.get(station['kind'], '监测站'),
            'latestTime': max(times) if times else None, 'groups': groups}


def map_point_series(db, query):
    """Seven daily values for one weather grid cell or model unit, ending at `day`."""
    from .dashboard import map_frame, snow_frame
    day = datetime.fromisoformat(query['day'][:10])
    days = min(14, max(2, int(query.get('days', 7))))
    variable, target, kind = query.get('variable', 'precipitation'), query['id'], query.get('kind', 'grid')
    if variable == 'snow':
        # One bounded series read from the result store instead of seven full frames.
        from .result_query import _entry, read
        entry = _entry(db, query['taskId'])
        ids = [u['id'] for u in entry['task']['result']['units']]
        if target not in ids:
            return {'id': target, 'variable': variable, 'rows': []}
        first = (day - timedelta(days=days - 1)).date().isoformat()
        end = (day + timedelta(days=1)).date().isoformat()
        try:
            values = {r['time'][:10]: r['value'] for r in read(db, {'resultId': query['taskId'], 'variable': 'swe',
                      'unit': ids.index(target), 'start': first, 'end': end, 'step': 'day'})['rows']}
        except ValueError:
            values = {}
        dates = [(day - timedelta(days=offset)).date().isoformat() for offset in range(days - 1, -1, -1)]
        return {'id': target, 'variable': variable, 'rows': [{'date': d, 'value': values.get(d)} for d in dates]}
    rows = []
    for offset in range(days - 1, -1, -1):
        current = (day - timedelta(days=offset)).date().isoformat()
        value = None
        try:
            if variable == 'snow':
                frame = snow_frame(db, query['taskId'], current)
                value = next((u['value'] for u in frame.get('units', []) if u['id'] == target), None)
            else:
                frame = map_frame(db, query['inputId'], current, variable, query.get('spatialVersion'), query.get('resultId'))
                pool = frame.get('cells', []) if kind == 'grid' else frame.get('unitValues', [])
                value = next((c.get('value') for c in pool if c.get('id') == target), None)
        except (ValueError, KeyError, FileNotFoundError):
            value = None
        rows.append({'date': current, 'value': value})
    return {'id': target, 'variable': variable, 'rows': rows}
