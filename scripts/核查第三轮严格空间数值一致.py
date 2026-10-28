"""Compare strict spatial versions without treating their identities as equal."""
import json
from pathlib import Path

from backend.common import ROOT, digest, sha_file
from backend.grid_spatial import directory, load, topology_complete
from scripts.构建平地出口空间 import data_bytes, write_new


def main():
    old_id = 'AGG-SPATIAL-4b471ab4055d1a70e0fa'
    corrected_id = 'AGG-SPATIAL-f797008bb3e82f87a73b'
    old, corrected = load(old_id), load(corrected_id)
    fine_old, fine_corrected = load(old['parentSpatialVersion']), load(corrected['parentSpatialVersion'])
    values = lambda value: [{k: v for k, v in u.items() if k != 'id'} for u in value['units']]
    members = lambda value: [g['members'] for g in value['aggregation']]
    fields = ('forcingGridVersion', 'grids', 'lakeAreaKm2', 'lakeWeights', 'domainAreaKm2',
              'areaCrs', 'temperatureElevationReference', 'basins')
    checks = {'fineUnitsExactlyEqual': fine_old['units'] == fine_corrected['units'],
              'aggregateNumericalUnitsEqual': values(old) == values(corrected),
              'sourceMemberMappingsEqual': members(old) == members(corrected),
              'forcingWeightsExactlyEqual': old['weights'] == corrected['weights'],
              'eachFieldEqual': {key: old[key] == corrected[key] for key in fields},
              'incompleteTopologyBoth': not topology_complete(old) and not topology_complete(corrected),
              'strictPitCountsBothZero': all(value['continuousTerrainDerivation']['selectionCounts']['shallowPit'] == 0
                                            for value in (fine_old, fine_corrected))}
    old_dir, corrected_dir = directory(fine_old['version']), directory(fine_corrected['version'])
    files = ('units.geojson', 'units-display.geojson', 'map.geojson', 'display.geojson',
             'display-context.json', 'grid.geojson', 'rivers-display.geojson', 'terrain-display.png')
    hashes = {name: {'older': sha_file(old_dir / name), 'corrected': sha_file(corrected_dir / name)} for name in files}
    checks['allFineAssetHashesEqual'] = all(row['older'] == row['corrected'] for row in hashes.values())
    if not (all(v for k, v in checks.items() if k != 'eachFieldEqual') and all(checks['eachFieldEqual'].values())):
        raise ValueError('两个严格候选的计算条件不一致，禁止参数迁移')
    result = {'olderFineSpatialVersion': fine_old['version'], 'olderAggregateSpatialVersion': old_id,
              'correctedFineSpatialVersion': fine_corrected['version'], 'correctedAggregateSpatialVersion': corrected_id,
              'fineManifestSHA256': {'older': sha_file(old_dir / 'manifest.json'),
                                    'corrected': sha_file(corrected_dir / 'manifest.json')},
              'numericalUnitsSHA256': digest(values(old)), 'sourceMemberMappingSHA256': digest(members(old)),
              'forcingWeightsSHA256': digest(old['weights']), 'fineAssetSHA256': hashes,
              'unitCounts': {'fine': len(fine_old['units']), 'aggregated': len(old['units'])},
              'checks': checks, 'maxAbsoluteWeightDifference': max(abs(a-b) for x,y in zip(old['weights'],corrected['weights'])
                                                                   for a,b in zip(x,y)),
              'important': ['聚合单元ID因父空间身份不同而不同，不能直接沿用旧状态。',
                            '数值结构一致支持显式迁移参数并重新预热；不能将率定任务误写成使用新空间。',
                            '原版方法文字与实际阈值不一致，规范版正确记载排除全部洼地。']}
    target = ROOT / 'build/第三轮整改-20261028/严格空间数值一致证据.json'
    write_new(target, data_bytes(result, indent=2))
    print(json.dumps({'path': str(target), 'checks': checks, 'numericalUnitsSHA256': result['numericalUnitsSHA256'],
                      'sourceMemberMappingSHA256': result['sourceMemberMappingSHA256']}, ensure_ascii=True))


if __name__ == '__main__':
    main()
