"""Read-only browser acceptance of layout, fixed labels, and calibration flow."""
import argparse
import json
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8882')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    out=Path(args.output);out.mkdir(parents=True,exist_ok=False)
    report={'layouts':[],'checks':{},'errors':[],'writes':[]}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1920,'height':1080})
        page.set_default_timeout(90000)
        page.on('pageerror',lambda e:report['errors'].append(str(e)))
        page.on('request',lambda r:report['writes'].append(r.url) if r.method not in ('GET','HEAD') else None)
        try:
            page.goto(args.url+'/?page=overview&date=2025-06-20',wait_until='domcontentloaded')
            page.locator('.screen-map[data-spatial-ready="true"]').wait_for()
            page.wait_for_function("document.querySelector('.business-date b')?.textContent==='2025-06-20'")
            report['bundle']=page.locator('script[type="module"]').get_attribute('src')
            def layout(label):
                page.wait_for_timeout(450)
                bounds=page.evaluate("""()=>Object.fromEntries(['.lake-screen','.hydrology-stack','.summary-stack','.rain-summary','.shore-summary','.balance-summary','.balance-grid','.screen-processes','.screen-replay'].map(s=>[s,document.querySelector(s).getBoundingClientRect().toJSON()]))""")
                viewport=page.viewport_size
                report['layouts'].append({'label':label,'viewport':viewport,'bounds':bounds})
                page.screenshot(path=str(out/(label+'.png')),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth')<=viewport['width']+1
                for column,child in [('.hydrology-stack','.shore-summary'),('.summary-stack','.balance-summary')]:
                    gap=bounds[column]['bottom']-bounds[child]['bottom']
                    assert 0<=gap<25,(label,column,gap)
                contents=page.evaluate("""()=>{
                    const rect=e=>e.getBoundingClientRect().toJSON();
                    return ['.hydrology-stack','.summary-stack'].map(s=>{
                        const panel=document.querySelector(s);
                        return {selector:s,panel:rect(panel),sections:[...panel.children].map(e=>({
                            rect:rect(e),children:[...e.querySelectorAll('.rain-summary-row,.observation-summary>div,.shore-row,.river-row,.inflow-total,.balance-grid>div')].map(rect)
                        }))};
                    });
                }""")
                for column in contents:
                    panel=column['panel'];previous=panel['top']
                    for section in column['sections']:
                        area=section['rect']
                        assert area['width']>=panel['width']-40,(label,'unfilled width',column['selector'],area)
                        assert area['top']>=previous-1,(label,'overlapping sections',column['selector'])
                        for child in section['children']:
                            assert child['top']>=area['top']-1 and child['bottom']<=area['bottom']+1,(label,'overflowing row',column['selector'],area,child)
                            assert child['left']>=area['left']-1 and child['right']<=area['right']+1,(label,'overflowing width',child)
                        previous=area['bottom']
                report['layouts'][-1]['contents']=contents
                assert bounds['.balance-summary']['bottom']<=bounds['.screen-processes']['top']+1
            for width,height in [(1920,1080),(1366,768)]:
                page.set_viewport_size({'width':width,'height':height})
                layout('普通-'+str(width))
                page.get_by_role('button',name='全屏展示',exact=True).click()
                layout('全屏-'+str(width))
                page.get_by_role('button',name='退出全屏',exact=True).click()
            page.set_viewport_size({'width':1920,'height':1080})
            page.wait_for_timeout(300)
            def labels():
                return page.locator('.basin-map-label').evaluate_all("es=>es.map(e=>({id:e.dataset.label,text:[...e.querySelectorAll('text')].map(t=>[t.getAttribute('x'),t.getAttribute('y')])}))")
            before=labels()
            page.locator('.screen-map-heading summary').click()
            page.get_by_label('测站',exact=True).uncheck()
            page.wait_for_timeout(200)
            assert labels()==before
            page.get_by_label('测站',exact=True).check()
            page.wait_for_timeout(200)
            assert labels()==before
            report['checks']['fixedGeographicLabels']=True
            page.locator('.screen-map-heading summary').click()
            page.get_by_role('button',name='模型方案与率定',exact=True).click()
            page.locator('.calibration-steps').wait_for()
            page.locator('.calibration-workbench[data-coverage-ready="true"]').wait_for()
            page.wait_for_timeout(800)
            page.get_by_role('button',name='1 资料与时段',exact=True).click()
            page.locator('[aria-label="流量率定起日"]').wait_for()
            report['schemeOptions']=page.get_by_label('率定方案',exact=True).locator('option').all_text_contents()
            report['periodCounts']=page.locator('.calibration-period-count').all_text_contents()
            assert '229' in report['periodCounts'][0] and '211' in report['periodCounts'][0],report['periodCounts']
            assert '970' in report['periodCounts'][1] and '335' in report['periodCounts'][1],report['periodCounts']
            page.screenshot(path=str(out/'率定-资料与时段.png'),full_page=True)
            page.get_by_role('button',name='2 选择参数',exact=True).click()
            page.get_by_role('button',name='积雪与冰川',exact=True).click()
            assert page.locator('.calibration-parameter-table tbody tr').count()==11
            report['checks']['allSnowAndIceParametersAvailable']=True
            page.screenshot(path=str(out/'率定-参数.png'),full_page=True)
            page.get_by_role('button',name='3 开始率定',exact=True).click()
            page.screenshot(path=str(out/'率定-开始与进度.png'),full_page=True)
            page.get_by_role('button',name='4 查看与采用',exact=True).click()
            page.screenshot(path=str(out/'率定-结果.png'),full_page=True)
            report['checks']['fourStepWorkflow']=True
            assert not report['errors'],report['errors']
            assert not report['writes'],report['writes']
        finally:
            (out/'界面核对.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            browser.close()
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
