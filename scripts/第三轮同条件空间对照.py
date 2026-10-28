"""Run one controlled spatial comparison with the existing complete forcing."""
import os
os.environ['OPENBLAS_NUM_THREADS']='1'
os.environ['OMP_NUM_THREADS']='1'
import json
from copy import deepcopy
from datetime import datetime
import numpy as np
import pandas as pd
from backend.common import ROOT,read_json,write_json,digest
from backend.database import Database
from backend.grid_simulation import run,model_identity,observations

def main():
    db=Database(ROOT/'build/grid-revision-20261026/workspace')
    config=read_json(ROOT/'config/operational/continuous-builtin-v1.json')
    folder=ROOT/'build/第三轮整改-20261028/空间对照';folder.mkdir(parents=True,exist_ok=True)
    snapshot=observations(db)
    fixed={'time':'2021-04-23T12:00:00','level':4543.66,'method':'first-quality-daily-level-at-noon',
           'source':snapshot['profile'],'reference':'SLC-working-datum'}
    base={'inputId':config['inputId'],'start':'2020-01-01T00:00:00','end':'2026-10-21T00:00:00',
          'parameters':config['parameters'],'partitionParameters':config['partitionParameters'],
          'routingMode':'source-separated','_fixedInitialization':fixed,'_observations':snapshot,
          '_modelIdentity':model_identity(),'periods':{'trainingStart':'2021-04-24','trainingEnd':'2025-01-01',
          'evaluationStart':'2025-01-01','evaluationEnd':'2026-10-21'},'_metricsOnly':True}
    summaries={};values={}
    for name,spatial in (('previous','AGG-SPATIAL-dbb6e03518db589dc16f'),('candidate','AGG-SPATIAL-dc21f45a0f12c9ec379c')):
        path=folder/(name+'.json')
        request={**base,'spatialVersion':spatial}
        if path.exists():
            result=read_json(path)
            if result['identity']['modelImplementation']!=base['_modelIdentity']:raise ValueError('旧对照代码身份改变，保留旧输出另设批次')
        else:
            print(json.dumps({'running':name,'spatial':spatial}),flush=True)
            result=run(db,request,progress=lambda p,m:print(json.dumps({'version':name,'progress':round(p,1),'message':m},ensure_ascii=False),flush=True) if round(p,1)%10<.3 else None)
            write_json(path,result)
        frame=pd.DataFrame(result['daily']);frame.index=pd.to_datetime(frame['date']);values[name]=frame
        summaries[name]={'spatialVersion':spatial,'metricsByYear':result['metrics'],'diagnostics':result['diagnostics'],
                         'identity':result['identity'],'initialization':result['initialization'],'units':len(result['units'])}
    a,b=values['previous'],values['candidate'];common=a.index.intersection(b.index);delta=b.loc[common,'lakeLevel']-a.loc[common,'lakeLevel']
    q=b.loc[common,'total']-a.loc[common,'total']
    receipt={'comparison':'only spatial source and receiver areas change; same model code, forcing, parameters, initial state recipe and anchor',
             'modelImplementation':base['_modelIdentity'],'parametersSHA256':digest(base['parameters']),
             'partitionParametersSHA256':digest(base['partitionParameters']),'forcingId':base['inputId'],
             'start':base['start'],'end':base['end'],'lakeAnchor':fixed,'results':summaries,
             'commonDays':len(common),'lakeDeltaMeanM':float(delta.mean()),'lakeDeltaMaximumAbsM':float(delta.abs().max()),
             'flowDeltaMeanM3s':float(q.mean()),'flowDeltaMaximumAbsM3s':float(q.abs().max()),
             'interpretation':'Conditional two-metre non-water terrain conditioning is being compared; preserved water/deep-pit receivers remain unresolved. This is not proof of topology or independent skill.'}
    write_json(ROOT/'docs/evidence/第三轮同条件空间对照-20261028.json',receipt)
    print(json.dumps(receipt,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
