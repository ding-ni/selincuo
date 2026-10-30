"""通过正式方案配置和普通任务入口提交双驱动率定，不实例化Jobs。"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from urllib.request import Request,urlopen
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, WORKSPACE, read_json, write_json
from backend.calibration_objective import validate_objective
from backend.calibration_config import FLOW_SELECTABLE_KEYS
from backend.two_stage_calibration import defaults


def api(path,body=None):
    request=Request('http://127.0.0.1:8882/api/model/'+path,
        data=json.dumps(body,ensure_ascii=False).encode('utf-8') if body is not None else None,
        headers={'Content-Type':'application/json'})
    with urlopen(request,timeout=180) as response:return json.load(response)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--smoke',action='store_true')
    parser.add_argument('--submit',action='store_true')
    args=parser.parse_args()
    boot=api('bootstrap')
    base=next(r['payload'] for r in boot['schemes'] if r['id']==boot['homeSchemeId'])
    initial=deepcopy(base['parameters'])
    folder=ROOT/'build'/('双驱动'+('贯通核验' if args.smoke else '完整率定')+'-'+uuid.uuid4().hex[:10])
    folder.mkdir(parents=True)
    variants=[('原驱动','INPUT-c00df956d638351ebeb4e96e'),('暖雨订正驱动','INPUT-67a56a9abcc50dc4a9033ab6')]
    if args.smoke:variants=variants[1:]
    records=[]
    for label,input_id in variants:
        value=deepcopy(base)
        for key in ('builtin','configurationSHA256','revision','familyId','parentRevisionId','id','created_at','resultId','adoptedParameterId'):
            value.pop(key,None)
        manifest=read_json(WORKSPACE/'inputs/products'/input_id/'manifest.json')
        value.update(name=('贯通核验 · ' if args.smoke else '双驱动完整率定 · ')+label,
                     parameters=deepcopy(initial),calibrationInitialParameters=deepcopy(initial),
                     partitionParameters={},calibrationInitialPartitionParameters={},
                     inputBinding='frozen',inputId=input_id,purpose='development',
                     simulationEnd='2026-10-21T00:00:00',initialHydrology={'soilFraction':.5},
                     inputRequirement={'transformationSHA256':manifest['transformationSHA256']})
        value['periods']={'warmupStart':'2017-01-01T00:00:00','trainingStart':'2024-01-01T00:00:00',
            'trainingEnd':'2025-01-01T00:00:00','evaluationStart':'2025-01-01T00:00:00','evaluationEnd':'2026-01-01T00:00:00'}
        value['initialization']={'mode':'manual','initialLakeLevel':4543.66,
            'initialLakeTime':'2021-04-23T12:00:00','initialLakeSource':'主站同一高程基准，2021-04-23实测4543.66m'}
        value['search']['keys']=list(FLOW_SELECTABLE_KEYS)
        value['search'].update(scope='duja',objectiveProfile='balanced',seed=42,seeds=[42,142,242])
        options=defaults()
        options.update(selectionMode='continuous-dual',reverseCheck=False,finalRefit=True,
            candidateSourceTaskId=None,flowConstraints=None,baselineTaskId=boot['homeResultId'],
            objective=validate_objective())
        options['lakePeriods']['evaluationEnd']='2026-10-21T00:00:00'
        if args.smoke:options.update(budgets={'runoff':18,'snow':18,'routing':12,'joint':12},
                                     allowExtension=False,candidateLimit=8,jointBudget=12)
        value['twoStage']=options
        scheme=api('schemes',{'payload':value})
        config=api('schemes/'+scheme['id']+'/calibration-configuration')
        row={'label':label,'schemeId':scheme['id'],'configuration':config}
        write_json(folder/(label+'-配置.json'),config)
        if args.submit:
            task=api('tasks',{'kind':'two-stage-calibration','researchSchemeId':scheme['id'],
                'calibrationConfigurationSHA256':config['configurationSHA256'],
                'name':value['name'],'adoptOnSuccess':False,'clientRequestId':str(uuid.uuid4())})
            row['taskId']=task['id']
        records.append(row)
        write_json(folder/'提交记录.json',records)
    print(json.dumps({'folder':str(folder),'tasks':[{k:r.get(k) for k in ('label','schemeId','taskId')} for r in records]},ensure_ascii=False))


if __name__=='__main__':main()
