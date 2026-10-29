"""Exercise the saved-result to manual lake-only trial through the real UI."""
import argparse
import json
from pathlib import Path
import sys
import time

import requests
from playwright.sync_api import sync_playwright

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, write_json
from backend.database import Database


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--task')
    args=parser.parse_args();output=ROOT/args.output
    output.mkdir(parents=True,exist_ok=False)
    base='http://127.0.0.1:8882/api/model/'
    def api(path):
        r=requests.get(base+path,timeout=90);r.raise_for_status();return r.json()
    original=api('bootstrap')['currentBusiness']
    original_result=api('tasks/'+original['resultId'])['result']
    errors=[];report={'baseAdoption':original,'errors':errors}
    if args.task:
        existing=api('tasks/'+args.task)
        db=Database();scheme=next(r for r in db.items('schemes') if r['id']==existing['request']['researchSchemeId'])
        db.set_setting('scheme-purpose:'+(scheme['payload'].get('familyId') or scheme['id']),'business')
        db.set_setting('development-task-ids',[r for r in db.setting('development-task-ids',[]) if r!=args.task])
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1920,'height':1080});page.set_default_timeout(90000)
        page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            page.goto('http://127.0.0.1:8882/?page=calibration',wait_until='domcontentloaded')
            page.locator('.calibration-workbench[data-coverage-ready="true"]').wait_for()
            coefficient=round(original_result['parameters']['lakeCoefficient']-.01,4)
            if args.task:
                task=existing;request=existing['request']
                page.get_by_role('button',name='我的计算记录',exact=True).click()
                page.locator('.calibration-history button').filter(has_text=task['name']).click()
            else:
                page.get_by_role('button',name='人工微调并试算',exact=True).click()
                page.get_by_role('button',name='1 资料与时段',exact=True).click()
                page.get_by_label('方案名称',exact=True).fill('界面验收 · 湖泊人工微调 · 20261029')
                page.get_by_role('button',name='2 选择参数',exact=True).click()
                page.get_by_role('button',name='湖泊',exact=True).click()
                page.get_by_label('湖面蒸发转换系数初值',exact=True).fill(str(coefficient))
                page.screenshot(path=str(output/'人工微调.png'),full_page=True)
                with page.expect_response(lambda response:response.url.endswith('/api/model/tasks') and response.request.method=='POST') as pending:
                    page.get_by_role('button',name='人工试算',exact=True).click()
                response=pending.value
                task=response.json()
                assert response.status==202,task
                request=response.request.post_data_json
            assert request['lakeOnlyBaseTaskId']==original['resultId'],request
            report.update(taskId=task['id'],submitted=request)
            deadline=time.monotonic()+900
            while time.monotonic()<deadline:
                status=next(row for row in api('tasks')['tasks'] if row['id']==task['id'])
                if status['status'] in ('succeeded','failed','cancelled'):break
                page.wait_for_timeout(1500)
            assert status['status']=='succeeded',status
            trial=api('tasks/'+task['id']);result=trial['result']
            assert result.get('landReuse'),result.keys()
            assert result['parameters']['lakeCoefficient']==coefficient
            for key,value in result['parameters'].items():
                if key!='lakeCoefficient':assert value==original_result['parameters'][key]
            for before,after in zip(original_result['daily'],result['daily']):
                assert before['date']==after['date']
                for key in ('duja','zajia','zagen','boqu','ali','fringe','total'):
                    assert before[key]==after[key],(before['date'],key)
            difference=max(abs(a['lakeLevel']-b['lakeLevel']) for a,b in zip(original_result['daily'],result['daily']) if a['lakeLevel'] is not None)
            assert difference>0
            page.get_by_role('button',name='保存并采用',exact=True).wait_for(state='visible')
            assert page.get_by_role('button',name='保存并采用',exact=True).is_enabled()
            page.screenshot(path=str(output/'人工试算结果.png'),full_page=True)
            assert api('bootstrap')['currentBusiness']['resultId']==original['resultId']
            assert not errors,errors
            report.update(checks={'manualDraftSaved':True,'onlyLakeRecomputed':True,'allRiverFlowsUnchanged':True,
                'adoptActionAvailable':True,'currentAdoptionUnchanged':True,'maximumLakeLevelChangeM':difference},
                reuse=result['landReuse'])
        finally:
            # These explicitly named acceptance objects are development records.
            # Keep their files and lineage while hiding them from daily use.
            if report.get('taskId'):
                db=Database();saved=db.task(report['taskId'])
                scheme=next(r for r in db.items('schemes') if r['id']==saved['request']['researchSchemeId'])
                db.set_setting('scheme-purpose:'+(scheme['payload'].get('familyId') or scheme['id']),'development')
                hidden=set(db.setting('development-task-ids',[]));hidden.add(report['taskId'])
                db.set_setting('development-task-ids',sorted(hidden))
                report['developmentSchemeId']=scheme['id']
            write_json(output/'人工微调核验.json',report)
            browser.close()
    print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__':main()
