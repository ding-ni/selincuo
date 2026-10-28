"""Find unambiguous flat outlets without filling depressions or changing data."""
from __future__ import annotations

import argparse
import json
from collections import Counter, deque
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.features import rasterize, shapes
from shapely.geometry import shape
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'GRID-SPATIAL-ddbb782dd4bdd3f00fa5'
DIRECTIONS = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]


def classify(z, valid, domain, target, pointer, unresolved):
    """Classify only flats with a unique reachable target and no unknown exit.

    No lower path is invented. A plateau is traversed through equal-elevation
    pixels, but ambiguity, unresolved downstream sinks and domain edges win over
    an apparent outlet. Target: 1..4 four rivers, 5 direct lake.
    """
    height, width = z.shape
    downhill_cache, flat_cache = {}, {}

    def strict(start):
        route, seen, cell = [], set(), start
        while True:
            y, x = cell
            if not (0 <= y < height and 0 <= x < width):
                result = ('edge', None)
                break
            if not valid[y, x]:
                result = ('nodata', cell)
                break
            if not domain[y, x]:
                result = ('edge', cell)
                break
            if target[y, x]:
                result = ('target', int(target[y, x]))
                break
            if cell in downhill_cache:
                result = downhill_cache[cell]
                break
            if cell in seen:
                result = ('loop', cell)
                break
            seen.add(cell)
            route.append(cell)
            code = int(pointer[y, x])
            if not code:
                result = ('stop', cell)
                break
            if code < 1 or code > 8:
                result = ('pointer', cell)
                break
            dy, dx = DIRECTIONS[code - 1]
            cell = y + dy, x + dx
        for cell in route:
            downhill_cache[cell] = result
        return result

    def plateau(start):
        if start in flat_cache:
            return flat_cache[start]
        y, x = start
        value = z[y, x]
        queue, group, exits = deque([start]), {start}, set()
        while queue:
            cy, cx = queue.popleft()
            for dy, dx in DIRECTIONS:
                ny, nx = cy + dy, cx + dx
                if not (0 <= ny < height and 0 <= nx < width):
                    exits.add(('edge', None))
                    continue
                if not valid[ny, nx]:
                    exits.add(('nodata', None))
                    continue
                delta = z[ny, nx] - value
                if delta > 1e-9:
                    continue
                if not domain[ny, nx]:
                    exits.add(('edge', None))
                    continue
                if target[ny, nx]:
                    exits.add(('target', int(target[ny, nx])))
                elif abs(delta) <= 1e-9:
                    if (ny, nx) not in group:
                        group.add((ny, nx))
                        queue.append((ny, nx))
                else:
                    state, value2 = strict((ny, nx))
                    exits.add((state, value2 if state == 'target' else None))
        target_ids = {value for state, value in exits if state == 'target'}
        unknown = {state for state, _ in exits if state != 'target'}
        if len(target_ids) == 1 and not unknown:
            result = ('unique', next(iter(target_ids)))
        elif len(target_ids) > 1:
            result = ('multiple_targets', 0)
        elif unknown:
            result = ('uncertain_exit', 0)
        else:
            result = ('no_exit', 0)
        for cell in group:
            flat_cache[cell] = result
        return result

    result = np.zeros(z.shape, dtype=np.uint8)
    statuses = Counter()
    for y, x in np.argwhere(unresolved):
        state, end = strict((int(y), int(x)))
        if state != 'stop':
            statuses[state] += 1
            continue
        value = z[end]
        ey, ex = end
        if not any(0 <= ey + dy < height and 0 <= ex + dx < width and
                   valid[ey + dy, ex + dx] and abs(z[ey + dy, ex + dx] - value) <= 1e-9
                   for dy, dx in DIRECTIONS):
            statuses['strict_local_low'] += 1
            continue
        outcome, destination = plateau(end)
        statuses[outcome] += 1
        if outcome == 'unique':
            result[y, x] = destination
    return result, dict(statuses)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    directory = ROOT / 'data/derived/spatial/versions' / VERSION
    features = json.loads((directory / 'map.geojson').read_text(encoding='utf-8'))['features']
    names = {f['properties'].get('id') or f['properties']['kind']: shape(f['geometry']) for f in features
             if f['properties']['kind'] in ('domain', 'lake', 'basin')}
    with rasterio.open(ROOT / 'data/raw/spatial/terrain-reference/dem-1km-raw.tif') as ds, \
            rasterio.open(directory / 'drainage-d8-raw.tif') as ps, \
            rasterio.open(directory / 'residual-destination.tif') as rs:
        z, pointer, residual, tr = ds.read(1).astype(float), ps.read(1), rs.read(1), ds.transform
        assert ds.transform == ps.transform == rs.transform and ds.crs == ps.crs == rs.crs
        valid = np.isfinite(z) & (z != ds.nodata) & (z > 1000)
    def mask(g):
        return rasterize([(g, 1)], out_shape=z.shape, transform=tr).astype(bool)
    domain = mask(names['domain'])
    target = np.zeros(z.shape, np.uint8)
    for code, key in enumerate(('zajia', 'zagen', 'boqu', 'ali', 'lake'), 1):
        target[mask(names[key])] = code
    output, stats = classify(z, valid, domain, target, pointer, residual == 7)
    area = Transformer.from_crs(4326, 6933, always_xy=True).transform
    remaining = transform(area, names['unresolved'])
    areas, components = {}, []
    for code, name in enumerate(('zajia', 'zagen', 'boqu', 'ali', 'fringe'), 1):
        polygons = [transform(area, shape(geom)) for geom, value in shapes(output, mask=output == code, transform=tr) if value == code]
        candidate = unary_union(polygons).intersection(remaining) if polygons else None
        areas[name] = candidate.area / 1e6 if candidate is not None else 0.0
        if candidate is not None and not candidate.is_empty:
            reproject = Transformer.from_crs(6933, 4326, always_xy=True).transform
            for part in getattr(candidate, 'geoms', [candidate]):
                if part.geom_type != 'Polygon' or part.area < 10_000:
                    continue
                point = transform(reproject, part.representative_point())
                components.append({'candidate': name, 'areaKm2': part.area / 1e6,
                                   'representativePoint': [point.x, point.y]})
    components.sort(key=lambda row: -row['areaKm2'])
    report = {'method': '8邻域等高平地不填洼出口检查；所有较低出口和域边界必须唯一指向同一已知目标',
              'statusCellCounts': stats, 'candidateAreasKm2': areas,
              'candidateCounts': {name: int((output == code).sum()) for code, name in enumerate(('zajia', 'zagen', 'boqu', 'ali', 'fringe'), 1)},
              'componentsOver001Km2': components,
              'interpretation': '候选以粗DEM及既有边界为依据，并非现场实测；需针对实际出口与既有湖泊连接证据人工核实后才能改变计算空间。'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
