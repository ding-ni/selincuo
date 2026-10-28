"""Acquire Copernicus GLO-90 per-pixel height error masks, preserving source bytes."""
import hashlib,json,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
from pathlib import Path
from urllib.request import urlopen
from xml.etree import ElementTree as ET
from scripts.获取公开连续地形 import ROOT,BASE,fetch

SOURCE=ROOT/'data/raw/spatial/公开连续地形-CopernicusGLO90-20261028'
OUT=ROOT/'data/raw/spatial/公开地形高程误差-CopernicusGLO90-20261028'
NS={'s':'http://s3.amazonaws.com/doc/2006-03-01/'}

def acquire(lat,lon):
    stem=f'Copernicus_DSM_COG_30_N{lat:02d}_00_E{lon:03d}_00';prefix=stem+'_DEM/'
    listing=ET.fromstring(fetch(BASE+'?list-type=2&prefix='+prefix));objects={x.findtext('s:Key',namespaces=NS):x for x in listing.findall('s:Contents',NS)}
    keys=[prefix+'AUXFILES/'+stem+'_HEM.tif',prefix+f'Copernicus_DSM_30_N{lat:02d}_00_E{lon:03d}_00.xml']
    rows=[]
    for key in keys:
        item=objects[key];path=OUT/key;path.parent.mkdir(parents=True,exist_ok=True)
        data=path.read_bytes() if path.exists() else fetch(BASE+key)
        if len(data)!=int(item.findtext('s:Size',namespaces=NS)):raise ValueError('HEM文件长度不匹配：'+key)
        etag=item.findtext('s:ETag',namespaces=NS).strip('"')
        if '-' not in etag and hashlib.md5(data).hexdigest()!=etag:raise ValueError('HEM ETag不匹配：'+key)
        if not path.exists():
            with path.open('xb') as stream:stream.write(data)
        rows.append({'path':key,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'etag':etag})
    return rows

def main():
    OUT.mkdir(parents=True,exist_ok=True);manifest=OUT/'来源清单.json'
    if manifest.exists():
        current=json.loads(manifest.read_text(encoding='utf-8'))
        for row in current['files']:
            if hashlib.sha256((OUT/row['path']).read_bytes()).hexdigest()!=row['sha256']:raise ValueError('已有HEM源文件改变')
        print(json.dumps({'existingVerified':len(current['files'])}));return
    files=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs={pool.submit(acquire,lat,lon):(lat,lon) for lat in range(29,34) for lon in range(87,93)}
        for future in as_completed(jobs):
            files.extend(future.result());print(json.dumps({'tile':jobs[future],'files':len(files)}),flush=True)
    value={'product':'Copernicus DEM GLO-90 Height Error Mask','provider':'Copernicus DEM 2021_1','source':BASE,
           'acquiredAt':datetime.now(timezone.utc).isoformat(),'description':'Per-pixel corresponding height error as standard deviation, per original XML; not an absolute datum offset or a proof that a depression is artificial.',
           'files':sorted(files,key=lambda r:r['path'])}
    with manifest.open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2)
    print(json.dumps({'files':len(files),'bytes':sum(r['bytes'] for r in files),'manifest':str(manifest)},ensure_ascii=False))

if __name__=='__main__':main()
