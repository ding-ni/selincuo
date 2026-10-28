"""Versioned grid/HRU geometry and conservative whole-domain drainage audit."""
import math
from pathlib import Path
import json
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
    if archived:
        if archived.get('version') != version:
            raise ValueError('空间清单与版本身份不一致')
        if archived.get('unitStrategy') == 'partition-elevation-surface-area-weighted-v1':
            body = {k: v for k, v in archived.items() if k != 'version'}
            if version != 'AGG-SPATIAL-' + digest(body)[:20]:
                raise ValueError('聚合空间清单摘要不一致')
            parent = load(archived['parentSpatialVersion'])
            if digest(parent) != archived['parentManifestSHA256']:
                raise ValueError('聚合空间的来源单元已改变')
        return archived
    old=read_json(ROOT/'data/derived/spatial/manifest.json')
    if old and old['version']==version:return old
    value=read_json(directory(version)/'manifest.json')
    if not value:raise FileNotFoundError('该空间版本不存在')
    return value


AGGREGATE_STRATEGY = 'partition-elevation-surface-area-weighted-v1'


def topology_complete(spatial):
    """Completeness requires a receiver for every unit, never an area cutoff.

    Historical manifests without explicit topology retain their recorded flag
    only when they contain no unresolved source unit or positive unknown area.
    New topology manifests must also declare all internal exchanges paired.
    """
    drainage=spatial.get('drainage') or {}
    if any(u.get('partition')=='unresolved' and u.get('areaKm2',0)>0 for u in spatial.get('units',[])):
        return False
    if drainage.get('unresolvedAreaKm2',0)>0:return False
    topology=drainage.get('topology')
    if topology is None:return bool(drainage.get('inflowComplete',False))
    receivers=topology.get('unitReceivers') or {}
    allowed=set(topology.get('nodes',[]))|set(topology.get('boundaries',[]))
    return (bool(topology.get('internalExchangesPaired')) and
            set(receivers)=={u['id'] for u in spatial.get('units',[])} and
            all(value in allowed for value in receivers.values()) and
            not topology.get('unresolvedReceivers'))
_AGGREGATE_SOURCE_FILES = ('units.geojson', 'units-display.geojson', 'display.geojson',
                           'rivers-display.geojson', 'display-context.json', 'grid.geojson',
                           'terrain-display.png')


def aggregate_manifest(parent, asset_hashes):
    """Describe the E3-type groups without evolving or copying member states.

    Each fine intersection appears once. P/E and temperature after the member's
    elevation correction are area-weighted before the shared nonlinear kernel.
    """
    if parent.get('unitStrategy') != 'grid-partition-elevation-surface-v2':
        raise ValueError('聚合单元需要逐气象格网相交的来源空间')
    if set(asset_hashes) != set(_AGGREGATE_SOURCE_FILES):
        raise ValueError('聚合空间缺少来源图层摘要')
    groups = {}
    for i, unit in enumerate(parent['units']):
        if not np.isfinite(unit['areaKm2']) or unit['areaKm2'] <= 0:
            raise ValueError('来源单元面积无效')
        grid = unit['gridIndex']
        if not isinstance(grid, int) or not 0 <= grid < len(parent['grids']):
            raise ValueError('来源单元的气象格网索引无效')
        groups.setdefault((unit['partition'], unit['elevationBandMin'], unit['kind']), []).append((i, unit))
    if not groups:
        raise ValueError('来源空间没有有效单元')
    units, aggregation, weights = [], [], []
    for (partition, band, kind), members in sorted(groups.items()):
        area = sum(u['areaKm2'] for _, u in members)
        identity = {'parentSpatialVersion': parent['version'], 'partition': partition,
                    'elevationBandMin': band, 'kind': kind}
        unit = {'id': 'AGG-' + digest(identity)[:20], 'partition': partition,
                'basin': 'zajia' if partition in ('duja', 'zajia_rest') else partition,
                'elevationBandMin': band, 'elevationBandMax': band + 500, 'kind': kind,
                'areaKm2': area, 'elevation': sum(u['areaKm2'] * u['elevation'] for _, u in members) / area}
        mapping = [{'unitId': u['id'], 'unitIndex': i, 'gridIndex': u['gridIndex'],
                    'areaWeight': u['areaKm2'] / area,
                    'elevationOffsetM': u['elevation'] - parent['grids'][u['gridIndex']]['referenceElevation']}
                   for i, u in members]
        if abs(sum(m['areaWeight'] for m in mapping) - 1.) > 1e-12:
            raise ValueError('聚合单元面积权重不闭合')
        row = [0.] * len(parent['grids'])
        for member in mapping:
            row[member['gridIndex']] += member['areaWeight']
        units.append(unit); aggregation.append({'unitId': unit['id'], 'members': mapping}); weights.append(row)
    result = {k: parent[k] for k in ('projectId', 'forcingResolution', 'elevationBandMeters', 'areaCrs',
              'forcingGridVersion', 'domainAreaKm2', 'lakeAreaKm2', 'grids', 'lakeWeights', 'bounds',
              'basins', 'drainage', 'temperatureElevationReference') if k in parent}
    result.update({'schemaVersion': 3, 'unitStrategy': AGGREGATE_STRATEGY,
                   'parentSpatialVersion': parent['version'], 'parentManifestSHA256': digest(parent),
                   'sourceAssetsSHA256': dict(sorted(asset_hashes.items())),
                   'unitTemperatureElevation': 'each source unit corrected before area-weighted grouping',
                   'units': units, 'weights': weights, 'aggregation': aggregation})
    result['version'] = 'AGG-SPATIAL-' + digest(result)[:20]
    return result


def _write_new(path, data):
    """Create a derived asset without silently replacing an existing file."""
    try:
        with path.open('xb') as stream:
            stream.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError('已有聚合空间文件与本次派生不一致：' + str(path))


def ensure_aggregate(parent_version):
    """Materialize only the reusable 31-group spatial derivative and its maps."""
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union
    parent = load(parent_version)
    source = directory(parent_version)
    hashes = {name: sha_file(source / name) for name in _AGGREGATE_SOURCE_FILES}
    manifest = aggregate_manifest(parent, hashes)
    folder = directory(manifest['version'])
    if (folder / 'manifest.json').is_file():
        actual = load(manifest['version'])
        if actual != manifest or any(not (folder / name).is_file() for name in
                ('units.geojson', *_AGGREGATE_SOURCE_FILES, 'aggregate-receipt.json')):
            raise ValueError('已存在的聚合空间不完整或身份不一致')
        receipt = read_json(folder / 'aggregate-receipt.json')
        if not receipt or any(sha_file(folder / name) != value for name, value in receipt['assetsSHA256'].items()):
            raise ValueError('聚合空间图层摘要不一致')
        return actual
    folder.mkdir(parents=True, exist_ok=True)
    for name in _AGGREGATE_SOURCE_FILES:
        if name not in ('units.geojson', 'units-display.geojson'):
            _write_new(folder / name, (source / name).read_bytes())
            if sha_file(folder / name) != hashes[name]:
                raise ValueError('来源图层复制校验失败：' + name)
    for name in ('units.geojson', 'units-display.geojson'):
        original = read_json(source / name)
        features = original['features']
        by_id = {f['properties']['id']: f for f in features}
        if len(by_id) != len(parent['units']) or any(u['id'] not in by_id for u in parent['units']):
            raise ValueError('来源几何与来源计算单元不一致')
        derived = []
        for unit, group in zip(manifest['units'], manifest['aggregation']):
            geom = unary_union([shape(by_id[m['unitId']]['geometry']) for m in group['members']])
            if geom.is_empty or not geom.is_valid:
                raise ValueError('聚合单元几何为空或无效：' + unit['id'])
            derived.append({'type': 'Feature', 'properties': unit, 'geometry': mapping(geom)})
        data = (json.dumps({'type': 'FeatureCollection', 'features': derived}, ensure_ascii=False,
                           separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
        _write_new(folder / name, data)
    produced = ('units.geojson', 'units-display.geojson', 'display.geojson', 'rivers-display.geojson',
                'display-context.json', 'grid.geojson', 'terrain-display.png')
    receipt = {'parentSpatialVersion': parent_version, 'sourceAssetsSHA256': hashes,
               'assetsSHA256': {name: sha_file(folder / name) for name in produced},
               'unitCount': len(manifest['units']), 'sourceUnitCount': len(parent['units'])}
    for name, value in (('aggregate-receipt.json', receipt), ('manifest.json', manifest)):
        _write_new(folder / name, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8'))
    return load(manifest['version'])

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
