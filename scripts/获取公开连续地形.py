"""Read-only public Copernicus GLO-90 acquisition; never replaces raw files."""
import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://copernicus-dem-90m.s3.amazonaws.com/'

def fetch(url,timeout=120):
    for attempt in range(4):
        try:
            with urlopen(url,timeout=timeout) as stream:return stream.read()
        except Exception:
            if attempt==3:raise
            time.sleep(1+attempt)


def acquire(folder, lat, lon):
    stem = f'Copernicus_DSM_COG_30_N{lat:02d}_00_E{lon:03d}_00'
    prefix = stem + '_DEM/'
    listing_url = BASE + '?list-type=2&prefix=' + prefix
    listing = fetch(listing_url)
    tree = ET.fromstring(listing)
    ns = {'s': 'http://s3.amazonaws.com/doc/2006-03-01/'}
    objects = {node.findtext('s:Key', namespaces=ns): node for node in tree.findall('s:Contents', ns)}
    keys = [prefix + stem + '_DEM.tif', prefix + 'AUXFILES/' + stem + '_WBM.tif',
            prefix + f'Copernicus_DSM_30_N{lat:02d}_00_E{lon:03d}_00.xml']
    output = []
    for key in keys:
        node = objects[key]
        target = folder / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            data = target.read_bytes()
        else:
            data = fetch(BASE + key)
            expected = int(node.findtext('s:Size', namespaces=ns))
            if len(data) != expected:
                raise ValueError('公开文件长度不符：' + key)
            etag = node.findtext('s:ETag', namespaces=ns).strip('"')
            if '-' not in etag and hashlib.md5(data).hexdigest() != etag:
                raise ValueError('公开文件ETag不符：' + key)
            with target.open('xb') as stream:
                stream.write(data)
        if len(data) != int(node.findtext('s:Size', namespaces=ns)):
            raise ValueError('已有文件长度不符，保留原件：' + key)
        etag = node.findtext('s:ETag', namespaces=ns).strip('"')
        if '-' not in etag and hashlib.md5(data).hexdigest() != etag:
            raise ValueError('已有文件摘要不符，保留原件：' + key)
        output.append({'path': key, 'url': BASE + key, 'bytes': len(data),
                       'sha256': hashlib.sha256(data).hexdigest(), 'etag': etag,
                       'sourceLastModified': node.findtext('s:LastModified', namespaces=ns)})
    listing_path = folder / prefix / '官方对象清单.xml'
    if not listing_path.exists():
        with listing_path.open('xb') as stream:
            stream.write(listing)
    return output


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--folder', default='data/raw/spatial/公开连续地形-CopernicusGLO90-20261028')
    args = parser.parse_args()
    folder = ROOT / args.folder
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / '来源清单.json'
    if manifest.exists():
        known = json.loads(manifest.read_text(encoding='utf-8'))
        for row in known['files']:
            if hashlib.sha256((folder / row['path']).read_bytes()).hexdigest() != row['sha256']:
                raise ValueError('已下载文件发生变化：' + row['path'])
        print(json.dumps({'existingVerified': True, 'files': len(known['files'])}))
        return
    rows = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        pending = {executor.submit(acquire, folder, lat, lon): (lat, lon)
                   for lat in range(29, 34) for lon in range(87, 93)}
        for future in as_completed(pending):
            rows.extend(future.result())
            print(json.dumps({'tile': pending[future], 'filesCompleted': len(rows)}), flush=True)
    result = {'product': 'Copernicus DEM GLO-90 public COG', 'source': BASE,
              'acquiredAt': datetime.now(timezone.utc).isoformat(),
              'bounds': [87, 29, 93, 34], 'horizontalCrs': 'EPSG:4326',
              'verticalDatum': 'EGM2008 orthometric heights; see each original XML',
              'unit': 'm', 'nominalResolutionArcSeconds': 3,
              'meaning': 'DSM with edited water surface; water mask is same-product evidence, not an independent survey.',
              'files': sorted(rows, key=lambda row: row['path'])}
    with manifest.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'files': len(rows), 'bytes': sum(row['bytes'] for row in rows),
                      'manifest': str(manifest)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
