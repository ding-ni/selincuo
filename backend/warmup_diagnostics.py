"""Diagnostics for initial-state sensitivity of continuous simulations."""
import numpy as np
import pandas as pd


def snapshot(result):
    cp=result['checkpoint'];units=result['units'];states=np.array(cp['unitStates'],float)
    areas=np.array([u['areaKm2'] for u in units],float)
    names=['snowSolidMm','snowLiquidMm','upperSoilMm','lowerSoilMm','deepSoilMm',
           'freeWaterMm','runoffAreaFraction','glacierMeltStorageMm']
    weighted={name:float(np.average(states[:,i],weights=areas)) for i,name in enumerate(names)}
    weighted['tensionWaterMm']=float(np.average(states[:,2:5].sum(axis=1),weights=areas))
    weighted['partitions']={}
    for partition in sorted({unit['partition'] for unit in units}):
        selected=np.array([unit['partition']==partition for unit in units])
        local={name:float(np.average(states[selected,i],weights=areas[selected])) for i,name in enumerate(names)}
        local['tensionWaterMm']=float(np.average(states[selected,2:5].sum(axis=1),weights=areas[selected]))
        weighted['partitions'][partition]=local
    weighted['routingStorageM3']=float(sum(v['channel']['storage']+sum(v['channel']['queue'])+
        sum(v['sources'].values()) for v in cp['routing'].values()))
    return {'validAt':cp['validAt'],'areaWeighted':weighted,'unitIds':cp['unitIds'],
            'unitStates':cp['unitStates'],'routing':cp['routing']}


def comparison(a,b):
    start=pd.Timestamp(a['initialization']['time']).normalize()+pd.Timedelta(days=1)
    end=start+pd.DateOffset(years=1)
    def frame(x):
        df=pd.DataFrame(x['daily']);df.index=pd.to_datetime(df.date)
        return df.loc[(df.index>=start)&(df.index<end)]
    left,right=frame(a),frame(b)
    joined=left[['total','lakeLevel']].join(right[['total','lakeLevel']],lsuffix='_a',rsuffix='_b').dropna()
    if len(joined)<300:raise ValueError('湖位起算首年完整日不足300，不能计算预热差异')
    q=np.asarray(joined.total_a-joined.total_b);scale=float(np.mean(np.abs(joined.total_a)))
    h=np.asarray(joined.lakeLevel_a-joined.lakeLevel_b)
    if scale<=1e-12:raise ValueError('参考流量过小，不能计算相对差异')
    return {'periodStart':start.isoformat(),'periodEndExclusive':end.isoformat(),'days':len(joined),
            'flowNrmsePercent':float(np.sqrt(np.mean(q*q))/scale*100),
            'flowVolumeRelativeDifferencePercent':float(abs(q.sum())/np.abs(joined.total_a.sum())*100),
            'lakeLevelMaxAbsoluteDifferenceM':float(np.max(np.abs(h))),
            'lakeLevelRmseDifferenceM':float(np.sqrt(np.mean(h*h))),
            'passes':bool(np.sqrt(np.mean(q*q))/scale<=.01 and np.max(np.abs(h))<=.01)}


def budget(result):
    df=pd.DataFrame(result['daily']);df.index=pd.to_datetime(df.date)
    anchor=pd.Timestamp(result['initialization']['time'])
    # Do not claim the partial anchor day as a complete day of lake storage.
    df=df.loc[df.index>anchor.normalize()].copy()
    cols=['lakeInflowM3','lakePrecipitationM3','lakeEvaporationM3','lakeDeltaVolumeM3','lakeBalanceErrorM3']
    out={}
    for label,freq in [('monthly','ME'),('annual','YE')]:
        rows=[]
        for date,group in df.groupby(pd.Grouper(freq=freq)):
            if group.empty:continue
            sums=group[cols].sum(min_count=1)
            levels=group[['observedLevel','lakeLevel']].dropna()
            rows.append({'period':date.strftime('%Y-%m' if label=='monthly' else '%Y'),
                         'calendarDays':len(group),'pairedLevelDays':len(levels),
                         **{key:float(sums[key]) if pd.notna(sums[key]) else None for key in cols},
                         'meanLevelBiasM':float((levels.lakeLevel-levels.observedLevel).mean()) if len(levels) else None})
        out[label]=rows
    return out
