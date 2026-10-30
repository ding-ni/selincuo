"""Check this round's visible controls against the running source platform."""
import argparse
from datetime import datetime
import json
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:8882')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = root / 'build' / ('正式复现与交互验收-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    folder.mkdir(parents=True, exist_ok=False)
    evidence = {'startedAt': datetime.now().isoformat(), 'base': args.base,
                'screenshots': [], 'layouts': [], 'errors': []}
    adopted = 'JOB-88b35b44b27d4e15840c'

    def write_report():
        with (folder / '界面验收.json').open('w', encoding='utf-8') as stream:
            json.dump(evidence, stream, ensure_ascii=False, indent=2)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1366, 'height': 768}, device_scale_factor=1)
        page.on('pageerror', lambda error: evidence['errors'].append(str(error)))
        page.goto(args.base + '/?page=overview', wait_until='networkidle', timeout=60000)
        page.locator('[data-chart-context]').first.wait_for(timeout=60000)
        assert page.get_by_label('当前成果', exact=True).input_value() == adopted
        graph = page.locator('[data-chart-context="' + adopted + ':model:lake"]').first
        other = page.locator('[data-chart-context="' + adopted + ':model:flow"]').first
        key = 'selinco:chart:' + adopted + ':model:lake'
        other_key = 'selinco:chart:' + adopted + ':model:flow'

        def saved(chart_key=key):
            return page.evaluate('(key) => JSON.parse(localStorage.getItem(key))', chart_key)

        def choose_day(value):
            page.get_by_label('选择回放日期', exact=True).fill(value)
            page.wait_for_function('(day) => document.querySelector(".business-date b")?.textContent === day', arg=value,
                                   timeout=60000)
            page.wait_for_function('document.querySelector(".lake-screen")?.getAttribute("aria-busy") === "false"', timeout=60000)

        graph.locator('[data-chart-action="reset"]').click()
        other.locator('[data-chart-action="reset"]').click()
        choose_day('2025-07-15')
        evidence['selectedReadings'] = [graph.locator('[data-selected-day]').inner_text(),
                                         other.locator('[data-selected-day]').inner_text()]
        plot = graph.locator('.chart').bounding_box()
        page.mouse.move(plot['x'] + plot['width'] * .65, plot['y'] + plot['height'] * .5)
        page.mouse.wheel(0, -900)
        page.wait_for_timeout(500)
        zoomed = saved()
        assert zoomed['zoom'][1] - zoomed['zoom'][0] < 99, zoomed
        assert saved(other_key)['zoom'] == [0, 100]
        page.mouse.down()
        page.mouse.move(plot['x'] + plot['width'] * .75, plot['y'] + plot['height'] * .5, steps=10)
        page.mouse.up()
        page.wait_for_timeout(300)
        panned = saved()
        assert panned['zoom'] != zoomed['zoom'], (zoomed, panned)
        outside_day = '2021-04-24' if panned['zoom'][0] > .2 else '2026-10-20'
        choose_day(outside_day)
        assert saved()['zoom'] == panned['zoom']
        assert graph.locator('[data-chart-action="locate"]').count() == 1
        assert '当前范围外' in graph.locator('[data-selected-day]').inner_text()
        graph.locator('[data-chart-action="locate"]').click()
        assert graph.locator('[data-chart-action="locate"]').count() == 0
        located = saved()
        graph.locator('[data-chart-action="reset"]').click()
        graph.locator('[data-chart-action="box"]').click()
        plot = graph.locator('.chart').bounding_box()
        page.mouse.move(plot['x'] + plot['width'] * .25, plot['y'] + plot['height'] * .35)
        page.mouse.down()
        page.mouse.move(plot['x'] + plot['width'] * .7, plot['y'] + plot['height'] * .75, steps=12)
        page.mouse.up()
        page.wait_for_timeout(250)
        boxed = saved()
        assert boxed['yRange'][0] < boxed['yRange'][1]
        assert boxed['zoom'][1] - boxed['zoom'][0] < 99
        graph.locator('[data-chart-action="expand"]').click()
        dialog = page.locator('dialog.chart-dialog[open]')
        assert dialog.count() == 1
        dialog.locator('[data-chart-action="reset"]').click()
        dialog.get_by_role('button', name='关闭放大图', exact=True).click()
        assert saved()['zoom'] == [0, 100] and saved()['yRange'] is None
        assert graph.locator('[data-chart-action="locate"]').count() == 0
        evidence['chartInteraction'] = {'wheel': zoomed, 'pan': panned, 'locate': located,
                                        'box': boxed, 'restored': saved(), 'other': saved(other_key)}

        choose_day('2025-07-15')
        page.get_by_label('地图主题', exact=True).select_option('terrain')
        page.wait_for_timeout(500)
        page.get_by_label('地图主题', exact=True).select_option('precipitation')
        page.wait_for_selector('.thematic-grid [data-grid]', timeout=60000)
        evidence['rainGrid'] = page.locator('.thematic-grid').evaluate('g => ({count:g.querySelectorAll("[data-grid]").length, values:[...g.querySelectorAll("[data-grid]")].slice(0,8).map(x=>({id:x.dataset.grid,value:x.dataset.value}))})')
        assert evidence['rainGrid']['count'] > 400
        anchors = page.locator('[data-label]').evaluate_all('items => items.map(x=>({id:x.dataset.label,anchor:x.dataset.anchor,text:x.querySelector("text").outerHTML}))')
        page.get_by_text('叠加要素', exact=True).click()
        page.get_by_label('测站', exact=True).uncheck()
        hidden = page.locator('[data-label]').evaluate_all('items => items.map(x=>({id:x.dataset.label,anchor:x.dataset.anchor,text:x.querySelector("text").outerHTML}))')
        assert anchors == hidden
        assert next(a for a in anchors if a['id'] == 'lake')['anchor'] == '89.05,31.759498'
        page.get_by_label('测站', exact=True).check()
        page.get_by_text('叠加要素', exact=True).click()
        evidence['fixedNames'] = anchors
        page.get_by_label('地图主题', exact=True).select_option('basin')

        for width, height in [(1366, 768), (1920, 1080)]:
            page.set_viewport_size({'width': width, 'height': height})
            for mode in ['模拟回放', '站点监测']:
                page.get_by_role('button', name=mode, exact=True).click()
                page.wait_for_timeout(600)
                if mode == '站点监测':
                    page.get_by_label('湖位近期过程时长', exact=True).select_option('180')
                    page.wait_for_timeout(200)
                    points = page.locator('.monitoring-trend-pixel')
                    evidence.setdefault('monitoring', {})['pointDimensions'] = points.evaluate_all('items=>items.slice(0,12).map(x=>({width:x.getBoundingClientRect().width,height:x.getBoundingClientRect().height}))')
                    assert all(abs(v['width'] - v['height']) < .1 and v['width'] <= 5 for v in evidence['monitoring']['pointDimensions'])
                    enlarge = page.get_by_role('button', name='放大色林错主站实测过程', exact=True)
                    if enlarge.count() == 0:
                        enlarge = page.locator('.monitoring-main [aria-label^="放大"]').first
                    enlarge.click()
                    station_dialog = page.locator('dialog.monitoring-dialog[open]')
                    assert station_dialog.count() == 1
                    station_dialog.get_by_role('button', name='关闭站点放大图', exact=True).click()
                for fullscreen in [False, True]:
                    if fullscreen:
                        page.get_by_role('button', name='全屏展示', exact=True).click()
                        page.wait_for_timeout(400)
                    tag = ('模拟' if mode == '模拟回放' else '监测') + ('全屏' if fullscreen else '普通') + str(width)
                    screenshot = folder / (tag + '.png')
                    page.screenshot(path=str(screenshot), full_page=True)
                    evidence['screenshots'].append(str(screenshot.relative_to(root)))
                    geometry = page.locator('.lake-screen').evaluate('element=>({x:element.getBoundingClientRect().x,y:element.getBoundingClientRect().y,width:element.getBoundingClientRect().width,height:element.getBoundingClientRect().height,scrollWidth:element.scrollWidth,scrollHeight:element.scrollHeight})')
                    assert geometry['scrollWidth'] <= geometry['width'] + 2, geometry
                    evidence['layouts'].append({'mode': tag, 'viewport': [width, height], 'geometry': geometry})
                    if fullscreen:
                        page.get_by_role('button', name='退出全屏', exact=True).click()
                        page.wait_for_timeout(150)

        page.goto(args.base + '/?page=calibration', wait_until='networkidle', timeout=60000)
        source = page.request.get(args.base + '/api/model/tasks/' + adopted).json()
        page.get_by_role('button', name='我的计算记录', exact=True).click()
        page.locator('.calibration-history button').filter(has_text=source['name']).click()
        page.get_by_role('button', name='复现本次率定', exact=True).wait_for(timeout=60000)
        assert page.locator('.calibration-record-table tbody tr').count() == 25
        original_kc = page.locator('[data-parameter="KC"]').inner_text()
        assert '0.72033723' in original_kc and '0.71435399' in original_kc
        with page.expect_download() as downloaded:
            page.get_by_role('button', name='导出本次设置', exact=True).click()
        download = downloaded.value
        download.save_as(str(folder / download.suggested_filename))
        exported = json.loads((folder / download.suggested_filename).read_text(encoding='utf-8'))
        assert len(exported['parameters']) == 25
        assert exported['counts']['rescored'] == 32569
        assert exported['counts']['jointProposals'] == 2000
        assert exported['counts']['basinCandidates'] == 773
        assert exported['objectiveSpec']['flowWeights'] == {'nse': .5, 'logNse': .25, 'volume': .25}
        assert exported['initialHydrology']['soilFraction'] == .5
        assert exported['initialization']['time'] == '2021-04-23T12:00:00'
        evidence['historicalSettings'] = {'taskId': exported['taskId'], 'kc': original_kc,
                                          'count': len(exported['parameters']), 'counts': exported['counts']}
        screenshot = folder / '本次率定记录.png'
        page.screenshot(path=str(screenshot), full_page=True)
        evidence['screenshots'].append(str(screenshot.relative_to(root)))
        page.goto(args.base + '/?page=model', wait_until='networkidle', timeout=60000)
        page.get_by_role('button', name='观测资料', exact=True).click()
        page.get_by_text('站点降水的用途', exact=True).click()
        report = page.get_by_role('link', name='下载降水订正对照', exact=True)
        report.wait_for(timeout=60000)
        response = page.request.get(urljoin(args.base, report.get_attribute('href')))
        assert response.ok
        evidence['rainReportDownload'] = {'url': report.get_attribute('href'), 'status': response.status,
                                          'bytes': len(response.body())}
        boot = page.request.get(args.base + '/api/model/bootstrap').json()
        assert boot['currentBusiness']['resultId'] == adopted
        assert not evidence['errors'], evidence['errors']
        evidence['adoptedAfter'] = boot['currentBusiness']
        browser.close()
    write_report()
    print(json.dumps({'report': str(folder / '界面验收.json'), 'screenshots': len(evidence['screenshots']),
                      'errors': evidence['errors']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
