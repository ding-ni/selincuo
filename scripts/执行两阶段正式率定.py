"""Run the approved operational fit once, using existing local data."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
import requests

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, canonical, read_json, write_json
from backend.database import Database
from backend.calculation_domain import ensure_domain
from backend.calibration_config import FLOW_SELECTABLE_KEYS
from backend.research_schemes import template, save
from backend.two_stage_calibration import defaults

DEVELOPMENT_SCHEMES=(
    'SCHEME-e81ff879c55d4c2e99ec','SCHEME-6ad5579cdcce423ebf4e','SCHEME-70a4e53bf920414b93e1',
    'SCHEME-7aa674405f204696a83f','SCHEME-3b5abc850668412f87b5','SCHEME-3a46a4f1081c4a4484f2',
    'SCHEME-80faa40cc37a418bb4e2','SCHEME-688ab9ae904744a08d0d','SCHEME-5f307fa4e7b04846a681',
    'SCHEME-23b0c0cbe2004d83ac06','SCHEME-3208d3a1f4f5485e8e85','SCHEME-5501d837383c4f048528',
    'SCHEME-ad0f68568a6249a18d7b','SCHEME-8d6b3f458c7541b4a8b3','SCHEME-ff57f0f6cd914bc590df',
    'SCHEME-ae20d620a0a94750a8c8','SCHEME-109ef2946cb74c6a878c','SCHEME-3ca95092641e4b1698b0')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--record',default='build/两阶段率定-20261029/启动记录.json')
    args=parser.parse_args()
    path=ROOT/args.record
    db=Database()
    record=read_json(path)
    if not record:
        domain=ensure_domain()
        value=template(db)
        value.update(name='色林错两阶段率定方案',spatialVersion=domain['aggregate'],
                     target='duja',purpose='business',twoStage=defaults())
        value['initialization']={'mode':'automatic'}
        value['periods']={'warmupStart':'2020-01-01T00:00:00','trainingStart':'2024-01-01T00:00:00',
                          'trainingEnd':'2025-01-01T00:00:00','evaluationStart':'2025-01-01T00:00:00',
                          'evaluationEnd':'2026-01-01T00:00:00'}
        value['search']['keys']=list(FLOW_SELECTABLE_KEYS)
        value['search'].update(scope='duja',objectiveProfile='balanced')
        value['calibrationInitialParameters']=deepcopy(value['parameters'])
        value['calibrationInitialPartitionParameters']={}
        scheme=save(db,value)
        record={'schemeId':scheme['id'],'domain':domain}
        write_json(path,record)
    # Classification uses explicit known development IDs. Unknown and manually
    # saved objects remain available. No scheme/task/result bytes are changed.
    ids=set(DEVELOPMENT_SCHEMES)
    for row in db.items('schemes'):
        if row['id'] in ids:
            db.set_setting('scheme-purpose:'+(row['payload'].get('familyId') or row['id']),'development')
    hidden=set(db.setting('development-task-ids',[]))
    archive_path=path.parent/'开发记录归档.json'
    archive=read_json(archive_path)
    if archive is None:
        before=db.rows('SELECT created_at FROM schemes WHERE id=?',(record['schemeId'],))[0]['created_at']
        previous=db.rows("SELECT id,name,kind,created_at FROM tasks WHERE created_at<? AND user_saved=0 AND kind IN ('simulation','continuation','trial','calibration','calibration-workflow','sensitivity','evaluation','continuous-reference')",(before,))
        archive={'basis':'用户将前轮计算定义为开发测试；按本轮开始前的明确编号归档，用户主动保存的记录保留',
                 'before':before,'tasks':previous}
        write_json(archive_path,archive)
    hidden.update(row['id'] for row in archive['tasks'])
    for row in db.rows('SELECT id,request,user_saved FROM tasks'):
        request=json.loads(row['request'])
        if request.get('researchSchemeId') in ids and not row['user_saved']:
            hidden.add(row['id'])
    db.set_setting('development-task-ids',sorted(hidden))
    if args.prepare_only:
        print(json.dumps(record,ensure_ascii=True),flush=True)
        return
    base='http://127.0.0.1:8882/api/model/'
    def api(route,payload=None):
        response=requests.post(base+route,json=payload,timeout=120) if payload is not None else requests.get(base+route,timeout=120)
        response.raise_for_status()
        return response.json()
    if record.get('taskId'):
        task=db.task(record['taskId'])
        if task['status'] in ('cancelled','interrupted','failed'):
            api('tasks/'+task['id']+'/resume',{})
        elif task['status']!='succeeded':
            raise ValueError('已有任务仍在运行，不重复提交')
    else:
        task=api('tasks',{'kind':'two-stage-calibration','researchSchemeId':record['schemeId'],
            'name':'色林错两阶段正式率定 · 2021—2026','adoptOnSuccess':True,
            'clientRequestId':'approved-two-stage-20261029'})
        record['taskId']=task['id']
        write_json(path,record)
    last=None
    while True:
        task=db.task(record['taskId'])
        marker=(task['status'],task['message'])
        if marker!=last:
            print(task['status'],round(task['progress'],2),task['message'],flush=True)
            last=marker
        if task['status'] in ('succeeded','failed','cancelled','interrupted'):
            if task['status']=='succeeded' and task['request'].get('adoptOnSuccess'):
                deadline=time.monotonic()+120
                while (db.setting('business-adopted-scheme-v1',{}).get('resultId')!=task['id']
                       and not task.get('result',{}).get('adoptionError') and time.monotonic()<deadline):
                    time.sleep(.5);task=db.task(task['id'])
                task=db.task(task['id'])
            break
        time.sleep(5)
    record.update(status=task['status'],message=task['message'])
    if task.get('result'):
        record.update(metrics=task['result'].get('metrics'),parameters=task['result'].get('parameters'),
                      adoption=task['result'].get('adoption'),adoptionError=task['result'].get('adoptionError'))
    write_json(path,record)
    print(json.dumps(record,ensure_ascii=True),flush=True)
    if task['status']!='succeeded':raise SystemExit(1)


if __name__=='__main__':main()
