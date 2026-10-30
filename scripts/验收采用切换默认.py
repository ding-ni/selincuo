"""用隔离浏览器模拟已采用成果切换；只读真实成果，不提交任务或变更采用关系。"""
import argparse
import json
import uuid
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:8882'
OLD = 'JOB-88b35b44b27d4e15840c'
NEW = 'JOB-05c3f62282ce4239aed1'
HISTORY = 'JOB-3354c8660e854cbfa96f'


def api(path):
    with urlopen(BASE + '/api/model/' + path, timeout=60) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected', choices=['stale', 'synchronized'], required=True)
    parser.add_argument('--preserve-history', action='store_true',
                        help='用真实非默认成果模拟用户正在查看的历史结果')
    args = parser.parse_args()
    bootstrap = api('bootstrap')
    assert bootstrap['homeResultId'] == OLD, '实际采用状态已改变，不能使用预设模拟场景'
    task = api('tasks/' + NEW)
    assert task['status'] == 'succeeded', '暖雨任务尚未成功'
    new_scheme_id = task['request']['researchSchemeId']
    new_scheme = next(row for row in api('schemes?includeExperiments=true')['schemes']
                      if row['id'] == new_scheme_id)
    fake_bootstrap = dict(bootstrap)
    fake_bootstrap['homeResultId'] = NEW
    fake_bootstrap['homeSchemeId'] = new_scheme_id
    fake_bootstrap['currentBusiness'] = {**bootstrap['currentBusiness'],
        'resultId': NEW, 'schemeId': new_scheme_id, 'name': '切换验收模拟方案',
        'inputId': task['result']['inputId']}
    fake_bootstrap['schemes'] = list(bootstrap['schemes'])
    if all(row['id'] != new_scheme_id for row in fake_bootstrap['schemes']):
        fake_bootstrap['schemes'].append(new_scheme)
    state = {'new': False}
    history = api('tasks/' + HISTORY) if args.preserve_history else None
    output = ROOT / 'build' / ('采用切换核验-' + args.expected + '-' + uuid.uuid4().hex[:10])
    output.mkdir(parents=True, exist_ok=False)
    errors = []
    requests = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1366, 'height': 768})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append({'method': request.method, 'url': request.url})
                if request.url.startswith(BASE + '/api/model/') else None)

        def mocked_bootstrap(route):
            if state['new']:
                route.fulfill(status=200, content_type='application/json',
                              body=json.dumps(fake_bootstrap, ensure_ascii=False))
            else:
                route.continue_()

        page.route('**/api/model/bootstrap', mocked_bootstrap)
        if history:
            page.route('**/api/model/tasks/' + OLD,
                       lambda route: route.fulfill(status=200, content_type='application/json',
                                                  body=json.dumps(history, ensure_ascii=False)))
        original_chart_id = HISTORY if history else OLD
        page.goto(BASE + '/?page=simulation', wait_until='networkidle', timeout=60000)
        page.locator('[data-chart-context="' + original_chart_id + ':lake"]').first.wait_for(timeout=60000)
        before_scheme = page.locator('select').first.input_value()
        before_chart = page.locator('[data-chart-context$=":lake"]').first.get_attribute('data-chart-context')
        page.screenshot(path=str(output / '切换前.png'), full_page=True)
        state['new'] = True
        page.get_by_role('button', name='刷新', exact=True).click()
        page.wait_for_function('(scheme) => document.querySelector("select")?.value === scheme',
                               arg=new_scheme_id, timeout=60000)
        expected_chart_id = HISTORY if history else (NEW if args.expected == 'synchronized' else OLD)
        if args.expected == 'synchronized':
            page.locator('[data-chart-context="' + expected_chart_id + ':lake"]').first.wait_for(timeout=60000)
        else:
            page.wait_for_timeout(1000)
        after_scheme = page.locator('select').first.input_value()
        after_chart = page.locator('[data-chart-context$=":lake"]').first.get_attribute('data-chart-context')
        page.screenshot(path=str(output / '切换后.png'), full_page=True)
        after_url = page.url
        browser.close()
    report = {'type': 'mock-adoption-with-real-result-read', 'at': datetime.now().isoformat(),
              'expected': args.expected, 'oldTaskId': OLD, 'newTaskId': NEW,
              'preservedHistoryTaskId': HISTORY if history else None,
              'scope': ('仅在隔离浏览器模拟采用切换和正在查看的非默认历史成果；未修改实际采用或提交模型任务。'
                        if history else '仅在隔离浏览器篡改bootstrap响应模拟采用切换；未修改实际采用或提交模型任务。'),
              'before': {'scheme': before_scheme, 'chart': before_chart},
              'after': {'scheme': after_scheme, 'chart': after_chart, 'url': after_url},
              'requests': requests, 'pageErrors': errors}
    with (output / '验收记录.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    assert not errors, errors
    assert before_chart == original_chart_id + ':lake'
    assert after_scheme == new_scheme_id
    assert after_chart == expected_chart_id + ':lake', report
    assert not any(item['method'] != 'GET' for item in requests), requests
    print(json.dumps({'output': str(output), 'beforeChart': before_chart,
                      'afterChart': after_chart, 'afterScheme': after_scheme,
                      'afterUrl': after_url}, ensure_ascii=False))


if __name__ == '__main__':
    main()
