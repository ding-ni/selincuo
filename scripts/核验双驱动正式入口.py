"""只读核验双驱动任务采用后的方案配置和业务页面，不创建计算任务。"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
TASKS = ('JOB-6536f36e07cc46868255', 'JOB-05c3f62282ce4239aed1')
GROUPS = ('陆面产流', '积雪与冰川', '汇流', '湖泊')


def digest(value):
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def get_json(url):
    with urlopen(url, timeout=90) as response:
        return json.load(response)


def equal_number(actual, expected):
    return math.isclose(float(actual), float(expected), rel_tol=0., abs_tol=1e-12)


def previous_day(value):
    return (date.fromisoformat(value[:10]) - timedelta(days=1)).isoformat()


def check_configuration(base, expected):
    health = get_json(base + '/api/health')
    if health.get('projectId') != 'selinco':
        raise ValueError('8882不是色林错源码服务。')
    workspace = Path(health['workspace']).resolve()
    active = read_json(ROOT / 'config/active-workspace.json')['workspace']
    configured = Path(active)
    if not configured.is_absolute():
        configured = ROOT / configured
    if workspace != configured.resolve():
        raise ValueError('服务工作区与本机当前工作区不一致。')
    db_path = workspace / 'database/hydrology.sqlite3'
    with sqlite3.connect('file:' + db_path.as_posix() + '?mode=ro', uri=True, timeout=30) as db:
        statuses = dict(db.execute('SELECT id,status FROM tasks WHERE id IN (?,?)', TASKS))
    if any(statuses.get(task) != 'succeeded' for task in TASKS):
        raise ValueError('两项正式任务须均成功后再核验：' + json.dumps(statuses, ensure_ascii=False))

    folder = workspace / 'tasks' / expected
    request, result = read_json(folder / 'request.json'), read_json(folder / 'result.json')
    if result.get('taskId') != expected or request['inputId'] != result['inputId']:
        raise ValueError('任务请求与最终成果身份不一致。')
    bootstrap = get_json(base + '/api/model/bootstrap')
    current = bootstrap['currentBusiness']
    scheme = current['schemeId']
    identifier = request['calibrationConfigurationSHA256']
    with sqlite3.connect('file:' + db_path.as_posix() + '?mode=ro', uri=True, timeout=30) as db:
        row = db.execute('SELECT value FROM settings WHERE key=?',
                         ('calibration-configuration:' + scheme,)).fetchone()
    # Do not invoke the configuration GET until the read-only database check
    # proves it is already bound; otherwise that endpoint can create a binding.
    if not row or json.loads(row[0]) != identifier:
        raise ValueError('正式方案尚未绑定任务的原始率定配置。')
    config = get_json(base + '/api/model/schemes/' + scheme + '/calibration-configuration')
    recipe_path = workspace / 'calibration-configurations' / (identifier + '.json')
    recipe = read_json(recipe_path)['content']
    manifest = read_json(workspace / 'inputs/products' / request['inputId'] / 'manifest.json')
    form = config['form']
    frozen = recipe['request']
    option_keys = ('mode', 'reverseCheck', 'finalRefit', 'seeds', 'budgets', 'allowExtension',
                   'selectionMode', 'candidateLimit', 'jointBudget', 'lakePeriods', 'objective')
    option_equal = all(form['twoStage'].get(k) == request['twoStage'].get(k) ==
                       frozen['twoStage'].get(k) for k in option_keys)
    initial = request['parameters']
    final = result['parameters']
    parameter_names = {item['key'] for item in bootstrap['parameterDefinitions']}
    selected = request['search']['keys']
    checks = {
        'twoTasksSucceeded': all(statuses.get(task) == 'succeeded' for task in TASKS),
        'adoptedTask': bootstrap.get('homeResultId') == current.get('resultId') == expected,
        'adoptedScheme': bootstrap.get('homeSchemeId') == scheme == config['schemeId'],
        'adoptedInput': current.get('inputId') == config['inputId'] == request['inputId'],
        'adoptedSpatialVersion': current.get('spatialVersion') == request['spatialVersion'],
        'configuredSchemeVisible': any(row['id'] == scheme for row in bootstrap['schemes']),
        'configurationIdentity': config['configurationSHA256'] == identifier == digest(recipe),
        'implementationMatches': bool(config['matchesCurrentImplementation']) and
                                 config['implementation'] == recipe['implementation'] and
                                 request['_modelIdentity'] == recipe['implementation']['model'],
        'inputManifestIdentity': digest(manifest) == recipe['inputManifestSHA256'],
        'observationSnapshotIdentity': config['observationSHA256'] == digest(request['_observations']),
        'all25InitialParameters': len(parameter_names) == 25 and set(initial) == parameter_names and
                                  initial == config['initialParameters'] == form['calibrationInitialParameters'] ==
                                  frozen['parameters'],
        'all25FinalParameters': set(final) == parameter_names and form['parameters'] == final,
        'all22SelectedAndBounds': len(selected) == len(set(selected)) == 22 and
                                  form['search'] == request['search'] == frozen['search'],
        'periods': form['periods'] == request['periods'] == frozen['periods'],
        'initialSoil': form['initialHydrology'] == request['initialHydrology'] == frozen['initialHydrology'],
        'lakeInitialization': config['initialization'] == request['_fixedInitialization'] == frozen['_fixedInitialization'],
        'simulationRange': config['start'] == request['start'] and config['end'] == request['end'],
        'displayCoverage': result['displayCoverage']['start'][:10] <= '2021-04-23' and
                           result['displayCoverage']['endExclusive'][:10] >= '2026-10-21',
        'objectiveSearchAndLakeStages': option_equal,
        'sharedParameterTransfer': form['parameterTransfer'] == request['parameterTransfer'] ==
                                   frozen['parameterTransfer'],
        'routingMode': form['routingMode'] == request['routingMode'] == frozen['routingMode'],
    }
    bad = [name for name, passed in checks.items() if not passed]
    if bad:
        raise ValueError('正式配置核对不一致：' + ', '.join(bad))
    return {'health': health, 'bootstrap': bootstrap, 'config': config, 'request': request,
            'result': result, 'schemeId': scheme, 'configurationSHA256': identifier,
            'checks': checks, 'recipeFileSHA256': sha(recipe_path), 'statuses': statuses}


def check_parameters(page, config, request, result):
    page.locator('.calibration-steps button').nth(1).click()
    initial, final, bounds = request['parameters'], result['parameters'], request['search']['bounds']
    selected = set(request['search']['keys'])
    rows = []
    for group in GROUPS:
        page.locator('.parameter-group-tabs').get_by_role('button', name=group, exact=True).click()
        data = page.locator('.calibration-parameter-table tbody tr').evaluate_all('''rows => rows.map(row => {
          const cells = row.children, inputs = [...row.querySelectorAll('input[type="number"]')];
          const checkbox = row.querySelector('input[type="checkbox"]');
          return {key: cells[1].querySelector('small').textContent.split(' · ')[0].trim(),
                  initialDisplay: inputs[0].value, initialTitle: inputs[0].title,
                  lower: inputs[1].value, upper: inputs[2].value,
                  final: row.querySelector('.parameter-result').getAttribute('title'),
                  selected: checkbox ? checkbox.checked : null};
        })''')
        for index, row in enumerate(data):
            key = row['key']
            if key not in initial or key not in final or key not in bounds:
                raise ValueError('前端显示了未登记参数：' + key)
            initial_input = page.locator('.calibration-parameter-table tbody tr').nth(index).locator('input[type="number"]').first
            initial_input.focus()
            raw_initial = initial_input.input_value()
            initial_input.blur()
            if (float(raw_initial) != initial[key] or float(row['initialTitle']) != initial[key] or
                    float(row['initialDisplay']) != float(format(initial[key], '.8g')) or
                    initial_input.input_value() != row['initialDisplay']):
                raise ValueError('前端初值显示、聚焦原值或失焦显示不一致：' + key)
            expected_select = key in selected if key != 'lakeCoefficient' else None
            if not (equal_number(row['lower'], bounds[key][0]) and
                    equal_number(row['upper'], bounds[key][1]) and
                    equal_number(row['final'], final[key]) and row['selected'] == expected_select):
                raise ValueError('前端参数初值、范围、结果或参选状态不一致：' + key)
            rows.append({'key': key, 'group': group, 'initial': initial[key],
                         'initialDisplay': row['initialDisplay'], 'focusedInitial': raw_initial,
                         'bounds': bounds[key], 'final': final[key], 'selected': expected_select})
    keys = {row['key'] for row in rows}
    if len(rows) != 23 or keys != selected | {'lakeCoefficient'}:
        raise ValueError('常规率定表未完整显示22项水文参数和湖泊系数。')
    if not page.get_by_role('button', name='保存方案', exact=True).is_disabled():
        raise ValueError('仅查看和聚焦初值后，方案被错误标记为已修改。')
    return rows


def check_search_controls(page, request):
    page.locator('.calibration-steps button').nth(2).click()
    options = request['twoStage']
    objective = options['objective']
    controls = {
        '杜加流量目标权重': objective['weights']['flow'],
        '主站湖位目标权重': objective['weights']['lake'],
        '陆面产流搜索次数': options['budgets']['runoff'],
        '积雪与冰川搜索次数': options['budgets']['snow'],
        '汇流搜索次数': options['budgets']['routing'],
        '水文联合细化搜索次数': options['budgets']['joint'],
        '全流域代表候选 · 组': options['candidateLimit'],
        '流量—湖位联合评价 · 次': options['jointBudget'],
    }
    for label, expected in controls.items():
        if not equal_number(page.get_by_label(label, exact=True).input_value(), expected):
            raise ValueError('前端搜索设置不一致：' + label)
    if page.get_by_label('搜索仍改善时追加一轮，累计最多36000次', exact=True).is_checked() != options['allowExtension']:
        raise ValueError('前端追加搜索设置不一致。')
    page.get_by_text('目标函数分项与搜索编号', exact=True).click()
    weights = {
        '流量过程权重': objective['flowWeights']['nse'],
        '低流量过程权重': objective['flowWeights']['logNse'],
        '径流总量权重': objective['flowWeights']['volume'],
        '湖位过程权重': objective['lakeWeights']['rmse'],
        '累计趋势权重': objective['lakeWeights']['trend'],
        '季节涨幅权重': objective['lakeWeights']['amplitude'],
        '高点日期权重': objective['lakeWeights']['peakTiming'],
        '湖位误差尺度 · m': objective['lakeScales']['rmseM'],
        '趋势误差尺度 · m': objective['lakeScales']['trendErrorM'],
        '涨幅误差尺度 · m': objective['lakeScales']['amplitudeErrorM'],
        '高点误差尺度 · 天': objective['lakeScales']['peakTimingErrorDays'],
    }
    for label, expected in weights.items():
        if not equal_number(page.get_by_label(label, exact=True).input_value(), expected):
            raise ValueError('前端目标函数不一致：' + label)
    seeds = [int(x) for x in re.split(r'[,，\s]+',
             page.get_by_label('多起点随机编号', exact=True).input_value().strip()) if x]
    if seeds != options['seeds']:
        raise ValueError('前端搜索起点与任务不一致。')
    return {'controls': controls, 'objectiveDetails': weights, 'seeds': seeds,
            'allowExtension': options['allowExtension']}


def screenshot(page, folder, name):
    path = folder / (name + '.png')
    page.screenshot(path=str(path), full_page=True)
    return {'file': path.name, 'sha256': sha(path)}


def check_annual_table(page):
    table = page.locator('.result-evidence .table-wrap')
    table.wait_for()
    rows = table.locator('tbody tr')
    years = [row.strip() for row in rows.locator('td:first-child').all_inner_texts()]
    if years != [str(year) for year in range(2021, 2027)]:
        raise ValueError('模拟评价表未包含完整年份：' + repr(years))
    table.evaluate('(node) => { node.scrollTop = node.scrollHeight; }')
    scroll = table.evaluate('''node => ({top: node.scrollTop, height: node.clientHeight,
                                        contentHeight: node.scrollHeight})''')
    for year in ('2025', '2026'):
        row = rows.filter(has=page.locator('td:first-child', has_text=re.compile('^' + year + '$')))
        visible = row.evaluate('''node => {
          const row = node.getBoundingClientRect(), container = node.closest('.table-wrap').getBoundingClientRect();
          return row.top >= container.top - 1 && row.bottom <= container.bottom + 1;
        }''')
        if not visible:
            raise ValueError('模拟评价表滚动到底后该年份未完整显示：' + year)
    return {'years': years, 'lastTwoRowsFullyVisible': True, **scroll}


def check_pages(base, expected, context, output, report):
    request, result, config = context['request'], context['result'], context['config']
    current_name = context['bootstrap']['currentBusiness']['name']
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for width, height in ((1366, 768), (1920, 1080)):
                page = browser.new_page(viewport={'width': width, 'height': height}, device_scale_factor=1)
                page.set_default_timeout(90000)
                page.on('pageerror', lambda error: report['pageErrors'].append(str(error)))
                page.on('console', lambda message: report['consoleErrors'].append(message.text)
                        if message.type == 'error' else None)
                page.on('response', lambda response: report['apiErrors'].append(
                    {'status': response.status, 'url': response.url})
                    if response.status >= 400 and response.url.startswith(base + '/api/') else None)
                page.on('request', lambda item: report['nonGetRequests'].append(
                    {'method': item.method, 'url': item.url}) if item.method not in ('GET', 'HEAD') else None)
                try:
                    page.goto(base + '/?page=overview', wait_until='networkidle', timeout=90000)
                    page.locator('.screen-map[data-spatial-ready="true"]').wait_for()
                    page.locator(f'[data-chart-context="{expected}:model:lake"]').wait_for()
                    page.locator(f'[data-chart-context="{expected}:model:flow"]').wait_for()
                    select = page.get_by_label('当前成果', exact=True)
                    options = select.locator('option')
                    if options.count() != 1 or select.input_value() != expected:
                        raise ValueError('首页应只展示当前正式成果。')
                    if page.evaluate('document.documentElement.scrollWidth') > width + 1:
                        raise ValueError('湖情总览出现横向溢出：' + str(width))
                    report['screens'].append({'page': 'overview', 'viewport': [width, height],
                        'resultOptions': options.count(), 'selectedResult': select.input_value(),
                        'bundle': page.locator('script[type="module"]').get_attribute('src'),
                        'screenshot': screenshot(page, output, f'湖情总览-{width}x{height}')})

                    page.goto(base + '/?page=results', wait_until='networkidle', timeout=90000)
                    result_panel = page.get_by_role('heading', name='成果中心', exact=True).locator('xpath=ancestor::section[1]')
                    rows = result_panel.locator('tbody tr')
                    page.locator(f'.result-view [data-chart-context="{expected}:lake"]').wait_for()
                    if rows.count() != 1 or '当前成果' not in rows.first.inner_text():
                        raise ValueError('成果中心当前页未只显示正式成果。')
                    if page.evaluate('document.documentElement.scrollWidth') > width + 1:
                        raise ValueError('成果中心出现横向溢出：' + str(width))
                    result_image = screenshot(page, output, f'成果中心-{width}x{height}')
                    annual_table = check_annual_table(page)
                    report['screens'].append({'page': 'results', 'viewport': [width, height],
                        'resultRows': rows.count(), 'chartTaskId': expected,
                        'annualTable': annual_table, 'screenshot': result_image})

                    page.goto(base + '/?page=calibration', wait_until='networkidle', timeout=90000)
                    page.locator('.calibration-workbench[data-coverage-ready="true"]').wait_for()
                    page.wait_for_function('(name) => document.querySelector(".calibration-current b")?.textContent === name',
                                           arg=current_name)
                    # Loading the adopted task selects its result step. Wait for
                    # that asynchronous selection before returning to settings.
                    page.locator('.calibration-adopted').wait_for()
                    page.locator('.calibration-steps').get_by_role('button', name='资料与时段', exact=False).click()
                    if page.get_by_label('预热起日', exact=True).input_value() != request['periods']['warmupStart'][:10]:
                        raise ValueError('前端预热日期与任务不一致。')
                    lake_line = page.locator('.calibration-data-line').inner_text()
                    if (config['initialization']['time'] not in lake_line or
                            str(config['initialization']['level']) not in lake_line):
                        raise ValueError('前端湖位起算时间或水位不一致。')
                    for prefix, periods in [('流量', request['periods']), ('湖位', request['twoStage']['lakePeriods'])]:
                        for suffix, key in [('率定起日', 'trainingStart'), ('率定止日', 'trainingEnd'),
                                            ('检验起日', 'evaluationStart'), ('检验止日', 'evaluationEnd')]:
                            expected_day = previous_day(periods[key]) if key.endswith('End') else periods[key][:10]
                            if page.get_by_label(prefix + suffix, exact=True).input_value() != expected_day:
                                raise ValueError('前端资料时段不一致：' + prefix + suffix)
                    parameter_rows = check_parameters(page, config, request, result)
                    if page.evaluate('document.documentElement.scrollWidth') > width + 1:
                        raise ValueError('率定页面出现横向溢出：' + str(width))
                    parameter_image = screenshot(page, output, f'率定参数-{width}x{height}')
                    search_controls = check_search_controls(page, request)
                    report['screens'].append({'page': 'calibration', 'viewport': [width, height],
                        'schemeName': current_name, 'parameterRows': parameter_rows,
                        'searchControls': search_controls, 'parameterScreenshot': parameter_image,
                        'screenshot': screenshot(page, output, f'率定设置-{width}x{height}')})

                    page.goto(base + '/?page=simulation', wait_until='networkidle', timeout=90000)
                    page.locator(f'.result-view [data-chart-context="{expected}:lake"]').wait_for()
                    # The label also contains its option texts, so matching
                    # its whole accessible text exactly misses this select.
                    scheme_input = page.locator('.simulation-form label').filter(
                        has_text=re.compile(r'^模型方案')).locator('select')
                    if (scheme_input.input_value() != context['schemeId'] or
                            scheme_input.locator('option:not([disabled])').count() != 1):
                        raise ValueError('历史模拟默认方案与当前采用不同。')
                    simulation_image = screenshot(page, output, f'历史模拟-{width}x{height}')
                    annual_table = check_annual_table(page)
                    report['screens'].append({'page': 'simulation', 'viewport': [width, height],
                        'selectedScheme': scheme_input.input_value(), 'chartTaskId': expected,
                        'annualTable': annual_table, 'screenshot': simulation_image})
                    if page.evaluate('document.documentElement.scrollWidth') > width + 1:
                        raise ValueError('页面出现横向溢出：' + str(width))
                finally:
                    page.close()
        finally:
            browser.close()
    if report['pageErrors'] or report['consoleErrors'] or report['apiErrors'] or report['nonGetRequests']:
        raise ValueError('业务页面有脚本错误或非只读请求：' + json.dumps(report, ensure_ascii=False))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-task', required=True, choices=TASKS)
    parser.add_argument('--output', required=True, type=Path, help='新建的独立输出目录')
    parser.add_argument('--base', default='http://127.0.0.1:8882')
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output = output.resolve()
    if ROOT != output and ROOT not in output.parents:
        raise ValueError('验收输出必须位于项目工作区内。')
    context = check_configuration(args.base.rstrip('/'), args.expected_task)
    output.mkdir(parents=True, exist_ok=False)
    report = {'createdAt': datetime.now().astimezone().isoformat(timespec='seconds'),
              'scope': '正式方案、冻结配置和业务页面的只读核对，不提交率定或其他写入请求。',
              'taskId': args.expected_task, 'schemeId': context['schemeId'],
              'configurationSHA256': context['configurationSHA256'],
              'configurationFileSHA256': context['recipeFileSHA256'],
              'inputId': context['request']['inputId'],
              'health': context['health'], 'taskStatuses': context['statuses'],
              'configurationChecks': context['checks'],
              'coverage': context['result'].get('displayCoverage'),
              'pages': {'screens': [], 'pageErrors': [], 'consoleErrors': [],
                        'apiErrors': [], 'nonGetRequests': []}}
    try:
        check_pages(args.base.rstrip('/'), args.expected_task, context, output, report['pages'])
        # Read the persisted binding again after all focus/blur interactions.
        # The page must not mark a draft dirty or modify the recorded recipe.
        after = check_configuration(args.base.rstrip('/'), args.expected_task)
        report['unchangedAfterViewing'] = {
            'configurationSHA256': after['configurationSHA256'],
            'configurationFileSHA256': after['recipeFileSHA256'],
            'matchesBefore': (after['configurationSHA256'] == context['configurationSHA256'] and
                              after['recipeFileSHA256'] == context['recipeFileSHA256']),
        }
        if not report['unchangedAfterViewing']['matchesBefore']:
            raise ValueError('查看设置后率定配置摘要发生变化。')
        report['passed'] = True
    except Exception as error:
        report.update(passed=False, error=repr(error))
        raise
    finally:
        path = output / '正式入口核验.json'
        with path.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        print(json.dumps({'passed': report['passed'], 'report': str(path), 'sha256': sha(path),
                          'taskId': args.expected_task}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
