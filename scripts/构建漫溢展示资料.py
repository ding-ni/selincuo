"""构建漫溢与应急页的离线展示资料。

输入：data/derived/spatial/湖周漫溢地形.tif（Copernicus GLO-90，EGM2008）、
本地 OSM 水体（色林错、班戈错及周边湖泊）、业务工作区中已采用的湖泊方案
（lake-models，见 backend/lake_revision.py）和正式成果逐日过程。

输出：data/derived/spatial/漫溢展示/
  overview.jpg          全湖地形晕渲底图（含湖泊）
  east-texture.jpg      东端三维区地形纹理
  east-heights.bin      东端高程 uint16（0.1 m，偏移 4000 m）
  east-domain.bin       东端可淹没区 uint8（1 色林错连通，2 班戈错连通）
  lakes.geojson         湖泊边界
  overflow-display.json 范围、基准偏移、溃口、流路、班戈错库容、2023 复现逐日帧

用法：python scripts/构建漫溢展示资料.py [业务工作区]
"""
from datetime import datetime, timezone
import heapq
import json
import math
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEM = ROOT / 'data/derived/spatial/湖周漫溢地形.tif'
OSM = ROOT / 'data/raw/spatial/独立水体-OSM-20261028/原始水体.json'
OUT = ROOT / 'data/derived/spatial/漫溢展示'
OVERVIEW = (88.45, 31.45, 89.75, 32.20)
EAST = (89.15, 31.55, 89.75, 31.95)
SILING, BANGE = 1138412, 19358475
REPLAY = ('2023-08-01', '2024-12-31')
HEIGHT_OFFSET, HEIGHT_SCALE = 4000., .1
RAMP = [(4450, '#aebf86'), (4560, '#c9cf9c'), (4700, '#dad2a3'), (4850, '#d2bd90'), (5000, '#bea383'),
        (5200, '#a68d77'), (5450, '#978a80'), (5700, '#d8d5d1'), (6100, '#f5f5f5')]


def window(dataset, bounds):
    from rasterio.windows import from_bounds
    w = from_bounds(*bounds, transform=dataset.transform).round_offsets().round_lengths()
    z = dataset.read(1, window=w).astype('float64')
    return z, dataset.window_transform(w)


def pixel_metres(transform, lat):
    return abs(transform.a) * 111320. * math.cos(math.radians(lat)), abs(transform.e) * 110950.


def hillshade(z, dx, dy, zf=1.6):
    gy, gx = np.gradient(z * zf, dy, dx)
    slope = np.pi / 2. - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    shade = np.zeros_like(z)
    for azimuth, weight in ((315, .5), (270, .2), (0, .18), (225, .12)):
        a, alt = math.radians(azimuth), math.radians(40)
        shade += weight * (np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(a - aspect))
    return np.clip(shade, 0, 1)


def ramp(z):
    stops = np.array([s for s, _ in RAMP], float)
    colours = np.array([[int(c[i:i + 2], 16) for i in (1, 3, 5)] for _, c in RAMP], float)
    return np.stack([np.interp(z, stops, colours[:, k]) for k in range(3)], axis=-1)


def relief(z, transform, lat, lakes_mask=None, water=(74, 144, 184), shore=(46, 104, 138)):
    dx, dy = pixel_metres(transform, lat)
    shade = hillshade(z, dx, dy)
    rgb = ramp(z) * (.38 + .78 * shade[..., None])
    if lakes_mask is not None and lakes_mask.any():
        from scipy.ndimage import binary_erosion
        inner = binary_erosion(lakes_mask, iterations=2)
        rgb[lakes_mask] = shore
        rgb[inner] = water
    return np.clip(rgb, 0, 255).astype('uint8')


def flat_water(z, min_cells):
    """GLO-90 edits lakes to a constant surface: label 8-connected flat cells."""
    from scipy import ndimage
    pad = np.pad(z, 1, mode='edge'); diff = np.zeros_like(z)
    for dr in (-1, 0, 1):
        for dk in (-1, 0, 1):
            if dr or dk:
                diff = np.maximum(diff, np.abs(pad[1 + dr:1 + dr + z.shape[0], 1 + dk:1 + dk + z.shape[1]] - z))
    flat = diff < .05
    labels, count = ndimage.label(flat, structure=np.ones((3, 3)))
    sizes = ndimage.sum(flat, labels, range(1, count + 1))
    keep = [i + 1 for i, s in enumerate(sizes) if s >= min_cells]
    return labels, keep


def osm_names():
    data = json.loads(OSM.read_text(encoding='utf-8'))
    rows = []
    for e in data['elements']:
        b, tags = e.get('bounds'), e.get('tags', {})
        name = tags.get('name:zh') or tags.get('name') or ''
        if b and name and not name.startswith(('ༀ',)):
            rows.append((e['id'], name, b))
    return rows


def lake_features(z, transform, min_km2=1.):
    """Lake polygons and masks from DEM flat water, named with OSM bounds."""
    from rasterio.features import shapes
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union
    lat = transform.f + transform.e * z.shape[0] / 2
    dx, dy = pixel_metres(transform, lat)
    labels, keep = flat_water(z, int(min_km2 * 1e6 / (dx * dy)))
    names = osm_names(); features, masks = [], {}
    for label in keep:
        mask = labels == label
        rows, cols = np.nonzero(mask)
        lon, lat_c = transform * (cols.mean() + .5, rows.mean() + .5)
        inside = [(i, n, b) for i, n, b in names if b['minlon'] <= lon <= b['maxlon'] and b['minlat'] <= lat_c <= b['maxlat']]
        osm_id, name = (None, '')
        if inside:
            x0, y0 = transform * (cols.min(), rows.max() + 1); x1, y1 = transform * (cols.max() + 1, rows.min())
            own = max(1e-9, (x1 - x0) * (y1 - y0))
            osm_id, name, _ = min(inside, key=lambda r: abs(math.log(max(1e-9, (r[2]['maxlon'] - r[2]['minlon']) * (r[2]['maxlat'] - r[2]['minlat'])) / own)))
        role = 'siling' if osm_id == SILING else 'bange' if osm_id == BANGE else 'lake'
        if role != 'lake':
            masks[role] = mask.copy() if role not in masks else (masks[role] | mask)
        geom = unary_union([shape(g) for g, v in shapes(mask.astype('uint8'), mask=mask, transform=transform) if v])
        km2 = float(mask.sum() * dx * dy / 1e6)
        features.append({'type': 'Feature', 'properties': {'osmId': osm_id, 'name': name if name.endswith('错') else ('色林错' if role == 'siling' else name),
                         'role': role, 'areaKm2': round(km2, 2), 'surfaceDemM': round(float(np.median(z[mask])), 2)},
                         'geometry': mapping(geom.simplify(abs(transform.a) * .8, preserve_topology=True))})
    return features, masks


def rasterize(features, shape, transform, role=None):
    from rasterio.features import rasterize as burn
    geoms = [f['geometry'] for f in features if role is None or f['properties']['role'] == role]
    if not geoms:
        return np.zeros(shape, bool)
    return burn([(g, 1) for g in geoms], out_shape=shape, transform=transform, all_touched=False).astype(bool)


def minimax_path(z, start, goal):
    """Lowest pass from `start` cells to `goal` cells (8-neighbour, cost = max elevation)."""
    h, w = z.shape
    cost = np.full(z.shape, np.inf); parent = np.full(z.size, -1, dtype=np.int64)
    heap = []
    for idx in np.flatnonzero(start.ravel()):
        cost.flat[idx] = z.flat[idx]; heapq.heappush(heap, (z.flat[idx], int(idx)))
    goal_flat = goal.ravel(); done = np.zeros(z.size, bool)
    steps = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    while heap:
        c, idx = heapq.heappop(heap)
        if done[idx]:
            continue
        done[idx] = True
        if goal_flat[idx]:
            path = [idx]
            while parent[path[-1]] >= 0:
                path.append(int(parent[path[-1]]))
            return c, path[::-1]
        r, k = divmod(idx, w)
        for dr, dk in steps:
            rr, kk = r + dr, k + dk
            if 0 <= rr < h and 0 <= kk < w:
                j = rr * w + kk
                if not done[j]:
                    nc = max(c, z.flat[j])
                    if nc < cost.flat[j]:
                        cost.flat[j] = nc; parent[j] = idx; heapq.heappush(heap, (nc, j))
    raise ValueError('东端地形中没有连通色林错与班戈错的路径')


def downhill_path(z, start, goal, dx, dy, ceiling):
    """Shortest channel from the saddle to the downstream lake, penalising climbs."""
    h, w = z.shape
    dist = np.full(z.size, np.inf); parent = np.full(z.size, -1, dtype=np.int64)
    dist[start] = 0.; heap = [(0., start)]; goal_flat = goal.ravel(); flat_z = z.ravel()
    steps = [(dr, dk, math.hypot(dr * dy, dk * dx)) for dr in (-1, 0, 1) for dk in (-1, 0, 1) if dr or dk]
    while heap:
        d, idx = heapq.heappop(heap)
        if d > dist[idx]:
            continue
        if goal_flat[idx]:
            path = [idx]
            while parent[path[-1]] >= 0:
                path.append(int(parent[path[-1]]))
            return path[::-1]
        r, k = divmod(idx, w)
        for dr, dk, length in steps:
            rr, kk = r + dr, k + dk
            if 0 <= rr < h and 0 <= kk < w:
                j = rr * w + kk
                if flat_z[j] > ceiling:
                    continue
                climb = max(0., flat_z[j] - flat_z[idx])
                nd = d + length * (1. + 20. * climb)
                if nd < dist[j]:
                    dist[j] = nd; parent[j] = idx; heapq.heappush(heap, (nd, j))
    raise ValueError('溃口下游没有通向班戈错的流路')


def cell_lonlat(transform, idx, width):
    r, k = divmod(idx, width)
    x, y = transform * (k + .5, r + .5)
    return float(x), float(y)


def hypsometry(z, mask, cell_area, surface, rises, block=None):
    from scipy.ndimage import binary_propagation
    rows = []
    for rise in rises:
        level = surface + rise
        wet = binary_propagation(mask, structure=np.ones((3, 3)), mask=(z <= level) | mask)
        if block is not None and (wet & block).any():
            break
        depth = np.where(wet, np.maximum(level - np.where(mask, surface, z), 0.), 0.)
        rows.append({'rise': round(float(rise), 2), 'areaKm2': float((wet * cell_area).sum() / 1e6),
                     'volumeKm3': float((depth * cell_area).sum() / 1e9)})
    return rows


def replay(db, bange_table, bange_area0):
    from backend import lake_revision
    from backend.published_results import result_file
    model = lake_revision.active(db)
    if model is None:
        raise SystemExit('业务工作区尚无已采用的湖泊方案，请先运行湖泊方案修订')
    daily = pd.DataFrame(model['daily']).set_index('date')
    source = pd.read_csv(result_file(model['sourceResultId'], 'daily.csv'), encoding='utf-8-sig').set_index('date')
    scale = model['lakeCoefficient'] / model['formalLakeCoefficient']
    rain = source['lakeRainMm']; evap = source['lakeEvaporationMm'] * scale
    inflow = source['total']
    onset = pd.Timestamp(model['onset']); outlet = model['outlet']
    vol = np.array([r['volumeKm3'] for r in bange_table]); area = np.array([r['areaKm2'] for r in bange_table])
    rise = np.array([r['rise'] for r in bange_table])
    stored, cumulative, frames = 0., 0., []
    for day in pd.date_range(*REPLAY, freq='D'):
        key = day.date().isoformat()
        row = daily.loc[key] if key in daily.index else None
        q = float(row['outflow']) if row is not None and pd.notna(row['outflow']) else 0.
        cumulative += q * 86400. / 1e9
        a_now = float(np.interp(stored, vol, area))
        stored = max(0., stored + q * 86400. / 1e9 + (float(rain.get(key, 0.)) - float(evap.get(key, 0.))) / 1000. * a_now / 1e3)
        hours = (day + pd.Timedelta(hours=12) - onset) / pd.Timedelta(hours=1)
        state = lake_revision.breach_state(hours, model['onsetLevelM'], outlet)
        frames.append({'d': key, 'H': None if row is None or pd.isna(row['level']) else round(float(row['level']), 4),
                       'Ho': None if row is None or pd.isna(row['observed']) else round(float(row['observed']), 3),
                       'Q': round(q, 2), 'V': round(cumulative, 4), 'I': None if pd.isna(inflow.get(key)) else round(float(inflow.get(key)), 1),
                       'crest': None if state is None else round(state[0], 3), 'width': None if state is None else round(state[1], 1),
                       'Hb': round(float(np.interp(stored, vol, rise)), 3), 'Ab': round(float(np.interp(stored, vol, area)), 2)})
    peak = max(frames, key=lambda f: f['Q'])
    def at(day):
        return next(f for f in frames if f['d'] == day)
    mid2024 = at('2024-06-30')
    checks = {'outflowTo20231231Km3': at('2023-12-31')['V'], 'outflowTo20240630Km3': mid2024['V'],
              'literatureOutflowKm3': .3, 'bangeRiseTo20240630M': mid2024['Hb'], 'literatureBangeRiseM': 2.3,
              'bangeAreaIncreasePercent20240630': round((mid2024['Ab'] / bange_area0 - 1) * 100, 1), 'literatureBangeAreaIncreasePercent': 18,
              'reportedBreachWidth': {'2023-09-23': 60, 'later': 200}, 'peakOutflowM3s': peak['Q'], 'peakDate': peak['d']}
    return {'start': REPLAY[0], 'end': REPLAY[1], 'onset': model['onset'], 'lakeModelId': model['id'],
            'onsetLevelM': model['onsetLevelM'], 'outlet': outlet, 'frames': frames, 'checks': checks,
            'fit': model['metrics'], 'literature': model['literature']}


def main():
    import rasterio
    from PIL import Image
    from backend.database import Database
    workspace = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / '运行程序/workspace-business'
    db = Database(workspace)
    OUT.mkdir(parents=True, exist_ok=True)
    with rasterio.open(DEM) as dataset:
        zo, to = window(dataset, OVERVIEW); ze, te = window(dataset, EAST)
    lat_o, lat_e = (OVERVIEW[1] + OVERVIEW[3]) / 2, (EAST[1] + EAST[3]) / 2
    lakes, _ = lake_features(zo, to)
    (OUT / 'lakes.geojson').write_text(json.dumps({'type': 'FeatureCollection', 'features': lakes}, ensure_ascii=False), encoding='utf-8')
    lake_o = rasterize(lakes, zo.shape, to)
    Image.fromarray(relief(zo, to, lat_o, lake_o)).save(OUT / 'overview.jpg', quality=88)
    _, east_masks = lake_features(ze, te)
    siling, bange = east_masks.get('siling'), east_masks.get('bange')
    if siling is None or bange is None:
        raise SystemExit('东端地形中未识别到色林错或班戈错水面')
    texture = relief(ze, te, lat_e, siling | bange, water=(45, 104, 138), shore=(58, 118, 148))
    Image.fromarray(texture).resize((texture.shape[1] * 2, texture.shape[0] * 2), Image.BICUBIC).save(OUT / 'east-texture.jpg', quality=90)
    heights = np.clip(np.round((ze - HEIGHT_OFFSET) / HEIGHT_SCALE), 0, 65535).astype('<u2')
    (OUT / 'east-heights.bin').write_bytes(heights.tobytes())

    pour, path = minimax_path(ze, siling, bange)
    w = ze.shape[1]
    land = [i for i in path if not siling.flat[i] and not bange.flat[i]]
    saddle = max(land, key=lambda i: ze.flat[i])
    s_lon, s_lat = cell_lonlat(te, saddle, w)
    dxe, dye = pixel_metres(te, lat_e)
    after = downhill_path(ze, saddle, bange, dxe, dye, float(ze.flat[saddle]) + .5)
    channel = [cell_lonlat(te, i, w) for i in after[::2]] + [cell_lonlat(te, after[-1], w)]
    before = [cell_lonlat(te, i, w) for i in path[:path.index(saddle) + 1] if not siling.flat[i]]
    from shapely.geometry import LineString
    line = LineString(before + channel) if len(before + channel) > 1 else None
    length = 0. if line is None else sum(math.hypot((b[0] - a[0]) * 111.32 * math.cos(math.radians(a[1])), (b[1] - a[1]) * 110.95)
                                         for a, b in zip(line.coords[:-1], line.coords[1:]))
    station = db.rows("SELECT lon,lat FROM stations WHERE id='SLC-E'")[0]
    distance = math.hypot((s_lon - station['lon']) * 111.32 * math.cos(math.radians(s_lat)), (s_lat - station['lat']) * 110.95)
    from backend import lake_revision
    model = lake_revision.active(db)
    offset = float(model['onsetLevelM']) - float(ze.flat[saddle])
    dx, dy = pixel_metres(te, lat_e); cell = np.full(ze.shape, dx * dy)
    bange_surface = float(np.median(ze[bange]))
    table = hypsometry(ze, bange, cell, bange_surface, np.arange(0, 8.01, .1), block=siling)
    from scipy.ndimage import binary_propagation
    domain = np.zeros(ze.shape, 'uint8')
    s_reach = binary_propagation(siling, structure=np.ones((3, 3)), mask=(ze <= model['onsetLevelM'] + 1.5 - offset) | siling)
    b_reach = binary_propagation(bange, structure=np.ones((3, 3)), mask=(ze <= bange_surface + table[-1]['rise']) | bange)
    domain[s_reach] = 1; domain[b_reach] = 2
    (OUT / 'east-domain.bin').write_bytes(domain.tobytes())
    series = replay(db, table, table[0]['areaKm2'])
    meta = {'schemaVersion': 1, 'builtAt': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'dem': {'source': 'Copernicus DEM GLO-90', 'verticalDatum': 'EGM2008', 'resolutionArcSec': 3},
            'overview': {'bounds': list(OVERVIEW), 'width': int(zo.shape[1]), 'height': int(zo.shape[0]), 'image': 'overview.jpg'},
            'east': {'bounds': list(EAST), 'width': int(ze.shape[1]), 'height': int(ze.shape[0]), 'heights': 'east-heights.bin',
                     'heightOffset': HEIGHT_OFFSET, 'heightScale': HEIGHT_SCALE, 'texture': 'east-texture.jpg', 'domain': 'east-domain.bin',
                     'pixelMetres': [round(dx, 2), round(dy, 2)], 'elevationRange': [float(ze.min()), float(ze.max())]},
            'datum': {'lakeMinusDemM': round(offset, 3), 'basis': '2023-09-21 起溢观测湖位减去 GLO-90 鞍部高程'},
            'outlet': {'lon': round(s_lon, 6), 'lat': round(s_lat, 6), 'demElevationM': round(float(ze.flat[saddle]), 2),
                       'onsetLevelM': model['onsetLevelM'], 'currentCrestM': model['outlet']['currentCrestM'],
                       'currentWidthM': model['outlet']['currentWidthM'], 'distanceToEastStationKm': round(distance, 2),
                       'channel': [[round(x, 6), round(y, 6)] for x, y in (line.coords if line is not None else [])],
                       'channelLengthKm': round(length, 2)},
            'bange': {'surfaceDemM': round(bange_surface, 2), 'baseAreaKm2': round(table[0]['areaKm2'], 2), 'hypsometry': table},
            'stations': {s['id']: [s['lon'], s['lat']] for s in db.rows("SELECT id,lon,lat FROM stations WHERE id LIKE 'SLC%'")},
            'lakes': 'lakes.geojson', 'replay': series}
    (OUT / 'overflow-display.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    print('鞍部', round(s_lon, 5), round(s_lat, 5), 'DEM', round(float(ze.flat[saddle]), 2), 'm，距东站', round(distance, 2), 'km')
    print('基准偏移（湖位−DEM）', round(offset, 2), 'm；流路', round(length, 1), 'km；班戈错 DEM 水面', round(bange_surface, 2), 'm')
    print('复现核对', json.dumps(series['checks'], ensure_ascii=False))
    print('输出', sorted((p.name, p.stat().st_size) for p in OUT.iterdir()))


if __name__ == '__main__':
    main()
