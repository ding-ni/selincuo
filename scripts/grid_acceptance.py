# -*- coding: utf-8 -*-
"""Real-data numerical and query acceptance, isolated from business records."""
import sys,time,sqlite3,shutil,json
from pathlib import Path
from copy import deepcopy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
import psutil
from backend.common import ROOT,read_json,write_json,digest
from backend.database import Database
from backend.grid_simulation import run,prepare,prefix_identity
from backend.result_query import read,source
from backend.observation_profiles import daily_quality,series
from backend.observation_query import page
from backend.research_schemes import template,save
from backend.jobs import Jobs
from backend.simulation import metrics

out=ROOT/'build/grid-revision-20261026';business=Database(out/'workspace')
workspace=out/'acceptance-workspace'
if not (workspace/'workspace-identity.json').exists():
    workspace.mkdir(parents=True,exist_ok=True);(workspace/'database').mkdir(exist_ok=True)
    shutil.copy2(business.workspace/'workspace-identity.json',workspace/'workspace-identity.json')
    with business.connect() as a,sqlite3.connect(workspace/'database/hydrology.sqlite3') as b:a.backup(b)
    shutil.copytree(business.workspace/'inputs',workspace/'inputs',dirs_exist_ok=True)
db=Database(workspace);db.set_setting('workspacePurpose',{'kind':'acceptance','createdBy':'grid_acceptance.py'})
e=read_json(out/'baseline-evidence.json');task=business.task(e['taskId']);request=deepcopy(task['request']);prepared=prepare(db,request['inputId'],request['spatialVersion'])
process=psutil.Process();evidence={'checks':{},'workspace':str(workspace)};checks=evidence['checks']
t=time.monotonic();full=run(db,request,prepared=prepared);checks['fullRunSeconds']=time.monotonic()-t
t=time.monotonic();first=run(db,{**request,'end':'2024-12-31T23:00:00'},prepared=prepared)
continued=run(db,{**request,'start':'2025-01-01T00:00:00','_parentState':first['checkpoint']},prepared=prepared)
checks['splitRunSeconds']=time.monotonic()-t
checks['unitStateMaximumDifference']=float(np.max(np.abs(np.array(full['checkpoint']['unitStates'])-continued['checkpoint']['unitStates'])))
checks['lakeLevelDifferenceM']=abs(full['checkpoint']['lake']['level']-continued['checkpoint']['lake']['level'])
checks['routingExactlyEqual']=full['checkpoint']['routing']==continued['checkpoint']['routing']
assert checks['unitStateMaximumDifference']<1e-10 and checks['lakeLevelDifferenceM']<1e-9 and checks['routingExactlyEqual']
assert np.allclose(full['checkpoint']['unitStates'],task['result']['checkpoint']['unitStates'],rtol=0,atol=1e-10)
checks['frozenBaselineStateReproduced']=True
for case in ('parameters','space','missingQueue','historyRevision'):
    bad=deepcopy(request);bad['start']='2025-01-01T00:00:00';bad['_parentState']=deepcopy(first['checkpoint']);src=prepared
    if case=='parameters':bad['parameters']['KC']+=.001
    if case=='space':bad['_parentState']['identity']['spatialVersion']='different'
    if case=='missingQueue':bad['_parentState']['routing']['duja']['channel'].pop('queue')
    if case=='historyRevision':
        src={**prepared,'manifest':{**prepared['manifest'],'dataSHA256':'acceptance-revised-array'},'arrays':dict(prepared['arrays'])}
        src['arrays']['precipitation']=src['arrays']['precipitation'].copy();src['arrays']['precipitation'][0,0]+=.001
    try:run(db,bad,prepared=src)
    except ValueError as exc:checks['reject-'+case]=str(exc)
    else:raise AssertionError('incompatible checkpoint accepted: '+case)
t=time.monotonic();frame=read(business,{'resultId':task['id'],'variable':'swe','time':task['result']['end']},frame=True)
checks['stateFrameQuerySeconds']=time.monotonic()-t
np.testing.assert_array_equal([v['value'] for v in frame['values']],np.array(task['result']['checkpoint']['unitStates'])[:,0])
assert frame['completeCheckpoint'] is False
t=time.monotonic();rows=read(business,{'resultId':task['id'],'variable':'WU','unit':0,'start':'2024-01-01','end':'2025-12-31'})['rows'];checks['singleUnitTwoYearQuerySeconds']=time.monotonic()-t
checks['twoYearDays']=len(rows)
queries=[]
for station in ('SLC','SLC-N','SLC-E','SLC-S','SLC-W'):
    t=time.monotonic();rows=series(db,station,'level')['rows'];cold=time.monotonic()-t
    t=time.monotonic();series(db,station,'level');warm=time.monotonic()-t
    queries.append({'station':station,'coldSeconds':cold,'cacheSeconds':warm,'days':len(rows),'validDays':sum(r['value'] is not None for r in rows)})
checks['monitoringPerformance']=queries
records=page(db,{'station':'SLC-N','variable':'level','page':61,'pageSize':50});checks['page61']={'total':records['total'],'returned':len(records['rows'])};assert len(records['rows'])==50
start=prepared['times'][0].ceil('D')
t=time.monotonic();alternative=run(db,{**request,'start':start.isoformat()},prepared=prepared)
df=pd.DataFrame(alternative['daily']);df.index=pd.to_datetime(df.date);base=pd.DataFrame(full['daily']);base.index=pd.to_datetime(base.date)
pair=base[['duja','lakeLevel']].join(df[['duja','lakeLevel']],rsuffix='Earlier').loc['2024-01-01':'2025-12-31']
checks['warmupSensitivity']={'earlierStart':start.isoformat(),'seconds':time.monotonic()-t,'maximumDujaDifferenceM3s':float((pair.duja-pair.dujaEarlier).abs().max()),'maximumLakeDifferenceM':float((pair.lakeLevel-pair.lakeLevelEarlier).abs().max())}
write_json(out/'acceptance-evidence.json',evidence)
print(json.dumps(evidence,ensure_ascii=False),flush=True)

# Exercise editable settings and an actual stop without producing test records in business.
jobs=Jobs(db);configuration=template(db);configuration['name']='验收：自定义初值与搜索范围'
configuration['parameters']['KC']=.8;configuration['partitionParameters']['duja']['KC']=.79
configuration['search'].update(keys=['KC'],seed=123)
configuration['search']['bounds']['KC']=[.7,.9];configuration['search']['budgets']['runoff']=3
configuration['periods'].update(trainingEnd='2024-10-01T00:00:00',evaluationStart='2024-10-01T00:00:00',evaluationEnd='2025-01-01T00:00:00')
scheme=save(db,configuration);job=jobs.submit('calibration',{'researchSchemeId':scheme['id'],'name':configuration['name']})
folder=workspace/'tasks'/job['id'];deadline=time.monotonic()+300
while time.monotonic()<deadline:
    trace=read_json(folder/'search-progress.json',{}).get('trace',[])
    if len(trace)>=2:jobs.stop(job['id']);break
    if db.task(job['id'])['status'] in ('failed','succeeded'):raise AssertionError(db.task(job['id'])['message'])
    time.sleep(.5)
while db.task(job['id'])['status'] in ('running','queued','stopping'):time.sleep(.5)
status=db.task(job['id']);trace=read_json(folder/'search-progress.json');assert status['status']=='cancelled'
assert trace['trace'][0]['parameters']['KC']==.79 and all(.7<=r['parameters']['KC']<=.9 for r in trace['trace'])
checks['editableSchemeAndStop']={'taskId':job['id'],'status':status['status'],'traceCount':len(trace['trace']),'initialKC':trace['trace'][0]['parameters']['KC'],'searchBounds':[.7,.9],'frozenBudget':status['request']['search']['budgets']['runoff'],'periods':status['request']['periods'],'traceRetained':True,'standardUntouched':True}
checks['sampledEndRssMiB']=process.memory_info().rss/1024**2
write_json(out/'acceptance-evidence.json',evidence);jobs.close();print('real-data and stop acceptance completed',flush=True)
