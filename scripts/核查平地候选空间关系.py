"""Check candidate routes against existing basin, lake, control and stream data."""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.features import rasterize, shapes
from shapely import make_valid
from shapely.geometry import shape
from shapely.ops import transform, unary_union

from scripts.核查色林错平地出口 import ROOT, VERSION, classify


def classified_candidates():
    """Return the original data and candidate mask, without changing any input."""
    version = ROOT / 'data/derived/spatial/versions' / VERSION
    old_map = json.loads((version / 'map.geojson').read_text(encoding='utf-8'))['features']
    geometry = {f['properties'].get('id') or f['properties']['kind']: shape(f['geometry'])
                for f in old_map if f['properties']['kind'] in ('domain', 'lake', 'basin', 'control')}
    to_area = Transformer.from_crs(4326, 6933, always_xy=True).transform
    area_map = {k: make_valid(transform(to_area, g)) for k, g in geometry.items()}

    with rasterio.open(ROOT / 'data/raw/spatial/terrain-reference/dem-1km-raw.tif') as dem, \
            rasterio.open(version / 'drainage-d8-raw.tif') as d8, \
            rasterio.open(version / 'residual-destination.tif') as residual_ds:
        assert dem.transform == d8.transform == residual_ds.transform
        assert dem.crs == d8.crs == residual_ds.crs
        z = dem.read(1).astype(float)
        pointer, residual, affine = d8.read(1), residual_ds.read(1), dem.transform
        valid = np.isfinite(z) & (z != dem.nodata) & (z > 1000)

    def mask(g):
        return rasterize([(g, 1)], out_shape=z.shape, transform=affine).astype(bool)

    domain = mask(geometry['domain'])
    targets = np.zeros(z.shape, dtype=np.uint8)
    for code, key in enumerate(('zajia', 'zagen', 'boqu', 'ali', 'lake'), 1):
        targets[mask(geometry[key])] = code
    # Preserve the original independent parameter scope whenever the first
    # downstream, already established basin pixel is in the control area.
    targets[mask(geometry['duja']) & (targets == 1)] = 6
    candidates, counts = classify(z, valid, domain, targets, pointer, residual == 7)
    return old_map, geometry, area_map, candidates, counts, affine, residual


def main():
    old_map, geometry, area_map, candidates, counts, affine, residual = classified_candidates()
    to_area = Transformer.from_crs(4326, 6933, always_xy=True).transform

    streams = gpd.read_file(ROOT / 'data/raw/spatial/stream-package/merged/selinco_4basin_streams_thr50cells.shp').to_crs(6933)
    stream_geom = unary_union(streams.geometry)
    raw_basins = {}
    source = ROOT / 'data/raw/spatial/source-package/subbasin_shp'
    for key, filename in (('zajia', '扎加藏布边界.shp'), ('zagen', '扎根藏布边界.shp'),
                          ('boqu', '波曲藏布边界.shp'), ('ali', '阿里藏布边界.shp')):
        raw = gpd.read_file(source / filename).to_crs(6933)
        raw_basins[key] = unary_union([make_valid(g) for g in raw.geometry])

    to_wgs = Transformer.from_crs(6933, 4326, always_xy=True).transform
    regions, totals = [], {}
    for code, label in ((1, 'zajia_rest'), (2, 'zagen'), (3, 'boqu'),
                        (4, 'ali'), (5, 'fringe'), (6, 'duja')):
        polygons = [transform(to_area, shape(g)) for g, value in
                    shapes(candidates, mask=candidates == code, transform=affine) if value == code]
        clipped = unary_union(polygons).intersection(area_map['unresolved']) if polygons else None
        totals[label] = clipped.area / 1e6 if clipped is not None else 0.0
        if clipped is None or clipped.is_empty:
            continue
        for part in getattr(clipped, 'geoms', [clipped]):
            if part.geom_type != 'Polygon' or part.area < 10_000:
                continue
            anchor = transform(to_wgs, part.representative_point())
            regions.append({'target': label, 'areaKm2': part.area / 1e6,
                            'anchor': [anchor.x, anchor.y],
                            'distanceToLakeKm': part.distance(area_map['lake']) / 1000,
                            'distanceToKnownDujaKm': part.distance(area_map['duja']) / 1000,
                            'distanceToDerivedStreamKm': part.distance(stream_geom) / 1000,
                            'rawBasinIntersectionsKm2': {key: part.intersection(g).area / 1e6
                                                        for key, g in raw_basins.items()}})
    regions.sort(key=lambda item: -item['areaKm2'])
    report = {'parentSpatialVersion': VERSION, 'statusCounts': counts, 'areaKm2': totals,
              'piecesOver001Km2': regions,
              'controlNote': '杜加目标仅指首个已知下游像元处于原杜加控制面；原控制面本身来自已有粗D8，不能作为现场核测。',
              'streamNote': '50格阈值河网由既有子流域粗DEM派生，非独立实测河网。',
              'limit': '距离、相交和单一低出口是当前分辨率下的条件证据，不证明真实湖间连通或真实洼地去向。'}
    out = ROOT / 'build/第二轮精修-20261028/空间/平地候选与既有地理资料关系.json'
    with out.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
