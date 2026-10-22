# coding: utf-8
"""Exercise the actual service; persist real controls, routing fit and resume."""
import argparse
import json
import sys
import time
from pathlib import Path
import numpy as np
import requests
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.common import read_json,write_json,now

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8886')
    parser.add_argument('--report',type=Path,default=ROOT/'docs/service-routing-v0.6.0.json')
    parser.add_argument('--workspace',type=Path,default=ROOT/'workspace')
    args=parser.parse_args()
    session=requests.Session();session.trust_env=False
    def api(path,body=None):
        url=args.url+'/api/model/'+path
        r=session.get(url,timeout=120) if body is None else session.post(url,json=body,timeout=120)
        r.raise_for_status();return r.json()
    report=read_json(args.report,{'createdAt':now(),'tasks':{}})
    experiment=read_json(ROOT/'docs/routing-partition-v0.6.0.json')
    req={'inputId':'INPUT-967613234ca58f1c6cfb44e2','start':'2023-01-01T00:00:00','end':'2026-01-01T00:00:00',
         'routingMode':'source-separated','objectiveProfile':'balanced','seed':42,'calibrationYear':2024}
    def execute(key,payload):
        task=api('tasks/'+report['tasks'][key]) if key in report['tasks'] else api('tasks',payload)
        report['tasks'][key]=task['id'];write_json(args.report,report)
        while task['status'] in ('queued','running','stopping'):
            time.sleep(3);task=api('tasks/'+task['id'])
        if task['status']!='succeeded':raise RuntimeError(task)
        api('tasks/'+task['id']+'/metadata',{'collection':'汇流与分区诊断','favorite':False})
        print(key+' '+task['id']+' succeeded',flush=True)
        return task
    ref=execute('reference',{**req,'kind':'simulation','parameters':experiment['cases']['reference']['baseline']['parameters'],
                            'name':'完整历史 · 湖位参考（v0.6分区水量诊断）'})
    base=execute('flowBaseline',{**req,'kind':'simulation','parameters':experiment['cases']['flow']['baseline']['parameters'],
                               'name':'汇流对照 · 固定陆面冰雪基准'})
    tuned=execute('routing',{**req,'kind':'calibration','stage':'routing','baseTaskId':base['id'],'maxEvaluations':120,
                           'parameters':ref['result']['parameters'],'name':'汇流调试 · 2024冻结陆面冰雪 · 120次'})
    # Deliberately submitted unrelated parameters above. Server must replace
    # them from the baseline and freeze every non-routing key.
    search=tuned['result']['calibration']['searchParameters']
    assert all(tuned['result']['parameters'][k]==base['result']['parameters'][k] for k in base['result']['parameters'] if k not in search)
    ref_trial=execute('referenceRouting',{**req,'kind':'trial','parameters':experiment['cases']['reference']['routing']['parameters'],
                            'name':'湖位参考参数 · 仅调整汇流的对照'})
    expected={'reference':experiment['cases']['reference']['baseline'],'flowBaseline':experiment['cases']['flow']['baseline'],
              'routing':experiment['cases']['flow']['routing'],'referenceRouting':experiment['cases']['reference']['routing']}
    current={'reference':ref,'flowBaseline':base,'routing':tuned,'referenceRouting':ref_trial}
    for key,task in current.items():
        result=task['result']
        assert len(result['daily'])==1096 and all(d['forcingHours']==24 for d in result['daily'])
        for year in ('2024','2025'):
            for variable in ('flow','lake'):
                assert abs(result['metrics'][year][variable]['rmse']-expected[key]['metrics'][year][variable]['rmse'])<1e-8
        partitions=result['modelDiagnostics']['partitions']
        assert len(partitions)==6
        for month in result['modelDiagnostics']['monthly']:
            matching=[m for part in partitions for m in part['monthly'] if m['month']==month['month']]
            assert len(matching)==6
            assert abs(sum(m['routedRunoffM3'] for m in matching)-month['lakeInflowM3'])<.001
            assert abs(sum(m['precipitationM3'] for m in matching)-month['landPrecipitationM3'])<.001
        assert max(p['balanceMaxErrorM3'] for p in partitions)<.001
    p=tuned['result']['parameters']
    part=execute('part',{**req,'kind':'simulation','parameters':p,'end':'2024-06-30T13:00:00','name':'验收 · 长退水状态分段'})
    tail=execute('tail',{**req,'kind':'continuation','parameters':p,'stateId':part['result']['stateId'],
                         'start':'2024-06-30T14:00:00','name':'验收 · 冰雪汇流长记忆续算'})
    paths={k:args.workspace/'tasks'/v['id']/'hourly.npz' for k,v in [('full',tuned),('part',part),('tail',tail)]}
    diffs={}
    with np.load(paths['full']) as full,np.load(paths['part']) as a,np.load(paths['tail']) as b:
        for key in full.files:
            combined=np.concatenate([a[key],b[key]])
            np.testing.assert_array_equal(full[key],combined)
            diffs[key]=0.
    assert tuned['result']['checkpointSHA256']==tail['result']['checkpointSHA256']
    # Routing cannot alter runoff generation; compare entire three-year arrays.
    with np.load(args.workspace/'tasks'/base['id']/'hourly.npz') as a,np.load(paths['full']) as b:
        for key in ('unitStates','unitDiagnostics'):np.testing.assert_array_equal(a[key],b[key])
    old_refs=api('tasks')['tasks']
    for t in old_refs:
        if t.get('favorite') and t.get('collection')=='完整历史' and t['id']!=ref['id']:
            api('tasks/'+t['id']+'/metadata',{'favorite':False})
    api('tasks/'+ref['id']+'/metadata',{'favorite':True,'collection':'完整历史',
        'notes':'沿用v0.5湖位参考参数，当前版本增加分区诊断，未认定为业务定型。汇流候选分别保留，未按2025反选。'})
    if 'parameters' not in report:
        report['parameters']=api('parameters',{'name':'v0.6汇流调试 · 2024冻结（非业务定型）','payload':{
            'parameters':p,'sourceTaskId':tuned['id'],'routingMode':'source-separated','calibrationYear':2024,
            'validationYearsExcludedFromSearch':[2025,2026]}})['id']
    report.update(completedAt=now(),status='complete',metrics={k:v['result']['metrics'] for k,v in current.items()},
                  frozenNonRoutingParametersVerified=True,generatedRunoffAndUnitStatesUnchanged=True,
                  sixExclusivePartitionsSumToLakeInflow=True,allHourlyArraysResumeExact=diffs,
                  checkpointExact=True,modelIdentity=tuned['result']['identity']['modelImplementation'])
    write_json(args.report,report)
    print(json.dumps({'status':'complete','tasks':report['tasks']},ensure_ascii=True),flush=True)

if __name__=='__main__':main()
