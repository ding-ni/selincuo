"""Acquire independent OpenStreetMap water geometries for outlet verification."""
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
FOLDER=ROOT/'data/raw/spatial/独立水体-OSM-20261028'
QUERY='[out:json][timeout:120];(way["natural"="water"](30,88,32.2,90.4);relation["natural"="water"](30,88,32.2,90.4););out tags geom;'

def main():
    FOLDER.mkdir(parents=True,exist_ok=True)
    target=FOLDER/'原始水体.json'
    if not target.exists():
        last=None
        for endpoint in ('https://overpass-api.de/api/interpreter','https://overpass.kumi.systems/api/interpreter','https://overpass.private.coffee/api/interpreter'):
            try:
                request=Request(endpoint+'?'+urlencode({'data':QUERY}),headers={'User-Agent':'SelincoResearch/0.11 (public watershed verification)'})
                with urlopen(request,timeout=180) as stream:data=stream.read()
                break
            except Exception as error:last=error
        else:raise last
        parsed=json.loads(data)
        if parsed.get('remark'):raise ValueError(parsed['remark'])
        with target.open('xb') as stream:stream.write(data)
    data=target.read_bytes();parsed=json.loads(data)
    manifest={'provider':'OpenStreetMap contributors','license':'ODbL 1.0','source':'https://overpass-api.de/api/interpreter',
              'query':QUERY,'acquiredAt':datetime.now(timezone.utc).isoformat(),'osmTimestamp':parsed.get('osm3s'),
              'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'objects':len(parsed['elements']),
              'use':'Independent mapped water boundaries and names; mapping is not a water-level or outlet survey.'}
    path=FOLDER/'来源清单.json'
    if not path.exists():
        with path.open('x',encoding='utf-8') as stream:json.dump(manifest,stream,ensure_ascii=False,indent=2)
    print(json.dumps(manifest,ensure_ascii=False))

if __name__=='__main__':main()
