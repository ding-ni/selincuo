from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path

import numpy as np

from .common import ROOT, digest, now, read_json, sha_file, write_json

NAMES = {'zajia': '扎加藏布', 'zagen': '扎根藏布', 'boqu': '波曲藏布', 'ali': '阿里藏布', 'fringe': '湖周补充区'}
DUJA = (89.15997904, 32.03057137)


def build_spatial(progress=None):
    import geopandas as gpd
    import rasterio
    from pyproj import Transformer
    from rasterio.features import rasterize, shapes
    from scipy.ndimage import distance_transform_edt
    from shapely import make_valid
    from shapely.geometry import shape, mapping, box, Polygon, MultiPolygon
    from shapely.ops import transform, unary_union

    source = ROOT / 'data/raw/spatial/source-package'
    terrain = ROOT / 'data/raw/spatial/terrain-reference'
    target = ROOT / 'data/derived/spatial'
    target.mkdir(parents=True, exist_ok=True)
    previous = read_json(target / 'manifest.json')
    if previous:
        import shutil
        previous_archive = target / 'versions' / previous['version']
        previous_archive.mkdir(parents=True, exist_ok=True)
        for name in ('manifest.json', 'map.geojson', 'units.geojson', 'grid.geojson', 'display.geojson'):
            if (target / name).is_file():
                shutil.copy2(target / name, previous_archive / name)
    to_area = Transformer.from_crs(4326, 6933, always_xy=True).transform
    to_wgs = Transformer.from_crs(6933, 4326, always_xy=True).transform
    inputs, repairs = {}, []

    def polygons(geom):
        geom = make_valid(geom)
        if isinstance(geom, (Polygon, MultiPolygon)):
            return geom
        return unary_union([g for g in getattr(geom, 'geoms', []) if isinstance(g, (Polygon, MultiPolygon))])

    def load(relative):
        path = source / relative
        frame = gpd.read_file(path)
        if frame.crs is None:
            raise ValueError(f'空间资料缺少坐标系：{relative}')
        inputs[relative] = sha_file(path)
        repairs.append({'source': relative, 'features': len(frame), 'invalidBefore': int((~frame.geometry.is_valid).sum()), 'crs': str(frame.crs)})
        frame = frame.to_crs(6933)
        frame.geometry = frame.geometry.map(polygons)
        return frame

    if progress:
        progress(5, '核对控制域、湖面和四流域')
    full = unary_union(load('full_basin_shp/basin1.shp').geometry)
    lake = unary_union(load('lake_shp/湖泊边界.shp').geometry)
    originals = {key: unary_union(load(f'subbasin_shp/{name}边界.shp').geometry)
                 for key, name in list(NAMES.items())[:4]}
    # Preserve all supplied control geometry. Lake has first priority, then the
    # four named basins; the remainder is an explicit fifth drainage zone.
    domain = polygons(unary_union([full, lake, *originals.values()]))
    occupied = lake
    basins = {}
    overlap = {}
    for key, geom in originals.items():
        overlap[key] = geom.intersection(occupied).area / 1e6
        basins[key] = polygons(geom.difference(occupied))
        occupied = occupied.union(basins[key])
    basins['fringe'] = polygons(domain.difference(occupied))

    glaciers, seen = [], set()
    for rel in ('glacier/zjzb/glacier_zjzb.shp', 'glacier/zgzb/glacier_zgzb.shp', 'glacier/alzb/glacier_alzb.shp'):
        for _, row in load(rel).iterrows():
            identifier = str(row.get('FCGI_ID') or row.get('GLIMS_ID') or digest(mapping(row.geometry)))
            if identifier in seen:
                continue
            seen.add(identifier)
            geom = polygons(row.geometry.intersection(domain).difference(lake))
            if not geom.is_empty and geom.area > 0:
                glaciers.append((identifier, geom))
    ice = polygons(unary_union([geom for _, geom in glaciers]))

    # This D8 source has an explicit, traceable code definition in the upstream
    # asset builder: 1=N,2=NE,4=E,...128=NW. It is not guessed from filenames.
    with rasterio.open(terrain / 'flow-direction-d8-1km.tif') as ds:
        pointer = ds.read(1)
        d8_transform = ds.transform
        d8_crs = ds.crs
    if str(d8_crs).upper() != 'EPSG:4326':
        raise ValueError('当前D8资料需为EPSG:4326')
    with rasterio.open(terrain / 'flow-accumulation-1km.tif') as ds:
        accumulation = ds.read(1)
        if ds.transform != d8_transform or accumulation.shape != pointer.shape:
            raise ValueError('流向与累积资料的空间定义不一致')
    mask = rasterize([(mapping(transform(to_wgs, basins['zajia'])), 1)], out_shape=pointer.shape, transform=d8_transform).astype(bool)
    rr, cc = rasterio.transform.rowcol(d8_transform, *DUJA)
    candidates = []
    for r in range(max(0, rr - 4), min(pointer.shape[0], rr + 5)):
        for c in range(max(0, cc - 4), min(pointer.shape[1], cc + 5)):
            if mask[r, c] and pointer[r, c] and accumulation[r, c] >= 100:
                distance = math.hypot(r - rr, c - cc)
                candidates.append((distance, -float(accumulation[r, c]), r, c))
    if not candidates:
        raise ValueError('杜加控制断面无法在登记河网附近定位，禁止以全流域替代')
    _, _, outlet_r, outlet_c = min(candidates)
    directions = {1: (-1, 0), 2: (-1, 1), 4: (0, 1), 8: (1, 1), 16: (1, 0), 32: (1, -1), 64: (0, -1), 128: (-1, -1)}
    upstream = np.zeros(pointer.shape, dtype=np.uint8)
    queue = deque([(outlet_r, outlet_c)])
    upstream[outlet_r, outlet_c] = 1
    while queue:
        r, c = queue.popleft()
        for dr, dc in directions.values():
            nr, nc = r + dr, c + dc
            if not (0 <= nr < pointer.shape[0] and 0 <= nc < pointer.shape[1]) or upstream[nr, nc] or not mask[nr, nc]:
                continue
            delta = directions.get(int(pointer[nr, nc]))
            if delta and (nr + delta[0], nc + delta[1]) == (r, c):
                upstream[nr, nc] = 1
                queue.append((nr, nc))
    pieces = [transform(to_area, shape(g)) for g, value in shapes(upstream, mask=upstream.astype(bool), transform=d8_transform) if value]
    duja = polygons(unary_union(pieces).intersection(basins['zajia']))
    if duja.area < basins['zajia'].area * 0.05:
        raise ValueError('杜加提取控制区过小，须复核断面与D8连接')
    partitions = {'duja': duja, 'zajia_rest': polygons(basins['zajia'].difference(duja)),
                  **{k: v for k, v in basins.items() if k != 'zajia'}}

    if progress:
        progress(30, '建立高程带、冰川比例和气象面积权重')
    dem_path = terrain / 'dem-1km-raw.tif'
    with rasterio.open(dem_path) as ds:
        dem = ds.read(1).astype(float)
        dem_transform, dem_bounds = ds.transform, ds.bounds
        missing = ~np.isfinite(dem) | (dem == ds.nodata) | (dem < 1000)
    if missing.all():
        raise ValueError('DEM没有有效高程')
    # Raster source fills voids only for elevation-band assignment, never water
    # balance area. The nearest-value method and affected area are recorded.
    nearest = distance_transform_edt(missing, return_distances=False, return_indices=True)
    filled = dem[tuple(nearest)]
    domain_pixels = rasterize([(mapping(transform(to_wgs, domain)), 1)], out_shape=dem.shape, transform=dem_transform).astype(bool)
    void_parts = [transform(to_area, shape(g)) for g, value in shapes(missing.astype(np.uint8), mask=missing & domain_pixels, transform=dem_transform) if value]
    void_area_km2 = unary_union(void_parts).intersection(domain).area / 1e6 if void_parts else 0.
    bands = (np.floor(filled / 500) * 500).astype(np.int32)
    band_polygons = defaultdict_geometry = {}
    for geo, elevation in shapes(bands, transform=dem_transform):
        band_polygons.setdefault(int(elevation), []).append(transform(to_area, shape(geo)))
    band_polygons = {height: polygons(unary_union(geoms).intersection(domain)) for height, geoms in band_polygons.items()}
    band_union = unary_union(list(band_polygons.values()))
    outside_dem = domain.difference(band_union)
    if outside_dem.area > 1:
        raise ValueError(f'DEM未覆盖控制域：{outside_dem.area / 1e6:.3f} km²，需补齐地形资料')

    units, unit_geometries = [], []
    for partition, geom in partitions.items():
        for elevation, band in sorted(band_polygons.items()):
            region = polygons(geom.intersection(band))
            if region.is_empty:
                continue
            for kind, part in [('glacier', region.intersection(ice)), ('land', region.difference(ice))]:
                part = polygons(part)
                if part.is_empty or part.area < 1:
                    continue
                identifier = f'{partition}-{elevation}-{kind}'
                units.append({'id': identifier, 'partition': partition, 'basin': 'zajia' if partition in ('duja', 'zajia_rest') else partition,
                              'kind': kind, 'elevationBandMin': elevation, 'elevationBandMax': elevation + 500,
                              'elevation': elevation + 250, 'areaKm2': part.area / 1e6})
                unit_geometries.append(part)

    west, south, east, north = transform(to_wgs, domain).bounds
    grids, grid_geoms = [], []
    for lati in range(math.floor(south * 10), math.ceil(north * 10) + 1):
        for loni in range(math.floor(west * 10), math.ceil(east * 10) + 1):
            lon, lat = loni / 10, lati / 10
            geom = transform(to_area, box(lon - 0.05, lat - 0.05, lon + 0.05, lat + 0.05))
            if geom.intersection(domain).area <= 1:
                continue
            cell_wgs = box(lon - 0.05, lat - 0.05, lon + 0.05, lat + 0.05)
            pixels = rasterize([(mapping(cell_wgs), 1)], out_shape=dem.shape, transform=dem_transform).astype(bool)
            elevation = float(filled[pixels].mean()) if pixels.any() else 4700.0
            grids.append({'id': f'{lon:.1f},{lat:.1f}', 'lon': lon, 'lat': lat, 'referenceElevation': elevation})
            grid_geoms.append(geom)
    weights = np.array([[g.intersection(u).area / u.area for g in grid_geoms] for u in unit_geometries])
    lake_weights = np.array([g.intersection(lake).area / lake.area for g in grid_geoms])
    for i, row in enumerate(weights):
        if abs(float(row.sum()) - 1) > 1e-5:
            raise ValueError(f'单元气象权重不闭合：{units[i]["id"]}')
        row /= row.sum()
    lake_weights /= lake_weights.sum()
    unit_total = sum(u['areaKm2'] for u in units)
    if abs(unit_total + lake.area / 1e6 - domain.area / 1e6) > 1e-4:
        raise ValueError('模型单元、湖面和控制域面积不闭合')

    def feature(geo, properties):
        return {'type': 'Feature', 'properties': properties, 'geometry': mapping(transform(to_wgs, geo))}

    fc = {'type': 'FeatureCollection', 'features': [feature(domain, {'kind': 'domain', 'name': '色林错总控制域'}),
          feature(lake, {'kind': 'lake', 'name': '色林错', 'areaKm2': lake.area / 1e6}),
          *[feature(g, {'kind': 'basin', 'id': k, 'name': NAMES[k], 'areaKm2': g.area / 1e6}) for k, g in basins.items()],
          feature(duja, {'kind': 'control', 'id': 'duja', 'name': '杜加控制区', 'areaKm2': duja.area / 1e6}),
          *[feature(g, {'kind': 'glacier', 'id': identifier}) for identifier, g in glaciers]]}
    write_json(target / 'map.geojson', fc)
    # Display-only simplification. Exact geometries and area weights above are
    # retained; small glacier polygons survive preserve_topology=True.
    display = {'type': 'FeatureCollection', 'features': []}
    display['features'] = [{**f, 'geometry': mapping(shape(f['geometry']).simplify(.001, preserve_topology=True))} for f in fc['features']]
    write_json(target / 'display.geojson', display)
    write_json(target / 'units.geojson', {'type': 'FeatureCollection', 'features': [feature(g, u) for g, u in zip(unit_geometries, units)]})
    write_json(target / 'grid.geojson', {'type': 'FeatureCollection', 'features': [feature(g.intersection(domain), info) for g, info in zip(grid_geoms, grids)]})
    snapped = rasterio.transform.xy(d8_transform, outlet_r, outlet_c)
    manifest = {'schemaVersion': 1, 'createdAt': now(), 'projectId': 'selinco', 'forcingResolution': '0.1deg',
                'areaCrs': 'EPSG:6933', 'sourcePolicy': 'union of supplied full basin, named basins and lake; lake priority; named basins clipped mutually',
                'domainAreaKm2': domain.area / 1e6, 'lakeAreaKm2': lake.area / 1e6,
                'originalFullBasinAreaKm2': full.area / 1e6, 'domainAddedAreaKm2': domain.difference(full).area / 1e6,
                'basins': [{'id': k, 'name': NAMES[k], 'areaKm2': g.area / 1e6, 'glacierAreaKm2': ice.intersection(g).area / 1e6} for k, g in basins.items()],
                'glacierCount': len(glaciers), 'glacierAreaKm2': ice.area / 1e6,
                'duja': {'stationCoordinate': list(DUJA), 'snappedCoordinate': list(snapped), 'areaKm2': duja.area / 1e6,
                         'pointerCodes': '1=N,2=NE,4=E,8=SE,16=S,32=SW,64=W,128=NW', 'upstreamCells': int(upstream.sum())},
                'units': units, 'grids': grids, 'weights': weights.tolist(), 'lakeWeights': lake_weights.tolist(),
                'elevationBandMeters': 500, 'elevationVoidFill': 'nearest valid source DEM pixel; elevation bands only',
                'elevationVoidFilledPixelsInDomain': int((missing & domain_pixels).sum()),
                'elevationVoidFilledAreaKm2': void_area_km2,
                'unitTemperatureElevation': '500 m band midpoint',
                'temperatureElevationReference': 'source DEM cell average; replace with registered ERA5-Land orography when available',
                'sourceFiles': inputs, 'repairs': repairs, 'removedOverlapKm2': overlap,
                'areaClosureErrorKm2': unit_total + lake.area / 1e6 - domain.area / 1e6,
                'bounds': [west, south, east, north]}
    manifest['version'] = 'SPATIAL-' + digest({k: v for k, v in manifest.items() if k not in ('createdAt',)})[:20]
    write_json(target / 'manifest.json', manifest)
    import shutil
    archive = target / 'versions' / manifest['version']
    archive.mkdir(parents=True, exist_ok=True)
    for name in ('manifest.json', 'map.geojson', 'units.geojson', 'grid.geojson', 'display.geojson'):
        shutil.copy2(target / name, archive / name)
    if progress:
        progress(100, f'空间产品就绪：{len(units)}个计算单元，{len(glaciers)}条冰川')
    return manifest
