"""Versioned grid/HRU geometry and conservative whole-domain drainage audit."""
import math
from pathlib import Path
import numpy as np
from .common import ROOT,read_json,write_json,digest,sha_file,within

INDEX=ROOT/'data/derived/spatial/grid-current.json'
NAMES={'zajia':'扎加藏布','zagen':'扎根藏布','boqu':'波曲藏布','ali':'阿里藏布','fringe':'其他已核实入湖集水区','unresolved':'排水归属未核实区','external':'其他闭合或外排区'}

def current():
    pointer=read_json(INDEX)
    return load(pointer['version']) if pointer else None

def directory(version):
    return within(ROOT/'data/derived/spatial/versions',version)

def load(version):
    archived=read_json(directory(version)/'manifest.json')
    if archived:return archived
    old=read_json(ROOT/'data/derived/spatial/manifest.json')
    if old and old['version']==version:return old
    value=read_json(directory(version)/'manifest.json')
    if not value:raise FileNotFoundError('该空间版本不存在')
    return value

def build(progress=None):
    import geopandas as gpd
    import rasterio
    from rasterio.features import rasterize,shapes
    from shapely import make_valid
    from shapely.geometry import shape,mapping,box,Polygon,MultiPolygon
    from shapely.ops import transform,unary_union
    from shapely.strtree import STRtree
    from pyproj import Transformer
    old=read_json(ROOT/'data/derived/spatial/manifest.json')
    if not old:raise ValueError('缺少既有空间来源')
    import shutil
    legacy=directory(old['version'])
    legacy.mkdir(parents=True,exist_ok=True)
    for file in (ROOT/'data/derived/spatial').iterdir():
        if file.is_file() and file.name not in ('grid-current.json',) and not (legacy/file.name).exists():
            shutil.copy2(file,legacy/file.name)
    area=Transformer.from_crs(4326,6933,always_xy=True).transform
    wgs=Transformer.from_crs(6933,4326,always_xy=True).transform
    repairs=[]
    def poly(geom):
        if not geom.is_valid:geom=make_valid(geom)
        if geom.geom_type in ('Polygon','MultiPolygon'):return geom
        return unary_union([g for g in getattr(geom,'geoms',[]) if g.geom_type in ('Polygon','MultiPolygon')])
    fc=read_json(ROOT/'data/derived/spatial/map.geojson')['features']
    exact={f['properties'].get('id',f['properties']['kind']):poly(transform(area,shape(f['geometry']))) for f in fc if f['properties']['kind'] in ('basin','lake','control','domain')}
    source=ROOT/'data/raw/spatial/source-package/full_basin_shp/basin1.shp'
    full=poly(unary_union(gpd.read_file(source).to_crs(6933).geometry))
    lake=exact['lake'];domain=poly(full.union(lake))
    occupied=lake;basins={}
    for key in ('zajia','zagen','boqu','ali'):
        basins[key]=poly(exact[key].intersection(domain).difference(occupied));occupied=occupied.union(basins[key])
    residual=poly(domain.difference(occupied))
    dem_path=ROOT/'data/raw/spatial/terrain-reference/dem-1km-raw.tif'
    with rasterio.open(dem_path) as ds:
        z=ds.read(1).astype(float);tr=ds.transform;nodata=ds.nodata;crs=str(ds.crs)
    if crs!='EPSG:4326':raise ValueError('全域DEM坐标定义不符')
    valid=np.isfinite(z)&(z!=nodata)&(z>1000)
    def mask(geom):return rasterize([(mapping(transform(wgs,geom)),1)],out_shape=z.shape,transform=tr).astype(bool)
    dm=mask(domain);lm=mask(lake);rm=mask(residual)
    if np.any(dm&~valid):raise ValueError('原始DEM未完整覆盖新控制域，禁止补零构建')
    known=np.zeros(z.shape,dtype=np.int16)
    for code,key in enumerate(('zajia','zagen','boqu','ali'),1):known[mask(basins[key])]=code
    h,w=z.shape;best=np.zeros_like(z);pointer=np.zeros(z.shape,dtype=np.uint8)
    directions=[(-1,0),(-1,1),(0,1),(1,1),(1,0),(1,-1),(0,-1),(-1,-1)]
    # Strictly downhill raw-DEM directions. True/unknown depressions remain sinks.
    lat=tr.f+(np.arange(h)+.5)*tr.e
    for code,(dr,dc) in enumerate(directions,1):
        ys=slice(max(0,-dr),min(h,h-dr));xs=slice(max(0,-dc),min(w,w-dc))
        yt=slice(max(0,dr),min(h,h+dr));xt=slice(max(0,dc),min(w,w+dc))
        distance=np.sqrt((dr*abs(tr.e)*111195)**2+(dc*abs(tr.a)*111195*np.cos(np.radians(lat[ys])))**2)[:,None]
        slope=(z[ys,xs]-z[yt,xt])/distance
        choose=valid[ys,xs]&valid[yt,xt]&(slope>best[ys,xs]+1e-12)
        best[ys,xs][choose]=slope[choose];pointer[ys,xs][choose]=code
    cache={};terminal_codes={1:'zajia',2:'zagen',3:'boqu',4:'ali',5:'fringe',6:'external',7:'unresolved'}
    def terminal(cell):
        path=[];seen=set()
        while True:
            y,x=cell
            if y<0 or x<0 or y>=h or x>=w:result=7;break
            if lm[y,x]:result=5;break
            if known[y,x]:result=int(known[y,x]);break
            if cell in cache:result=cache[cell];break
            if not dm[y,x] or not valid[y,x] or cell in seen:result=7;break
            seen.add(cell);path.append(cell);code=int(pointer[y,x])
            if not code:result=7;break
            dr,dc=directions[code-1];cell=(y+dr,x+dc)
        for p in path:cache[p]=result
        return result
    allocation=np.zeros(z.shape,dtype=np.uint8)
    cells=np.argwhere(rm)
    for i,(row,col) in enumerate(cells):
        allocation[row,col]=terminal((int(row),int(col)))
        if progress and i%1000==0:progress(5+20*i/max(1,len(cells)),'核查全域排水终点（保留闭合洼地）')
    grouped={}
    for geo,code in shapes(allocation,mask=rm,transform=tr):
        grouped.setdefault(int(code),[]).append(transform(area,shape(geo)))
    used=unary_union(list(basins.values())+[lake])
    allocation_areas={}
    for code in (1,2,3,4,5,6):
        key=terminal_codes[code]
        geom=poly(unary_union(grouped.get(code,[])).intersection(residual).difference(used))
        allocation_areas[key]=geom.area/1e6
        if not geom.is_empty:basins[key]=poly(basins.get(key,Polygon()).union(geom));used=used.union(geom)
    basins['unresolved']=poly(domain.difference(used))
    if 'fringe' not in basins:basins['fringe']=Polygon()
    duja=poly(exact['duja'].intersection(basins['zajia']))
    partitions={'duja':duja,'zajia_rest':poly(basins['zajia'].difference(duja)),**{k:g for k,g in basins.items() if k!='zajia'}}
    old_units=read_json(ROOT/'data/derived/spatial/units.geojson')['features'];bands={}
    for f in old_units:
        g=shape(f['geometry'])
        if not g.is_valid:repairs.append(f['properties']['id']);g=make_valid(g)
        p=f['properties'];bands.setdefault((p['elevationBandMin'],p['kind']),[]).append(poly(transform(area,g)))
    bands={k:poly(unary_union(v)) for k,v in bands.items()}
    grid_geometries=[poly(transform(area,box(g['lon']-.05,g['lat']-.05,g['lon']+.05,g['lat']+.05))) for g in old['grids']]
    tree=STRtree(grid_geometries);units=[];geometries=[]
    for partition,geometry in partitions.items():
        for (elevation,kind),band in sorted(bands.items()):
            region=poly(geometry.intersection(band))
            if region.is_empty:continue
            for index in sorted(map(int,tree.query(region,predicate='intersects'))):
                part=poly(region.intersection(grid_geometries[index]))
                if part.is_empty or part.area<=1e-6:continue
                grid=old['grids'][index];identity={'grid':grid['id'],'partition':partition,'elevationBandMin':elevation,'kind':kind}
                units.append({'id':'GRID-'+digest(identity)[:20],**identity,'gridIndex':index,'basin':'zajia' if partition in ('duja','zajia_rest') else partition,'elevationBandMax':elevation+500,'elevation':elevation+250,'areaKm2':part.area/1e6})
                geometries.append(part)
        if progress:progress(30+40*len(units)/1800,'建立逐格网冰雪与陆面单元')
    total=sum(u['areaKm2'] for u in units);error=total+lake.area/1e6-domain.area/1e6
    if abs(error)>max(.01,domain.area/1e6*1e-6):raise ValueError('新格网面积不闭合：'+str(error))
    lake_weights=np.array([g.intersection(lake).area/lake.area for g in grid_geometries]);lake_weights/=lake_weights.sum()
    manifest={'schemaVersion':2,'projectId':'selinco','unitStrategy':'grid-partition-elevation-surface-v2','forcingResolution':'0.1deg','elevationBandMeters':500,'areaCrs':'EPSG:6933','parentSpatialVersion':old['version'],'forcingGridVersion':digest(old['grids']),
              'domainAreaKm2':domain.area/1e6,'lakeAreaKm2':lake.area/1e6,'originalFullBasinAreaKm2':full.area/1e6,'legacyUnionOutsideOriginalKm2':exact['domain'].difference(full).area/1e6,'domainAddedLakeAreaKm2':lake.difference(full).area/1e6,
              'sourcePolicy':'provided full boundary plus supplied lake; no unverified named-basin expansion','grids':old['grids'],'units':units,'lakeWeights':lake_weights.tolist(),'bounds':list(transform(wgs,domain).bounds),
              'basins':[{'id':k,'name':NAMES[k],'areaKm2':g.area/1e6,'includedInLakeInflow':k not in ('unresolved','external')} for k,g in basins.items()],
              'areaClosureErrorKm2':error,'geometryRepairs':repairs,'sourceFiles':{'fullBoundary':sha_file(source),'rawDem':sha_file(dem_path)},
              'drainage':{'method':'raw-full-domain-downhill-D8-v1; no forced depression drainage','residualOriginalKm2':residual.area/1e6,'residualAllocationKm2':allocation_areas,'unresolvedAreaKm2':basins['unresolved'].area/1e6,'inflowComplete':basins['unresolved'].area<1,'limitation':'粗DEM闭洼或边界去向无法证明时保持未核实，不计入已核实入湖合计'},
              'temperatureElevationReference':old['temperatureElevationReference'],'unitTemperatureElevation':'500 m vertical-band midpoint; each grid independently forced'}
    manifest['version']='GRID-SPATIAL-'+digest(manifest)[:20];folder=directory(manifest['version']);folder.mkdir(parents=True,exist_ok=True)
    for filename,array in [('drainage-d8-raw.tif',pointer),('residual-destination.tif',allocation)]:
        with rasterio.open(folder/filename,'w',driver='GTiff',height=h,width=w,count=1,dtype='uint8',crs=crs,transform=tr,nodata=0,compress='deflate') as dst:dst.write(array,1)
    write_json(folder/'drainage-provenance.json',{'sourceDEM':sha_file(dem_path),'codes':terminal_codes,'d8Directions':directions,'method':manifest['drainage']['method'],'noDepressionFilling':True,'unitGeometryAreaKm2':total,'lakeWeightsSum':float(lake_weights.sum()),'gridUnitMapping':[{'id':u['id'],'grid':u['grid'],'partition':u['partition'],'areaKm2':u['areaKm2']} for u in units]})
    def feature(g,p):return {'type':'Feature','properties':p,'geometry':mapping(poly(transform(wgs,g)))}
    maps=[feature(domain,{'kind':'domain','name':'色林错核查控制域'}),feature(lake,{'kind':'lake','name':'色林错','areaKm2':lake.area/1e6})]
    maps +=[feature(g,{'kind':'basin','id':k,'name':NAMES[k],'areaKm2':g.area/1e6}) for k,g in basins.items() if not g.is_empty]
    maps.append(feature(duja,{'kind':'control','id':'duja','name':'杜加控制区','areaKm2':duja.area/1e6}))
    maps +=[f for f in fc if f['properties']['kind']=='glacier']
    write_json(folder/'map.geojson',{'type':'FeatureCollection','features':maps})
    write_json(folder/'display.geojson',{'type':'FeatureCollection','features':[{**f,'geometry':mapping(make_valid(shape(f['geometry'])).simplify(.001,preserve_topology=True))} for f in maps]})
    write_json(folder/'units.geojson',{'type':'FeatureCollection','features':[feature(g,u) for g,u in zip(geometries,units)]})
    write_json(folder/'units-display.geojson',{'type':'FeatureCollection','features':[feature(g.simplify(50,preserve_topology=True),u) for g,u in zip(geometries,units)]})
    write_json(folder/'grid.geojson',{'type':'FeatureCollection','features':[feature(g.intersection(domain),{**info,'areaKm2':g.intersection(domain).area/1e6}) for g,info in zip(grid_geometries,old['grids']) if not g.intersection(domain).is_empty]})
    import shutil
    shutil.copy2(ROOT/'data/derived/spatial/rivers-display.geojson',folder/'rivers-display.geojson')
    context=read_json(ROOT/'data/derived/spatial/display-context.json');context={**context,'domainBounds':manifest['bounds'],'lakeBounds':list(transform(wgs,lake).bounds),'labels':[]}
    for f in maps:
        if f['properties']['kind'] not in ('lake','basin'):continue
        point=shape(f['geometry']).representative_point();context['labels'].append({**f['properties'],'anchor':[point.x,point.y]})
    write_json(folder/'display-context.json',context)
    # Full raw DEM, not the four-catchment filled mosaic, supplies the display.
    from PIL import Image
    from rasterio.warp import reproject,Resampling
    from rasterio.transform import from_bounds
    values=np.full((850,1200),np.nan,dtype=np.float32)
    with rasterio.open(dem_path) as ds:reproject(rasterio.band(ds,1),values,src_transform=ds.transform,src_crs=ds.crs,dst_transform=from_bounds(*context['bounds'],1200,850),dst_crs='EPSG:4326',src_nodata=ds.nodata,dst_nodata=np.nan,resampling=Resampling.bilinear)
    valid_display=np.isfinite(values);fill=np.where(valid_display,values,5000);dy,dx=np.gradient(fill);shade=np.clip(.66-dx*.006+dy*.008,.22,.98)
    color=np.stack([212*shade+35,220*shade+31,207*shade+40,np.where(valid_display,210,0)],axis=-1)
    Image.fromarray(np.clip(color,0,255).astype(np.uint8),'RGBA').save(folder/'terrain-display.png')
    write_json(folder/'manifest.json',manifest);write_json(INDEX,{'version':manifest['version']})
    if progress:progress(100,f'逐格网空间核查完成：{len(units)}单元，未核实{basins["unresolved"].area/1e6:.1f}km²')
    return manifest
