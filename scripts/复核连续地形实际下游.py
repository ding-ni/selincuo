"""Trace raw floating-point drainage without replacing real depressions."""
import json
import math
from pathlib import Path
import numpy as np
import rasterio
from rasterio.features import rasterize,shapes
from scipy import ndimage
from numba import njit
from shapely.geometry import shape,mapping
from shapely.ops import unary_union,transform
from backend.common import read_json,sha_file,digest
from scripts.核查第三轮连续地形 import ROOT,OUT,SOURCE,AREA,polygons,write_new

@njit
def pointers(z,tr_a,tr_e,north):
    h,w=z.shape;ptr=np.zeros((h,w),np.uint8)
    dy=np.array([-1,-1,0,1,1,1,0,-1]);dx=np.array([0,1,1,1,0,-1,-1,-1])
    for y in range(1,h-1):
        ex=tr_a*111195.*math.cos((north+(y+.5)*tr_e)*math.pi/180)
        ny=abs(tr_e)*111195.
        for x in range(1,w-1):
            best=0.;code=0
            for k in range(8):
                diff=float(z[y,x])-float(z[y+dy[k],x+dx[k]])
                slope=diff/math.sqrt((dx[k]*ex)**2+(dy[k]*ny)**2)
                if slope>best+1e-12:best=slope;code=k+1
            ptr[y,x]=code
    return ptr

@njit
def resolve_flats(z,pointer,target):
    """Route exactly level cells toward existing lower edges by BFS distance.

    This is the lower-edge portion of standard flat resolution: it changes
    only routing choices on equal-height cells, never elevation or storage.
    Closed flats keep zero directions and become terminal depressions.
    """
    h,w=z.shape;queue=np.empty(h*w,np.int64);a=0;b=0
    dy=np.array([-1,-1,0,1,1,1,0,-1]);dx=np.array([0,1,1,1,0,-1,-1,-1])
    for y in range(1,h-1):
        for x in range(1,w-1):
            if pointer[y,x] or target[y,x]:queue[b]=y*w+x;b+=1
    while a<b:
        p=queue[a];a+=1;y=p//w;x=p%w
        for k in range(8):
            yy,xx=y+dy[k],x+dx[k]
            if 0<yy<h-1 and 0<xx<w-1 and pointer[yy,xx]==0 and target[yy,xx]==0 and z[yy,xx]==z[y,x]:
                pointer[yy,xx]=(k+4)%8+1
                queue[b]=yy*w+xx;b+=1
    return pointer

@njit
def trace(pointer,targets,mask):
    h,w=pointer.shape;cache=targets.copy();path=np.empty(h*w,np.int64)
    dy=np.array([-1,-1,0,1,1,1,0,-1]);dx=np.array([0,1,1,1,0,-1,-1,-1])
    for sy in range(h):
        for sx in range(w):
            if not mask[sy,sx] or cache[sy,sx]:continue
            y,x=sy,sx;n=0
            while True:
                if cache[y,x]:outcome=cache[y,x];break
                path[n]=y*w+x;n+=1
                if y==0 or x==0 or y==h-1 or x==w-1:outcome=7;break
                code=pointer[y,x]
                if code==0:outcome=1000+y*w+x;break
                y,x=y+dy[code-1],x+dx[code-1]
                if n>=h*w:raise ValueError('流向循环')
            for i in range(n):cache[path[i]//w,path[i]%w]=outcome
    return cache

def main():
    path=OUT/'公开连续DSM.tif'
    with rasterio.open(path) as ds:z=ds.read(1);tr=ds.transform
    with rasterio.open(OUT/'公开水体掩膜.tif') as ds:wb=ds.read(1)
    fc=read_json(SOURCE/'map.geojson')['features']
    geo={f['properties'].get('id') or f['properties']['kind']:shape(f['geometry']) for f in fc if f['properties']['kind'] in ('basin','lake','control','domain')}
    def burn(g,touched=False):return rasterize([(mapping(g),1)],out_shape=z.shape,transform=tr,dtype='uint8',all_touched=touched).astype(bool)
    residual=burn(geo['unresolved'],True);targets=np.zeros(z.shape,np.int32)
    for code,key in ((1,'zajia'),(2,'zagen'),(3,'boqu'),(4,'ali'),(5,'fringe'),(6,'duja'),(8,'lake')):targets[burn(geo[key])]=code
    report=read_json(OUT/'连续地形核查.json');waterlabels,_=ndimage.label(wb==2,structure=np.ones((3,3)))
    for node in report['nodes']:
        targets[(waterlabels==node['waterMaskComponent'])&(targets==0)]=node['code']
    # Actual main-lake water may extend beyond the frozen shoreline. Absorb
    # its connected edited-water component using a separate receiver.
    lake_components=np.unique(waterlabels[(targets==8)&(waterlabels>0)])
    targets[np.isin(waterlabels,lake_components)]=8
    print('raw floating D8',flush=True)
    ptr=pointers(z,tr.a,tr.e,tr.f);ptr=resolve_flats(z,ptr,targets)
    print('trace actual receivers',flush=True)
    result=trace(ptr,targets,residual)
    with rasterio.open(OUT/'实际接收节点.tif','w',driver='GTiff',height=z.shape[0],width=z.shape[1],count=1,dtype='int32',crs=4326,transform=tr,nodata=0,compress='deflate',tiled=True) as ds:ds.write(np.where(residual,result,0).astype(np.int32),1)
    groups={}
    for geometry,code in shapes(result.astype(np.int32),mask=residual,transform=tr):groups.setdefault(int(code),[]).append(shape(geometry))
    rows=[];features=[]
    nodes={r['code']:r for r in report['nodes']};names={1:'扎加藏布',2:'扎根藏布',3:'波曲藏布',4:'阿里藏布',5:'湖周直接入湖区',6:'杜加上游',7:'地形边缘',8:'色林错'}
    for code,pieces in groups.items():
        geom=polygons(unary_union(pieces).intersection(geo['unresolved']))
        area=transform(AREA,geom).area/1e6
        if area<1e-10:continue
        node=nodes.get(code)
        if code>=1000:
            y,x=divmod(code-1000,z.shape[1]);lon,lat=tr*(x+.5,y+.5)
            props={'code':code,'name':'局部地形洼地','anchor':[lon,lat],'dsmSurfaceM':float(z[y,x]),'category':'depression'}
        elif node:props={**node,'category':'water-node'}
        else:props={'code':code,'name':names.get(code,str(code)),'category':'direct' if code!=7 else 'edge'}
        props['geometryAreaKm2']=area;rows.append(props);features.append({'type':'Feature','properties':props,'geometry':mapping(geom)})
    summary={'parentSpatialVersion':SOURCE.name,'demSHA256':sha_file(path),'waterSHA256':sha_file(OUT/'公开水体掩膜.tif'),
             'method':'raw-float-D8+equal-height-lower-edge-BFS; known water first; no filling; stable slope tie order',
             'rows':sorted(rows,key=lambda r:r['geometryAreaKm2'],reverse=True),
             'areaByCategory':{cat:sum(r['geometryAreaKm2'] for r in rows if r['category']==cat) for cat in ('direct','water-node','depression','edge')},
             'residualGeometryKm2':transform(AREA,geo['unresolved']).area/1e6,
             'assignedGeometryKm2':sum(r['geometryAreaKm2'] for r in rows)}
    write_new(OUT/'实际下游归属.geojson',{'type':'FeatureCollection','features':features});write_new(OUT/'实际下游核查.json',summary)
    print(json.dumps({'areaByCategory':summary['areaByCategory'],'top':summary['rows'][:20]},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
