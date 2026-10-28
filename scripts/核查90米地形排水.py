"""Conservative drainage tracing of the existing unresolved area on new DEM.

The DEM, geometries, current spatial pointer, and all existing results stay
read-only. Ties and masked-neighbour exits are deliberately left unknown.
"""
import hashlib
import json
import math
import os
import time
from pathlib import Path

os.environ['GDAL_PAM_ENABLED'] = 'NO'
import numpy as np
import rasterio
from numba import njit
from rasterio.features import rasterize, shapes
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform, unary_union

from scripts.核查色林错平地出口 import classify

ROOT = Path(__file__).resolve().parents[1]
SOURCE_VERSION = 'GRID-SPATIAL-ddbb782dd4bdd3f00fa5'
SOURCE = ROOT / 'data/derived/spatial/versions' / SOURCE_VERSION
DEM = ROOT / 'slc_dem/slc_dem'
DESTINATION = ROOT / 'build/第二轮精修-20261028/空间'


@njit
def downhill(z, valid, domain, unresolved, pixel_lon, pixel_lat, north, lat_step):
    height, width = z.shape
    pointer = np.zeros(z.shape, np.uint8)
    dy = np.array([-1, -1, 0, 1, 1, 1, 0, -1])
    dx = np.array([0, 1, 1, 1, 0, -1, -1, -1])
    for y in range(height):
        latitude = north - (y + .5) * lat_step
        east_west = pixel_lon * 111195. * math.cos(latitude * math.pi / 180.)
        north_south = pixel_lat * 111195.
        for x in range(width):
            if not unresolved[y, x] or not valid[y, x]:
                continue
            best, selected, tied, boundary = 0., 0, False, False
            for index in range(8):
                yy, xx = y + dy[index], x + dx[index]
                if yy < 0 or yy >= height or xx < 0 or xx >= width or not valid[yy, xx] or not domain[yy, xx]:
                    # The absent/outside height could hide an exit.
                    boundary = True
                    continue
                difference = int(z[y, x]) - int(z[yy, xx])
                if difference <= 0:
                    continue
                distance = math.sqrt((dy[index] * north_south) ** 2 + (dx[index] * east_west) ** 2)
                slope = difference / distance
                if slope > best + 1e-12:
                    best, selected, tied = slope, index + 1, False
                elif abs(slope - best) <= 1e-12:
                    tied = True
            if boundary:
                pointer[y, x] = 10
            elif tied:
                pointer[y, x] = 9
            else:
                pointer[y, x] = selected
    return pointer


@njit
def direct_targets(pointer, valid, domain, unresolved, targets):
    """Trace strict downhill paths and memoize their first known target."""
    height, width = pointer.shape
    cached = np.zeros(pointer.shape, dtype=np.uint8)
    path = np.empty(np.count_nonzero(unresolved) + 1, dtype=np.int64)
    dy = np.array([-1, -1, 0, 1, 1, 1, 0, -1])
    dx = np.array([0, 1, 1, 1, 0, -1, -1, -1])
    for sy in range(height):
        for sx in range(width):
            if not unresolved[sy, sx] or cached[sy, sx]:
                continue
            y, x, count = sy, sx, 0
            while True:
                if y < 0 or y >= height or x < 0 or x >= width or not valid[y, x] or not domain[y, x]:
                    outcome = 8
                    break
                if targets[y, x] > 0:
                    outcome = targets[y, x]
                    break
                if cached[y, x] > 0:
                    outcome = cached[y, x]
                    break
                if count >= len(path):
                    outcome = 8
                    break
                path[count] = y * width + x
                count += 1
                code = pointer[y, x]
                if code == 0:
                    outcome = 7
                    break
                if code > 8:
                    outcome = 8
                    break
                y, x = y + dy[code - 1], x + dx[code - 1]
            for index in range(count):
                value = path[index]
                cached[value // width, value % width] = outcome
    return cached


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    started = time.perf_counter()
    features = json.loads((SOURCE / 'map.geojson').read_text(encoding='utf-8'))['features']
    geometries = {f['properties'].get('id') or f['properties']['kind']: shape(f['geometry'])
                  for f in features if f['properties']['kind'] in ('basin', 'lake', 'domain', 'control')}
    with rasterio.open(DEM) as ds:
        z = ds.read(1)
        valid = (ds.read_masks(1) != 0) & np.isfinite(z) & (z != ds.nodata)
        affine = ds.transform
        profile = {'driver': 'GTiff', 'width': ds.width, 'height': ds.height, 'count': 1,
                   'dtype': 'uint8', 'crs': ds.crs, 'transform': affine, 'nodata': 0,
                   'compress': 'deflate', 'tiled': True}
    def mask(geom):
        return rasterize([(geom, 1)], out_shape=z.shape, transform=affine).astype(bool)
    domain, unresolved = mask(geometries['domain']), mask(geometries['unresolved'])
    if np.any(unresolved & ~valid):
        raise ValueError('未判定区域的高分辨率DEM缺值，禁止自动补值')
    targets = np.zeros(z.shape, dtype=np.uint8)
    for code, key in ((1, 'zajia'), (2, 'zagen'), (3, 'boqu'), (4, 'ali'),
                      (5, 'fringe'), (5, 'lake')):
        targets[mask(geometries[key])] = code
    targets[mask(geometries['duja']) & (targets == 1)] = 6
    if np.any(unresolved & (targets != 0)):
        raise ValueError('残余区与已知分区栅格中心重叠')
    pointer = downhill(z, valid, domain, unresolved, abs(affine.a), abs(affine.e), affine.f, abs(affine.e))
    pointer_ready = time.perf_counter()
    selected, status = classify(z, valid, domain, targets, pointer, unresolved)
    direct = direct_targets(pointer, valid, domain, unresolved, targets)
    direct_mask = unresolved & (direct >= 1) & (direct <= 6)
    if int(direct_mask.sum()) != status.get('target', 0):
        raise ValueError('独立严格路径追踪与平地分类器的直接目标计数不一致')
    if np.any(direct_mask & (selected > 0)):
        raise ValueError('直接目标和平台目标重复计入')
    selected[direct_mask] = direct[direct_mask]
    traced = time.perf_counter()
    project = Transformer.from_crs(4326, 6933, always_xy=True).transform
    remaining = transform(project, geometries['unresolved'])
    codes = {1: 'zajia_rest', 2: 'zagen', 3: 'boqu', 4: 'ali', 5: 'fringe', 6: 'duja'}
    areas, counts, geometries_count = {}, {}, {}
    for code, name in codes.items():
        polys = [transform(project, shape(geometry)) for geometry, value in
                 shapes(selected, mask=selected == code, transform=affine) if value == code]
        counts[name] = int((selected == code).sum())
        geometries_count[name] = len(polys)
        areas[name] = unary_union(polys).intersection(remaining).area / 1e6 if polys else 0.
    geo_done = time.perf_counter()
    raster_path = DESTINATION / '90米残余排水候选-含严格下坡.tif'
    if raster_path.exists():
        raise FileExistsError('候选文件已存在，禁止覆盖')
    with rasterio.open(raster_path, 'w', **profile) as dst:
        dst.write(selected, 1)
    report = {'demFilesSHA256': {p.name: sha(p) for p in sorted(DEM.iterdir()) if p.is_file()},
              'sourceSpatialVersion': SOURCE_VERSION, 'candidateRasterSHA256': sha(raster_path),
              'method': 'original unresolved cells; steepest strictly lower D8 at 90m; equal plateau with exactly one reachable target and no unknown exit; reject equal-slope ties and nodata/domain-edge neighbours; no filling',
              'targetCodes': codes,
              'targetMeaning': 'fringe target includes valid pixels of original lake and previously established lake-draining fringe; duja target is existing control geometry',
              'statusCellCounts': status,
              'directTargetCellCount': int(direct_mask.sum()),
              'uniquePlateauCellCount': int(status.get('unique', 0)),
              'pointerCounts': {str(k): int(v) for k, v in zip(*np.unique(pointer[unresolved], return_counts=True))},
              'candidateCellCounts': counts, 'candidateAreaKm2': areas, 'candidatePolygonCounts': geometries_count,
              'seconds': {'pointer': pointer_ready - started, 'trace': traced - pointer_ready,
                          'geometry': geo_done - traced, 'total': time.perf_counter() - started},
              'limitations': ['DEM source product and vertical units/datum not supplied; only relative ordering is used.',
                              'DEM masked at model boundary; missing or outside neighbours remain unknown.',
                              'Reaching a previously classified basin is an existing-boundary constraint, not an independently surveyed drainage proof.',
                              'Areas still ending at sinks, ambiguous outlets or missing boundary are not forced into the lake.']}
    out = DESTINATION / '90米残余排水核查-含严格下坡.json'
    with out.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    main()
