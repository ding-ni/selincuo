"""Check real dashboard data with the source frontend and read-only 8882 API."""
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright

run_id=str(time.time_ns())
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1366,'height':768})
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    def proxy(route):
        request=route.request
        if request.method!='GET':
            errors.append('blocked non-GET request: '+request.method+' '+request.url)
            route.abort()
            return
        response=route.fetch(url=request.url.replace('127.0.0.1:8895','127.0.0.1:8882'),timeout=120000)
        route.fulfill(response=response)
    page.route('**/api/**',proxy)
    page.route('**/spatial/**',proxy)
    page.goto('http://127.0.0.1:8895/?page=overview&result=JOB-88b35b44b27d4e15840c&date=2026-10-20',timeout=120000)
    page.wait_for_selector('.screen-processes [data-chart-context] canvas',timeout=120000)
    page.wait_for_timeout(2500)
    graphs=page.locator('.screen-processes [data-chart-context]')
    assert graphs.count()==2,graphs.count()
    dimensions=[]
    for i in range(2):
        graph=graphs.nth(i)
        canvas=graph.locator('canvas').first
        item=canvas.evaluate("el=>({width:el.width,height:el.height,css:el.getBoundingClientRect().toJSON()})")
        assert item['width']>100 and item['height']>30,item
        box=canvas.bounding_box()
        page.mouse.move(box['x']+box['width']*.55,box['y']+box['height']*.55)
        key=graph.get_attribute('data-chart-context')
        page.mouse.wheel(0,-700)
        page.wait_for_timeout(700)
        state=json.loads(page.evaluate("key=>localStorage.getItem('selinco:chart:'+key)",key))
        assert state['zoom'][1]-state['zoom'][0]<99,state
        dimensions.append({'key':key,'canvas':item,'zoom':state['zoom']})
        graph.locator('[data-chart-action="reset"]').click()
    page.screenshot(path=str(Path('build')/('大屏图表高度核验-'+run_id+'.png')),full_page=True)
    assert not errors,errors
    print(json.dumps({'graphs':dimensions,'pageErrors':errors,'screenshot':'build/大屏图表高度核验-'+run_id+'.png'},ensure_ascii=False))
    browser.close()
