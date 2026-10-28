"""Quantify local-pit spill thresholds while retaining mapped water nodes."""
import json
import heapq
import numpy as np
import rasterio
from rasterio.features import rasterize
from scipy import ndimage
from skimage.segmentation import watershed
from shapely.geometry import shape,mapping
from backend.common import read_json,sha_file
from scripts.核查第三轮连续地形 import OUT,SOURCE,BASE,write_new

def main():
    with rasterio.open(OUT/'公开连续DSM.tif') as ds:z=ds.read(1);tr=ds.transform
    with rasterio.open(OUT/'公开水体掩膜.tif') as ds:wb=ds.read(1)
    water=read_json(OUT/'连续地形核查.json')['nodes'];raw=read_json(OUT/'实际下游核查.json')
    known=np.zeros(z.shape,np.int32)
    fc=read_json(SOURCE/'map.geojson')['features']
    geo={f['properties'].get('id') or f['properties']['kind']:shape(f['geometry']) for f in fc if f['properties']['kind'] in ('basin','lake','control','domain')}
    for code,name in ((1,'zajia'),(2,'zagen'),(3,'boqu'),(4,'ali'),(5,'fringe'),(6,'duja'),(8,'lake')):
        m=rasterize([(mapping(geo[name]),1)],out_shape=z.shape,transform=tr,dtype='uint8').astype(bool);known[m]=code
    known[0,:]=7;known[-1,:]=7;known[:,0]=7;known[:,-1]=7
    wc,_=ndimage.label(wb==2,structure=np.ones((3,3)))
    remap=np.zeros(int(wc.max())+1,np.int32)
    for n in water:remap[n['waterMaskComponent']]=n['code']
    remap[np.unique(wc[(known==8)&(wc>0)])]=8
    wm=remap[wc];known[(known==0)&(wm>0)]=wm[(known==0)&(wm>0)]
    nodes={r['code']:r for r in water};pit_map={};start=max(nodes)+1
    for row in raw['rows']:
        if row['category']!='depression':continue
        code=start+len(pit_map);pit_map[code]=row['code'];nodes[code]={**row,'code':code,'rawCode':row['code']}
        x,y=~tr*tuple(row['anchor']);known[round(y-.5),round(x-.5)]=code
    print(json.dumps({'waterNodes':len(water),'dryPitNodes':len(pit_map)}),flush=True)
    p=OUT/'保留洼地分水区.tif'
    if p.exists():
        with rasterio.open(p) as ds:labels=ds.read(1)
    else:
        labels=watershed(z,markers=known,connectivity=np.ones((3,3))).astype(np.int32)
        with rasterio.open(p,'w',driver='GTiff',height=z.shape[0],width=z.shape[1],count=1,dtype='int32',crs=4326,transform=tr,nodata=0,compress='deflate',tiled=True) as ds:ds.write(labels,1)
    size=max(nodes)+1;saddles=np.full((size,size),np.inf,np.float32);locations={}
    for dy,dx in ((0,1),(1,0),(1,1),(1,-1)):
        ya=slice(0,z.shape[0]-dy);yb=slice(dy,z.shape[0]);xa=slice(max(0,-dx),min(z.shape[1],z.shape[1]-dx));xb=slice(max(0,dx),min(z.shape[1],z.shape[1]+dx))
        a,b=labels[ya,xa],labels[yb,xb];m=a!=b
        yy,xx=np.where(m);xx+=max(0,-dx)
        aa,bb=a[m],b[m];height=np.maximum(z[ya,xa][m],z[yb,xb][m])
        order=np.argsort(height,kind='stable')
        for k in order:
            one,two=int(aa[k]),int(bb[k]);e=float(height[k])
            if e<float(saddles[one,two]):
                saddles[one,two]=saddles[two,one]=e
                locations[(min(one,two),max(one,two))]=[int(yy[k]),int(xx[k]),int(yy[k]+dy),int(xx[k]+dx)]
    distance=np.full(size,np.inf);receiver=np.zeros(size,np.int32);previous=np.zeros(size,np.int32);queue=[]
    for code in [*BASE,*[n['code'] for n in water]]:
        distance[code]=-np.inf;receiver[code]=code;heapq.heappush(queue,(-np.inf,code))
    while queue:
        cost,current=heapq.heappop(queue)
        if cost!=distance[current]:continue
        for nxt in np.flatnonzero(np.isfinite(saddles[current])):
            new=max(cost,float(saddles[current,nxt]))
            if new<distance[nxt]:distance[nxt]=new;receiver[nxt]=receiver[current];previous[nxt]=current;heapq.heappush(queue,(new,int(nxt)))
    rows=[]
    for code,raw_code in pit_map.items():
        n=nodes[code];nextcode=int(previous[code]);point=locations.get((min(code,nextcode),max(code,nextcode)))
        coords=[]
        if point:
            for y,x in (point[:2],point[2:]):coords.append(list(tr*(x+.5,y+.5)))
        chain=[];cur=code
        while cur in pit_map and cur not in chain:chain.append(cur);cur=int(previous[cur])
        max_depth=max(float(distance[c]-nodes[c]['dsmSurfaceM']) for c in chain) if np.isfinite(distance[code]) else None
        rows.append({**n,'potentialReceiverCode':int(receiver[code]),'spillThresholdM':float(distance[code]) if np.isfinite(distance[code]) else None,
                     'spillDepthM':float(distance[code]-n['dsmSurfaceM']) if np.isfinite(distance[code]) else None,
                     'maxChainPitDepthM':max_depth,'nextNodeCode':nextcode,'saddleCoordinates':coords,
                     'pathCodes':chain+[cur]})
    summary={str(t):{'areaKm2':sum(r['geometryAreaKm2'] for r in rows if r['maxChainPitDepthM'] is not None and r['maxChainPitDepthM']<=t),
                     'pits':sum(r['maxChainPitDepthM'] is not None and r['maxChainPitDepthM']<=t for r in rows)} for t in (.1,.5,1,2,4,10)}
    result={'method':'8-neighbour-marker-watershed-with-all-raw-residual-sinks; minimum-saddle graph to named receiver or retained water',
            'demSHA256':sha_file(OUT/'公开连续DSM.tif'),'actualDrainageSHA256':sha_file(OUT/'实际下游核查.json'),
            'thresholdSensitivity':summary,'pits':rows,'waterNodes':water,'verticalReference':'EGM2008 m; no station-datum comparison',
            'note':'Quantifies terrain conditioning alternatives. Threshold is not authorization to erase real lakes or invent bathymetry.'}
    write_new(OUT/'洼地溢流门槛.json',result)
    print(json.dumps({'sensitivity':summary,'largestPits':sorted(rows,key=lambda r:r['geometryAreaKm2'],reverse=True)[:8]},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
