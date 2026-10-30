"""Exercise a local isolated chart fixture; does not access platform APIs."""
from pathlib import Path
import json
from playwright.sync_api import sync_playwright

URL = 'http://127.0.0.1:8895/图表交互检查.html'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1366,'height':768})
    failures=[]
    page.on('pageerror',lambda error:failures.append(str(error)))
    def geometry(route):
        filename=route.request.url.split('/spatial/',1)[1].split('?',1)[0]
        source=Path('data/derived/spatial')/filename
        if source.is_file():route.fulfill(status=200,body=source.read_bytes(),content_type='application/json')
        else:route.fulfill(status=404,body='')
    page.route('**/spatial/**',geometry)
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    graph=page.locator('[data-chart-context="test:chart-a"]').first
    other=page.locator('[data-chart-context="test:chart-b"]').first
    assert graph.locator('[data-selected-day]').inner_text().startswith('日期 2024-01-21')
    assert '模拟 4,540.2 m' in graph.locator('[data-selected-day]').inner_text()
    assert '实测 缺测' in graph.locator('[data-selected-day]').inner_text()
    plot=graph.locator('.chart').bounding_box()
    page.mouse.move(plot['x']+plot['width']*.55,plot['y']+plot['height']*.55)
    page.mouse.wheel(0,-700)
    page.wait_for_timeout(600)
    first=json.loads(page.evaluate("localStorage.getItem('selinco:chart:test:chart-a')"))
    assert first['zoom'][1]-first['zoom'][0]<99,first
    assert page.evaluate("localStorage.getItem('selinco:chart:test:chart-b')") is None
    assert other.locator('[data-selected-day]').inner_text().startswith('日期 2024-01-21')
    graph.locator('[data-chart-action="reset"]').click()
    page.wait_for_timeout(250)
    page.mouse.move(plot['x']+plot['width']*.50,plot['y']+plot['height']*.50)
    page.mouse.down()
    page.mouse.move(plot['x']+plot['width']*.57,plot['y']+plot['height']*.50,steps=8)
    page.mouse.up()
    graph.locator('[data-chart-action="box"]').click()
    page.mouse.move(plot['x']+plot['width']*.28,plot['y']+plot['height']*.30)
    page.mouse.down()
    page.mouse.move(plot['x']+plot['width']*.72,plot['y']+plot['height']*.74,steps=12)
    page.mouse.up()
    page.wait_for_timeout(700)
    box=json.loads(page.evaluate("localStorage.getItem('selinco:chart:test:chart-a')"))
    assert box['zoom'][1]-box['zoom'][0]<99,box
    assert box['yRange'][0]<box['yRange'][1],box
    assert graph.locator('[data-selected-day]').inner_text().startswith('日期 2024-01-21')
    assert graph.locator('[data-chart-action="locate"]').count()==1
    before_width=box['zoom'][1]-box['zoom'][0]
    graph.locator('[data-chart-action="locate"]').click()
    located=json.loads(page.evaluate("localStorage.getItem('selinco:chart:test:chart-a')"))
    assert abs((located['zoom'][1]-located['zoom'][0])-before_width)<.01,located
    assert not graph.locator('[data-chart-action="locate"]').count()
    if graph.locator('[data-chart-action="box"]').get_attribute('aria-pressed')=='true':
        graph.locator('[data-chart-action="box"]').click()
    graph.locator('[data-chart-action="expand"]').click()
    dialog=page.locator('dialog.chart-dialog[open]')
    assert dialog.count()==1
    dialog.locator('[data-chart-action="reset"]').click()
    dialog.get_by_role('button',name='关闭放大图').click()
    page.wait_for_timeout(250)
    assert not graph.locator('[data-chart-action="locate"]').count()
    restored=json.loads(page.evaluate("localStorage.getItem('selinco:chart:test:chart-a')"))
    assert restored['zoom']==[0,100] and restored['yRange'] is None,restored
    assert page.locator('.monitoring-trend-pixel').count()>0
    page.get_by_role('button',name='放大测试站实测过程').click()
    station=page.locator('dialog.monitoring-dialog[open]')
    assert station.count()==1 and '缺测' in station.inner_text()
    details=station.locator('.chart').evaluate("el=>({width:el.clientWidth,height:el.clientHeight})")
    assert details['width']>300 and details['height']>100,details
    station.get_by_role('button',name='关闭站点放大图').click()
    page.wait_for_selector('.screen-map[data-spatial-ready="true"]')
    assert page.locator('.thematic-grid .grid-cell').count()==9
    assert page.locator('.screen-geo.lake').evaluate("el=>getComputedStyle(el).fill")=='none'
    assert page.locator('[data-label="lake"]').get_attribute('data-anchor')=='89.05,31.759498'
    assert '0.1°格网原值' in page.locator('.screen-map-legend').inner_text()
    cell=page.locator('.thematic-grid .grid-cell[data-grid="grid-4"]')
    cell.dispatch_event('click')
    assert page.locator('#map-selection').inner_text()=='grid-4:5'
    assert not failures,failures
    print(json.dumps({'wheel':first['zoom'],'box':box['zoom'],'boxY':box['yRange'],'locate':located['zoom'],'restored':restored['zoom'],
                      'isolatedPoints':page.locator('.monitoring-trend-pixel').count(),'gridCells':9,'mapClick':'grid-4:5','errors':failures},ensure_ascii=False))
    browser.close()
