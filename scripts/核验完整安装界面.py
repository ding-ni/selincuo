"""Read-only packaged UI checks; artifacts stay in a selected build folder."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


def wait_for_background(page):
    # Range-cache requests may outlive the already rendered current frame.
    try:
        page.wait_for_load_state('networkidle', timeout=5000)
    except PlaywrightTimeoutError:
        pass

parser=argparse.ArgumentParser()
parser.add_argument('--url',required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
args.output.mkdir(parents=True,exist_ok=False)
records=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    for width,height in [(1366,768),(1920,1080)]:
        page=browser.new_page(viewport={'width':width,'height':height})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(args.url,wait_until='domcontentloaded',timeout=60000)
        wait_for_background(page)
        page.get_by_role('combobox',name='当前成果').wait_for(timeout=180000)
        page.locator('.lake-screen[aria-busy="false"]').wait_for(timeout=180000)
        page.wait_for_timeout(1500)
        choice=page.get_by_role('combobox',name='当前成果')
        assert choice.locator('option').count()==1
        assert choice.input_value()=='JOB-05c3f62282ce4239aed1'
        default_result=choice.input_value()
        page.screenshot(path=str(args.output/f'总览-{width}.png'))
        page.get_by_role('button',name='全屏展示',exact=True).click()
        page.get_by_role('button',name='退出全屏',exact=True).wait_for()
        page.wait_for_timeout(500)
        page.screenshot(path=str(args.output/f'总览全屏-{width}.png'))
        page.get_by_role('button',name='退出全屏',exact=True).click()
        page.get_by_role('button',name='站点监测',exact=True).click()
        wait_for_background(page)
        page.locator('.monitoring-shore').first.wait_for()
        assert page.locator('.monitoring-shore').count()==4
        page.screenshot(path=str(args.output/f'监测-{width}.png'))
        page.get_by_role('button',name='全屏展示',exact=True).click()
        page.get_by_role('button',name='退出全屏',exact=True).wait_for()
        page.wait_for_timeout(500)
        page.screenshot(path=str(args.output/f'监测全屏-{width}.png'))
        page.get_by_role('button',name='退出全屏',exact=True).click()
        # Open the configuration through a normal user navigation.
        page.get_by_role('button',name='模型方案与率定',exact=False).first.click()
        page.get_by_text('当前最佳',exact=True).wait_for(timeout=180000)
        wait_for_background(page)
        page.screenshot(path=str(args.output/f'率定-{width}.png'))
        assert not page.locator('.error').filter(has_text='计算记录不存在').count()
        assert not errors,errors
        records.append({'width':width,'height':height,'singleDefault':default_result,'fullscreen':True,'monitoringStations':4,'errors':errors})
        page.close()
    browser.close()
(args.output/'report.json').write_text(json.dumps({'passed':True,'checks':records},ensure_ascii=False,indent=2),encoding='utf-8')
