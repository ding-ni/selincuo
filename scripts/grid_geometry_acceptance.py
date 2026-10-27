# -*- coding: utf-8 -*-
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT,read_json,write_json,sha_file
from backend.grid_spatial import current,directory
from shapely.geometry import shape
from shapely import make_valid
from shapely.ops import transform,unary_union
from shapely.strtree import STRtree
from pyproj import Transformer
spatial=current();folder=directory(spatial['version']);project=Transformer.from_crs(4326,6933,always_xy=True).transform
features=read_json(folder/'units.geojson')['features'];stored=[shape(f['geometry']) for f in features]
projected_repairs=[];geometries=[]
for f,g in zip(features,stored):
    before=transform(project,g);after=make_valid(before)
    if not before.is_valid:projected_repairs.append({'id':f['properties']['id'],'beforeAreaKm2':before.area/1e6,'afterAreaKm2':after.area/1e6,'differenceKm2':(after.area-before.area)/1e6})
    geometries.append(after)
union=unary_union(geometries);overlap=(sum(g.area for g in geometries)-union.area)/1e6
maps=read_json(folder/'map.geojson')['features'];lake=transform(project,shape(next(f for f in maps if f['properties']['kind']=='lake')['geometry']));domain=transform(project,shape(next(f for f in maps if f['properties']['kind']=='domain')['geometry']))
closure=union.union(lake).symmetric_difference(domain).area/1e6
repairs=[]
for feature in read_json(ROOT/'data/derived/spatial/units.geojson')['features']:
    geo=shape(feature['geometry'])
    if not geo.is_valid:
        before=transform(project,geo).area/1e6;after=transform(project,make_valid(geo)).area/1e6
        repairs.append({'id':feature['properties']['id'],'beforeAreaKm2':before,'afterAreaKm2':after,'differenceKm2':after-before})
evidence={'version':spatial['version'],'units':len(features),'forcingGrids':len(spatial['grids']),'allOutputGeometriesValid':all(g.is_valid for g in stored),'reprojectionRoundoffRepairs':projected_repairs,'unitOverlapKm2':overlap,'geometricClosureDifferenceKm2':closure,'lakeUnitOverlapKm2':union.intersection(lake).area/1e6,'sourceDEMResolution':'0.01 degrees; full-domain raw DEM, no forced depression filling','repairs':repairs,'drainage':spatial['drainage'],'controlDomainPolicy':spatial['sourcePolicy'],'addedLakeAreaKm2':spatial['domainAddedLakeAreaKm2'],'excludedLegacyOutsideLandKm2':spatial['legacyUnionOutsideOriginalKm2']-spatial['domainAddedLakeAreaKm2']}
assert abs(overlap)<.01 and closure<.01 and evidence['lakeUnitOverlapKm2']<.01
write_json(ROOT/'build/grid-revision-20261026/geometry-evidence.json',evidence);write_json(folder/'geometry-repair-details.json',{'sourceSHA256':sha_file(ROOT/'data/derived/spatial/units.geojson'),'repairs':repairs,'reprojectionRoundoffRepairs':projected_repairs});print(json.dumps(evidence,ensure_ascii=False),flush=True)
