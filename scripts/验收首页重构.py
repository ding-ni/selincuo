"""Focused browser acceptance; no numerical model is submitted or rerun."""
import argparse
import json
import math
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def snapshot(page):
    return page.evaluate("""() => ({
      day:document.querySelector('.business-date b').textContent,
      cards:[...document.querySelectorAll('.metric-card > b')].map(e=>e.textContent),
      chartDays:[...document.querySelectorAll('.screen-processes .chart')].map(e=>e.dataset.day),
      fills:[...document.querySelectorAll('.rain-unit')].map(e=>e.getAttribute('fill'))
    })""")


def await_day(page, day):
    page.wait_for_function("""d => document.querySelector('.business-date b')?.textContent===d
      && document.querySelector('.lake-screen')?.getAttribute('aria-busy')==='false'""", arg=day)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8882')
    parser.add_argument('--output', default=str(ROOT / 'build' / ('首页最终验收-' + uuid.uuid4().hex[:12])))
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    report = {'url': args.url, 'screens': [], 'checks': {}, 'errors': [], 'httpErrors': []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1920, 'height': 1080}, device_scale_factor=1)
        page.set_default_timeout(90000)
        page.on('pageerror', lambda e: report['errors'].append(str(e)))
        page.on('response', lambda r: report['httpErrors'].append({'status': r.status, 'url': r.url}) if r.status >= 400 else None)
        page.goto(args.url, wait_until='networkidle', timeout=90000)
        page.locator('.lake-screen[aria-busy="false"] .screen-metrics').wait_for()
        result_id = page.get_by_label('当前成果').input_value()
        saved = page.request.get(args.url + '/api/model/tasks/' + result_id).json()['result']
        daily = {r['date'][:10]: r for r in saved['daily']}
        report['resultId'] = result_id
        report['health'] = page.request.get(args.url + '/api/health').json()

        def consistent():
            frame = snapshot(page)
            assert frame['chartDays'] == [frame['day'], frame['day']], frame
            row = daily[frame['day']]
            for index, key, precision in [(0, 'lakeLevel', 3), (2, 'total', 2)]:
                if row.get(key) is None:
                    assert frame['cards'][index].startswith('—'), frame
                else:
                    text = frame['cards'][index].replace('m³/s', '').replace('m', '').replace(',', '')
                    assert math.isclose(float(text), row[key], abs_tol=0.51 * 10 ** -precision), frame
            return frame

        for width, height in [(1920, 1080), (1366, 768)]:
            page.set_viewport_size({'width': width, 'height': height})
            page.wait_for_timeout(350)
            geometry = page.evaluate("""() => ({width:innerWidth,height:innerHeight,
              scrollWidth:document.documentElement.scrollWidth,scrollHeight:document.documentElement.scrollHeight,
              map:document.querySelector('.map-stage').getBoundingClientRect().toJSON(),
              replay:document.querySelector('.screen-replay').getBoundingClientRect().toJSON(),
              summary:document.querySelector('.summary-stack').getBoundingClientRect().toJSON(),
              balance:document.querySelector('.balance-summary').getBoundingClientRect().toJSON()})""")
            page.screenshot(path=str(output / f'首页-{width}.png'), full_page=True)
            assert geometry['scrollWidth'] <= width + 1, geometry
            assert geometry['scrollHeight'] <= height + 1, geometry
            assert geometry['balance']['bottom'] <= geometry['summary']['bottom'] + 1, geometry
            report['screens'].append(geometry)
        report['checks']['oneScreenAtBothSizes'] = True
        page.set_viewport_size({'width': 1920, 'height': 1080})
        theme = page.get_by_label('地图主题')
        theme.select_option('precipitation')
        page.locator('.rain-unit').first.wait_for()
        page.locator('.lake-screen[aria-busy="false"]').wait_for()
        assert page.locator('.rain-unit').count() == len(saved['units'])
        assert '含湖面订正' in page.locator('.screen-map-legend').inner_text()
        consistent()
        page.screenshot(path=str(output / '模型日降水.png'))
        report['checks']['modelRainUsesActualUnits'] = True

        held, requests = {}, []
        hold_days = {'2024-06-11', '2024-09-23'}
        failure = {'armed': True}

        def gate(route):
            day = parse_qs(urlsplit(route.request.url).query).get('day', [''])[0]
            if day in hold_days:
                held.setdefault(day, []).append(route)
            elif day == '2024-11-07' and failure['armed']:
                failure['armed'] = False
                route.abort('failed')
            else:
                route.continue_()

        page.route('**/api/model/map-frame?**', gate)
        date_input = page.get_by_label('选择回放日期')
        before = consistent()
        date_input.fill('2024-06-11')
        page.wait_for_function("document.querySelector('.lake-screen').getAttribute('aria-busy')==='true'")
        page.wait_for_timeout(250)
        assert snapshot(page) == before
        assert held.get('2024-06-11')
        hold_days.remove('2024-06-11')
        for route in held.pop('2024-06-11'): route.continue_()
        await_day(page, '2024-06-11')
        consistent()
        report['checks']['delayedRequestPreservesWholeFrame'] = True

        date_input.fill('2024-09-23')
        page.wait_for_timeout(200)
        assert held.get('2024-09-23')
        date_input.fill('2025-08-19')
        await_day(page, '2025-08-19')
        newest = consistent()
        hold_days.remove('2024-09-23')
        for route in held.pop('2024-09-23'): route.continue_()
        page.wait_for_timeout(300)
        assert snapshot(page) == newest
        report['checks']['lateResponseCannotReplaceLatestDay'] = True

        date_input.fill('2024-11-07')
        page.locator('.frame-error').wait_for()
        assert snapshot(page) == newest
        assert '资料不足' not in page.locator('.frame-error').inner_text()
        page.locator('.frame-error').get_by_role('button', name='重试').click()
        await_day(page, '2024-11-07')
        consistent()
        report['checks']['failedRequestPreservesFrameAndRetries'] = True

        page.get_by_label('播放速度').select_option('600')
        page.get_by_role('button', name='播放', exact=True).click()
        days = set()
        for _ in range(38):
            page.wait_for_timeout(100)
            days.add(consistent()['day'])
        page.get_by_role('button', name='暂停', exact=True).click()
        assert len(days) >= 3, days
        report['checks']['playbackWholeFrames'] = sorted(days)

        theme.select_option('terrain')
        page.locator('image.terrain-image').wait_for()
        page.locator('.lake-screen[aria-busy="false"]').wait_for()
        assert '已登记1 km DEM' in page.locator('.screen-map-legend').inner_text()
        assert page.locator('.basin').first.evaluate("e=>getComputedStyle(e).fill") in ('none', 'rgba(0, 0, 0, 0)', 'transparent')
        page.screenshot(path=str(output / '地形高程.png'))
        theme.select_option('snow')
        page.locator('.snow-unit').first.wait_for()
        assert page.locator('.snow-unit').count() == len(saved['units'])
        page.screenshot(path=str(output / '日末雪水当量.png'))
        theme.select_option('basin')
        page.locator('.basin-map-key').wait_for()
        page.locator('.basin-map-key button.fringe').click()
        selected = page.locator('.basin-selection.fringe')
        selected.wait_for()
        assert selected.get_attribute('d') == page.locator('.basin[data-basin="fringe"]').get_attribute('d')
        assert selected.get_attribute('fill-rule') == 'evenodd'
        assert selected.evaluate('e=>getComputedStyle(e).stroke') == 'none'
        report['checks']['terrainSnowAndExactFringeGeometry'] = True

        page.get_by_role('button', name='站点监测', exact=True).click()
        page.locator('.monitor-clock').wait_for()
        page.wait_for_load_state('networkidle')
        page.on('request', lambda r: requests.append(r.url))
        assert page.get_by_label('当前成果').count() == 0
        assert page.locator('.inflow-summary').count() == 0
        assert page.locator('.balance-summary').count() == 0
        page.get_by_label('选择回放日期').fill('2021-04-23')
        await_day(page, '2021-04-23')
        theme.select_option('terrain')
        page.wait_for_timeout(300)
        assert not any(any(x in url for x in ('map-frame?', 'snow-frame?', 'map-range?', 'result-capabilities?')) for url in requests), requests
        page.screenshot(path=str(output / '站点监测.png'))
        report['checks']['monitoringIndependentOfSimulation'] = True
        assert not report['errors'], report['errors']
        assert not report['httpErrors'], report['httpErrors']
        browser.close()
    with (output / '验收结果.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'output': str(output), 'checks': report['checks'], 'errors': report['errors']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
