"""核对隔离安装版的七日预报、湖泊水量平衡及本地报告下载。"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build'
SOURCE_RESULT = 'JOB-05c3f62282ce4239aed1'
LAKE_MODEL = 'LAKE-1eca494c917349eab34d'
DAYS = 7
HOURS = DAYS * 24


def require(condition, message):
    if not condition:
        raise ValueError(message)


class LocalRequests(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def service(port, workspace, version):
    require(1024 <= port <= 65535 and port != 8882, '请指定非8882的隔离验收端口')
    host = f'http://127.0.0.1:{port}'
    client = build_opener(ProxyHandler({}), LocalRequests())

    def call(path, payload=None, binary=False):
        require(path.startswith('/api/'), '仅请求本机平台API')
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = Request(host + path, data=body, headers={'Content-Type': 'application/json'})
        try:
            with client.open(request, timeout=180) as response:
                return (response.headers.get_content_type(), response.read()) if binary else json.load(response)
        except HTTPError as exc:
            message = exc.read().decode('utf-8', errors='replace')
            raise ValueError(f'{path}：HTTP {exc.code} {message}') from exc

    health = call('/api/health')
    require(health.get('projectId') == 'selinco' and health.get('version') == version
            and Path(health.get('workspace', '')).resolve() == workspace,
            '平台版本或工作区与指定的隔离安装任务不符')
    return call, health


def completed(call, task):
    deadline = time.monotonic() + 240
    while task['status'] in ('queued', 'running', 'stopping') and time.monotonic() < deadline:
        time.sleep(2)
        task = call('/api/model/tasks/' + task['id'])
    require(task['status'] == 'succeeded', f"{task['id']}：{task['status']}，{task.get('message')}")
    return task


def reference_workspace(output):
    """从本机业务库只读提取采用关系和正式驱动，在独立目录执行源码。"""
    from backend.common import WORKSPACE, within
    from backend.database import Database
    from backend.forecasts import source_context
    from backend.published_results import result_file

    checkpoint = json.loads(result_file(SOURCE_RESULT, 'final-state.json').read_text(encoding='utf-8-sig'))
    identifier = checkpoint['inputId']
    workspace = output / '源码对照工作区'
    db = Database(workspace)
    source_path = WORKSPACE / 'database/hydrology.sqlite3'
    with closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)) as source:
        rows = source.execute(
            "SELECT * FROM operational_records WHERE (collection='lake-models' AND id=? "
            "AND json_extract(payload,'$.status')='active') OR (collection='lake-curves' AND id=("
            "SELECT json_extract(payload,'$.curveId') FROM operational_records WHERE collection='lake-models' AND id=?))",
            (LAKE_MODEL, LAKE_MODEL)).fetchall()
        require(sum(row[0] == 'lake-models' for row in rows) == 1, '源码业务库缺少对应的正式湖泊模型')
        product = source.execute('SELECT * FROM input_products WHERE id=?', (identifier,)).fetchone()
        require(product is not None, '源码业务库缺少正式驱动')
    from backend.operational_store import initialize
    initialize(db)
    with db.connect() as target:
        target.executemany('INSERT INTO operational_records VALUES(?,?,?,?,?,?)', rows)
        target.execute('INSERT INTO input_products VALUES(?,?,?,?)', product)
    origin = within(WORKSPACE / 'inputs/products', identifier)
    destination = within(workspace / 'inputs/products', identifier)
    for relative in ('manifest.json', json.loads(product[-1])['data']):
        target = within(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(within(origin, relative), target)
    context = source_context(db, SOURCE_RESULT)
    require(context['lakeModel']['id'] == LAKE_MODEL, '源码初始状态未挂接正式湖泊方案')
    return db


def max_difference(compiled, reference, name, limit, np):
    require(len(compiled) == len(reference) == HOURS, f'{name}过程未覆盖逐时168小时')
    diff = float(np.max(np.abs(np.asarray(compiled) - np.asarray(reference))))
    require(np.isfinite(diff) and diff <= limit, f'{name}源码与安装值最大差异为{diff:g}，超出{limit:g}')
    return diff


def lake_audit(result, lake_coefficient, np):
    from backend.lake_curves import area, storage
    control, curve = result['control'], result['lakeCurve']
    level = np.r_[result['initialLakeLevel'], control['lakeLevel']]
    outflow, inflow = np.asarray(control['outflow']), np.asarray(control['inflow'])
    rain, evap = np.asarray(control['lakeRainMm']), np.asarray(control['lakeEvaporationMm'])
    require(np.max(outflow) > 0 and np.min(outflow) >= 0, '溃口应按正水头自然外泄')
    require(np.allclose(rain.reshape(DAYS, 24).sum(axis=1), 1., rtol=0, atol=1e-10),
            '日降水转换成小时过程后不守恒')
    require(np.allclose(evap.reshape(DAYS, 24).sum(axis=1), .5 * lake_coefficient, rtol=0, atol=1e-10),
            '湖面蒸发未使用正式湖泊方案的率定系数')
    river_total = sum(np.asarray(values) for values in control['rivers'].values())
    require(np.allclose(inflow, river_total, rtol=0, atol=1e-8), '杜加不应在扎加外再次计入总入湖')
    net_flux = (inflow - outflow) * 3600 + (rain - evap) * area(level[:-1], curve) / 1000
    volume = storage(level, curve)
    step_error = float(np.max(np.abs(np.diff(volume) - net_flux)))
    total_error = float(abs(volume[-1] - volume[0] - net_flux.sum()))
    require(np.isfinite(step_error) and step_error < .01 and total_error < .01,
            f'库容变化与逐时水量通量不闭合：{step_error:g} / {total_error:g} m³')
    diagnostics = result['diagnostics']
    land = max(row['landBalanceMaximumMm'] for row in diagnostics)
    route = max(row['routingBalanceMaximumM3'] for row in diagnostics)
    require(np.isfinite([land, route]).all() and land < 1e-6 and route < 1e-4, '产汇流水账不闭合')
    return {'landMaximumMm': land, 'routingMaximumM3': route,
            'lakeMaximumHourlyM3': step_error, 'lakeTotalM3': total_error}


def write_new(path, payload):
    with path.open('xb') as stream:
        stream.write(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--workspace', required=True, type=Path, help='隔离安装实际打开的工作区')
    parser.add_argument('--output', required=True, type=Path, help='同一build批次内的独立空目录')
    parser.add_argument('--reference', type=Path, help='同一build批次内已算好的源码预报结果')
    args = parser.parse_args()
    workspace, output = args.workspace.resolve(), args.output.resolve()
    require(BUILD in output.parents, '收据目录须位于本项目build中')
    batch_name = output.relative_to(BUILD).parts[0]
    batch = BUILD / batch_name
    local_verification = batch in workspace.parents
    ssd_verification = ROOT not in workspace.parents and batch_name.removeprefix('离线交付') in workspace.parts[:-1]
    require((local_verification or ssd_verification) and workspace not in output.parents
            and output not in workspace.parents and output != workspace,
            '验收仅限同一build批次内独立的安装工作区及收据目录')
    require(not output.exists(), '收据目录已存在，请使用新目录')
    sys.path.insert(0, str(ROOT))
    os.environ['NUMBA_CACHE_DIR'] = str(output / '源码编译缓存')
    from backend.common import VERSION
    call, health = service(args.port, workspace, VERSION)
    source_info = call('/api/model/forecasts/issue?' + urlencode({'sourceResultId': SOURCE_RESULT}))
    require((source_info.get('lakeModel') or {}).get('id') == LAKE_MODEL, '安装版未采用正式湖泊方案')
    issue = datetime.fromisoformat(source_info['stateTime'])
    forcing = {'name': '离线交付七日对照', 'sourceResultId': SOURCE_RESULT, 'issueTime': issue.isoformat(),
               'days': DAYS, 'daily': [{'date': (issue + timedelta(days=i)).date().isoformat(),
                                       'precipitationMm': 1., 'temperatureC': 2., 'evaporationMm': .5}
                                      for i in range(DAYS)]}
    if args.reference:
        reference_path = args.reference.resolve()
        require(batch in reference_path.parents, '源码预检结果须来自同一构建批次')
        reference = json.loads(reference_path.read_text(encoding='utf-8-sig'))
        reference_forcing = reference_path.parents[2] / '预报气象' / reference['inputId'] / '气象输入说明.json'
        weather_record = json.loads(reference_forcing.read_text(encoding='utf-8-sig'))
        require(weather_record['daily'] == forcing['daily'] and weather_record['issueTime'] == forcing['issueTime']
                and reference['sourceResultId'] == SOURCE_RESULT and reference['lakeModelId'] == LAKE_MODEL,
                '已有源码预检的起报和日表必须与安装情景一致')
    request = {'name': forcing['name'], 'sourceResultId': SOURCE_RESULT, 'issueTime': forcing['issueTime']}
    output.mkdir(parents=True, exist_ok=False)
    print('正在提交安装版七日预报', flush=True)
    weather = call('/api/model/forecast-inputs', forcing)
    payload = {**request, 'inputId': weather['id']}
    check = call('/api/model/forecasts/preflight', payload)
    require(check['ready'], str(check.get('deficiencies')))
    task = completed(call, call('/api/model/forecasts', payload))
    result = task['result']

    import numpy as np
    from backend.forecast_inputs import create_manual
    from backend.forecasts import run
    if not args.reference:
        print('正在从同一正式状态计算源码对照', flush=True)
        db = reference_workspace(output)
        reference_weather = create_manual(db, forcing)
        reference = run(db, {**request, 'inputId': reference_weather['id']})
    require(result['sourceResultId'] == SOURCE_RESULT and result['lakeModelId'] == reference['lakeModelId'] == LAKE_MODEL,
            '安装与源码预报未使用同一正式湖泊模型与历史状态')
    require(result['curveId'] == reference['curveId'] and result['outlet'] == reference['outlet'],
            '安装与源码的库容关系或溃口参数不同')
    require(result['hours'] == HOURS and result['memberCount'] == 1
            and result['initialLakeSource']['method'] == 'model-state'
            and result['stateAdvancement']['hours'] == 0, '预报未从冻结末状态直接完成七日逐时计算')
    expected = [(issue + timedelta(hours=i + 1)).isoformat() for i in range(HOURS)]
    require(expected == result['times'] == reference['times'], '逐时过程起止或时间间隔不同')
    limits = {'lakeLevel': 1e-9, 'dujaFlow': 1e-7, 'inflow': 1e-7, 'outflow': 1e-7,
              'lakeRainMm': 1e-10, 'lakeEvaporationMm': 1e-10}
    differences = {name: max_difference(result['control'][name], reference['control'][name], name, limit, np)
                   for name, limit in limits.items()}
    audit = lake_audit(result, source_info['lakeModel']['lakeCoefficient'], np)
    print('安装与源码逐时过程比对完成，开始生成日报', flush=True)
    report_task = completed(call, call('/api/model/reports', {'reportType': 'monitoring-daily', 'date': '2026-09-13'}))
    artifacts = report_task['result']['artifacts']
    report_json = next(artifact for artifact in artifacts if artifact['name'].endswith('.json'))
    report = call(report_json['downloadUrl'])
    require(report.get('date') == '2026-09-13'
            and any(row['station_id'] == 'SLC' and row['variable'] == 'level' for row in report['rows']),
            '日报未包含指定历史日期的有效主站实测水位')
    pdf = next(artifact for artifact in artifacts if artifact['name'].endswith('.pdf'))
    mime, content = call(pdf['downloadUrl'], binary=True)
    require(mime == 'application/pdf' and content.startswith(b'%PDF-') and content.rstrip().endswith(b'%%EOF')
            and len(content) == pdf['bytes'], '下载的日报不是完整 PDF')
    write_new(output / '监测日报.pdf', content)
    receipt = {'passed': True, 'version': VERSION, 'port': args.port, 'health': health,
               'sourceResultId': SOURCE_RESULT, 'lakeModelId': LAKE_MODEL, 'curveId': result['curveId'],
               'lakeEvaporationCoefficient': source_info['lakeModel']['lakeCoefficient'],
               'stateTime': issue.isoformat(), 'hours': HOURS, 'weatherId': weather['id'],
               'forecastTaskId': task['id'], 'forecastId': result['id'], 'forecastSummary': result['summary'],
               'referenceForecastId': reference['id'],
               'maximumDifferencesAgainstSource': differences, 'waterBalance': audit,
               'reportTaskId': report_task['id'], 'pdfBytes': len(content)}
    write_new(output / '离线业务验收.json', (json.dumps(receipt, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    print(json.dumps(receipt, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
