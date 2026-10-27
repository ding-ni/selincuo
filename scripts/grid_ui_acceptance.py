# -*- coding: utf-8 -*-
"""Actual pages, responsive themes, replay and durable chart interactions."""
import json,time
from pathlib import Path
from playwright.sync_api import sync_playwright,TimeoutError as BrowserTimeout
root=Path(__file__).resolve().parents[1];folder=root/'build/grid-revision-20261026';out=folder/'ui';out.mkdir(exist_ok=True)
baseline=json.loads((folder/'baseline-evidence.json').read_text(encoding='utf-8'))['taskId']
evidence={'screens':[],'checks':[],'errors':[]}
def rendered(page):
    try:page.wait_for_load_state('networkidle')
    except BrowserTimeout:
        page.locator('.screen-result-choice select').wait_for()
    page.locator('.lake-level-card + .screen-panel .chart').wait_for()
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':900},device_scale_factor=1)
    page.set_default_timeout(60000);page.on('pageerror',lambda e:evidence['errors'].append(str(e)))
    page.on('pageerror',lambda e:print('PAGE ERROR: '+str(e),flush=True))
    page.set_default_navigation_timeout(60000)
    page.goto('http://127.0.0.1:5175/');rendered(page)
    page.locator('.screen-result-choice select').select_option(baseline)
    page.locator('.screen-provenance').get_by_text('逐格网 · 连续湖位',exact=False).wait_for()
    page.locator('[data-basin="unresolved"]').first.wait_for()
    assert page.locator('.meteorological-grid path').count()==535
    evidence['checks'].append('535 real meteorological grids and unresolved drainage layer visible')
    for width,height in ((1920,1080),(1440,900),(1366,768),(768,1000)):
        page.set_viewport_size({'width':width,'height':height})
        for dark in (False,True):
            active=page.locator('.lake-screen').evaluate("e=>e.classList.contains('screen-dark')")
            if active!=dark:page.get_by_role('button',name='汇报深色' if dark else '日常浅色',exact=True).click()
            page.screenshot(path=str(out/f'grid-{width}-{"dark" if dark else "light"}.png'),full_page=True)
            sizes=page.locator('.map-stage').evaluate('(e)=>({w:e.clientWidth,h:e.clientHeight,overflow:document.documentElement.scrollWidth>innerWidth})')
            assert not sizes['overflow'];evidence['screens'].append({'width':width,'height':height,'dark':dark,**sizes})
            if width>=1051:assert 1.25<=sizes['w']/sizes['h']<=1.45
    page.set_viewport_size({'width':1440,'height':900})
    page.get_by_role('button',name='湖区',exact=True).click();page.get_by_role('button',name='放大地图',exact=True).click()
    transform=page.locator('.map-world').get_attribute('transform')
    page.set_viewport_size({'width':1366,'height':768});page.set_viewport_size({'width':1440,'height':900})
    assert page.locator('.map-world').get_attribute('transform')==transform
    evidence['checks'].append('map zoom survives resize round trip')
    page.get_by_label('地图图层').select_option('precipitation')
    page.locator('.grid-cell').first.wait_for();page.locator('.map-range-controls span').wait_for(state='hidden')
    initial=page.locator('.screen-map-legend').inner_text()
    page.get_by_label('选择回放日期').fill('2025-06-01');page.locator('.map-status').wait_for(state='hidden')
    assert page.locator('.screen-map-legend').inner_text()==initial
    evidence['checks'].append('replay-period scale stays fixed as actual day changes')
    page.get_by_role('button',name='全流域',exact=True).click()
    await_ready="""async()=>{const ec=await import('/node_modules/.vite/deps/echarts_core.js');const root=document.querySelector('.lake-level-card + .screen-panel .chart');const c=ec.getInstanceByDom(root);return !!c}"""
    assert page.evaluate(await_ready)
    page.evaluate("""async()=>{const ec=await import('/node_modules/.vite/deps/echarts_core.js');ec.getInstanceByDom(document.querySelector('.lake-level-card + .screen-panel .chart')).dispatchAction({type:'legendUnSelect',name:'模拟湖位'})}""")
    for i in range(3):
        page.get_by_label('选择回放日期').fill('2025-06-0'+str(i+2));page.set_viewport_size({'width':1400+i*10,'height':900})
        page.reload();rendered(page)
        hidden=page.evaluate("""async()=>{const ec=await import('/node_modules/.vite/deps/echarts_core.js');return ec.getInstanceByDom(document.querySelector('.lake-level-card + .screen-panel .chart')).getOption().legend[0].selected['模拟湖位']===false}""")
        assert hidden
    evidence['checks'].append('hidden curve survives three data/replay/resize/browser-refresh rounds')
    for name in ('监测资料','参数率定','历史模拟','计算成果','模型状态','模型资料','系统管理'):
        page.get_by_role('button',name=name,exact=True).click()
        if name=='监测资料':
            page.get_by_label('测站',exact=True).select_option('115DB')
            assert page.get_by_label('监测要素').locator('option').all_text_contents()==['降水']
        if name=='参数率定':page.get_by_role('button',name='我的方案',exact=True).click();page.get_by_role('button',name='保存新修订',exact=True).wait_for()
        if name=='模型状态':
            page.locator('.result-toolbar select').first.select_option(baseline)
            page.get_by_role('button',name='状态分布',exact=True).click();page.locator('.state-spatial .snow-unit').first.wait_for()
            assert page.locator('.state-spatial .snow-unit').count()==1597
            page.get_by_role('button',name='状态库',exact=True).click()
            page.get_by_role('button',name='查看标准成果末状态',exact=True).click()
            page.locator('.state-inspector tbody tr').first.wait_for()
            assert page.locator('.state-inspector tbody tr').count()==30
            assert page.locator('.state-inspector th').count()==9
            assert 'GRID-' not in page.locator('.state-inspector tbody tr').first.inner_text()
            evidence['checks'].append('legacy checkpoint displays its own 30 units and eight state columns while grid result selected')
        for width in (1920,1440,1366,768):
            page.set_viewport_size({'width':width,'height':900 if width!=1366 else 768})
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'),name
        page.screenshot(path=str(out/f'page-{name}-768.png'),full_page=True)
    browser.close()
(folder/'ui-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(evidence,ensure_ascii=False),flush=True)
assert not evidence['errors']
