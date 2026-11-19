"""从已核验的迁移工作区提取只读离线演示，使用发布地形和承灾体计算漫溢。"""
from argparse import ArgumentParser
from contextlib import closing, contextmanager
from pathlib import Path
from shutil import copyfile
import sqlite3
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import forecast_inputs, forecasts, operational_store as store, risk_scenarios as risk
from backend.common import read_json, sha_file, within, write_json


FORECAST_ID = 'FORECAST-ff4668df1d644fb49e0b'
SCENARIOS = (
    ('RISK-8924a10538914d9cae2d', 'CURRENT'),
    ('RISK-606ba00c60eb4c30becb', 'HIGH-WATER'),
)


class Records:
    def __init__(self, workspace, db):
        self.workspace = workspace
        self.db = db
        db.row_factory = sqlite3.Row

    @contextmanager
    def connect(self):
        with self.db:
            yield self.db

    def rows(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args)]


def copy_checked(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    copyfile(source, destination)
    if sha_file(source) != sha_file(destination):
        raise ValueError(f'演示资料复制校验失败：{destination}')


def demo_record(record, **fields):
    return {**{k: v for k, v in record.items() if k not in ('trashed', 'trashedAt', 'trashReason')},
            'demo': True, **fields}


def build(source_folder, output, scratch):
    source_folder, output, scratch = (Path(p).resolve() for p in (source_folder, output, scratch))
    scratch.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    source_uri = (source_folder / 'database/hydrology.sqlite3').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(source_uri, uri=True)) as source_db, closing(sqlite3.connect(':memory:')) as scratch_db:
        source, staging = Records(source_folder, source_db), Records(scratch, scratch_db)
        store.create_schema(scratch_db)
        forecast = forecasts.detail(source, FORECAST_ID)
        weather = forecast_inputs.detail(source, forecast['inputId'])
        history = forecasts.history(source, FORECAST_ID)
        if forecast['hours'] != 240 or weather['hours'] != 240 or forecast['times'] != [r['time'] for r in weather['preview']]:
            raise ValueError('离线演示的气象和预报时段不一致')
        if not history['rows']:
            raise ValueError('离线演示缺少起报前的湖位和流量过程')

        weather_path = 'weather/逐时气象.npz'
        copy_checked(within(source_folder, weather['dataPath']), output / weather_path)
        weather = demo_record(weather, name='离线演示 · 10 天手工气象', dataPath=weather_path)
        write_json(output / 'weather/气象输入说明.json', weather)
        artifact = forecast['memberArtifact']
        artifact_path = 'forecast/逐时集合预报.npz'
        copy_checked(within(source_folder, artifact['path']), output / artifact_path)
        checkpoints = []
        for checkpoint in forecast['memberCheckpointIndex']:
            source_path = within(source_folder, checkpoint['path'])
            path = f'forecast/{source_path.name}'
            copy_checked(source_path, output / path)
            checkpoints.append({**checkpoint, 'path': path})
        forecast = demo_record(forecast, name='离线演示 · 10 天预报',
                               memberArtifact={**artifact, 'path': artifact_path}, memberCheckpointIndex=checkpoints)
        store.save(staging, forecasts.COLLECTION, forecast)

        index = read_json(risk.EXPOSURE_BUNDLE / '承灾体资料.json')
        exposures = []
        for item in index['adopted']:
            identifier = 'DEMO-EXPOSURE-' + item['kind'].upper()
            path = f"承灾体/{item['file']}"
            copy_checked(risk.EXPOSURE_BUNDLE / item['file'], scratch / path)
            count = len(read_json(scratch / path)['features'])
            if count != item['featureCount']:
                raise ValueError(f"随包承灾体数量不符：{item['name']}")
            record = {'id': identifier, 'name': item['name'], 'kind': item['kind'],
                      'bundleFile': item['file'], 'featureCount': count, 'coordinateSystem': 'EPSG:4326',
                      'source': f"{index['source']} / {index['license']} / 下载 {item['downloadedAtUTC']}",
                      'builtin': True, 'demo': True}
            exposures.append(record)
            store.save(staging, 'exposures', {**record, 'path': path})
        exposure_ids = [r['id'] for r in exposures]

        scenarios = []
        for original, label in SCENARIOS:
            scenario_id, result_id = 'DEMO-RISK-' + label, 'DEMO-RISK-RUN-' + label
            scenario = demo_record(risk.detail(source, original), id=scenario_id, originalScenarioId=original,
                                   resultId=result_id, exposureIds=exposure_ids)
            store.save(staging, risk.COLLECTION, scenario)
            folder = scratch / 'results' / result_id
            result = risk.run(staging, {'scenarioId': scenario_id, 'forecastId': FORECAST_ID}, folder)
            result = demo_record(result, id=result_id, scenario=scenario, path=f'results/{result_id}/inundation.npz')
            scenario['peakFrame'] = result['frameRows'].index(next(
                i for i, row in enumerate(result['rows']) if row['time'] == result['comparisons'][0]['peakTime']))
            result['peakFrame'] = scenario['peakFrame']
            target = output / 'results' / result_id
            copy_checked(folder / 'inundation.npz', target / 'inundation.npz')
            copy_checked(folder / 'risk-hourly.csv', target / 'risk-hourly.csv')
            with np.load(target / 'inundation.npz', allow_pickle=False) as archive:
                if list(archive['frameRows']) != result['frameRows'] or len(result['rows']) != forecast['hours']:
                    raise ValueError(f'漫溢回放与逐时预报不一致：{scenario["name"]}')
            write_json(target / '结果.json', result)
            scenarios.append(scenario)
            effects = result['comparisons'][0]['effects']
            print(f"{scenario['name']}：峰值帧 {scenario['peakFrame']}，承灾体 {effects['count']}，"
                  f"中断道路 {effects['roadLengthM']:.1f} m，过水道路 {effects['overtoppedRoadLengthM']:.1f} m，"
                  f"受影响居民点 {effects['settlementCount']}", flush=True)

    write_json(output / 'demo.json', {'forecast': forecast, 'weather': weather, 'history': history,
                                    'scenarios': scenarios, 'exposureIds': exposure_ids, 'exposures': exposures})


if __name__ == '__main__':
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='已核验的迁移工作区，不修改原资料')
    parser.add_argument('--output', required=True, help='新建的演示资料目录')
    parser.add_argument('--scratch', required=True, help='新建的计算中间资料目录')
    args = parser.parse_args()
    build(args.source, args.output, args.scratch)
