"""Materialize the operational land domain; unresolved backdrop has no HRUs."""
from copy import deepcopy
import json

from .common import ROOT, digest, read_json, sha_file
from .grid_spatial import directory, ensure_aggregate, load, _write_new

STRICT_PARENT = 'GRID-SPATIAL-0a6b907d2496b6fab2ce'


def ensure_domain(parent_version=STRICT_PARENT):
    from pyproj import Transformer
    from shapely.geometry import shape, mapping
    from shapely.ops import transform, unary_union
    from shapely import make_valid, set_precision

    parent = load(parent_version)
    source = directory(parent_version)
    original_path = ROOT/'data/derived/spatial/map.geojson'
    original = read_json(original_path)
    def polygon(g):
        g = make_valid(g) if not g.is_valid else g
        if g.geom_type in ('Polygon','MultiPolygon'):
            return set_precision(g,1e-9)
        return set_precision(unary_union([p for p in getattr(g,'geoms',[]) if p.geom_type in ('Polygon','MultiPolygon')]),1e-9)
    original_geometries = {f['properties'].get('id') or f['properties']['kind']:polygon(shape(f['geometry']))
                           for f in original['features'] if f['properties']['kind'] in ('basin','control','lake','domain')}
    lake = original_geometries['lake']
    basins = {k:polygon(original_geometries[k].difference(lake)) for k in ('zajia','zagen','boqu','ali')}
    duja = polygon(original_geometries['duja'].intersection(basins['zajia']))
    targets = {'duja':duja,'zajia_rest':polygon(basins['zajia'].difference(duja)),
               **{k:g for k,g in basins.items() if k!='zajia'}}
    project = Transformer.from_crs(4326,6933,always_xy=True).transform
    def area(g):
        return transform(project,g).area
    units, features, by_partition = [], [], {}
    excluded = [u for u in parent['units'] if u['partition'] in ('unresolved','external')]
    source_features = read_json(source/'units.geojson')['features']
    known = {u['id']:u for u in parent['units'] if u['partition'] not in ('unresolved','external')}
    for f in source_features:
        old = known.get(f['properties']['id'])
        if old is None:
            continue
        g = polygon(shape(f['geometry']))
        remaining = g
        pieces = {}
        for key,target in targets.items():
            if not remaining.is_empty and remaining.intersects(target):
                piece = polygon(remaining.intersection(target))
                if not piece.is_empty and area(piece)>1e-4:
                    pieces[key] = piece
                    remaining = polygon(remaining.difference(piece))
        if not remaining.is_empty and area(remaining)>1e-4:
            pieces['fringe'] = remaining
        total = sum(area(p) for p in pieces.values())
        if not total:
            raise ValueError('业务计算范围丢失来源单元：'+old['id'])
        for key,piece in pieces.items():
            identity = {'sourceUnitId':old['id'],'partition':key,'scope':'four-original-basins-and-direct-fringe-v1'}
            unit = {**old,**identity,'id':'GRID-'+digest(identity)[:20],
                    'basin':'zajia' if key in ('duja','zajia_rest') else key,
                    'areaKm2':old['areaKm2']*area(piece)/total}
            units.append(unit)
            features.append({'type':'Feature','properties':unit,'geometry':mapping(piece)})
            by_partition.setdefault(key,[]).append(piece)
    display_only_area = sum(u['areaKm2'] for u in excluded)
    calculated_area = sum(u['areaKm2'] for u in units)
    if abs(calculated_area+display_only_area-sum(u['areaKm2'] for u in parent['units']))>1e-7:
        raise ValueError('业务计算范围面积不闭合')
    manifest = deepcopy(parent)
    manifest.pop('version',None)
    manifest.update(parentSpatialVersion=parent_version,parentManifestSHA256=digest(parent),units=units)
    manifest['calculationDomain'] = {
        'policy':'four-original-shp-basins-and-strict-direct-fringe; unknown-and-external-display-only-v1',
        'originalBoundariesSHA256':sha_file(original_path),'calculationLandAreaKm2':calculated_area,
        'displayOnlyAreaKm2':display_only_area,'excludedSourceUnits':[u['id'] for u in excluded],
        'namedBasinBoundaries':'original SHP-derived polygons; no terrain expansion of river or Duja boundary'}
    drainage = manifest['drainage']
    drainage.update(unresolvedAreaKm2=0.,displayOnlyUnresolvedAreaKm2=display_only_area,
                    calculationDomainComplete=True,inflowComplete=False,
                    limitation='背景未定区域仅展示，不参与本方案计算。')
    drainage['topology'] = {'status':'calculation-domain-only','unitReceivers':{
        u['id']:('zajia' if u['partition'] in ('duja','zajia_rest') else u['partition']) for u in units},
        'nodes':['zajia','zagen','boqu','ali','fringe'],'boundaries':['lake'],
        'internalExchangesPaired':True,'unresolvedReceivers':[]}
    manifest['basins'] = [{**row,'areaKm2':sum(u['areaKm2'] for u in units if u['basin']==row['id'])}
                          for row in parent['basins'] if row['id'] not in ('external','unresolved')]
    manifest['version'] = 'GRID-SPATIAL-'+digest(manifest)[:20]
    folder = directory(manifest['version'])
    folder.mkdir(parents=True,exist_ok=True)
    def put(name,value):
        _write_new(folder/name,(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode('utf-8'))
    put('units.geojson',{'type':'FeatureCollection','features':features})
    put('units-display.geojson',{'type':'FeatureCollection','features':[
        {**f,'geometry':mapping(shape(f['geometry']).simplify(.0005,preserve_topology=True))} for f in features]})
    maps = []
    for f in original['features']:
        props = deepcopy(f['properties'])
        key = props.get('id') or props['kind']
        if key=='fringe':
            g = polygon(unary_union(by_partition.get('fringe',[])))
            props.update(name='湖周直接来水',areaKm2=sum(u['areaKm2'] for u in units if u['partition']=='fringe'))
            maps.append({**f,'properties':props,'geometry':mapping(g)})
        else:
            maps.append(f)
    background = [f for f in read_json(source/'map.geojson')['features'] if f['properties'].get('id') in ('unresolved','external')]
    maps.extend([{**f,'properties':{**f['properties'],'displayOnly':True,'name':'流域背景'}} for f in background])
    put('map.geojson',{'type':'FeatureCollection','features':maps})
    put('display.geojson',{'type':'FeatureCollection','features':[
        {**f,'geometry':mapping(polygon(shape(f['geometry'])).simplify(.001,preserve_topology=True))} for f in maps]})
    for name in ('display-context.json','grid.geojson','rivers-display.geojson','terrain-display.png'):
        _write_new(folder/name,(source/name).read_bytes())
    put('manifest.json',manifest)
    aggregate = ensure_aggregate(manifest['version'])
    return {'fine':manifest['version'],'aggregate':aggregate['version'],
            'units':len(aggregate['units']),'sourceUnits':len(units),
            'calculationLandAreaKm2':calculated_area,'displayOnlyAreaKm2':display_only_area}
