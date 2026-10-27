# -*- coding: utf-8 -*-
"""Same forcing/quality/anchor comparison; frozen files are read-only."""
import sys,time,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from backend.database import Database
from backend.common import ROOT,read_json,write_json,sha_file
from backend.simulation import metrics
from backend.grid_simulation import run,prepare
from backend.published_results import result_file,current_id,detail

folder=ROOT/'build/grid-revision-20261026';db=Database(folder/'workspace')
evidence=read_json(folder/'baseline-evidence.json');task=db.task(evidence['taskId']);request=task['request']
start=time.monotonic();trial=run(db,{**request,'end':'2025-01-01T00:00:00','_metricsOnly':True},prepared=prepare(db,request['inputId'],request['spatialVersion']))
elapsed=time.monotonic()-start
published=current_id();legacy=detail(published)['result'];source=result_file(published,'hourly.npz')
with np.load(source,allow_pickle=False) as arrays:
    times=pd.to_datetime(arrays['time']);volume=arrays['continuousLakeVolumeChange'];anchor=task['result']['initialization'];at=pd.Timestamp(anchor['time'])
    continuous=anchor['level']+(volume-volume[times.get_loc(at)])/(task['result']['diagnostics']['lakeAreaKm2']*1e6)
    continuous[times<at]=np.nan
    hourly=pd.DataFrame({'oldContinuousLevel':continuous,'oldWindowLevel':arrays['lakeLevel'],'oldDuja':arrays['flow'][:,0]},index=times)
    old=hourly.resample('D',closed='right',label='left').mean();old['oldContinuousLevel']=old.oldContinuousLevel.where(hourly.oldContinuousLevel.resample('D',closed='right',label='left').count()==24)
new=pd.DataFrame(task['result']['daily']);new.index=pd.to_datetime(new.date)
combined=new[['lakeLevel','duja','observedLevel','observedFlow']].join(old)
combined.loc[at.normalize(),['lakeLevel','oldContinuousLevel']]=np.nan
scores={}
for year,group in combined.groupby(combined.index.year):
    pair=group.dropna(subset=['lakeLevel','oldContinuousLevel','observedLevel'])
    scores[str(year)]={'commonLakeDays':len(pair),'newContinuous':metrics(pair.observedLevel,pair.lakeLevel),'oldContinuousSameAnchorQuality':metrics(pair.observedLevel,pair.oldContinuousLevel),'oldWindowFrozen':legacy.get('metrics',{}).get(str(year)),
                       'newDuja':metrics(group.observedFlow,group.duja),'oldDujaSameQuality':metrics(group.observedFlow,group.oldDuja)}
combined.to_csv(folder/'same-input-comparison.csv',index_label='date',encoding='utf-8-sig')
report={
    'inputId':request['inputId'],
    'inputDataSHA256':read_json(db.workspace/'inputs/products'/request['inputId']/'manifest.json')['dataSHA256'],
    'qualityProfile':request['_observations']['profile'],'anchor':anchor,'scores':scores,
    'trainingRunWithoutFullStorageSeconds':elapsed,'baselineTaskId':task['id'],
    'standardHourlySHA256':sha_file(source),
    'limitations':[
        '新空间排除未核实排水区；旧方案将全部残余区入湖，比较须同时说明控制域差异',
        '两方案使用相同气象、冻结湖面降水转换、日值口径和连续锚点',
        '旧窗口式评分独立列示，不与连续评分混称精度提升',
        '2023为预热，2024训练，2025保持评价用途',
    ],
}
write_json(folder/'comparison-evidence.json',report);print(json.dumps(report,ensure_ascii=False),flush=True)
