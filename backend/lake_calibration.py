"""Bounded lake evaporation fit conditional on frozen hourly basin inflow."""
from copy import deepcopy
import numpy as np
import pandas as pd

from .common import digest
from .simulation import metrics


def response(source, request, baseline):
    times=source['times']
    ids=np.flatnonzero((times>=pd.Timestamp(baseline['start'])) & (times<=pd.Timestamp(baseline['end'])))
    times=times[ids]
    anchor=baseline.get('initialization')
    if not anchor:
        raise ValueError('湖泊率定需要起算水位')
    evaporation=source['arrays']['evaporation'][ids]@np.asarray(source['spatial']['lakeWeights'])/1000
    evaporation[times<=pd.Timestamp(anchor['time'])]=0.
    derivative=-np.cumsum(evaporation)
    derivative[times<pd.Timestamp(anchor['time'])]=np.nan
    slopes=pd.Series(derivative,index=times).resample('D',closed='right',label='left').mean()
    frame=pd.DataFrame(baseline['daily'])
    frame.index=pd.to_datetime(frame.date)
    frame['slope']=slopes.reindex(frame.index)
    frame['baseLevel']=frame['lakeLevel']
    return frame


def assess(frame,coefficient,baseline_coefficient,start,end,anchor):
    selected=frame.loc[(frame.index>=pd.Timestamp(start)) & (frame.index<pd.Timestamp(end))].copy()
    selected=selected.loc[selected.index!=pd.Timestamp(anchor['time']).normalize()]
    simulated=selected.baseLevel+(coefficient-baseline_coefficient)*selected.slope
    report=metrics(selected.observedLevel,simulated)
    pair=pd.DataFrame({'observed':selected.observedLevel,'simulated':simulated}).dropna()
    if len(pair)>1:
        x=(pair.index-pair.index[0]).days.to_numpy()/365.2425
        report['observedTrendMPerYear']=float(np.polyfit(x,pair.observed,1)[0])
        report['simulatedTrendMPerYear']=float(np.polyfit(x,pair.simulated,1)[0])
        report['firstToLastErrorChangeM']=float((pair.simulated-pair.observed).iloc[-1]-(pair.simulated-pair.observed).iloc[0])
    report['countByYear']={str(y):int(n) for y,n in pair.groupby(pair.index.year).size().items()}
    return report


def fit(frame,baseline_coefficient,bounds,start,end,anchor):
    pairs=frame.loc[(frame.index>=pd.Timestamp(start)) & (frame.index<pd.Timestamp(end))]
    pairs=pairs.loc[pairs.index!=pd.Timestamp(anchor['time']).normalize()]
    pairs=pairs[['observedLevel','baseLevel','slope']].dropna()
    if len(pairs)<30 or float(np.sum(pairs.slope**2))<1e-12:
        raise ValueError('所选湖位率定期不足30个有效日或蒸发响应不足')
    unconstrained=baseline_coefficient+float(np.sum(pairs.slope*(pairs.observedLevel-pairs.baseLevel))/np.sum(pairs.slope**2))
    coefficient=float(np.clip(unconstrained,*bounds))
    observations=[{'date':t.isoformat(),'observed':float(v)} for t,v in pairs.observedLevel.items()]
    return {'algorithm':'bounded-linear-least-squares-on-frozen-inflow-v1',
        'parameter':'lakeCoefficient','initial':baseline_coefficient,'bounds':list(bounds),
        'value':coefficient,'unconstrainedValue':unconstrained,'converged':True,
        'termination':'bounded-optimum' if coefficient!=unconstrained else 'analytic-optimum',
        'boundary':coefficient in bounds,'trainingPeriod':{'start':start,'end':end},
        'training':assess(frame,coefficient,baseline_coefficient,start,end,anchor),
        'fixedTrainingDates':[r['date'] for r in observations],
        'fixedTrainingObservationSHA256':digest(observations),
        'trace':[{'value':v,'metrics':assess(frame,v,baseline_coefficient,start,end,anchor)}
                 for v in sorted(set([float(bounds[0]),float(baseline_coefficient),coefficient,float(bounds[1])]))]}
