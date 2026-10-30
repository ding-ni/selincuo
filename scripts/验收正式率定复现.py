"""Exercise the visible replay controls without modifying the adopted result."""
import argparse
from datetime import datetime
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--resume', metavar='TASK_ID')
    parser.add_argument('--base', default='http://127.0.0.1:8882')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = root / 'build' / ('正式率定前端复现-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    folder.mkdir(parents=True, exist_ok=False)
    evidence = {'startedAt': datetime.now().isoformat(), 'base': args.base, 'screenshots': [], 'errors': []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1366, 'height': 768}, device_scale_factor=1)
        page.on('pageerror', lambda error: evidence['errors'].append(str(error)))
        page.goto(args.base + '/?page=calibration', wait_until='networkidle', timeout=60000)
        page.get_by_role('button', name='复现本次率定', exact=True).wait_for(timeout=60000)
        if args.resume:
            page.get_by_role('button', name='复现记录', exact=True).click()
            record = page.locator('.calibration-history button').filter(has_text='正式率定复现')
            record.wait_for(timeout=60000)
            with page.expect_response(lambda r: r.url.endswith('/tasks/' + args.resume), timeout=60000) as selected:
                record.click()
            assert selected.value.json()['id'] == args.resume
            with page.expect_response(lambda r: r.url.endswith('/tasks/' + args.resume + '/resume') and r.request.method == 'POST', timeout=120000) as resumed:
                page.get_by_role('button', name='继续本次复现', exact=True).click()
            assert resumed.value.ok, resumed.value.text()
            page.locator('.calibration-progress').wait_for(timeout=60000)
            evidence['resume'] = {'taskId': args.resume, 'status': resumed.value.status,
                                  'runningText': page.locator('.calibration-progress').inner_text()}
            screenshot = folder / '前端接续.png'
            page.screenshot(path=str(screenshot), full_page=True)
            evidence['screenshots'].append(str(screenshot.relative_to(root)))
            evidence['bootstrapAfterResume'] = page.request.get(args.base + '/api/model/bootstrap').json()['currentBusiness']
            assert evidence['bootstrapAfterResume']['resultId'] == 'JOB-88b35b44b27d4e15840c'
            assert not evidence['errors'], evidence['errors']
            with (folder / '前端接续收据.json').open('x', encoding='utf-8') as stream:
                json.dump(evidence, stream, ensure_ascii=False, indent=2)
            print(json.dumps({'report': str(folder / '前端接续收据.json'), 'resume': evidence['resume']}, ensure_ascii=False), flush=True)
            browser.close()
            return
        row = page.locator('[data-parameter="KC"]')
        evidence['historicalKc'] = row.inner_text()
        assert '0.72033723' in evidence['historicalKc'] and '0.71435399' in evidence['historicalKc']
        assert page.locator('.calibration-record-table tbody tr').count() == 25
        with page.expect_response(lambda r: r.url.endswith('/replay-plan'), timeout=120000) as received:
            page.get_by_role('button', name='复现本次率定', exact=True).click()
        plan = received.value.json()
        evidence['plan'] = plan
        assert received.value.ok and plan['available'], plan.get('missing')
        assert len(plan['configuration']['selectedKeys']) == 22
        assert plan['counts'] == {'rescored': 32569, 'eligible': 9861, 'selectedForBasin': 256,
                                  'jointProposals': 2000, 'basinCandidates': 773}
        dialog = page.get_by_role('dialog', name='复现本次率定', exact=True)
        dialog.wait_for()
        screenshot = folder / '冻结设置.png'
        page.screenshot(path=str(screenshot), full_page=True)
        evidence['screenshots'].append(str(screenshot.relative_to(root)))
        if args.start:
            with page.expect_response(lambda r: r.url.endswith('/replay') and r.request.method == 'POST',
                                      timeout=120000) as submitted:
                dialog.get_by_role('button', name='开始独立复现', exact=True).click()
            response = submitted.value
            task = response.json()
            evidence['submission'] = {'status': response.status, 'taskId': task.get('id'),
                                      'kind': task.get('kind'), 'request': response.request.post_data_json}
            assert response.ok and task['kind'] == 'calibration-replay', task
            assert task['request']['replay']['automaticAdoption'] is False
            page.locator('.calibration-progress').wait_for(timeout=60000)
            screenshot = folder / '独立复现运行.png'
            page.screenshot(path=str(screenshot), full_page=True)
            evidence['screenshots'].append(str(screenshot.relative_to(root)))
            evidence['runningText'] = page.locator('.calibration-progress').inner_text()
            evidence['bootstrapAfterStart'] = page.request.get(args.base + '/api/model/bootstrap').json()['currentBusiness']
            assert evidence['bootstrapAfterStart']['resultId'] == 'JOB-88b35b44b27d4e15840c'
        assert not evidence['errors'], evidence['errors']
        browser.close()
    path = folder / '前端启动收据.json'
    with path.open('x', encoding='utf-8') as stream:
        json.dump(evidence, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'report': str(path), 'submission': evidence.get('submission')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
