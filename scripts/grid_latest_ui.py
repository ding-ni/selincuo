# -*- coding: utf-8 -*-
"""Verify the latest real candidate extends past observations on the overview."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

folder = Path(__file__).resolve().parents[1] / 'build/grid-revision-20261026'
latest = json.loads((folder / 'extension-evidence.json').read_text(encoding='utf-8'))['fullResultTaskId']
errors = []
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1920,'height':1080},device_scale_factor=1)
    page.set_default_timeout(90000)
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto('http://127.0.0.1:5175/?result=' + latest)
    page.wait_for_load_state('networkidle')
    page.locator('.screen-provenance').get_by_text('逐格网 · 连续湖位',exact=False).wait_for()
    assert page.locator('.screen-result-choice select').input_value() == latest
    assert page.get_by_label('选择回放日期').input_value() == '2026-10-20'
    assert page.locator('.lake-level-card .primary-number').inner_text().strip() != '—'
    assert page.locator('.screen-station').count() >= 5
    for dark in (False,True):
        active=page.locator('.lake-screen').evaluate("e=>e.classList.contains('screen-dark')")
        if active!=dark:page.get_by_role('button',name='汇报深色' if dark else '日常浅色',exact=True).click()
        page.screenshot(path=str(folder/'ui'/('grid-latest-1920-'+('dark' if dark else 'light')+'.png')),full_page=True)
    evidence={'resultId':latest,'latestReplayDay':'2026-10-20','continuousLevel':page.locator('.lake-level-card .primary-number').inner_text(),'errors':errors}
    browser.close()
assert not errors
(folder/'latest-ui-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
print(evidence,flush=True)
