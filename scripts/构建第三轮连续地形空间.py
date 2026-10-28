"""Build a conditional continuous-terrain candidate, never alter current.

Direct D8 receivers and non-water pits with an entire <=2 m pit-chain are
assigned only to their existing receiver. Independent water components and
all other outcomes remain unresolved. This is not surveyed connectivity.
"""
from copy import deepcopy
import json
import math
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import Polygon, mapping, shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

from backend.common import ROOT, digest, read_json, sha_file
from backend.grid_spatial import directory, ensure_aggregate, load
from scripts.构建平地出口空间 import data_bytes, polygons, write_new

PARENT = 'GRID-SPATIAL-c82d78ed63283ed20fcb'
AUDIT = ROOT / 'build/第三轮整改-20261028/连续地形'
DESTINATIONS = {1: 'zajia_rest', 2: 'zagen', 3: 'boqu', 4: 'ali',
                5: 'fringe', 6: 'duja', 8: 'fringe'}
PARTITIONS = tuple(dict.fromkeys(DESTINATIONS.values()))
PROJECT = Transformer.from_crs(4326, 6933, always_xy=True).transform
THRESHOLD_M = 2.


def area(geometry):
    return transform(PROJECT, geometry).area


def feature(geometry, properties):
    return {'type': 'Feature', 'properties': properties, 'geometry': mapping(polygons(geometry))}


def targets(actual, thresholds):
    pits = {int(p['rawCode']): p for p in thresholds['pits']}
    if len(pits) != len(thresholds['pits']):
        raise ValueError('洼地编码重复')
    grouped = {name: [] for name in PARTITIONS}
    counts = {'direct': 0, 'shallowPit': 0, 'remaining': 0}
    for row in actual['features']:
        props = row['properties']
        code = int(props['code'])
        target = DESTINATIONS.get(code)
        kind = 'direct' if target else 'remaining'
        if code >= 1000:
            pit = pits.get(code)
            if pit is None:
                raise ValueError('洼地候选缺少溢流证据：' + str(code))
            depth = pit.get('maxChainPitDepthM')
            receiver = pit.get('potentialReceiverCode')
            if (isinstance(depth, (int, float)) and not isinstance(depth, bool) and
                    math.isfinite(depth) and 0 <= depth <= THRESHOLD_M and receiver in DESTINATIONS):
                target = DESTINATIONS[receiver]
                kind = 'shallowPit'
        counts[kind] += 1
        if target:
            grouped[target].append(polygons(shape(row['geometry'])))
    candidates = {key: polygons(unary_union(items)) if items else Polygon() for key, items in grouped.items()}
    if any(area(a.intersection(b)) > .1 for i, a in enumerate(candidates.values())
           for b in list(candidates.values())[i + 1:]):
        raise ValueError('不同接收区的候选几何发生重叠')
    return candidates, counts


def main():
    source = directory(PARENT)
    parent = load(PARENT)
    actual_path = AUDIT / '实际下游归属.geojson'
    audit_path = AUDIT / '实际下游核查.json'
    thresholds_path = AUDIT / '洼地溢流门槛.json'
    actual, audit, thresholds = read_json(actual_path), read_json(audit_path), read_json(thresholds_path)
    if (audit['parentSpatialVersion'] != PARENT or thresholds['actualDrainageSHA256'] != sha_file(audit_path)
            or thresholds['demSHA256'] != audit['demSHA256']):
        raise ValueError('地形、下游几何与洼地证据身份不一致')
    if audit['demSHA256'] != sha_file(AUDIT / '公开连续DSM.tif'):
        raise ValueError('连续地形摘要不一致')
    if audit['waterSHA256'] != sha_file(AUDIT / '公开水体掩膜.tif'):
        raise ValueError('水体掩膜摘要不一致')
    old_map = read_json(source / 'map.geojson')['features']
    old_geometries = {f['properties'].get('id') or f['properties']['kind']: shape(f['geometry']) for f in old_map
                      if f['properties']['kind'] in ('basin', 'lake', 'control', 'domain')}
    unresolved = polygons(old_geometries['unresolved'])
    candidates, counts = targets(actual, thresholds)
    candidates = {key: polygons(g.intersection(unresolved)) for key, g in candidates.items()}
    candidate_union = polygons(unary_union(list(candidates.values())))
    originals = read_json(source / 'units.geojson')['features']
    displays = read_json(source / 'units-display.geojson')['features']
    original = {f['properties']['id']: f for f in originals}
    display = {f['properties']['id']: f for f in displays}
    if set(original) != {u['id'] for u in parent['units']} or set(display) != set(original):
        raise ValueError('父空间单元与图层不一致')
    units = {u['id']: deepcopy(u) for u in parent['units']}
    geometry_updates, transfers = {}, []
    transferred = {name: [] for name in PARTITIONS}
    max_split_error = max_topology_error = 0.
    # Index candidate polygons to avoid repeatedly overlaying the whole domain.
    pieces, owners = [], []
    for name, geometry in candidates.items():
        for part in getattr(geometry, 'geoms', [geometry]):
            if part.geom_type == 'Polygon' and not part.is_empty:
                pieces.append(part)
                owners.append(name)
    tree = STRtree(pieces)
    for unit in parent['units']:
        if unit['partition'] != 'unresolved':
            continue
        geometry = polygons(shape(original[unit['id']]['geometry']))
        found = tree.query(geometry, predicate='intersects')
        grouped = {}
        for index in found:
            grouped.setdefault(owners[int(index)], []).append(pieces[int(index)])
        allocations = {name: polygons(geometry.intersection(unary_union(items))) for name, items in grouped.items()}
        allocations = {name: item for name, item in allocations.items() if not item.is_empty and area(item) > 1e-6}
        if not allocations:
            continue
        remainder = polygons(geometry.difference(unary_union(list(allocations.values()))))
        parts = {name: area(item) for name, item in allocations.items()}
        remaining = area(remainder)
        total = remaining + sum(parts.values())
        original_area = area(geometry)
        split_error = abs(total - original_area)
        topology_error = area(geometry.symmetric_difference(unary_union([remainder, *allocations.values()])))
        max_split_error = max(max_split_error, split_error)
        max_topology_error = max(max_topology_error, topology_error)
        if topology_error > max(10., original_area * 1e-6) or split_error > max(100., original_area * 1e-5):
            raise ValueError('来源单元拆分面积不闭合：' + unit['id'])
        left = unit['areaKm2'] * remaining / total
        if left > 1e-14 and not remainder.is_empty:
            units[unit['id']]['areaKm2'] = left
            geometry_updates[unit['id']] = remainder
        else:
            units.pop(unit['id'])
        for name, geometry in allocations.items():
            model_area = unit['areaKm2'] * parts[name] / total
            identity = {'grid': unit['grid'], 'partition': name,
                        'elevationBandMin': unit['elevationBandMin'], 'kind': unit['kind']}
            identifier = 'GRID-' + digest(identity)[:20]
            if identifier not in units:
                units[identifier] = {**unit, **identity, 'id': identifier, 'areaKm2': 0.,
                                     'basin': 'zajia' if name in ('duja', 'zajia_rest') else name}
            elif any(units[identifier][key] != unit[key] for key in ('gridIndex', 'kind', 'elevationBandMin', 'elevation')):
                raise ValueError('待合并来源单元的气象格网、地表或高程不一致')
            if identifier not in geometry_updates:
                geometry_updates[identifier] = polygons(shape(original[identifier]['geometry'])) if identifier in original else Polygon()
            geometry_updates[identifier] = polygons(geometry_updates[identifier].union(geometry))
            units[identifier]['areaKm2'] += model_area
            transferred[name].append(geometry)
            transfers.append({'sourceUnitId': unit['id'], 'targetUnitId': identifier,
                              'partition': name, 'areaKm2': model_area})
    effective = {name: polygons(unary_union(items)) if items else Polygon() for name, items in transferred.items()}
    reassigned = polygons(unary_union(list(effective.values())))
    coverage_difference = area(candidate_union.symmetric_difference(reassigned))
    if coverage_difference > 1000.:
        raise ValueError('候选与来源计算单元覆盖差额超过0.001平方公里')
    old_total = sum(u['areaKm2'] for u in parent['units'])
    new_total = sum(u['areaKm2'] for u in units.values())
    if abs(old_total - new_total) > 1e-8:
        raise ValueError('模型陆域面积不守恒')
    by_partition = {name: sum(r['areaKm2'] for r in transfers if r['partition'] == name) for name in PARTITIONS}
    new_map = dict(old_geometries)
    new_map['unresolved'] = polygons(unresolved.difference(reassigned))
    for name in ('zagen', 'boqu', 'ali', 'fringe'):
        new_map[name] = polygons(old_geometries[name].union(effective[name]))
    new_map['zajia'] = polygons(old_geometries['zajia'].union(effective['duja']).union(effective['zajia_rest']))
    new_map['duja'] = polygons(old_geometries['duja'].union(effective['duja']))
    manifest = deepcopy(parent)
    manifest.pop('version')
    manifest['parentSpatialVersion'], manifest['parentManifestSHA256'] = PARENT, digest(parent)
    manifest['units'] = list(units.values())
    manifest['areaClosureErrorKm2'] = new_total + parent['lakeAreaKm2'] - parent['domainAreaKm2']
    for basin in manifest['basins']:
        name = basin['id']
        if name == 'unresolved':
            basin['areaKm2'] -= sum(by_partition.values())
        elif name == 'zajia':
            basin['areaKm2'] += by_partition['duja'] + by_partition['zajia_rest']
        elif name in by_partition:
            basin['areaKm2'] += by_partition[name]
    drainage = manifest['drainage']
    drainage['method'] += '; continuous-DSM-raw-D8-and-conditional-shallow-pit-chain<=2m-v1'
    drainage['unresolvedAreaKm2'] -= sum(by_partition.values())
    drainage['inflowComplete'] = False
    drainage['continuousTerrainReassignmentKm2'] = by_partition
    drainage['topology'] = {'status': 'incomplete', 'unitReceivers': {}, 'nodes': [], 'boundaries': [],
                            'unresolvedReceivers': ['remaining-depressions-and-independent-water-bodies'],
                            'internalExchangesPaired': False}
    drainage['limitation'] = ('连续DSM的D8和不超过2米浅洼链为条件性地形处理，不是实测连通；'
                              '独立水体及其他洼地保留未定，未计入已判定入湖。')
    source_files = ('manifest.json', 'map.geojson', 'units.geojson', 'units-display.geojson',
                    'display.geojson', 'display-context.json')
    manifest['continuousTerrainDerivation'] = {
        'method': 'raw floating D8 + <=2 m max non-water pit chain with established receiver; candidate only',
        'parentAssetsSHA256': {name: sha_file(source / name) for name in source_files},
        'actualDrainageSHA256': sha_file(actual_path), 'actualAuditSHA256': sha_file(audit_path),
        'pitThresholdsSHA256': sha_file(thresholds_path), 'demSHA256': audit['demSHA256'],
        'waterMaskSHA256': audit['waterSHA256'], 'generatorSHA256': sha_file(Path(__file__)),
        'pitThresholdM': THRESHOLD_M, 'selectionCounts': counts,
        'modelAreaKm2ByPartition': by_partition, 'candidateCoverageDifferenceKm2': coverage_difference / 1e6,
        'maxUnitProjectedSplitApproximationErrorM2': max_split_error, 'maxUnitNativeTopologyErrorM2': max_topology_error,
        'limitations': '阈值为条件性假设；真实水体、较深洼地和边缘保持未定；没有证明实际溢流或外排。'}
    manifest['version'] = 'GRID-SPATIAL-' + digest(manifest)[:20]
    folder = directory(manifest['version'])
    folder.mkdir(parents=True, exist_ok=False)
    exact_features, display_features = [], []
    for identifier, unit in units.items():
        if identifier in geometry_updates:
            geometry = geometry_updates[identifier]
            exact_features.append(feature(geometry, unit))
            display_features.append(feature(geometry.simplify(.0005, preserve_topology=True), unit))
        else:
            exact_features.append({**original[identifier], 'properties': unit})
            display_features.append({**display[identifier], 'properties': unit})
    write_new(folder / 'units.geojson', data_bytes({'type': 'FeatureCollection', 'features': exact_features}))
    write_new(folder / 'units-display.geojson', data_bytes({'type': 'FeatureCollection', 'features': display_features}))
    maps = []
    for item in old_map:
        properties = deepcopy(item['properties'])
        key = properties.get('id') or properties['kind']
        if key in ('zajia', 'zagen', 'boqu', 'ali', 'fringe', 'unresolved', 'duja'):
            properties['areaKm2'] = area(new_map[key]) / 1e6
            maps.append(feature(new_map[key], properties))
        else:
            maps.append(item)
    write_new(folder / 'map.geojson', data_bytes({'type': 'FeatureCollection', 'features': maps}))
    write_new(folder / 'display.geojson', data_bytes({'type': 'FeatureCollection', 'features': [
        {**f, 'geometry': mapping(polygons(shape(f['geometry']).simplify(.001, preserve_topology=True)))} for f in maps]}))
    context = read_json(source / 'display-context.json')
    for item in context.get('labels', []):
        key = item.get('id') or item['kind']
        if key in ('zajia', 'zagen', 'boqu', 'ali', 'fringe', 'unresolved'):
            item['areaKm2'] = area(new_map[key]) / 1e6
    write_new(folder / 'display-context.json', data_bytes(context, indent=2))
    for name in ('grid.geojson', 'rivers-display.geojson', 'terrain-display.png'):
        write_new(folder / name, (source / name).read_bytes())
    write_new(folder / 'continuous-terrain-accepted.geojson', data_bytes({'type': 'FeatureCollection', 'features':
              [feature(g, {'partition': name}) for name, g in effective.items() if not g.is_empty]}))
    write_new(folder / 'continuous-terrain-transfers.json', data_bytes({'transfers': transfers}, indent=2))
    write_new(folder / 'manifest.json', data_bytes(manifest, indent=2))
    aggregate = ensure_aggregate(manifest['version'])
    summary = {'parentVersion': PARENT, 'spatialVersion': manifest['version'], 'aggregateVersion': aggregate['version'],
               'sourceUnits': len(parent['units']), 'newSourceUnits': len(units), 'aggregateUnits': len(aggregate['units']),
               'transferAreaKm2': by_partition, 'remainingUnresolvedKm2': drainage['unresolvedAreaKm2'],
               'residualGeometryKm2': area(unresolved) / 1e6, 'remainingUnresolvedGeometryKm2': area(new_map['unresolved']) / 1e6,
               'areaClosureErrorKm2': manifest['areaClosureErrorKm2'], 'originalModelLandAreaKm2': old_total,
               'newModelLandAreaKm2': new_total, 'candidateCoverageDifferenceKm2': coverage_difference / 1e6,
               'rasterVectorDifferenceKm2': audit['residualGeometryKm2'] - audit['assignedGeometryKm2'],
               'maxUnitProjectedSplitApproximationErrorM2': max_split_error,
               'maxUnitNativeTopologyErrorM2': max_topology_error, 'currentPointerModified': False,
               'pitThresholdM': THRESHOLD_M, 'selectionCounts': counts,
               'inflowComplete': False, 'conditionalNotSurveyed': True}
    write_new(folder / 'continuous-terrain-receipt.json', data_bytes(summary, indent=2))
    print(json.dumps(summary, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
