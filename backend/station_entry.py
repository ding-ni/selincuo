"""Manual entry of station observations into the local observation store.

Typed values go through the same versioned contract as spreadsheet and database
imports (data_exchange.import_records), so they are validated, unit-checked and
stored as an ordinary append-mode source. Later entries for the same station,
variable and day replace earlier ones; nothing is deleted.
"""
import pandas as pd

from .data_exchange import import_records

SOURCE_KEY = 'manual/站点手工录入'
# Daily totals for rain gauges; the main station's lake level is an instantaneous reading.
FIELDS = {'precipitation': ('mm', 'day', 0., 300.), 'level': ('m', 'irregular', 4530., 4560.)}


def stations(db):
    """Stations that can take manual entries, from the station register."""
    from .observation_query import catalog
    rows = []
    for s in catalog(db)['stations']:
        variables = {v['id'] for v in s.get('variables', [])}
        kinds = [v for v in ('precipitation', 'level') if v in variables]
        if kinds:
            rows.append({'id': s['id'], 'name': s['name'], 'variables': kinds})
    return rows


def save(db, payload):
    """Write manual station values. payload: {records: [{station, variable, date, value[, hour]}]}."""
    allowed = {s['id']: set(s['variables']) for s in stations(db)}
    records = []
    for index, row in enumerate(payload.get('records') or [], 1):
        station, variable = row.get('station'), row.get('variable')
        if station not in allowed or variable not in allowed[station]:
            raise ValueError(f'第{index}条：站点或观测项目不支持手工录入')
        unit, step, low, high = FIELDS[variable]
        try:
            value = float(row.get('value'))
        except (TypeError, ValueError):
            raise ValueError(f'第{index}条：数值无效') from None
        if not low <= value <= high:
            raise ValueError(f'第{index}条：{"日雨量须为0—300 mm" if variable == "precipitation" else "湖位超出色林错合理范围"}')
        day = pd.Timestamp(row.get('date')).normalize()
        if variable == 'precipitation':
            time = day
        else:
            hour = int(row.get('hour', 8))
            if not 0 <= hour <= 23:
                raise ValueError(f'第{index}条：观测时刻须为0—23时')
            time = day + pd.Timedelta(hours=hour)
        records.append({'station': station, 'variable': variable, 'time': time.isoformat(),
                        'value': value, 'unit': unit, 'step': step, 'quality': 'valid'})
    if not records:
        raise ValueError('没有需要保存的录入值')
    result = import_records(db, {
        'schemaVersion': 1, 'projectId': 'selinco', 'timeZone': 'Asia/Shanghai',
        'sourceKey': SOURCE_KEY, 'mode': 'append', 'duplicatePolicy': 'replace', 'records': records,
        'sourceMetadata': {'method': 'manual-entry', 'enteredBy': payload.get('operator') or '平台用户',
                           'note': payload.get('note') or ''}})
    return {**result, 'saved': len(records)}
