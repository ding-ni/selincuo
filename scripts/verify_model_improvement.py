# coding: utf-8
"""Verify real packaged source routing, continuous storage and version guards."""
import argparse
import sys
import time
from pathlib import Path
import requests
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.common import read_json,write_json,now


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--url',default='http://127.0.0.1:8879')
    p.add_argument('--workspace',type=Path,default=Path('F:/色林错预警预报平台/运行程序/workspace'))
    args=p.parse_args()
    session=requests.Session();session.trust_env=False
    def api(path,payload=None):
        url=args.url+'/api/model/'+path
        r=session.get(url,timeout=120) if payload is None else session.post(url,json=payload,timeout=120)
        r.raise_for_status();return r.json()
    run=read_json(ROOT/'docs/model-improvement-v0.5.0.json')
    full=api('tasks/'+run['tasks']['sourceLake'])
    report_path=ROOT/'docs/packaged-historical-verification-v0.5.0.json'
    report=read_json(report_path,{'createdAt':now(),'tasks':{},'version':'0.5.0'})
    def execute(key,payload,success=True):
        identifier=report['tasks'].get(key)
        task=api('tasks/'+identifier) if identifier else api('tasks',payload)
        report['tasks'][key]=task['id'];write_json(report_path,report)
        while task['status'] in ('running','queued','stopping'):
            time.sleep(2);task=api('tasks/'+task['id'])
        assert task['status']==('succeeded' if success else 'failed'),task.get('message')
        api('tasks/'+task['id']+'/metadata',{'collection':'数值验收','notes':'真实历史分段一致性或预期版本拒绝验收；非业务率定任务。'})
        return task
    request={**full['request'],'parameters':full['result']['parameters'],'kind':'simulation'}
    request.pop('baseTaskId',None)
    part=execute('part',{**request,'end':'2025-07-01T12:00:00','name':'数值验收 · 分水源调蓄前段'})
    tail=execute('tail',{**request,'kind':'continuation','start':'2025-07-01T13:00:00',
                 'stateId':part['result']['stateId'],'name':'数值验收 · 分水源及湖泊水当量续算'})
    checks={}
    with np.load(args.workspace/'tasks'/full['id']/'hourly.npz') as f, np.load(args.workspace/'tasks'/part['id']/'hourly.npz') as a, np.load(args.workspace/'tasks'/tail['id']/'hourly.npz') as b:
        for key in f.files:
            x,y=f[key],np.concatenate([a[key],b[key]])
            np.testing.assert_allclose(x,y,atol=1e-8,rtol=1e-12,equal_nan=True)
            diff=np.abs(x-y)
            checks[key]=float(np.nanmax(diff)) if np.isfinite(diff).any() else None
    assert full['result']['checkpointSHA256']==tail['result']['checkpointSHA256']
    old=api('tasks/JOB-25a4e32481394e928ca7')
    rejected=execute('oldStateRejected',{**request,'kind':'continuation','start':'2026-01-01T01:00:00','end':'2026-01-01T07:00:00',
                  'stateId':old['result']['stateId'],'name':'数值验收 · 旧模型状态应被拒绝'},False)
    assert '不一致' in rejected['message']
    r=full['result'];diag=r['modelDiagnostics']
    assert diag['landBalanceMaxErrorM3']<1e-3 and diag['catchmentBalanceMaxErrorM3']<1e-3
    assert len(diag['monthly'])==36 and all(m['complete'] for m in diag['monthly'])
    lake_error=max(abs(d.get('lakeBalanceErrorM3') or 0) for d in r['daily'])
    assert lake_error<1e-5
    frame=api('map-frame?inputId='+r['inputId']+'&day=2025-07-15&variable=precipitation')
    assert frame['complete'] and frame['hours']==24 and len(frame['cells'])==535
    missing=api('map-frame?inputId='+r['inputId']+'&day=2026-08-25&variable=temperature')
    assert not missing['complete'] and not missing['cells']
    report.update(completedAt=now(),fullTaskId=full['id'],hours=r['hours'],days=len(r['daily']),
                  continuationMaxDifference=checks,finalCheckpointIdentical=True,oldStateRejected=True,
                  lakeDailyBalanceMaxErrorM3=lake_error,catchmentMaxBalanceErrorM3=diag['catchmentBalanceMaxErrorM3'],
                  landMaxBalanceErrorM3=diag['landBalanceMaxErrorM3'],completeMonths=36,
                  mapCells=535,missingFutureDayRemainsEmpty=True,status='verified')
    write_json(report_path,report)
    print('Verified source routing, full/split state, old-state rejection, real map data and 36 monthly budgets.',flush=True)


if __name__=='__main__':main()
