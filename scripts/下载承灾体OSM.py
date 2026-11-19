"""下载色林错—班戈错一带的 OSM 道路与居民点，保存为随安装包发布的承灾体资料。

输出到 data/derived/spatial/承灾体/，每类一个 GeoJSON 及一份来源说明。
已有同名文件时另存带时间戳的新版本，不覆盖。
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / 'derived' / 'spatial' / '承灾体'
BOUNDS = [88.45, 31.45, 89.75, 32.2]  # west, south, east, north：与漫溢展示总览范围一致
ENDPOINTS = ['https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter']
QUERIES = {
    'road': 'way[highway~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|track|service)$"]({bbox});',
    'settlement': 'nwr[place~"^(city|town|village|hamlet|isolated_dwelling|locality)$"]({bbox});',
}
NAMES = {'road': 'OSM道路', 'settlement': 'OSM居民点'}


def fetch(clause, output):
    west, south, east, north = BOUNDS
    query = '[out:json][timeout:90];(' + clause.format(bbox=f'{south},{west},{north},{east}') + ');' + output
    last = None
    for url in ENDPOINTS:
        for attempt in range(3):
            try:
                response = requests.post(url, data={'data': query}, timeout=(15, 120),
                                         headers={'User-Agent': 'SelincoPlatform/exposure-bundle'})
                response.raise_for_status()
                body = response.json()
                if body.get('remark') or not body.get('elements'):
                    raise ValueError('查询未完整返回：' + str(body.get('remark') or '无要素'))
                return body['elements'], url
            except (requests.RequestException, ValueError, KeyError) as error:
                last = error; time.sleep(3 * (attempt + 1))
    raise SystemExit(f'下载失败：{last}')


def features(kind, elements):
    rows = []
    for element in elements:
        tags = element.get('tags') or {}
        if kind == 'road':
            coordinates = [[p['lon'], p['lat']] for p in element.get('geometry', [])]
            if len(coordinates) < 2:
                continue
            geometry = {'type': 'LineString', 'coordinates': coordinates}
        else:
            point = element.get('center', element)
            if 'lon' not in point:
                continue
            geometry = {'type': 'Point', 'coordinates': [point['lon'], point['lat']]}
        rows.append({'type': 'Feature', 'id': f"{element['type']}/{element['id']}", 'geometry': geometry,
                     'properties': {'name': tags.get('name:zh') or tags.get('name', ''),
                                    'class': tags.get('highway') or tags.get('place'), **tags}})
    return rows


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%MZ')
    manifest = {'source': 'OpenStreetMap contributors', 'license': 'ODbL 1.0', 'bounds': BOUNDS,
                'downloadedAtUTC': stamp, 'files': []}
    kinds = sys.argv[1:] or list(QUERIES)
    for kind, clause in ((k, QUERIES[k]) for k in kinds):
        elements, url = fetch(clause, 'out geom;' if kind == 'road' else 'out center;')
        rows = features(kind, elements)
        path = OUT / f'{kind}.geojson'
        if path.exists():
            path = OUT / f'{kind}-{stamp}.geojson'
        path.write_text(json.dumps({'type': 'FeatureCollection', 'features': rows}, ensure_ascii=False), encoding='utf-8')
        manifest['files'].append({'kind': kind, 'name': NAMES[kind], 'file': path.name, 'featureCount': len(rows),
                                  'endpoint': url, 'query': clause})
        print(kind, len(rows), path.name)
    target = OUT / '承灾体来源.json'
    if target.exists():
        target = OUT / f'承灾体来源-{stamp}.json'
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding='utf-8')
    print('manifest', target.name)


if __name__ == '__main__':
    sys.exit(main())
