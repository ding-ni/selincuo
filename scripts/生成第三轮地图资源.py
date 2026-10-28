"""Shared display anchors and multiscale terrain; never changes model geometry."""
import argparse
import json
import sqlite3
from pathlib import Path
import numpy as np
from backend.common import ROOT, read_json, sha_file, digest

RAW=ROOT/'data/raw/spatial/公开连续地形-CopernicusGLO90-20261028'
OUT=ROOT/'data/derived/spatial'

def save(path,data):
    data=(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode('utf-8')
    if path.exists():
        if path.read_bytes()!=data:raise ValueError('已有资源不同，禁止覆盖：'+str(path))
    else:
        with path.open('xb') as f:f.write(data)

def presentation():
    from shapely.geometry import shape
    from shapely.ops import unary_union
    context=read_json(OUT/'display-context.json')
    source=OUT/'versions/GRID-SPATIAL-c82d78ed63283ed20fcb/map.geojson'
    features=read_json(source)['features']
    lake=unary_union([shape(f['geometry']) for f in features if f['properties']['kind']=='lake'])
    fringe=unary_union([shape(f['geometry']) for f in features if f['properties'].get('id')=='fringe'])
    near=fringe.intersection(lake.buffer(.18))
    part=max(getattr(near,'geoms',[near]),key=lambda g:g.area)
    fringe_anchor=part.representative_point()
    labels=[]
    for i,row in enumerate(context['labels']):
        row={k:v for k,v in row.items() if k not in ('areaKm2','placement')}
        row['id']=row.get('id') or 'lake'
        row['priority']=60-i
        row['preferredOffset']=[0,0]
        if row['id']=='fringe':
            row['name']='湖周直接入湖区';row['anchor']=[fringe_anchor.x,fringe_anchor.y]
        labels.append(row)
    dbpath=ROOT/'build/grid-revision-20261026/workspace/database/hydrology.sqlite3'
    conn=sqlite3.connect('file:'+dbpath.as_posix()+'?mode=ro',uri=True)
    conn.row_factory=sqlite3.Row
    stations=[dict(r) for r in conn.execute('SELECT id,name,lon,lat FROM stations WHERE lon IS NOT NULL AND lat IS NOT NULL')]
    conn.close()
    priorities={'SLC':100,'DUJA':99,'SLC-N':98,'SLC-E':97,'SLC-S':96,'SLC-W':95}
    offsets={'SLC-N':[30,-20],'SLC-E':[44,0],'SLC-S':[30,20],'SLC-W':[-38,0],'SLC':[34,-17],'DUJA':[34,-17]}
    for row in stations:
        row.update(coordinate=[row['lon'],row['lat']],priority=priorities.get(row['id'],80),preferredOffset=offsets.get(row['id'],[34,-17]))
    value={'schemaVersion':1,'id':'selinco-presentation-v1','coordinateSystem':'EPSG:4326',
           'bounds':context['bounds'],'domainBounds':context['domainBounds'],'lakeBounds':context['lakeBounds'],
           'labels':labels,'riverLabels':context['riverLabels'],'stations':stations,
           'symbols':{'stationRadius':3.8,'stationStrokeWidth':1.4,'labelStrokeWidth':1.4},
           'viewPadding':{'domain':30,'lake':30,'selection':36},
           'source':'Shared business display anchors based on the existing monitoring view; coordinate metadata from registered stations.',
           'displayOnly':True,'geometryPolicy':'Use each result\'s frozen geometries and areas; anchors do not redefine computation.'}
    save(OUT/'presentation.json',value)
    print(json.dumps({'presentation':str(OUT/'presentation.json'),'stations':len(stations)}),flush=True)

def terrain():
    import rasterio
    from rasterio.merge import merge
    from rasterio.warp import reproject,Resampling
    from rasterio.transform import from_bounds
    from PIL import Image
    manifest=read_json(RAW/'来源清单.json')
    if not manifest:raise ValueError('连续公开地形尚未全部校验')
    files=[RAW/r['path'] for r in manifest['files'] if r['path'].endswith('_DEM.tif')]
    sources=[rasterio.open(p) for p in files]
    bounds=read_json(OUT/'display-context.json')['bounds']
    mosaic,tr=merge(sources,bounds=bounds,nodata=-9999)
    for ds in sources:ds.close()
    z=mosaic[0];valid=z>-1000
    if not valid.all():raise ValueError('地形展示范围有缺测，不补零')
    identity=digest({'raw':sha_file(RAW/'来源清单.json'),'style':'hypsometric-metric-hillshade-v2','bounds':bounds})[:16]
    folder=OUT/('地形展示-'+identity);folder.mkdir(exist_ok=True)
    stops=[4400.,4600.,4800.,5000.,5500.,6000.]
    colors=['#78998a','#a5b299','#cac7a1','#c5b093','#b1a596','#ebe9e2']
    rgb=np.array([[int(c[i:i+2],16) for i in (1,3,5)] for c in colors])
    levels=[]
    for factor,minzoom in ((1,1),(2,1.7),(4,3)):
        w,h=1200*factor,850*factor
        values=np.empty((h,w),np.float32)
        reproject(z,values,src_transform=tr,src_crs=4326,dst_transform=from_bounds(*bounds,w,h),dst_crs=4326,resampling=Resampling.bilinear)
        dx=(bounds[2]-bounds[0])/w*111195*np.cos(np.deg2rad((bounds[1]+bounds[3])/2))
        dy=(bounds[3]-bounds[1])/h*111195
        gy,gx=np.gradient(values,dy,dx)
        shade=np.clip(.86+(-gx*.65+gy*.55),.5,1.12)
        rgba=np.empty((h,w,4),np.uint8)
        for channel in range(3):rgba[:,:,channel]=np.clip(np.interp(values,stops,rgb[:,channel])*shade,0,255)
        rgba[:,:,3]=245
        path=folder/f'terrain-{w}.png'
        if not path.exists():Image.fromarray(rgba,'RGBA').save(path,optimize=False)
        levels.append({'minZoom':minzoom,'image':'/spatial/'+folder.name+'/'+path.name,'imageSHA256':sha_file(path),
                       'width':w,'height':h,'bounds':bounds,'displayPixelSizeMetres':[float(dx),float(dy)]})
        print(json.dumps({'terrainLevel':w,'bytes':path.stat().st_size}),flush=True)
    value={'schemaVersion':1,'bounds':bounds,'image':levels[0]['image'],'levels':levels,'stops':stops,'colors':colors,
           'minimum':float(z.min()),'maximum':float(z.max()),'unit':'m','source':'Copernicus GLO-90（3角秒）',
           'sourceResolutionArcSeconds':3,'sourceResolutionMetresAtDomainCentre':[float(111195/1200*np.cos(np.deg2rad(31.7))),111195/1200],
           'sourceManifestSHA256':sha_file(RAW/'来源清单.json'),'verticalReference':'EGM2008 orthometric metres',
           'displayOnly':True,'note':'Multiscale display sampling does not increase the DEM\'s real ground resolution.'}
    save(OUT/'terrain-current.json',value)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--presentation-only',action='store_true');args=parser.parse_args()
    presentation()
    if not args.presentation_only:terrain()
