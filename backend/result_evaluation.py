"""Additional period-specific reports derived from a result's frozen daily rows."""
import pandas as pd
from .simulation import metrics

def report(task):
    result=task.get('result') or {};periods=task.get('request',{}).get('periods')
    if result.get('modelFamily')!='grid-continuous-v1' or not periods or not result.get('daily'):return None
    workflow=result.get('calibrationWorkflow')
    if workflow:
        records={}
        for index,fit in enumerate(workflow.get('flowFits',[])):
            for key,label in [('training','流量率定'),('validation','流量分年检验')]:
                metric=fit.get(key);period=fit.get(key+'Period')
                if metric and period and period.get('start'):
                    records['flow-%d-%s'%(index,key)]={'label':fit['fit']+' · '+label,
                        'start':period['start'],'endExclusive':period['end'],'flow':metric,'lake':None}
        lake=workflow.get('lake')
        if lake:
            lake_periods=task['request']['twoStage']['lakePeriods']
            for name,fit,key,period,label in [
                ('lake-training',lake['split'],'training',lake['split']['trainingPeriod'],'湖位率定'),
                ('lake-time-check',lake['split'],'validation',
                 {'start':lake_periods['evaluationStart'],'end':lake_periods['evaluationEnd']},'湖位时间检验'),
                ('lake-all-data',lake['final'],'training',lake['final']['trainingPeriod'],'全部湖位资料拟合')]:
                records[name]={'label':label,'start':period['start'],'endExclusive':period['end'],
                               'flow':None,'lake':fit[key]}
        return {'source':'frozen-two-stage-search-reports','periods':records,
                'evaluationUsedForSearch':bool(workflow.get('finalRefit')),
                'dataUse':'split-checks-then-all-data-refit' if workflow.get('finalRefit') else 'time-split-checks'}
    frame=pd.DataFrame(result['daily']);frame.index=pd.to_datetime(frame.date)
    anchor=result.get('initialization')
    if anchor:frame.loc[frame.index==pd.Timestamp(anchor['time']).normalize(),'lakeLevel']=float('nan')
    records={}
    scheme=result.get('scheme') or {}
    all_refit=bool(scheme.get('twoStage',{}).get('finalRefit') and
                   (scheme.get('adoptedParameterId') or scheme.get('parameterSources',{}).get('sourceTaskId')))
    for key,label,start,end in [('training','率定时段评价',periods['trainingStart'],periods['trainingEnd']),('evaluation','时间评价' if all_refit else '留出评价期',periods['evaluationStart'],periods['evaluationEnd'])]:
        group=frame.loc[(frame.index>=pd.Timestamp(start))&(frame.index<pd.Timestamp(end))]
        records[key]={'label':label,'start':start,'endExclusive':end,'flow':metrics(group.observedFlow,group.duja),'lake':metrics(group.observedLevel,group.lakeLevel)}
    return {'source':'frozen-result-daily','periods':records,'evaluationUsedForSearch':all_refit}
