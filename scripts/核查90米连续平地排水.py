"""Resolve linked equal-height flats by descending downstream dependencies.

Strict downhill pointers and basin boundaries remain fixed. Each flat only
receives an outcome when all accessible lower/equal exits have the same known
outcome. No surface is filled and unknown boundaries are never made outlets.
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
from pyproj import Transformer
from rasterio.features import rasterize, shapes
from shapely.geometry import shape
from shapely.ops import transform, unary_union

from scripts.核查90米地形排水 import SOURCE, DEM, DESTINATION, downhill, sha


@njit
def label_plateaus(z, unresolved):
    height, width = z.shape
    n = np.count_nonzero(unresolved)
    labels = np.full(z.shape, -1, np.int32)
    members = np.empty(n, np.int64)
    starts = np.empty(n, np.int64)
    levels = np.empty(n, z.dtype)
    dy = np.array([-1, -1, 0, 1, 1, 1, 0, -1])
    dx = np.array([0, 1, 1, 1, 0, -1, -1, -1])
    size, count = 0, 0
    for sy in range(height):
        for sx in range(width):
            if not unresolved[sy, sx] or labels[sy, sx] >= 0:
                continue
            starts[count], levels[count] = size, z[sy, sx]
            labels[sy, sx] = count
            members[size] = sy * width + sx
            size += 1
            scan = starts[count]
            while scan < size:
                position = members[scan]
                y, x = position // width, position % width
                for k in range(8):
                    yy, xx = y + dy[k], x + dx[k]
                    if (yy >= 0 and yy < height and xx >= 0 and xx < width and
                            unresolved[yy, xx] and labels[yy, xx] < 0 and z[yy, xx] == z[y, x]):
                        labels[yy, xx] = count
                        members[size] = yy * width + xx
                        size += 1
                scan += 1
            count += 1
    return labels, members, starts[:count], levels[:count]


@njit
def resolve_plateaus(z, valid, domain, unresolved, targets, pointer, labels, members, starts, order):
    height, width = z.shape
    outcome = np.zeros(z.shape, dtype=np.uint8)
    dy = np.array([-1, -1, 0, 1, 1, 1, 0, -1])
    dx = np.array([0, 1, 1, 1, 0, -1, -1, -1])
    errors = 0
    for item in range(len(order)):
        component = order[item]
        start = starts[component]
        end = starts[component + 1] if component + 1 < len(starts) else len(members)
        has_stop = False
        for i in range(start, end):
            position = members[i]
            y, x = position // width, position % width
            code = pointer[y, x]
            if code == 0:
                has_stop = True
                continue
            if code > 8:
                outcome[y, x] = 8
                continue
            yy, xx = y + dy[code - 1], x + dx[code - 1]
            if yy < 0 or yy >= height or xx < 0 or xx >= width or not valid[yy, xx] or not domain[yy, xx]:
                outcome[y, x] = 8
            elif targets[yy, xx] > 0:
                outcome[y, x] = targets[yy, xx]
            elif unresolved[yy, xx] and z[yy, xx] < z[y, x] and outcome[yy, xx] > 0:
                outcome[y, x] = outcome[yy, xx]
            else:
                # Valid direct pointers must reach a previously processed,
                # lower cell or a known target; no guessed fallback.
                outcome[y, x] = 8
                errors += 1
        if not has_stop:
            continue
        possible, unknown = 0, False
        for i in range(start, end):
            position = members[i]
            y, x = position // width, position % width
            if pointer[y, x] > 8:
                unknown = True
            for k in range(8):
                yy, xx = y + dy[k], x + dx[k]
                if yy < 0 or yy >= height or xx < 0 or xx >= width or not valid[yy, xx] or not domain[yy, xx]:
                    unknown = True
                    continue
                if z[yy, xx] > z[y, x]:
                    continue
                if targets[yy, xx] > 0:
                    possible |= 1 << int(targets[yy, xx])
                elif unresolved[yy, xx]:
                    if z[yy, xx] == z[y, x]:
                        if labels[yy, xx] != component:
                            unknown = True
                            errors += 1
                    elif 1 <= outcome[yy, xx] <= 6:
                        possible |= 1 << int(outcome[yy, xx])
                    else:
                        unknown = True
                else:
                    unknown = True
        if unknown:
            resolved = 8
        elif possible == 0:
            resolved = 7
        elif possible & (possible - 1):
            resolved = 9
        else:
            resolved = 0
            for destination in range(1, 7):
                if possible == (1 << destination):
                    resolved = destination
        for i in range(start, end):
            position = members[i]
            y, x = position // width, position % width
            if pointer[y, x] == 0:
                outcome[y, x] = resolved
    return outcome, errors


def classify(z, valid, domain, unresolved, targets, pointer):
    labels, members, starts, levels = label_plateaus(z, unresolved)
    order = np.argsort(levels, kind='stable')
    outcome, errors = resolve_plateaus(z, valid, domain, unresolved, targets, pointer,
                                       labels, members, starts, order)
    if errors:
        raise ValueError('高程依赖排序或已知边界不一致：' + str(errors))
    return outcome, len(starts)


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
    def mask(g):
        return rasterize([(g, 1)], out_shape=z.shape, transform=affine).astype(bool)
    domain, unresolved = mask(geometries['domain']), mask(geometries['unresolved'])
    if np.any(unresolved & ~valid):
        raise ValueError('残余区的高分辨率DEM缺值')
    targets = np.zeros(z.shape, dtype=np.uint8)
    for code, key in ((1, 'zajia'), (2, 'zagen'), (3, 'boqu'), (4, 'ali'), (5, 'fringe'), (5, 'lake')):
        targets[mask(geometries[key])] = code
    targets[mask(geometries['duja']) & (targets == 1)] = 6
    if np.any(unresolved & (targets != 0)):
        raise ValueError('残余区与已知分区栅格中心重叠')
    pointer = downhill(z, valid, domain, unresolved, abs(affine.a), abs(affine.e), affine.f, abs(affine.e))
    outcome, component_count = classify(z, valid, domain, unresolved, targets, pointer)
    trace_end = time.perf_counter()
    selected = np.where((outcome >= 1) & (outcome <= 6), outcome, 0).astype(np.uint8)
    with rasterio.open(DESTINATION / '90米残余排水候选-含严格下坡.tif') as ds:
        previous = ds.read(1)
    compare = {'sameTargetCells': int(((previous == selected) & (previous != 0)).sum()),
               'additionalResolvedCells': int(((previous == 0) & (selected != 0)).sum()),
               'noLongerResolvedCells': int(((previous != 0) & (selected == 0)).sum()),
               'changedTargetCells': int(((previous != 0) & (selected != 0) & (previous != selected)).sum())}
    if compare['changedTargetCells']:
        raise ValueError('连续平地方法对已经分类的目标产生冲突，停止生成候选')
    forward = Transformer.from_crs(4326, 6933, always_xy=True).transform
    remaining = transform(forward, geometries['unresolved'])
    codes = {1: 'zajia_rest', 2: 'zagen', 3: 'boqu', 4: 'ali', 5: 'fringe', 6: 'duja'}
    areas, counts = {}, {}
    for code, name in codes.items():
        polys = [transform(forward, shape(g)) for g, value in
                 shapes(selected, mask=selected == code, transform=affine) if value == code]
        areas[name] = unary_union(polys).intersection(remaining).area / 1e6 if polys else 0.
        counts[name] = int((selected == code).sum())
    raster_path = DESTINATION / '90米残余排水候选-连续平地.tif'
    if raster_path.exists():
        raise FileExistsError('候选文件已存在，禁止覆盖')
    with rasterio.open(raster_path, 'w', **profile) as ds:
        ds.write(selected, 1)
    status_path = DESTINATION / '90米残余排水状态-连续平地.tif'
    if status_path.exists():
        raise FileExistsError('状态文件已存在，禁止覆盖')
    with rasterio.open(status_path, 'w', **profile) as ds:
        ds.write(outcome, 1)
    report = {'sourceSpatialVersion': 'GRID-SPATIAL-ddbb782dd4bdd3f00fa5',
              'demFilesSHA256': {p.name: sha(p) for p in sorted(DEM.iterdir()) if p.is_file()},
              'candidateRasterSHA256': sha(raster_path), 'statusRasterSHA256': sha(status_path),
              'codes': codes, 'unknownCodes': {7: 'flat or low with no known exit',
                                               8: 'downstream unknown, boundary or tied steepest slope',
                                               9: 'different resolved plateau destinations'},
              'method': 'strict downhill D8; equal-height connected components, resolve lower first; all accessible exits same target and no unknown; no DEM filling',
              'components': component_count, 'outcomeCellCounts': {str(k): int(v) for k, v in zip(*np.unique(outcome[unresolved], return_counts=True))},
              'comparisonWithOnePass': compare, 'candidateAreaKm2': areas, 'candidateCellCounts': counts,
              'seconds': {'trace': trace_end - started, 'total': time.perf_counter() - started},
              'limitations': ['DEM source and vertical datum/units not supplied, relative ordering only.',
                              'Existing boundaries and coarse derived rivers do not independently prove downstream connectivity.',
                              'True vs false sinks, lakes, spills and boundary exits require independent additional data.']}
    out = DESTINATION / '90米残余排水核查-连续平地.json'
    with out.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    main()
