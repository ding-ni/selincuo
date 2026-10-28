"""Build a new fine-DEM drainage version without changing older versions.

Inputs are the old frozen spatial assets, the traceable 90m drainage raster,
and coarse flat-outlet evidence. Disagreeing fine/coarse outcomes stay unknown.
"""
import json
import os
from copy import deepcopy
from pathlib import Path

os.environ['GDAL_PAM_ENABLED'] = 'NO'
import rasterio
from pyproj import Transformer
from rasterio.features import shapes
from shapely.geometry import Polygon, mapping, shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

from backend.common import digest, read_json, sha_file
from backend.grid_spatial import directory, ensure_aggregate, load
from scripts.核查平地候选空间关系 import classified_candidates
from scripts.核查90米地形排水 import DEM, DESTINATION, SOURCE, SOURCE_VERSION
from scripts.构建平地出口空间 import data_bytes, polygons, write_new

ROOT = Path(__file__).resolve().parents[1]
CODES = {1: 'zajia_rest', 2: 'zagen', 3: 'boqu', 4: 'ali', 5: 'fringe', 6: 'duja'}
FORWARD = Transformer.from_crs(4326, 6933, always_xy=True).transform


def area(g):
    return transform(FORWARD, g).area


def group_raster(values, affine, boundary):
    groups = {}
    for code, name in CODES.items():
        pieces = [shape(g) for g, value in shapes(values, mask=values == code, transform=affine) if value == code]
        groups[name] = polygons(unary_union(pieces).intersection(boundary)) if pieces else Polygon()
    return groups


def main():
    parent = load(SOURCE_VERSION)
    old_map, old_geometries, _, coarse_raster, _, coarse_affine, _ = classified_candidates()
    raster_path = DESTINATION / '90米残余排水候选-连续平地.tif'
    status_path = DESTINATION / '90米残余排水状态-连续平地.tif'
    candidate_report = read_json(DESTINATION / '90米残余排水核查-连续平地.json')
    if (candidate_report['candidateRasterSHA256'] != sha_file(raster_path) or
            candidate_report['statusRasterSHA256'] != sha_file(status_path)):
        raise ValueError('90米候选或状态图层摘要不匹配')
    if any(sha_file(DEM / name) != sha for name, sha in candidate_report['demFilesSHA256'].items()):
        raise ValueError('90米DEM原件摘要已改变')
    with rasterio.open(raster_path) as ds:
        fine_raster, fine_affine = ds.read(1), ds.transform
    unresolved = old_geometries['unresolved']
    fine = group_raster(fine_raster, fine_affine, unresolved)
    coarse = group_raster(coarse_raster, coarse_affine, unresolved)
    # Contradictory known targets are not resolved by assuming one DEM is right.
    conflicts, accepted = {}, {}
    for name, geom in fine.items():
        contrary = unary_union([g for other, g in coarse.items() if other != name and not g.is_empty])
        conflicts[name] = polygons(geom.intersection(contrary))
        accepted[name] = polygons(geom.difference(contrary))
    if any(area(a.intersection(b)) > .1 for i, a in enumerate(accepted.values())
           for b in list(accepted.values())[i + 1:]):
        raise ValueError('90米目标分区之间出现重叠')

    original = read_json(SOURCE / 'units.geojson')['features']
    display = read_json(SOURCE / 'units-display.geojson')['features']
    by_id = {f['properties']['id']: f for f in original}
    display_by_id = {f['properties']['id']: f for f in display}
    if set(by_id) != {u['id'] for u in parent['units']} or set(display_by_id) != set(by_id):
        raise ValueError('父空间单元与几何不匹配')
    units = {u['id']: deepcopy(u) for u in parent['units']}
    new_geometry, transfers, transferred = {}, [], {name: [] for name in CODES.values()}
    max_projected_split_error, max_topology_error = 0., 0.
    pieces, owners = [], []
    for name, geom in accepted.items():
        for part in getattr(geom, 'geoms', [geom]):
            if part.geom_type == 'Polygon' and not part.is_empty:
                pieces.append(part)
                owners.append(name)
    tree = STRtree(pieces)
    for unit in parent['units']:
        if unit['partition'] != 'unresolved':
            continue
        geometry = polygons(shape(by_id[unit['id']]['geometry']))
        indices = tree.query(geometry, predicate='intersects')
        grouped = {}
        for index in indices:
            grouped.setdefault(owners[int(index)], []).append(pieces[int(index)])
        allocations = {name: polygons(geometry.intersection(unary_union(items))) for name, items in grouped.items()}
        allocations = {name: g for name, g in allocations.items() if not g.is_empty and area(g) > 1e-6}
        if not allocations:
            continue
        remainder = polygons(geometry.difference(unary_union(list(allocations.values()))))
        part_areas = {name: area(g) for name, g in allocations.items()}
        remain_area = area(remainder)
        total = remain_area + sum(part_areas.values())
        original_area = area(geometry)
        projected_error = abs(total - original_area)
        # Splitting a long geographic segment inserts vertices; projecting
        # those curved segments separately can change its planar approximation
        # by a few square metres. Check true native-coordinate topology too.
        reassembled = unary_union([remainder, *allocations.values()])
        topology_error = area(geometry.symmetric_difference(reassembled))
        max_projected_split_error = max(max_projected_split_error, projected_error)
        max_topology_error = max(max_topology_error, topology_error)
        if topology_error > max(10., original_area * 1e-6) or projected_error > max(100., original_area * 1e-5):
            raise ValueError('来源单元拆分面积不闭合：' + unit['id'] +
                             f'; original={original_area:.6f} m2, parts={total:.6f} m2, '
                             f'projectedError={projected_error:.6f} m2, topologyError={topology_error:.6f} m2')
        left = unit['areaKm2'] * remain_area / total
        if left > 1e-14 and not remainder.is_empty:
            units[unit['id']]['areaKm2'] = left
            new_geometry[unit['id']] = remainder
        else:
            units.pop(unit['id'])
        for name, geom in allocations.items():
            allocation_area = unit['areaKm2'] * part_areas[name] / total
            identity = {'grid': unit['grid'], 'partition': name,
                        'elevationBandMin': unit['elevationBandMin'], 'kind': unit['kind']}
            identifier = 'GRID-' + digest(identity)[:20]
            if identifier not in units:
                units[identifier] = {**unit, **identity, 'id': identifier, 'areaKm2': 0.,
                                     'basin': 'zajia' if name in ('duja', 'zajia_rest') else name}
            elif any(units[identifier][key] != unit[key] for key in ('gridIndex', 'kind', 'elevationBandMin', 'elevation')):
                raise ValueError('待合并来源单元的网格、地表或高程不一致')
            if identifier not in new_geometry:
                new_geometry[identifier] = polygons(shape(by_id[identifier]['geometry'])) if identifier in by_id else Polygon()
            new_geometry[identifier] = polygons(new_geometry[identifier].union(geom))
            units[identifier]['areaKm2'] += allocation_area
            transferred[name].append(geom)
            transfers.append({'sourceUnitId': unit['id'], 'targetUnitId': identifier,
                              'partition': name, 'areaKm2': allocation_area})
    effective = {name: polygons(unary_union(items)) if items else Polygon() for name, items in transferred.items()}
    accepted_union = polygons(unary_union(list(accepted.values())))
    reassigned = polygons(unary_union(list(effective.values())))
    gap = area(accepted_union.symmetric_difference(reassigned))
    if gap > 1000:
        raise ValueError('候选和原来源单元覆盖的差额超过0.001 km²')
    old_total = sum(u['areaKm2'] for u in parent['units'])
    new_total = sum(u['areaKm2'] for u in units.values())
    if abs(old_total - new_total) > 1e-8:
        raise ValueError('模型陆域面积不守恒')
    transferred_area = {name: sum(r['areaKm2'] for r in transfers if r['partition'] == name) for name in CODES.values()}
    if not transferred_area['fringe'] > 0 or not transferred_area['duja'] > 0:
        raise ValueError('新来源目标与预期不符')

    new_map = dict(old_geometries)
    new_map['unresolved'] = polygons(unresolved.difference(reassigned))
    for name in ('zagen', 'boqu', 'ali', 'fringe'):
        new_map[name] = polygons(old_geometries[name].union(effective[name]))
    new_map['zajia'] = polygons(old_geometries['zajia'].union(effective['duja']).union(effective['zajia_rest']))
    new_map['duja'] = polygons(old_geometries['duja'].union(effective['duja']))
    manifest = deepcopy(parent)
    manifest.pop('version')
    manifest['parentSpatialVersion'], manifest['parentManifestSHA256'] = SOURCE_VERSION, digest(parent)
    manifest['units'] = list(units.values())
    manifest['areaClosureErrorKm2'] = new_total + parent['lakeAreaKm2'] - parent['domainAreaKm2']
    for basin in manifest['basins']:
        key = basin['id']
        if key == 'unresolved':
            basin['areaKm2'] -= sum(transferred_area.values())
        elif key == 'zajia':
            basin['areaKm2'] += transferred_area['duja'] + transferred_area['zajia_rest']
        elif key in transferred_area:
            basin['areaKm2'] += transferred_area[key]
    drainage = manifest['drainage']
    drainage['method'] += '; 90m-strict-downhill-linked-unique-flats-v1; reject-coarse-conflicts'
    drainage['unresolvedAreaKm2'] -= sum(transferred_area.values())
    drainage['inflowComplete'] = False
    drainage['fineDemReassignmentKm2'] = transferred_area
    drainage['limitation'] = '90米相对地形与原分区约束推断；粗细地形冲突、未解低点和平地及边界继续保持未核实，不计入已核实入湖。'
    scripts = [Path(__file__), ROOT / 'scripts/核查90米连续平地排水.py', ROOT / 'scripts/核查90米地形排水.py',
               ROOT / 'scripts/核查平地候选空间关系.py', ROOT / 'scripts/核查色林错平地出口.py',
               ROOT / 'scripts/构建平地出口空间.py']
    manifest['fineDemDerivation'] = {
        'method': candidate_report['method'], 'demFilesSHA256': candidate_report['demFilesSHA256'],
        'candidateRasterSHA256': sha_file(raster_path), 'statusRasterSHA256': sha_file(status_path),
        'parentAssetsSHA256': {name: sha_file(SOURCE / name) for name in
                               ('manifest.json', 'map.geojson', 'units.geojson', 'units-display.geojson',
                                'drainage-d8-raw.tif', 'residual-destination.tif')},
        'scriptsSHA256': {p.name: sha_file(p) for p in scripts},
        'excludedCoarseConflictKm2': {name: area(g) / 1e6 for name, g in conflicts.items()},
        'acceptedCandidateAreaKm2': area(accepted_union) / 1e6,
        'effectiveGeometryAreaKm2': area(reassigned) / 1e6,
        'modelAreaKm2ByPartition': transferred_area, 'outcomeCellCounts': candidate_report['outcomeCellCounts'],
        'candidateCoverageDifferenceKm2': gap / 1e6,
        'maxUnitProjectedSplitApproximationErrorM2': max_projected_split_error,
        'maxUnitNativeTopologyErrorM2': max_topology_error,
        'limitations': 'DEM产品与垂直单位/基准未知，仅取相对坡序；已有分区/河网非独立实测；未强制填洼。'}
    manifest['version'] = 'GRID-SPATIAL-' + digest(manifest)[:20]
    folder = directory(manifest['version'])
    folder.mkdir(parents=True, exist_ok=False)
    def feature(geometry, properties):
        return {'type': 'Feature', 'properties': properties, 'geometry': mapping(polygons(geometry))}
    exact_features, display_features = [], []
    for identifier, unit in units.items():
        if identifier in new_geometry:
            geom = new_geometry[identifier]
            exact_features.append(feature(geom, unit))
            display_features.append(feature(polygons(geom.simplify(.0005, preserve_topology=True)), unit))
        else:
            exact_features.append({**by_id[identifier], 'properties': unit})
            display_features.append({**display_by_id[identifier], 'properties': unit})
    for name, features in (('units.geojson', exact_features), ('units-display.geojson', display_features)):
        write_new(folder / name, data_bytes({'type': 'FeatureCollection', 'features': features}))
    maps = []
    for item in old_map:
        props = deepcopy(item['properties'])
        key = props.get('id') or props['kind']
        if key in ('zajia', 'zagen', 'boqu', 'ali', 'fringe', 'unresolved', 'duja'):
            props['areaKm2'] = area(new_map[key]) / 1e6
            maps.append(feature(new_map[key], props))
        else:
            maps.append(item)
    write_new(folder / 'map.geojson', data_bytes({'type': 'FeatureCollection', 'features': maps}))
    display_map = [{**f, 'geometry': mapping(polygons(shape(f['geometry']).simplify(.001, preserve_topology=True)))} for f in maps]
    write_new(folder / 'display.geojson', data_bytes({'type': 'FeatureCollection', 'features': display_map}))
    context = read_json(SOURCE / 'display-context.json')
    for item in context.get('labels', []):
        key = item.get('id') or item['kind']
        if key in ('zajia', 'zagen', 'boqu', 'ali', 'fringe', 'unresolved'):
            point = new_map[key].representative_point()
            item['areaKm2'], item['anchor'] = area(new_map[key]) / 1e6, [point.x, point.y]
    write_new(folder / 'display-context.json', data_bytes(context, indent=2))
    for name in ('grid.geojson', 'rivers-display.geojson', 'terrain-display.png', 'drainage-d8-raw.tif', 'residual-destination.tif'):
        write_new(folder / name, (SOURCE / name).read_bytes())
    # These coarse rasters remain source provenance, not final fine classifications.
    write_new(folder / 'fine-dem-destination.tif', raster_path.read_bytes())
    write_new(folder / 'fine-dem-status.tif', status_path.read_bytes())
    write_new(folder / 'fine-dem-accepted.geojson', data_bytes({'type': 'FeatureCollection', 'features':
              [feature(g, {'partition': name}) for name, g in effective.items() if not g.is_empty]}))
    write_new(folder / 'fine-dem-conflicts.geojson', data_bytes({'type': 'FeatureCollection', 'features':
              [feature(g, {'fineTarget': name}) for name, g in conflicts.items() if not g.is_empty]}))
    write_new(folder / 'fine-dem-transfers.json', data_bytes({'transfers': transfers}, indent=2))
    write_new(folder / 'manifest.json', data_bytes(manifest, indent=2))
    aggregate = ensure_aggregate(manifest['version'])
    summary = {'parentVersion': SOURCE_VERSION, 'spatialVersion': manifest['version'],
               'aggregateVersion': aggregate['version'], 'sourceUnits': len(parent['units']),
               'newSourceUnits': len(manifest['units']), 'aggregateUnits': len(aggregate['units']),
               'transferAreaKm2': transferred_area, 'conflictingCoarseFineKm2': sum(area(g) for g in conflicts.values()) / 1e6,
               'remainingUnresolvedKm2': drainage['unresolvedAreaKm2'],
               'areaClosureErrorKm2': manifest['areaClosureErrorKm2'],
               'candidateCoverageDifferenceKm2': gap / 1e6,
               'maxUnitProjectedSplitApproximationErrorM2': max_projected_split_error,
               'maxUnitNativeTopologyErrorM2': max_topology_error, 'currentPointerModified': False}
    write_new(folder / 'fine-dem-receipt.json', data_bytes(summary, indent=2))
    print(json.dumps(summary, ensure_ascii=True))


if __name__ == '__main__':
    main()
