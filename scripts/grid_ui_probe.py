"""Read-only browser reconnaissance for the grid revision."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as BrowserTimeout
out=Path(__file__).resolve().parents[1]/'build/grid-revision-20261026/ui'
out.mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':900},device_scale_factor=1)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.set_default_timeout(120000)
    page.goto('http://127.0.0.1:5175/')
    try:page.wait_for_load_state('networkidle',timeout=15000)
    except BrowserTimeout:errors.append('networkidle timeout during task polling')
    page.screenshot(path=str(out/'initial-1440.png'),full_page=True)
    print(json.dumps({'errors':errors,'buttons':page.get_by_role('button').all_text_contents(),'text':page.locator('main').inner_text()[:5000]},ensure_ascii=False),flush=True)
    page.get_by_role('button',name='参数率定',exact=True).click()
    page.get_by_role('button',name='我的方案',exact=True).click()
    page.get_by_role('button',name='保存新修订',exact=True).wait_for()
    page.screenshot(path=str(out/'schemes-1440.png'),full_page=True)
    print(json.dumps({'errors':errors,'schemeText':page.locator('main').inner_text()[:4000]},ensure_ascii=False),flush=True)
    browser.close()
