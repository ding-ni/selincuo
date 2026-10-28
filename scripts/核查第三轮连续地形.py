"""Continuous public DSM / independent water diagnostics for residual drainage.

Watershed labels below are *potential spill receivers*, not an instruction to
fill every lake or send its water to Selinco. Real water bodies remain nodes.
"""
import json
import os
from pathlib import Path
os.environ['GDAL_PAM_ENABLED']='NO'
import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.features import rasterize,shapes
from scipy import ndimage
from skimage.segmentation import watershed
from shapely.geometry import shape,mapping,Polygon,LineString
from shapely.ops import unary_union,polygonize,transform
from shapely import make_valid
from pyproj import Transformer
from backend.common import ROOT,read_json,sha_file,digest

RAW=ROOT/'data/raw/spatial/公开连续地形-CopernicusGLO90-20261028'
OSM=ROOT/'data/raw/spatial/独立水体-OSM-20261028'
SOURCE=ROOT/'data/derived/spatial/versions/GRID-SPATIAL-c82d78ed63283ed20fcb'
OUT=ROOT/'build/第三轮整改-20261028/连续地形'
AREA=Transformer.from_crs(4326,6933,always_xy=True).transform
BASE={1:'zajia_rest',2:'zagen',3:'boqu',4:'ali',5:'fringe',6:'duja',7:'external-boundary',8:'lake'}

def write_new(path,data):
    if isinstance(data,dict):data=(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode()
    if path.exists():
        if path.read_bytes()!=data:raise ValueError('已有输出不同：'+str(path))
    else:
        with path.open('xb') as f:f.write(data)

def polygons(g):
    if not g.is_valid:g=make_valid(g)
    if g.geom_type in ('Polygon','MultiPolygon'):return g
    return unary_union([p for p in getattr(g,'geoms',[]) if p.geom_type in ('Polygon','MultiPolygon')])

def osm_waters():
    rows=[]
    for obj in read_json(OSM/'原始水体.json')['elements']:
        if obj['type']=='way':
            coords=[(p['lon'],p['lat']) for p in obj.get('geometry',[])]
            if len(coords)<4 or coords[0]!=coords[-1]:continue
            geom=polygons(Polygon(coords))
        else:
            outer=[];inner=[]
            for part in obj.get('members',[]):
                coords=[(p['lon'],p['lat']) for p in part.get('geometry',[])]
                if len(coords)>1:(inner if part.get('role')=='inner' else outer).append(LineString(coords))
            geom=polygons(unary_union(list(polygonize(unary_union(outer)))))
            if inner:geom=polygons(geom.difference(unary_union(list(polygonize(unary_union(inner))))))
        if geom.is_empty:continue
        tags=obj.get('tags',{})
        rows.append({'id':'OSM-'+obj['type']+'-'+str(obj['id']),
                     'name':tags.get('name:zh',tags.get('name')),'geometry':geom,'tags':tags})
    return rows

def mosaic():
    OUT.mkdir(parents=True,exist_ok=True)
    path=OUT/'公开连续DSM.tif';water=OUT/'公开水体掩膜.tif'
    manifest=read_json(RAW/'来源清单.json')
    if not manifest:raise ValueError('公开DEM未完成来源校验')
    if not path.exists():
        for suffix,target,dtype in (('_DEM.tif',path,'float32'),('_WBM.tif',water,'uint8')):
            files=[RAW/r['path'] for r in manifest['files'] if r['path'].endswith(suffix)]
            sources=[rasterio.open(p) for p in files]
            values,tr=merge(sources,nodata=-9999 if suffix=='_DEM.tif' else 255)
            profile=sources[0].profile
            for ds in sources:ds.close()
            profile.update(driver='GTiff',height=values.shape[1],width=values.shape[2],transform=tr,dtype=dtype,
                           nodata=-9999 if suffix=='_DEM.tif' else 255,tiled=True,blockxsize=512,blockysize=512,compress='deflate')
            with rasterio.open(target,'w',**{k:v for k,v in profile.items() if k!='driver'}) as dst:dst.write(values)
            print(json.dumps({'mosaic':target.name,'shape':values.shape}),flush=True)
    return path,water

def main():
    path,water=mosaic()
    with rasterio.open(path) as ds:z=ds.read(1);tr=ds.transform
    with rasterio.open(water) as ds:wb=ds.read(1)
    if np.any((z<1000)|~np.isfinite(z)) or (wb==255).any():raise ValueError('连续地形拼接缺测')
    fc=read_json(SOURCE/'map.geojson')['features']
    geometries={f['properties'].get('id') or f['properties']['kind']:shape(f['geometry']) for f in fc if f['properties']['kind'] in ('basin','lake','control','domain')}
    residual=geometries['unresolved']
    def burn(g,touched=False):return rasterize([(mapping(g),1)],out_shape=z.shape,transform=tr,dtype='uint8',all_touched=touched).astype(bool)
    residual_mask=burn(residual,True)
    known=np.zeros(z.shape,np.int32)
    for code,name in ((1,'zajia'),(2,'zagen'),(3,'boqu'),(4,'ali'),(5,'fringe'),(6,'duja'),(8,'lake')):
        if name in geometries:known[burn(geometries[name])]=code
    known[0,:]=7;known[-1,:]=7;known[:,0]=7;known[:,-1]=7
    components,n=ndimage.label(wb==2,structure=np.ones((3,3)))
    # Buffering thousands of slivers as vectors creates needless topology
    # work. A metric-bounded raster distance is enough to select nearby water.
    focus=ndimage.distance_transform_edt(~residual_mask)<=500
    ids=np.unique(components[focus&(components>0)])
    del focus
    slices=ndimage.find_objects(components)
    waters=osm_waters();nodes=[]
    for index in ids:
        sl=slices[index-1];local=components[sl]==index
        if local.sum()<4:continue
        if np.any((known[sl]==8)&local):continue
        if not np.any(local & (known[sl]==0)):continue
        node_id=100+len(nodes)
        # Marker does not replace an accepted catchment receiver.
        view=known[sl];view[local & (view==0)]=node_id
        ys,xs=np.where(local);cy,cx=float((ys+sl[0].start).mean()),float((xs+sl[1].start).mean())
        lon,lat=tr*(cx+.5,cy+.5)
        from shapely.geometry import Point
        nearby=[r for r in waters if r['geometry'].intersects(Point(lon,lat).buffer(.005))]
        matched=max(nearby,key=lambda r:r['geometry'].area) if nearby else None
        nodes.append({'code':node_id,'id':matched['id'] if matched else 'WBM-'+str(index),
                      'name':matched['name'] if matched and matched['name'] else f'水体（{lon:.3f}°E，{lat:.3f}°N）',
                      'anchor':[lon,lat],'waterPixels':int(local.sum()),'dsmSurfaceM':float(np.median(z[sl][local])),
                      'independentWater':bool(matched),'waterMaskComponent':int(index)})
    print(json.dumps({'nodes':len(nodes),'residualPixels':int(residual_mask.sum())}),flush=True)
    candidate=OUT/'潜在接收节点.tif'
    if candidate.exists():
        with rasterio.open(candidate) as ds:allocation=ds.read(1)
    else:
        print('priority watershed: preserve lake markers',flush=True)
        allocation=watershed(z,markers=known,connectivity=np.ones((3,3))).astype(np.int32)
        with rasterio.open(candidate,'w',driver='GTiff',height=z.shape[0],width=z.shape[1],count=1,dtype='int32',crs=4326,transform=tr,nodata=0,compress='deflate',tiled=True) as dst:dst.write(allocation,1)
    # Cross-label saddles form a potential spill graph. They do not enable
    # outflow until the water node has sufficient incremental storage.
    count=100+len(nodes)
    saddles=np.full((count,count),np.inf,np.float32)
    positions={}
    for dy,dx in ((0,1),(1,0),(1,1),(1,-1)):
        ya=slice(0,z.shape[0]-dy);yb=slice(dy,z.shape[0]);xa=slice(max(0,-dx),min(z.shape[1],z.shape[1]-dx));xb=slice(max(0,dx),min(z.shape[1],z.shape[1]+dx))
        a,b=allocation[ya,xa],allocation[yb,xb];mask=a!=b
        aa,bb=a[mask],b[mask];elev=np.maximum(z[ya,xa][mask],z[yb,xb][mask])
        np.minimum.at(saddles,(aa,bb),elev);np.minimum.at(saddles,(bb,aa),elev)
    # Minimax path from each water marker to a named receiver / domain edge.
    import heapq
    distance=np.full(count,np.inf);receiver=np.zeros(count,np.int32);previous=np.zeros(count,np.int32)
    queue=[]
    for code in BASE:distance[code]=-np.inf;receiver[code]=code;heapq.heappush(queue,(-np.inf,code))
    while queue:
        cost,current=heapq.heappop(queue)
        if cost!=distance[current]:continue
        for nxt in np.flatnonzero(np.isfinite(saddles[current])):
            new=max(cost,float(saddles[current,nxt]))
            if new<distance[nxt]:distance[nxt]=new;receiver[nxt]=receiver[current];previous[nxt]=current;heapq.heappush(queue,(new,int(nxt)))
    groups={};features=[]
    for geo,code in shapes(allocation.astype(np.int32),mask=residual_mask,transform=tr):groups.setdefault(int(code),[]).append(shape(geo))
    stats=[]
    nodes_by={row['code']:row for row in nodes}
    for code,pieces in sorted(groups.items()):
        geom=polygons(unary_union(pieces).intersection(residual))
        props={**nodes_by.get(code,{'code':code,'name':BASE.get(code,str(code))}),
               'geometryAreaKm2':transform(AREA,geom).area/1e6}
        if code>=100:props.update(potentialReceiver=BASE.get(int(receiver[code])),spillSaddleM=float(distance[code]),
                                   sillAboveSurfaceM=float(distance[code]-props['dsmSurfaceM']),nextNodeCode=int(previous[code]))
        stats.append(props);features.append({'type':'Feature','properties':props,'geometry':mapping(geom)})
    result={'sourceManifestSHA256':sha_file(RAW/'来源清单.json'),'independentWaterSHA256':sha_file(OSM/'原始水体.json'),
            'parentSpatialVersion':SOURCE.name,'method':'skimage-marker-watershed-8-neighbour; true water markers retained; potential spill graph only',
            'rasterShape':list(z.shape),'sourceVerticalReference':'EGM2008 orthometric metres','nodes':nodes,'residualReceivers':stats,
            'originalResidualGeometryKm2':transform(AREA,residual).area/1e6,
            'rasterClippedGeometryKm2':sum(r['geometryAreaKm2'] for r in stats),
            'allResidualAssigned':not np.any(allocation[residual_mask]==0),'saddleNote':'DSM surface is not lake bathymetry. Connection is potential at the saddle, not current lake outflow.'}
    write_new(OUT/'潜在排水归属.geojson',{'type':'FeatureCollection','features':features})
    write_new(OUT/'连续地形核查.json',result)
    print(json.dumps({'receivers':stats,'geometryKm2':result['rasterClippedGeometryKm2']},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
