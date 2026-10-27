"""Versioned station location metadata; no observation or model changes."""
import json
import math
from .common import ROOT, canonical, now, read_json


def wgs84_to_gcj02(lon, lat):
    """Conventional approximate GCJ-02 transform, not a survey transformation."""
    if not (72.004 <= lon <= 137.8347 and .8293 <= lat <= 55.8271):
        return lon, lat
    x, y = lon - 105., lat - 35.
    a, ee = 6378245., .006693421622965943
    dlat = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*math.sqrt(abs(x))
    dlat += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    dlat += (20*math.sin(y*math.pi) + 40*math.sin(y*math.pi/3))*2/3
    dlat += (160*math.sin(y*math.pi/12) + 320*math.sin(y*math.pi/30))*2/3
    dlon = 300 + x + 2*y + .1*x*x + .1*x*y + .1*math.sqrt(abs(x))
    dlon += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    dlon += (20*math.sin(x*math.pi) + 40*math.sin(x*math.pi/3))*2/3
    dlon += (150*math.sin(x*math.pi/12) + 300*math.sin(x*math.pi/30))*2/3
    r = math.radians(lat)
    magic = 1 - ee * math.sin(r)**2
    dlat *= 180 / ((a*(1-ee)/(magic*math.sqrt(magic)))*math.pi)
    dlon *= 180 / ((a/math.sqrt(magic))*math.cos(r)*math.pi)
    return lon + dlon, lat + dlat


def gcj02_to_wgs84(lon, lat):
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError('站点经纬度无效')
    x, y = lon, lat
    for _ in range(12):
        gx, gy = wgs84_to_gcj02(x, y)
        dx, dy = gx-lon, gy-lat
        x, y = x-dx, y-dy
        if max(abs(dx), abs(dy)) < 1e-10:
            return x, y
    raise ValueError('站点坐标近似转换未收敛')


def sync_locations(db):
    registry = read_json(ROOT/'config/station-locations.json')
    if not registry:
        return []
    if registry.get('schemaVersion') != 1 or registry.get('coordinateSystem') != 'GCJ-02':
        raise ValueError('站点坐标登记格式或坐标系不支持')
    changes = []
    with db.connect() as conn:
        for item in registry['stations']:
            row = conn.execute('SELECT * FROM stations WHERE id=?', (item['id'],)).fetchone()
            if row is None:
                continue
            metadata = json.loads(row['metadata'])
            previous = metadata.get('location', {})
            if previous.get('registryRevision') == registry['revision']:
                continue
            lon, lat = gcj02_to_wgs84(item['lon'], item['lat'])
            metadata['location'] = {
                'registryRevision': registry['revision'], 'source': registry['source'],
                'sourceCRS': 'GCJ-02', 'sourceLon': item['lon'], 'sourceLat': item['lat'],
                'originalText': item['originalText'], 'kCode': item['kCode'],
                'mapCRS': 'WGS84', 'conversion': 'iterative-inverse-gcj02-v1',
                'accuracy': '近似坐标转换，未作测量控制点校核', 'surveyVerified': False,
            }
            # Horizontal coordinates never imply a verified vertical datum.
            metadata.setdefault('datumVerified', False)
            conn.execute('UPDATE stations SET lon=?,lat=?,metadata=? WHERE id=?',
                         (lon, lat, canonical(metadata), row['id']))
            changes.append({'station': row['id'], 'previous': {'lon': row['lon'], 'lat': row['lat'], 'location': previous},
                            'lon': lon, 'lat': lat, 'location': metadata['location']})
        if changes:
            conn.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                         (now(), 'stations.locations', canonical({'revision': registry['revision'], 'changes': changes})))
    return changes


def sync_datums(db):
    registry = read_json(ROOT/'config/station-datums.json')
    if not registry:
        return []
    if (registry.get('schemaVersion') != 1 or registry.get('status') not in ('user-confirmed','user-retracted')
            or registry.get('referenceStation') not in registry.get('stationIds', [])):
        raise ValueError('湖位基准确认登记格式无效')
    changes = []
    with db.connect() as conn:
        for identifier in registry['stationIds']:
            row = conn.execute('SELECT * FROM stations WHERE id=?', (identifier,)).fetchone()
            if row is None:
                continue
            metadata = json.loads(row['metadata'])
            previous = {'datumVerified': metadata.get('datumVerified'), 'verticalDatum': metadata.get('verticalDatum')}
            if (metadata.get('verticalDatum') or {}).get('registryRevision') == registry['revision']:
                continue
            metadata['datumVerified'] = registry['commonDatumConfirmed'] is True and registry['zeroUnified'] is True
            metadata['verticalDatum'] = {k: registry[k] for k in ('status', 'source', 'confirmation', 'referenceStation', 'zeroUnified', 'namedDatum')}
            metadata['verticalDatum']['registryRevision'] = registry['revision']
            metadata['verticalDatum']['role'] = 'working-reference' if identifier==registry['referenceStation'] else 'zero-under-review'
            conn.execute('UPDATE stations SET metadata=? WHERE id=?', (canonical(metadata), identifier))
            changes.append({'station': identifier, 'previous': previous, 'verticalDatum': metadata['verticalDatum']})
        if changes:
            conn.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                         (now(), 'stations.datums', canonical({'revision': registry['revision'], 'changes': changes})))
    return changes
