"""Additional period-specific reports derived from a result's frozen daily rows."""
import pandas as pd
from .simulation import metrics

def report(task):
    result=task.get('result') or {};periods=task.get('request',{}).get('periods')
    if result.get('modelFamily')!='grid-continuous-v1' or not periods or not result.get('daily'):return None
    frame=pd.DataFrame(result['daily']);frame.index=pd.to_datetime(frame.date)
    anchor=result.get('initialization')
    if anchor:frame.loc[frame.index==pd.Timestamp(anchor['time']).normalize(),'lakeLevel']=float('nan')
    records={}
    for key,label,start,end in [('training','训练期',periods['trainingStart'],periods['trainingEnd']),('evaluation','留出评价期',periods['evaluationStart'],periods['evaluationEnd'])]:
        group=frame.loc[(frame.index>=pd.Timestamp(start))&(frame.index<pd.Timestamp(end))]
        records[key]={'label':label,'start':start,'endExclusive':end,'flow':metrics(group.observedFlow,group.duja),'lake':metrics(group.observedLevel,group.lakeLevel)}
    return {'source':'frozen-result-daily','periods':records,'evaluationUsedForSearch':False}
