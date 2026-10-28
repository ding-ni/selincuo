"""Derive a new, isolated spatial version from conservative flat outlets.

Only old unresolved land can move, and only to its unique downstream target.
Existing source units, initial states, frozen results and the current pointer
are never changed. A later simulation must start with a compatible fresh state.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.features import shapes
from shapely import make_valid
from shapely.geometry import Polygon, mapping, shape
from shapely.ops import transform, unary_union

from backend.common import ROOT, digest, read_json, sha_file
from backend.grid_spatial import directory, ensure_aggregate, load
from scripts.核查平地候选空间关系 import classified_candidates
from scripts.核查色林错平地出口 import VERSION


def polygons(geometry):
    fixed = make_valid(geometry) if not geometry.is_valid else geometry
    if fixed.geom_type in ('Polygon', 'MultiPolygon'):
        return fixed
    parts = [polygons(item) for item in getattr(fixed, 'geoms', [])
             if item.geom_type in ('Polygon', 'MultiPolygon', 'GeometryCollection')]
    return unary_union(parts) if parts else Polygon()


def data_bytes(value, indent=None):
    return (json.dumps(value, ensure_ascii=False, indent=indent, allow_nan=False,
                       separators=None if indent else (',', ':')) + '\n').encode('utf-8')


def write_new(path, data):
    with path.open('xb') as stream:
        stream.write(data)


def main():
    parent = load(VERSION)
    source = directory(VERSION)
    old_map, old_geometry, _, raster, counts, affine, residual = classified_candidates()
    project = Transformer.from_crs(4326, 6933, always_xy=True).transform
    def measured_area(geom):
        return transform(project, geom).area
    labels = {1: 'zajia_rest', 2: 'zagen', 3: 'boqu', 4: 'ali', 5: 'fringe', 6: 'duja'}
    candidate = {}
    for code, partition in labels.items():
        parts = [shape(g) for g, value in
                 shapes(raster, mask=raster == code, transform=affine) if value == code]
        candidate[partition] = polygons(unary_union(parts).intersection(old_geometry['unresolved'])) if parts else Polygon()
    for i, a in enumerate(candidate.values()):
        for b in list(candidate.values())[i + 1:]:
            if measured_area(a.intersection(b)) > 1e-4:
                raise ValueError('候选栅格在不同归属之间出现面重叠')

    exact = read_json(source / 'units.geojson')['features']
    display = read_json(source / 'units-display.geojson')['features']
    exact_by_id = {f['properties']['id']: f for f in exact}
    display_by_id = {f['properties']['id']: f for f in display}
    if len(exact_by_id) != len(parent['units']) or set(exact_by_id) != {u['id'] for u in parent['units']}:
        raise ValueError('原计算单元和精确几何身份不一致')
    if set(display_by_id) != set(exact_by_id):
        raise ValueError('原展示几何和计算单元身份不一致')

    units = {u['id']: deepcopy(u) for u in parent['units']}
    # Overlay in the common native WGS84 geometry representation. Reprojecting
    # only one of two shared edges creates spurious boundary slivers; project
    # afterwards solely to measure the areas used for transfer weights.
    updated_geom = {}
    transferred_parts = {part: [] for part in labels.values()}
    transfers = []
    for unit in parent['units']:
        if unit['partition'] != 'unresolved':
            continue
        geom = polygons(shape(exact_by_id[unit['id']]['geometry']))
        if not any(not g.is_empty and geom.intersects(g) for g in candidate.values()):
            continue
        portions = {part: polygons(geom.intersection(g)) for part, g in candidate.items() if not g.is_empty}
        portions = {part: g for part, g in portions.items() if not g.is_empty and measured_area(g) > 1e-6}
        if not portions:
            continue
        remainder = polygons(geom.difference(unary_union(list(portions.values()))))
        area_of_remainder = measured_area(remainder)
        areas_of_portions = {part: measured_area(piece) for part, piece in portions.items()}
        total_area = area_of_remainder + sum(areas_of_portions.values())
        original_area = measured_area(geom)
        if abs(total_area - original_area) > max(.1, original_area * 1e-8):
            raise ValueError('拆分来源单元时面积未闭合：' + unit['id'])
        area = unit['areaKm2']
        remaining_model_area = area * area_of_remainder / total_area
        if remaining_model_area > 1e-14 and not remainder.is_empty:
            units[unit['id']]['areaKm2'] = remaining_model_area
            updated_geom[unit['id']] = remainder
        else:
            units.pop(unit['id'])
        for part, piece in portions.items():
            part_area = area * areas_of_portions[part] / total_area
            identity = {'grid': unit['grid'], 'partition': part,
                        'elevationBandMin': unit['elevationBandMin'], 'kind': unit['kind']}
            identifier = 'GRID-' + digest(identity)[:20]
            if identifier not in units:
                new_unit = {**unit, **identity, 'id': identifier, 'areaKm2': 0.,
                            'basin': 'zajia' if part in ('duja', 'zajia_rest') else part}
                units[identifier] = new_unit
            else:
                expected = units[identifier]
                if any(expected[k] != unit[k] for k in ('gridIndex', 'elevation', 'kind', 'elevationBandMin')):
                    raise ValueError('待合并单元的格网、高程或类型不一致')
            if identifier not in updated_geom:
                updated_geom[identifier] = polygons(shape(exact_by_id[identifier]['geometry'])) if identifier in exact_by_id else Polygon()
            updated_geom[identifier] = polygons(updated_geom[identifier].union(piece))
            units[identifier]['areaKm2'] += part_area
            transferred_parts[part].append(piece)
            transfers.append({'sourceUnitId': unit['id'], 'targetUnitId': identifier,
                              'partition': part, 'areaKm2': part_area})

    effective = {part: polygons(unary_union(geoms)) if geoms else Polygon()
                 for part, geoms in transferred_parts.items()}
    reassigned = polygons(unary_union(list(effective.values())))
    candidate_area = sum(measured_area(g) for g in candidate.values())
    reassigned_area = measured_area(reassigned)
    if abs(candidate_area - reassigned_area) > 1000:
        raise ValueError('候选面积与来源单元实际覆盖相差超过0.001km²')
    old_total = sum(u['areaKm2'] for u in parent['units'])
    new_total = sum(u['areaKm2'] for u in units.values())
    if abs(old_total - new_total) > 1e-8:
        raise ValueError('模型陆域面积未闭合')
    by_part = {part: sum(row['areaKm2'] for row in transfers if row['partition'] == part)
               for part in labels.values()}
    if not (by_part['duja'] > 0 and by_part['fringe'] > 0):
        raise ValueError('本机候选与预期来源不符，停止派生')

    new_area_map = dict(old_geometry)
    new_area_map['unresolved'] = polygons(old_geometry['unresolved'].difference(reassigned))
    for part in ('zagen', 'boqu', 'ali', 'fringe'):
        new_area_map[part] = polygons(old_geometry[part].union(effective[part]))
    zajia_added = polygons(effective['zajia_rest'].union(effective['duja']))
    new_area_map['zajia'] = polygons(old_geometry['zajia'].union(zajia_added))
    new_area_map['duja'] = polygons(old_geometry['duja'].union(effective['duja']))

    manifest = deepcopy(parent)
    manifest.pop('version')
    manifest['parentSpatialVersion'] = VERSION
    manifest['parentManifestSHA256'] = digest(parent)
    manifest['units'] = list(units.values())
    manifest['areaClosureErrorKm2'] = new_total + parent['lakeAreaKm2'] - parent['domainAreaKm2']
    for basin in manifest['basins']:
        if basin['id'] == 'unresolved':
            basin['areaKm2'] -= sum(by_part.values())
        elif basin['id'] == 'zajia':
            basin['areaKm2'] += by_part['duja'] + by_part['zajia_rest']
        elif basin['id'] in by_part:
            basin['areaKm2'] += by_part[basin['id']]
    drainage = manifest['drainage']
    drainage['method'] += '; unique-flat-outlet-only-v1'
    drainage['unresolvedAreaKm2'] -= sum(by_part.values())
    drainage['inflowComplete'] = False
    drainage['flatOutletReassignmentKm2'] = by_part
    drainage['limitation'] = ('等高平地只按现有粗DEM和既有控制面判别唯一出口；'
                              '其余未核实去向仍不计入入湖合计，候选去向不是现场实测。')
    sources = ('manifest.json', 'map.geojson', 'units.geojson', 'units-display.geojson',
               'drainage-d8-raw.tif', 'residual-destination.tif')
    script_paths = (Path(__file__), ROOT / 'scripts/核查平地候选空间关系.py',
                    ROOT / 'scripts/核查色林错平地出口.py')
    manifest['flatOutletDerivation'] = {
        'method': 'equal elevation 8-neighbour plateau; all lower exits resolve to one existing target; no unknown or boundary exits; no filling',
        'sourceAssetsSHA256': {name: sha_file(source / name) for name in sources},
        'scriptsSHA256': {p.name: sha_file(p) for p in script_paths},
        'targetScope': 'first established target pixel; duja control separated from remaining zajia',
        'candidateAreaKm2': candidate_area / 1e6, 'effectiveGeometryAreaKm2': reassigned_area / 1e6,
        'modelAreaKm2ByPartition': by_part, 'statusCellCounts': counts,
        'originalModelAreaKm2': old_total, 'newModelAreaKm2': new_total,
        'limitations': '粗DEM与既有控制边界的条件推断，不代替独立测绘；其他洼地和域边界保持原状态。'}
    manifest['version'] = 'GRID-SPATIAL-' + digest(manifest)[:20]
    folder = directory(manifest['version'])
    folder.mkdir(parents=True, exist_ok=False)

    def feature(geom, properties):
        return {'type': 'Feature', 'properties': properties,
                'geometry': mapping(polygons(geom))}

    exact_features, display_features = [], []
    for identifier, unit in units.items():
        if identifier in updated_geom:
            geom = updated_geom[identifier]
            exact_features.append(feature(geom, unit))
            display_features.append(feature(polygons(geom.simplify(.0005, preserve_topology=True)), unit))
        else:
            exact_features.append({**exact_by_id[identifier], 'properties': unit})
            display_features.append({**display_by_id[identifier], 'properties': unit})
    write_new(folder / 'units.geojson', data_bytes({'type': 'FeatureCollection', 'features': exact_features}))
    write_new(folder / 'units-display.geojson', data_bytes({'type': 'FeatureCollection', 'features': display_features}))

    maps = []
    for f in old_map:
        props = deepcopy(f['properties'])
        key = props.get('id') or props['kind']
        if key in ('zajia', 'zagen', 'boqu', 'ali', 'fringe', 'unresolved', 'duja'):
            geom = new_area_map[key]
            props['areaKm2'] = measured_area(geom) / 1e6
            maps.append(feature(geom, props))
        else:
            maps.append(f)
    write_new(folder / 'map.geojson', data_bytes({'type': 'FeatureCollection', 'features': maps}))
    display_map = []
    for f in maps:
        simplified = polygons(make_valid(shape(f['geometry'])).simplify(.001, preserve_topology=True))
        display_map.append({**f, 'geometry': mapping(simplified)})
    write_new(folder / 'display.geojson', data_bytes({'type': 'FeatureCollection', 'features': display_map}))

    context = read_json(source / 'display-context.json')
    for item in context.get('labels', []):
        key = item.get('id') or item['kind']
        if key in new_area_map and key in ('zajia', 'zagen', 'boqu', 'ali', 'fringe', 'unresolved'):
            geom = new_area_map[key]
            point = geom.representative_point()
            item['areaKm2'], item['anchor'] = measured_area(geom) / 1e6, [point.x, point.y]
    write_new(folder / 'display-context.json', data_bytes(context, indent=2))
    for name in ('grid.geojson', 'rivers-display.geojson', 'terrain-display.png', 'drainage-d8-raw.tif'):
        write_new(folder / name, (source / name).read_bytes())

    with rasterio.open(source / 'residual-destination.tif') as old_raster:
        profile = old_raster.profile.copy()
    allocation = residual.copy()
    for code in labels:
        # Preserve the existing schema: target code 6 here is duja, a subset of
        # the established zajia code 1, not the old external code 6.
        allocation[raster == code] = 1 if code == 6 else code
    with rasterio.open(folder / 'residual-destination.tif', 'w', **profile) as output:
        output.write(allocation, 1)
    write_new(folder / 'flat-outlet-transfers.json', data_bytes({'transfers': transfers}, indent=2))
    write_new(folder / 'manifest.json', data_bytes(manifest, indent=2))
    aggregate = ensure_aggregate(manifest['version'])
    summary = {'parentVersion': VERSION, 'spatialVersion': manifest['version'],
               'aggregateVersion': aggregate['version'], 'sourceUnits': len(parent['units']),
               'newSourceUnits': len(manifest['units']), 'aggregateUnits': len(aggregate['units']),
               'transferAreaKm2': by_part, 'remainingUnresolvedKm2': drainage['unresolvedAreaKm2'],
               'areaClosureErrorKm2': manifest['areaClosureErrorKm2'],
               'candidateVersusCoveredKm2': (candidate_area - reassigned_area) / 1e6,
               'currentPointerModified': False}
    write_new(folder / 'flat-outlet-receipt.json', data_bytes(summary, indent=2))
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
