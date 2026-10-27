"""Read-only numerical audit of a saved continuous run and its E3 comparison.

Example: python -B scripts/check_reconstructed_continuous.py --workspace ...
         --task-id ... --e3-daily ... --output ...
The output is created exclusively; existing evidence is never overwritten.
"""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from backend.common import digest, read_json, sha_file, within
from backend.grid_simulation import model_identity, prepare, run
from backend.grid_spatial import ensure_aggregate


def nested_difference(a, b):
    if isinstance(a, dict):
        if set(a) != set(b): return float('inf')
        return max((nested_difference(a[k], b[k]) for k in a), default=0.)
    if isinstance(a, list):
        if len(a) != len(b): return float('inf')
        return max((nested_difference(x, y) for x, y in zip(a, b)), default=0.)
    if isinstance(a, (int, float)): return abs(a - b)
    return 0. if a == b else float('inf')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--e3-daily', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    task = within(workspace / 'tasks', args.task_id)
    request = read_json(task / 'request.json')
    original = read_json(task / 'result.json')
    if original['taskId'] != args.task_id or original['modelFamily'] != 'grid-continuous-v1':
        raise ValueError('任务或连续模型身份不一致')
    if original['identity']['modelImplementation'] != model_identity():
        raise ValueError('当前数值实现与所存任务不同，不能当作相同运行复算')
    if digest(original['checkpoint']) != original['checkpointSHA256']:
        raise ValueError('成果完整末状态摘要不一致')
    manifest = read_json(task / 'hourly-manifest.json')
    if not manifest.get('complete') or digest(manifest['files']) != manifest['sha256']:
        raise ValueError('逐时产物清单不完整')
    for name, expected in manifest['files'].items():
        if sha_file(within(task / 'hourly.zarr', name)) != expected:
            raise ValueError('逐时文件摘要不一致：' + name)
    spatial = ensure_aggregate('GRID-SPATIAL-ddbb782dd4bdd3f00fa5')
    if spatial['version'] != original['spatialVersion']:
        raise ValueError('聚合空间与成果身份不一致')

    previous = pd.read_csv(args.e3_daily, parse_dates=['date']).set_index('date')
    previous = previous.loc[previous.index < '2026-01-01']
    actual = pd.read_csv(task / 'daily.csv', parse_dates=['date']).set_index('date').reindex(previous.index)
    mapping = {'duja': 'duja', 'total': 'total', 'lakeLevel': 'lakeLevel',
               'lakePrecipitationM3': 'lakeP', 'lakeEvaporationM3': 'lakeE',
               'lakeDeltaVolumeM3': 'lakeNet', 'lakeInflowM3': 'lakeI', 'excludedRunoff': 'excluded'}
    comparison = {}
    for field, reference in mapping.items():
        delta = (actual[field] - previous[reference]).abs().dropna()
        comparison[field] = {'days': len(delta), 'maxAbs': float(delta.max())}
        if len(delta) == 0 or delta.max() > (1e-5 if field.endswith('M3') or field == 'excludedRunoff' else 1e-8):
            raise ValueError('与同域31组受控实验不一致：' + field)
    balance = float(actual.lakeBalanceErrorM3.abs().max())
    total_error = float((actual.total - actual[['zajia', 'zagen', 'boqu', 'ali', 'fringe']].sum(axis=1, min_count=5)).abs().max())
    if balance > 1e-5 or total_error > 1e-8:
        raise ValueError('湖泊或入湖分量收支不闭合')

    observation = request['_observations']
    request['_observations'] = {'profile': observation['profile'], 'level': {}, 'flow': {}}
    request['_metricsOnly'] = True
    prepared = prepare(SimpleNamespace(workspace=workspace), request['inputId'], request['spatialVersion'])
    first = run(None, {**request, 'end': '2025-12-31T23:00:00'}, prepared=prepared)
    second = run(None, {**request, 'start': '2026-01-01T00:00:00', '_parentState': first['checkpoint']}, prepared=prepared)
    a, b = original['checkpoint'], second['checkpoint']
    physical = {'unitStateMaxAbs': float(np.max(np.abs(np.array(a['unitStates']) - np.array(b['unitStates'])))),
                'lakeLevelAbsM': abs(a['lake']['level'] - b['lake']['level']),
                'continuousLakeVolumeAbsM3': abs(a['continuousLakeVolume'] - b['continuousLakeVolume']),
                'routingStateMaxAbs': nested_difference(a['routing'], b['routing'])}
    ref = pd.DataFrame(original['daily']).set_index('date')
    split = pd.concat([pd.DataFrame(first['daily']), pd.DataFrame(second['daily'])]).set_index('date')
    common = split.index.intersection(ref.index)
    joined = ref.loc[common].join(split.loc[common], lsuffix='_full', rsuffix='_split')
    daily = {}
    for field in ('duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total', 'lakeLevel'):
        delta = (pd.to_numeric(joined[field + '_full'], errors='coerce') -
                 pd.to_numeric(joined[field + '_split'], errors='coerce')).abs().dropna()
        daily[field] = {'days': len(delta), 'maxAbs': float(delta.max())}
        if len(delta) == 0 or delta.max() > 1e-8: raise ValueError('续算或观测缺测对照失败：' + field)
    if (physical['unitStateMaxAbs'] > 1e-9 or physical['lakeLevelAbsM'] > 1e-9 or
            physical['continuousLakeVolumeAbsM3'] > 1e-3 or physical['routingStateMaxAbs'] > 1e-4):
        raise ValueError('续算或观测缺测后的末状态与整段不一致')
    report = {'taskId': args.task_id, 'workspace': str(workspace), 'resultSource': original['resultSource'],
              'modelIdentity': original['identity'], 'inputId': original['inputId'],
              'start': original['start'], 'end': original['end'], 'initialization': original['initialization'],
              'metrics': original['metrics'], 'hourlyChunkFilesChecked': len(manifest['files']),
              'filesSHA256': {name: sha_file(task / name) for name in ('request.json', 'result.json', 'daily.csv', 'hourly-manifest.json')},
              'e3ReferenceSHA256': sha_file(args.e3_daily), 'e3DailyMaxAbs': comparison,
              'lakeDailyBalanceMaxAbsM3': balance, 'totalComponentMaxAbsM3S': total_error,
              'excludedUnverifiedAreaKm2': spatial['drainage']['unresolvedAreaKm2'],
              'splitAt': ['2025-12-31T23:00:00', '2026-01-01T00:00:00'],
              'evaluationObservationsRemoved': ['level', 'flow'], 'splitPhysicalStateMaxAbs': physical,
              'splitDailyMaxAbs': daily,
              'limitations': ['2023年4月20日主站日值按12时代表而非已确认瞬时值',
                              '排水归属未核实区单独记账，不进入已核实入湖总量',
                              '2025和2026已参与开发查看，不作为未触碰的独立验证',
                              '长期真实预热与独立冰雪观测不足，不能宣称科学最优']}
    with Path(args.output).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print('连续数值证据已生成：', args.output)


if __name__ == '__main__': main()
