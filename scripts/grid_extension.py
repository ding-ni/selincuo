# -*- coding: utf-8 -*-
"""Verify true forcing extension and launch one complete latest-period result."""
import sys,json,time,requests
from pathlib import Path
from copy import deepcopy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from backend.common import ROOT,read_json,write_json
from backend.database import Database
from backend.grid_simulation import prepare,run,prefix_identity
out=ROOT/'build/grid-revision-20261026';db=Database(out/'workspace')
task=db.task(read_json(out/'baseline-evidence.json')['taskId']);state=task['result']['checkpoint']
latest=read_json(db.workspace/'downloads/era5-land/latest-status.json');input_id=latest['inputId']
end=pd.Timestamp(latest['verifiedEnd']).floor('D').isoformat();prepared=prepare(db,input_id,state['identity']['spatialVersion'])
assert prefix_identity(prepared,state['validAt'])==state['forcingPrefixSHA256']
request={**deepcopy(task['request']),'inputId':input_id,'end':end}
t=time.monotonic();full=run(db,request,prepared=prepared)
continued=run(db,{**request,'start':(pd.Timestamp(state['validAt'])+pd.Timedelta(hours=1)).isoformat(),'_parentState':state},prepared=prepared)
maximum=float(np.max(np.abs(np.array(full['checkpoint']['unitStates'])-continued['checkpoint']['unitStates'])));lake=abs(full['checkpoint']['lake']['level']-continued['checkpoint']['lake']['level'])
assert maximum<1e-10 and lake<1e-9 and full['checkpoint']['routing']==continued['checkpoint']['routing']
evidence={'inputId':input_id,'verifiedEnd':latest['verifiedEnd'],'lastCompleteLocalDay':str(pd.Timestamp(end)-pd.Timedelta(days=1))[:10],'historicalPrefixExactlyEqual':True,'fullVsExtendedStateMaximumDifference':maximum,'lakeLevelDifferenceM':lake,'routingExactlyEqual':True,'numericVerificationSeconds':time.monotonic()-t,'latestMetrics':full['metrics']}
write_json(out/'extension-evidence.json',evidence);print(json.dumps(evidence,ensure_ascii=False),flush=True)
base='http://127.0.0.1:8882/api/model/'
def api(path,value=None):
    r=requests.post(base+path,json=value,timeout=120) if value is not None else requests.get(base+path,timeout=120)
    r.raise_for_status();return r.json()
configuration=api('research-template');configuration['name']='逐格网连续候选 · 最新完整驱动';configuration['inputId']=input_id
scheme=api('schemes',{'payload':configuration})
job=api('tasks',{'kind':'simulation','researchSchemeId':scheme['id'],'name':'逐格网连续模拟（2023—'+evidence['lastCompleteLocalDay']+'）','end':end,'clientRequestId':'grid-latest-complete-20261026'})
evidence['fullResultTaskId']=job['id'];evidence['schemeId']=scheme['id'];write_json(out/'extension-evidence.json',evidence)
last=None
while True:
    job=api('tasks/'+job['id']);status=(job['status'],int(job['progress']//10))
    if status!=last:print(job['status'],round(job['progress'],1),job['message'],flush=True);last=status
    if job['status'] in ('succeeded','failed','cancelled'):break
    time.sleep(2)
evidence['fullResultStatus']=job['status'];write_json(out/'extension-evidence.json',evidence)
if job['status']!='succeeded':raise SystemExit(job['message'])
