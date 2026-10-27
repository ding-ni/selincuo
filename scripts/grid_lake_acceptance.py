# -*- coding: utf-8 -*-
import sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT,read_json,write_json
from backend.database import Database
from backend.jobs import Jobs
from backend.research_schemes import template,save
out=ROOT/'build/grid-revision-20261026';db=Database(out/'acceptance-workspace');value=template(db)
value['name']='隔离验收：湖泊独立参数与停止';value['target']='lake';value['search'].update(scope='background',keys=['lakeCoefficient']);value['search']['bounds']['lakeCoefficient']=[1.2,1.5];value['search']['budgets']['lake']=3
scheme=save(db,value);jobs=Jobs(db);job=jobs.submit('calibration',{'researchSchemeId':scheme['id'],'name':value['name']});folder=db.workspace/'tasks'/job['id'];deadline=time.monotonic()+300
while time.monotonic()<deadline:
    trace=read_json(folder/'search-progress.json',{}).get('trace',[])
    if len(trace)>=2:jobs.stop(job['id']);break
    if db.task(job['id'])['status'] in ('failed','succeeded'):raise AssertionError(db.task(job['id'])['message'])
    time.sleep(.5)
while db.task(job['id'])['status'] in ('queued','running','stopping'):time.sleep(.5)
task=db.task(job['id']);trace=read_json(folder/'search-progress.json');assert task['status']=='cancelled'
assert trace['trace'][0]['parameters']=={'lakeCoefficient':1.5} and trace['evaluationExcluded']
evidence={'taskId':job['id'],'status':task['status'],'target':'lake','traceCount':len(trace['trace']),'trainingPeriod':trace['trainingPeriod'],'evaluationExcluded':True,'initial':trace['trace'][0],'second':trace['trace'][1],'tracePreserved':True,'termination':trace['termination']}
write_json(out/'lake-calibration-evidence.json',evidence);print(evidence,flush=True);jobs.close()
