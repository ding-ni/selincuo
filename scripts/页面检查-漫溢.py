"""漫溢与应急、实况更新页面的浏览器检查：截图并收集控制台错误。只读，不提交计算。

用法：python scripts/页面检查-漫溢.py [输出目录]
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'docs/evidence/页面检查')
OUT.mkdir(parents=True, exist_ok=True)
BASE = 'http://127.0.0.1:8882/'

with sync_playwright() as p:
    browser = p.chromium.launch(args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
    page = browser.new_page(viewport={'width': 1600, 'height': 900})
    errors = []
    page.on('console', lambda m: errors.append(m.text[:300]) if m.type == 'error' else None)
    page.on('pageerror', lambda e: errors.append('pageerror: ' + str(e)[:300]))

    page.goto(BASE + '?page=risk')
    page.wait_for_selector('.ov-view canvas', timeout=120000)
    page.wait_for_timeout(3000)
    # Jump to the outflow peak event so the flow, depth colouring and warning are visible.
    page.locator('.ov-events li').nth(3).click()
    page.wait_for_timeout(2500)
    page.screenshot(path=str(OUT / '漫溢-复现-洪峰.png'))
    page.get_by_role('button', name='溃口').first.click()
    page.wait_for_timeout(2500)
    page.screenshot(path=str(OUT / '漫溢-复现-溃口近景.png'))
    page.get_by_role('button', name='平面').click()
    page.wait_for_timeout(2500)
    page.screenshot(path=str(OUT / '漫溢-复现-平面.png'))
    page.get_by_role('button', name='三维').click()
    page.get_by_role('button', name='预报情景推演').click()
    page.wait_for_timeout(3000)
    page.screenshot(path=str(OUT / '漫溢-情景推演.png'))
    print('scenario page has content:', page.locator('.ov-side h3').count() > 0)

    page.goto(BASE + '?page=forecast')
    page.wait_for_selector('.su', timeout=120000)
    page.wait_for_selector('.su-table, .su-blocked, .empty', timeout=180000)
    page.wait_for_timeout(1000)
    page.screenshot(path=str(OUT / '预测-实况更新.png'), full_page=True)
    # Station-entry mode: open the entry cells (nothing is saved).
    if page.locator('.su-edit input').count():
        page.locator('.su-edit input').check()
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / '预测-实况更新-录入.png'), full_page=True)
        print('station entry inputs:', page.locator('.su-table input').count())
    page.get_by_role('button', name='未来气象').click()
    page.wait_for_selector('.fw-table, .fw-ensemble', timeout=240000)
    page.wait_for_timeout(1000)
    page.screenshot(path=str(OUT / '预测-未来气象-站点预报.png'), full_page=True)
    print('station forecast columns:', page.locator('.fw-table thead th').count())
    page.get_by_role('button', name='历史同期估算').click()
    page.wait_for_timeout(1500)
    page.screenshot(path=str(OUT / '预测-未来气象-历史同期.png'), full_page=True)
    page.get_by_role('button', name='预报计算').click()
    page.wait_for_timeout(1500)
    page.screenshot(path=str(OUT / '预测-预报计算.png'))
    print('\n'.join(errors[:20]) or 'no console errors')
    browser.close()
