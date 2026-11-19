"""Lake-connected inundation scenarios, spill estimates and exposure comparison."""
from copy import deepcopy
import base64
import io
import json
import math
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd

from .common import ROOT, now, read_json, write_json, within
from . import operational_store as store


COLLECTION = 'risk-scenarios'
RESULTS = 'risk-results'
METHOD = 'lake-connected-static-inundation-and-weir-v1'


def _number(value, label, lo=None, hi=None):
    if isinstance(value, bool):
        raise ValueError(label+'须为有效数值')
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(label+'须为有效数值') from None
    if not math.isfinite(result) or lo is not None and result < lo or hi is not None and result > hi:
        raise ValueError(label+'超出有效范围')
    return result


def _dem_paths():
    packaged = ROOT/'data/derived/spatial/湖周漫溢地形.tif'
    if packaged.is_file():
        return [packaged]
    roots = [ROOT/'data/raw/spatial/公开连续地形-CopernicusGLO90-20261028']
    return [p for root in roots if root.is_dir() for p in root.glob('*/*_DEM.tif')]


DISPLAY = ROOT/'data/derived/spatial/漫溢展示/overflow-display.json'


def display():
    try:
        return read_json(DISPLAY)
    except (OSError, ValueError):
        return None


def bange_rise(volume_km3, meta=None):
    """Rise of Bange Co for a received volume, from the DEM hypsometry (no own inflow or loss)."""
    meta = meta or display()
    table = (meta or {}).get('bange', {}).get('hypsometry') or []
    if not table:
        return None
    return float(np.interp(volume_km3, [r['volumeKm3'] for r in table], [r['rise'] for r in table]))


def default_scenario(db):
    """Baseline from the adopted lake model and the DEM saddle of the 2023 breach."""
    from .lake_revision import active
    model, meta = active(db), display() or {}
    outlet = (model or {}).get('outlet') or {}
    spot = meta.get('outlet') or {}
    crest = float(outlet.get('currentCrestM', 4543.96)); width = float(outlet.get('currentWidthM', 60.))
    base = {'crestM': round(crest, 2), 'widthM': round(width, 1), 'coefficient': float(outlet.get('coefficient', .577)), 'releaseCapacityM3s': 0.}
    return {'name': '现状溃口', 'demOffsetM': round(-float(meta.get('datum', {}).get('lakeMinusDemM', -2.41)), 3),
            'datumSource': meta.get('datum', {}).get('basis', '2023-09-21 起溢观测湖位减去 GLO-90 鞍部高程'),
            'marginKm': 5., 'initialLevelOffsetM': 0.,
            'outlet': {'lon': spot.get('lon', 89.36958), 'lat': spot.get('lat', 31.77958), **base},
            'alternatives': [
                {'name': '开挖泄流槽（降槛0.3 m、加宽至120 m）', **base, 'crestM': round(crest - .3, 2), 'widthM': 120.},
                {'name': '人工泄流 50 m³/s', **base, 'releaseCapacityM3s': 50.},
                {'name': '封堵抬槛（抬高0.3 m）', **base, 'crestM': round(crest + .3, 2)}],
            'exposureIds': exposure_ids(db)}


EXPOSURE_BUNDLE = ROOT / 'data/derived/spatial/承灾体'


def ensure_bundled_exposures(db):
    """Register the OSM roads and settlements shipped with the installation, once per workspace."""
    index = read_json(EXPOSURE_BUNDLE / '承灾体资料.json') if (EXPOSURE_BUNDLE / '承灾体资料.json').is_file() else None
    if not index:
        return []
    existing = store.list_records(db, 'exposures')
    have = {r.get('bundleFile') for r in existing}
    added = []
    for item in index.get('adopted', []):
        if item['file'] in have:
            continue
        value = read_json(EXPOSURE_BUNDLE / item['file'])
        if not value or not value.get('features'):
            continue
        record = import_exposure(db, {'name': item['name'], 'kind': item['kind'], 'geojson': value,
                                      'source': f"{index['source']} / {index['license']} / 下载 {item['downloadedAtUTC']}"})
        added.append(store.save(db, 'exposures', {**record, 'bundleFile': item['file'], 'builtin': True}, identifier=record['id']))
    return added


def exposure_ids(db):
    ensure_bundled_exposures(db)
    return [r['id'] for r in store.list_records(db, 'exposures')]


def catalog(db):
    ensure_bundled_exposures(db)
    return {'scenarios': store.list_records(db, COLLECTION),
            'results': store.list_records(db, RESULTS),
            'exposures': store.list_records(db, 'exposures'),
            'terrainAvailable': bool(_dem_paths()), 'method': METHOD}


def save_scenario(db, payload, identifier=None):
    old = store.get(db, COLLECTION, identifier) if identifier else {}
    value = {**old, **deepcopy(payload)}
    value['name'] = str(value.get('name') or '湖周漫溢情景').strip()
    value['demOffsetM'] = _number(value.get('demOffsetM', 0), '湖位到DEM的高程偏移', -1000, 1000)
    value['marginKm'] = _number(value.get('marginKm', 5), '湖周范围', .2, 50)
    value['initialLevelOffsetM'] = _number(value.get('initialLevelOffsetM', 0), '起报湖位抬升', -2, 3)
    outlet = value.get('outlet') or {}
    value['outlet'] = {'lon': _number(outlet.get('lon'), '溢口经度', -180, 180),
        'lat': _number(outlet.get('lat'), '溢口纬度', -90, 90),
        'crestM': _number(outlet.get('crestM'), '溢口高程', 4000, 6500),
        'widthM': _number(outlet.get('widthM'), '溢口宽度', .01, 10000),
        'coefficient': _number(outlet.get('coefficient', .6), '流量系数', .01, 1),
        'releaseCapacityM3s': _number(outlet.get('releaseCapacityM3s', 0), '人工泄流能力', 0, 1e7)}
    value['mode'] = 'scenario'
    value['datumSource'] = str(value.get('datumSource') or '演示情景高程偏移，未经实测核定')
    value['method'] = METHOD
    value['exposureIds'] = list(dict.fromkeys(value.get('exposureIds') or []))
    alternatives = []
    for item in value.get('alternatives') or []:
        candidate = {**value['outlet'], **item}
        alternatives.append({'name': str(item.get('name') or '调整方案'),
            'crestM': _number(candidate['crestM'], '方案溢口高程', 4000, 6500),
            'widthM': _number(candidate['widthM'], '方案溢口宽度', .01, 10000),
            'coefficient': _number(candidate['coefficient'], '方案流量系数', .01, 1),
            'releaseCapacityM3s': _number(candidate.get('releaseCapacityM3s', 0), '方案人工泄流能力', 0, 1e7)})
    value['alternatives'] = alternatives
    return store.save(db, COLLECTION, value, identifier=identifier, prefix='RISK-')


def detail(db, identifier):
    return store.get(db, COLLECTION, identifier)


def result_detail(db, identifier):
    if identifier.startswith('DEMO-RISK-RUN-'):
        from . import demo_examples
        return demo_examples.risk_result(identifier)
    return store.get(db, RESULTS, identifier)


def weir_discharge(level_m, outlet):
    head = max(0., float(level_m)-float(outlet['crestM']))
    return (2./3.)*float(outlet['coefficient'])*float(outlet['widthM'])*math.sqrt(2.*9.80665)*head**1.5


def simulate_spill(times, inflow, rain, evap, initial_level, curve, outlet, cancelled=None):
    from .lake_curves import storage, level, area
    v = storage(initial_level, curve)
    h = float(initial_level)
    offset = curve.get('offsetM', 0.)
    minimum, maximum = curve['minLevel']-offset, curve['maxLevel']-offset
    crest = float(outlet['crestM'])
    release_floor = storage(max(crest, minimum), curve) if crest <= maximum else None
    rows = []
    for index, time in enumerate(times):
        if cancelled and cancelled():
            raise InterruptedError('已停止漫溢情景计算')
        a = area(h, curve)
        incoming = float(inflow[index])*3600.
        precipitation = float(rain[index])/1000.*a
        evaporation = float(evap[index])/1000.*a
        before = v
        available = (max(0., v + incoming + precipitation - evaporation - release_floor)
                     if release_floor is not None else 0.)
        spill = min(weir_discharge(h, outlet)*3600., available)
        released = min(float(outlet.get('releaseCapacityM3s', 0))*3600., available-spill)
        total_outflow = spill+released
        v += incoming + precipitation - evaporation - total_outflow
        h = level(v, curve)
        rows.append({'time': str(time), 'level': h, 'inflowM3s': float(inflow[index]),
            'spillM3s': spill/3600., 'spillM3': spill, 'inflowM3': incoming,
            'releasedM3s': released/3600., 'releasedM3': released,
            'totalOutflowM3s': total_outflow/3600., 'totalOutflowM3': total_outflow,
            'precipitationM3': precipitation, 'evaporationM3': evaporation,
            'storageChangeM3': v-before,
            'balanceErrorM3': (v-before)-(incoming+precipitation-evaporation-total_outflow)})
    return rows


def connected_depth(elevation, valid, lake_mask, level_m):
    """Eight-neighbour connectivity; existing lake cells seed the wet domain."""
    from scipy.ndimage import binary_propagation
    elevation = np.asarray(elevation, dtype=float)
    valid = np.asarray(valid, dtype=bool)
    lake_mask = np.asarray(lake_mask, dtype=bool)
    wettable = valid & ((elevation <= level_m) | lake_mask)
    seeds = lake_mask & valid
    if not seeds.any():
        raise ValueError('当前地形范围没有有效湖区种子')
    connected = binary_propagation(seeds, structure=np.ones((3, 3)), mask=wettable)
    # Satellite DEM over existing water is not lake bathymetry.
    expansion = connected & ~lake_mask
    depth = np.where(expansion, np.maximum(0., level_m-elevation), 0.)
    return depth, connected


def _lake_geometry():
    from shapely.geometry import shape
    from shapely.ops import unary_union
    value = read_json(ROOT/'data/derived/spatial/map.geojson')
    geometries = [shape(f['geometry']) for f in value['features'] if f['properties'].get('kind') == 'lake']
    if not geometries:
        raise ValueError('缺少已登记湖泊边界')
    return unary_union(geometries)


def load_terrain(scenario, max_size=180):
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.features import rasterize
    from rasterio.merge import merge
    from rasterio.transform import array_bounds
    from shapely.geometry import box, mapping
    lake = _lake_geometry()
    west, south, east, north = lake.bounds
    margin = scenario['marginKm']/111.195
    bounds = (west-margin/math.cos(math.radians((south+north)/2)), south-margin,
              east+margin/math.cos(math.radians((south+north)/2)), north+margin)
    datasets = []
    try:
        for path in _dem_paths():
            ds = rasterio.open(path)
            if ds.crs.to_epsg() == 4326 and box(*ds.bounds).intersects(box(*bounds)):
                datasets.append(ds)
            else:
                ds.close()
        if not datasets:
            raise ValueError('湖周范围缺少可读取的Copernicus地形，请补充安装资料')
        if len(datasets) == 1 and datasets[0].name.endswith('湖周漫溢地形.tif'):
            covered = datasets[0].bounds
            if bounds[0] < covered.left or bounds[1] < covered.bottom or bounds[2] > covered.right or bounds[3] > covered.top:
                raise ValueError('所选湖周范围超出安装包地形覆盖，请将扩展距离设为20公里以内或补充地形')
        dx, dy = (bounds[2]-bounds[0])/max_size, (bounds[3]-bounds[1])/max_size
        data, transform = merge(datasets, bounds=bounds, res=(dx, dy), nodata=-9999.,
                                dtype='float32', resampling=Resampling.average)
        elevation = data[0].astype(float)
        valid = np.isfinite(elevation) & (elevation != -9999.) & (elevation > 1000)
        lake_mask = rasterize([(mapping(lake), 1)], out_shape=elevation.shape,
                              transform=transform, all_touched=True).astype(bool)
        lake_mask &= valid
        if not lake_mask.any():
            raise ValueError('所选地形未覆盖湖泊有效边界')
        actual_bounds = list(array_bounds(*elevation.shape, transform))
        latitudes = transform.f+(np.arange(elevation.shape[0])+.5)*transform.e
        cell_areas = (abs(transform.a)*111195.*np.cos(np.radians(latitudes)) *
                      abs(transform.e)*111195.)[:, None]*np.ones((1, elevation.shape[1]))
        return {'elevation': elevation, 'valid': valid, 'lakeMask': lake_mask,
                'transform': transform, 'bounds': actual_bounds, 'cellAreas': cell_areas,
                'source': 'Copernicus GLO-90，湖周面积平均展示网格',
                'verticalReference': 'EGM2008 orthometric metres',
                'sampling': 'area-average; existing lake depth unavailable'}
    finally:
        for ds in datasets:
            ds.close()


def _forecast_source(db, identifier):
    from .forecasts import detail as forecast_detail
    value = forecast_detail(db, identifier)
    result = value.get('result', value)
    if result.get('status') in ('failed', 'running', 'queued'):
        raise ValueError('漫溢情景需要已完成的预测结果')
    return result


def _series_source(result):
    times = result.get('times') or result.get('time')
    control = result.get('control') or {}
    if isinstance(control, list):
        times = [r['time'] for r in control]
        control = {k: [r.get(k) for r in control] for k in control[0] if k != 'time'}
    inflow = control.get('inflowM3s') or control.get('inflow') or control.get('total')
    rain = control.get('lakeRainMm')
    evap = control.get('lakeEvaporationMm')
    initial = result.get('initialLakeLevel')
    curve = result.get('lakeCurve') or result.get('curve')
    if not times or inflow is None or rain is None or evap is None or initial is None or not curve:
        raise ValueError('预测缺少逐时来水、湖面降水蒸发、初始湖位或曲线配置')
    n = len(times)
    if any(len(v) != n or not np.isfinite(np.asarray(v, dtype=float)).all() for v in (inflow, rain, evap)):
        raise ValueError('预测逐时水量数据不完整')
    return times, inflow, rain, evap, float(initial), curve


# Road surface = ground + embankment; closed when water over the surface exceeds CLOSE_DEPTH_M.
# Same rule as the front end (overflow-data.mjs ROAD_RULE). Embankment height is a default, not surveyed.
ROAD_EMBANKMENT_M = 1.0
ROAD_CLOSE_DEPTH_M = 0.3


def _flood_geometry(depth, terrain, threshold=0.):
    from rasterio.features import shapes
    from shapely.geometry import shape
    from shapely.ops import unary_union
    mask = np.asarray(depth) > threshold
    if not mask.any():
        from shapely.geometry import GeometryCollection
        return GeometryCollection()
    geometries = [shape(g) for g, value in shapes(mask.astype('uint8'), mask=mask,
                    transform=terrain['transform']) if value]
    return unary_union(geometries)


def assess_exposures(db, exposure_ids, depth, terrain):
    from shapely.geometry import shape
    from shapely.ops import transform
    from pyproj import Transformer
    flood = _flood_geometry(depth, terrain)
    wet_zone = _flood_geometry(depth, terrain, ROAD_EMBANKMENT_M)
    closed_zone = _flood_geometry(depth, terrain, ROAD_EMBANKMENT_M + ROAD_CLOSE_DEPTH_M)
    project = Transformer.from_crs(4326, 6933, always_xy=True).transform
    rows = []
    for identifier in exposure_ids:
        record = store.get(db, 'exposures', identifier)
        source = read_json(within(db.workspace, record['path']))
        for index, f in enumerate(source['features']):
            geometry = shape(f['geometry'])
            line = geometry.geom_type in ('LineString', 'MultiLineString')
            zone = wet_zone if line else flood
            if geometry.is_empty or zone.is_empty or not geometry.intersects(zone):
                continue
            intersection = geometry.intersection(zone)
            closed = geometry.intersection(closed_zone) if line and not closed_zone.is_empty else None
            values = []
            for coordinate in ([geometry.coords[0]] if geometry.geom_type == 'Point' else [intersection.representative_point().coords[0]]):
                col, row = ~terrain['transform'] * coordinate
                r, c = int(row), int(col)
                if 0 <= r < depth.shape[0] and 0 <= c < depth.shape[1]:
                    values.append(float(depth[r, c]))
            if geometry.geom_type != 'Point':
                from rasterio.features import rasterize
                mask = rasterize([(f['geometry'], 1)], out_shape=depth.shape,
                                 transform=terrain['transform'], all_touched=True).astype(bool)
                if mask.any():
                    values.append(float(np.max(depth[mask])))
            props = f.get('properties') or {}
            row = {'exposureId': identifier, 'featureId': str(f.get('id', index)),
                'name': str(props.get('name') or props.get('名称') or '未命名对象'),
                'kind': record.get('kind', 'facility'), 'geometryType': geometry.geom_type,
                'maxDepthM': max(values, default=0.), 'affectedLengthM': 0.,
                'source': record.get('source', record['name'])}
            if line:
                wet_m = float(transform(project, intersection).length)
                closed_m = float(transform(project, closed).length) if closed is not None and not closed.is_empty else 0.
                row.update({'affectedLengthM': closed_m, 'overtoppedLengthM': max(0., wet_m - closed_m),
                            'maxDepthOverSurfaceM': max(0., row['maxDepthM'] - ROAD_EMBANKMENT_M),
                            'roadState': 'closed' if closed_m > 0 else 'overtopped'})
            rows.append(row)
    return {'objects': rows, 'count': len(rows),
            'roadLengthM': sum(r['affectedLengthM'] for r in rows),
            'overtoppedRoadLengthM': sum(r.get('overtoppedLengthM', 0.) for r in rows),
            'settlementCount': sum(r['kind'] == 'settlement' for r in rows),
            'roadRule': {'embankmentM': ROAD_EMBANKMENT_M, 'closeDepthM': ROAD_CLOSE_DEPTH_M}}


def run(db, request, folder=None, progress=None, cancelled=None):
    scenario = detail(db, request['scenarioId'])
    source = _forecast_source(db, request['forecastId'])
    times, inflow, rain, evap, initial, curve = _series_source(source)
    current = initial
    initial += float(scenario.get('initialLevelOffsetM', 0.))
    terrain = load_terrain(scenario, max_size=420)
    # New inundation is measured against today's shoreline (forecast start level), not the DEM-era lake.
    _, baseline = connected_depth(terrain['elevation'], terrain['valid'], terrain['lakeMask'], current+scenario['demOffsetM'])
    meta = display()

    def flooded(level):
        depth, wet = connected_depth(terrain['elevation'], terrain['valid'], terrain['lakeMask'], level+scenario['demOffsetM'])
        return np.where(wet & ~baseline, depth, 0.), wet
    if progress:
        progress(15, '已读取湖周地形与预测水量')
    folder = Path(folder) if folder else db.workspace/'risk-runs'/('情景-'+pd.Timestamp.utcnow().strftime('%Y%m%d%H%M%S%f'))
    folder.mkdir(parents=True, exist_ok=True)
    paths, comparisons, primary_rows, picks = [], [], None, []
    for k, (name, outlet) in enumerate([(scenario['name'], scenario['outlet'])] +
                [(a['name'], {**scenario['outlet'], **a}) for a in scenario.get('alternatives', [])]):
        rows = simulate_spill(times, inflow, rain, evap, initial, curve, outlet, cancelled)
        peak = max(r['level'] for r in rows)
        depth, wet = flooded(peak)
        frame = pd.DataFrame(rows); frame.index = pd.to_datetime(frame['time']) - pd.Timedelta(hours=1)
        daily = frame[['level', 'totalOutflowM3s']].resample('D').mean()
        volume = float(frame['totalOutflowM3'].sum())
        effects = assess_exposures(db, scenario.get('exposureIds', []), depth, terrain)
        comparisons.append({'name': name, 'peakLevelM': peak,
            'firstSpillTime': next((r['time'] for r in rows if r['spillM3'] > 0), None),
            'spillVolumeM3': sum(r['spillM3'] for r in rows),
            'peakSpillM3s': max(r['spillM3s'] for r in rows),
            'releaseVolumeM3': sum(r['releasedM3'] for r in rows),
            'peakReleaseM3s': max(r['releasedM3s'] for r in rows),
            'totalOutflowM3': sum(r['totalOutflowM3'] for r in rows),
            'peakTotalOutflowM3s': max(r['totalOutflowM3s'] for r in rows),
            'inundatedAreaKm2': float(terrain['cellAreas'][depth > 0].sum()/1e6),
            'initialLevelM': initial, 'finalLevelM': rows[-1]['level'],
            'peakTime': max(rows, key=lambda r: r['level'])['time'],
            'bangeRiseM': bange_rise(volume/1e9, meta),
            'series': {'dates': [d.date().isoformat() for d in daily.index], 'level': daily['level'].round(4).tolist(),
                       'outflow': daily['totalOutflowM3s'].round(3).tolist()},
            'maxDepthM': float(depth.max()), 'effects': effects, 'outlet': outlet,
            'balanceMaxErrorM3': max(abs(r['balanceErrorM3']) for r in rows)})
        if k == 0:
            primary_rows = rows
            # Daily frames plus the peak hour keep the archive small at ~200 m resolution.
            picks = sorted(set(range(23, len(rows), 24)) | {len(rows) - 1, int(np.argmax([r['level'] for r in rows]))})
            depths = np.stack([flooded(rows[i]['level'])[0].astype('float32') for i in picks])
            np.savez_compressed(folder/'inundation.npz', elevation=terrain['elevation'].astype('float32'),
                valid=terrain['valid'], lakeMask=terrain['lakeMask'], depths=depths, frameRows=np.asarray(picks),
                bounds=terrain['bounds'], transform=np.array(tuple(terrain['transform'])))
        if progress:
            progress(20+65*(k+1)/(1+len(scenario.get('alternatives', []))), '已完成'+name)
    value = {'name': scenario['name'], 'scenarioId': scenario['id'], 'forecastId': request['forecastId'],
        'status': 'succeeded', 'mode': 'scenario', 'method': METHOD, 'createdAt': now(),
        'scenario': scenario, 'lakeCurve': curve, 'rows': primary_rows, 'comparisons': comparisons,
        'terrain': {'bounds': terrain['bounds'], 'width': terrain['elevation'].shape[1],
                    'height': terrain['elevation'].shape[0], 'source': terrain['source'],
                    'verticalReference': terrain['verticalReference'], 'lakeBedDepthAvailable': False},
        'path': str((folder/'inundation.npz').relative_to(db.workspace)).replace('\\','/'),
        'frameRows': picks, 'initialLevelM': initial, 'currentLevelM': current, 'initialLevelOffsetM': scenario.get('initialLevelOffsetM', 0.),
        'limitations': ['静态地形连通淹没情景；未计算下游洪波传播与到达时间',
                        '已有湖面无水下地形，不输出湖内水深', '溢口和高程偏移采用本次情景配置']}
    value = store.save(db, RESULTS, value, prefix='RISK-RUN-')
    pd.DataFrame(primary_rows).to_csv(folder/'risk-hourly.csv', index=False, encoding='utf-8-sig')
    write_json(folder/'result.json', value)
    if progress:
        progress(100, '漫溢情景与方案比选完成')
    return value


def result_exposures(db, identifier):
    """Return the road and point data selected for this saved run."""
    result = result_detail(db, identifier)
    if identifier.startswith('DEMO-RISK-RUN-'):
        from . import demo_examples
        selected = set(result['scenario']['exposureIds'])
        return {'items': [
            {'id': record['id'], 'source': record['source'],
             'features': read_json(within(EXPOSURE_BUNDLE, record['bundleFile']))['features']}
            for record in demo_examples.index()['exposures'] if record['id'] in selected
        ]}
    items = []
    for exposure_id in result['scenario'].get('exposureIds', []):
        record = store.get(db, 'exposures', exposure_id)
        geojson = read_json(within(db.workspace, record['path']))
        items.append({'id': exposure_id, 'source': record['source'], 'features': geojson['features']})
    return {'items': items}


def terrain_frame(db, identifier, index=0, depth_only=False):
    value = result_detail(db, identifier)
    if identifier.startswith('DEMO-RISK-RUN-'):
        from .demo_examples import DEMO_ROOT
        directory = DEMO_ROOT
    else:
        directory = db.workspace
    index = int(index)
    with np.load(within(directory, value['path']), allow_pickle=False) as arrays:
        picks = arrays['frameRows'].tolist() if 'frameRows' in arrays else list(range(len(value['rows'])))
        if not 0 <= index < len(picks):
            raise ValueError('回放时刻超出结果范围')
        row = value['rows'][picks[index]]
        terrain = dict(value['terrain'])
        if not depth_only:
            valid = arrays['valid']
            elevations = np.where(valid, arrays['elevation'], 0.)
            terrain.update(elevations=elevations.tolist(), valid=valid.tolist(), lakeMask=arrays['lakeMask'].tolist())
        depth = arrays['depths'][index]
        west, south, east, north = terrain['bounds']
        height, width = depth.shape
        latitudes = north-(np.arange(height)+.5)*(north-south)/height
        cell_area_km2 = (east-west)/width*(north-south)/height*111.195**2*np.cos(np.radians(latitudes))
        return {'terrain': terrain, 'depths': (depth if depth_only else np.round(depth, 3)).tolist(), 'index': index, 'frames': len(picks),
                'inundatedAreaKm2': float((np.count_nonzero(depth > 0, axis=1)*cell_area_km2).sum()),
                'time': row['time'], 'level': row['level'],
                'outlet': value['scenario']['outlet'], 'demOffsetM': value['scenario']['demOffsetM']}


def import_exposure(db, payload):
    from shapely.geometry import shape, mapping
    value = payload.get('geojson')
    if value is None:
        filename = str(payload.get('filename') or '')
        raw = base64.b64decode(payload.get('dataBase64') or '', validate=True)
        if filename.lower().endswith(('.json', '.geojson')):
            value = json.loads(raw.decode('utf-8-sig'))
        elif filename.lower().endswith('.zip'):
            import geopandas as gpd
            if not zipfile.is_zipfile(io.BytesIO(raw)):
                raise ValueError('Shapefile压缩包无效')
            frame = gpd.read_file(io.BytesIO(raw))
            if frame.crs is None:
                raise ValueError('Shapefile缺少坐标系，请提供完整配套文件')
            value = json.loads(frame.to_crs(4326).to_json())
        else:
            raise ValueError('请提供GeoJSON或包含完整配套文件的Shapefile ZIP')
    if not isinstance(value, dict) or value.get('type') != 'FeatureCollection' or not value.get('features'):
        raise ValueError('承灾体资料须为非空GeoJSON要素集')
    for feature in value['features']:
        geometry = shape(feature.get('geometry'))
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError('承灾体几何为空或无效')
        if not (-180 <= geometry.bounds[0] <= geometry.bounds[2] <= 180 and -90 <= geometry.bounds[1] <= geometry.bounds[3] <= 90):
            raise ValueError('GeoJSON须使用WGS84经纬度')
    record = store.save(db, 'exposures', {'name': str(payload.get('name') or '承灾体资料'),
        'kind': payload.get('kind', 'facility'), 'source': str(payload.get('source') or payload.get('filename') or '用户导入'),
        'featureCount': len(value['features']), 'coordinateSystem': 'EPSG:4326'}, prefix='EXPOSURE-')
    path = db.workspace/'risk-exposures'/(record['id']+'.geojson')
    write_json(path, value)
    record['path'] = str(path.relative_to(db.workspace)).replace('\\','/')
    return store.save(db, 'exposures', record, identifier=record['id'])


def fetch_osm(db, request, folder=None, progress=None, cancelled=None):
    import requests
    bounds = request.get('bbox') or read_json(ROOT/'data/derived/spatial/presentation.json')['lakeBounds']
    if len(bounds) != 4:
        raise ValueError('公开地图查询需要west,south,east,north范围')
    west, south, east, north = map(float, bounds)
    bbox = ','.join(map(str, [south, west, north, east]))
    kinds = request.get('features', ['roads', 'settlements'])
    # Ways need full geometry (out geom); settlements need one point per feature (out center).
    queries = []
    if 'roads' in kinds:
        queries.append('way[highway]('+bbox+');out geom;')
    if 'settlements' in kinds:
        queries.append('nwr[place~"city|town|village|hamlet|isolated_dwelling|locality"]('+bbox+');out center;')
    if not queries:
        raise ValueError('请选择道路或居民点')
    elements = []
    for query in queries:
        if cancelled and cancelled():
            raise InterruptedError('已停止公开地图读取')
        response = requests.post('https://overpass-api.de/api/interpreter',
            data={'data': '[out:json][timeout:90];'+query}, timeout=(10, 120))
        response.raise_for_status()
        elements.extend(response.json().get('elements', []))
    features = {'road': [], 'settlement': []}
    for element in elements:
        tags = element.get('tags') or {}
        kind = 'road' if tags.get('highway') else 'settlement'
        if kind == 'road':
            coordinates = [[p['lon'], p['lat']] for p in element.get('geometry', [])]
            if len(coordinates) < 2:
                continue
            geometry = {'type': 'LineString', 'coordinates': coordinates}
        else:
            point = element.get('center', element)
            if 'lon' not in point:
                continue
            geometry = {'type': 'Point', 'coordinates': [point['lon'], point['lat']]}
        features[kind].append({'type': 'Feature', 'id': str(element['id']), 'geometry': geometry,
                              'properties': {'name': tags.get('name', ''), **tags}})
    records = [import_exposure(db, {'name': 'OSM道路' if kind == 'road' else 'OSM居民点', 'kind': kind,
        'source': 'OpenStreetMap contributors / ODbL / '+now(),
        'geojson': {'type': 'FeatureCollection', 'features': rows}})
        for kind, rows in features.items() if rows]
    return {'exposures': records, 'source': 'OpenStreetMap contributors / ODbL',
            'featureCount': sum(r['featureCount'] for r in records)}


def attachment(db, payload):
    scenario = detail(db, payload['scenarioId'])
    name = Path(str(payload.get('filename') or '应急预案.pdf')).name
    raw = base64.b64decode(payload.get('dataBase64') or '', validate=True)
    if not raw:
        raise ValueError('预案附件为空')
    folder = db.workspace/'risk-plans'/scenario['id']
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/(pd.Timestamp.utcnow().strftime('%Y%m%d%H%M%S%f')+'-'+name)
    path.write_bytes(raw)
    scenario.setdefault('attachments', []).append({'name': name, 'bytes': len(raw),
        'path': str(path.relative_to(db.workspace)).replace('\\','/'), 'createdAt': now()})
    return store.save(db, COLLECTION, scenario, identifier=scenario['id'])
