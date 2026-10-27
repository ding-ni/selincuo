"""Versioned observation views. Raw rows are never corrected or resampled in place."""
import json
from collections import OrderedDict
from threading import RLock
import numpy as np
import pandas as pd
from .common import digest
from .observation_query import SeriesView,revision
from .observations import daily_series

QUALITY_VERSION='time-weighted-level-v2-gap6h-cover18h'
_cache=OrderedDict();_lock=RLock()

def _cached(db,key,make):
    identity=(revision(db),key)
    with _lock:
        if identity in _cache:return _cache[identity]
    value=make()
    with _lock:
        _cache[identity]=value
        while len(_cache)>48:_cache.popitem(last=False)
    return value

def daily_quality(db,station,variable='level'):
    def make():
        records=SeriesView(db).observations(station,variable)
        if not records:return []
        frame=pd.DataFrame(records);frame['t']=pd.to_datetime(frame.time)
        start=frame.t.min().floor('D');end=frame.t.max().floor('D')
        if variable!='level':
            old=daily_series(SeriesView(db),station,variable)
            return [{'time':t.isoformat(),'value':None if pd.isna(v) else float(v),'coverageHours':None,'maximumGapHours':None,'source':'legacy-variable-rule','quality':'valid' if pd.notna(v) else 'missing'} for t,v in old.reindex(pd.date_range(start,end,freq='D')).items()]
        valid=frame[frame.quality.isin(['valid','qualified'])&frame.value.notna()]
        daily=valid[valid.step.eq('day')].sort_values('t').groupby(valid[valid.step.eq('day')].t.dt.floor('D')).value.last()
        points=frame[~frame.step.eq('day')].sort_values('t').drop_duplicates('t',keep='last').copy()
        points.loc[~points.quality.isin(['valid','qualified']),'value']=np.nan
        # Trapezoids only between actual neighboring observations, never beyond endpoints.
        sums={};covered={};gaps={};day_ns=86400*10**9
        times=points.t.to_numpy(dtype='datetime64[ns]').astype('int64');values=points.value.to_numpy(float)
        for i in range(1,len(times)):
            a,b=int(times[i-1]),int(times[i]);hours=(b-a)/3.6e12
            if hours<=0:continue
            first=a//day_ns;last=(b-1)//day_ns
            for day in range(first,last+1):
                lo=max(a,day*day_ns);hi=min(b,(day+1)*day_ns)
                if hi<=lo:continue
                gaps[day]=max(gaps.get(day,0.),hours)
                if hours>6 or not np.isfinite(values[i-1:i+1]).all():continue
                va=values[i-1]+(values[i]-values[i-1])*(lo-a)/(b-a)
                vb=values[i-1]+(values[i]-values[i-1])*(hi-a)/(b-a)
                duration=(hi-lo)/3.6e12
                sums[day]=sums.get(day,0.)+(va+vb)*.5*duration
                covered[day]=covered.get(day,0.)+duration
        result=[]
        for day in pd.date_range(start,end,freq='D'):
            index=day.value//day_ns;hours=covered.get(index,0.)
            if day in daily.index:value=float(daily.loc[day]);source='source-daily';quality='source-daily-valid';coverage=None
            elif hours>=18-1e-9:value=float(sums[index]/hours);source=QUALITY_VERSION;quality='coverage-valid';coverage=hours
            else:value=None;source=QUALITY_VERSION;quality='insufficient-time-coverage';coverage=hours
            result.append({'time':day.isoformat(),'value':value,'coverageHours':coverage,'coverageFraction':coverage/24 if coverage is not None else None,'maximumGapHours':gaps.get(index),'source':source,'quality':quality,'reason':None if value is not None else '有效时间覆盖不足18小时；不跨越6小时以上缺口'})
        return result
    return _cached(db,('quality',station,variable),make)

def quality_series(db,station,variable='level'):
    rows=daily_quality(db,station,variable)
    return pd.Series([r['value'] for r in rows],index=pd.to_datetime([r['time'] for r in rows]),dtype=float)

def raw_points(db,station,variable):
    return [{'time':r['time'],'value':r['value'] if r['quality'] in ('valid','qualified') else None,'rawValue':r['value'],'quality':r['quality'],'step':r['step'],'sourceRecordId':r['id']} for r in SeriesView(db).observations(station,variable)]

def _paired_points(db,station):
    def points(identifier):
        rows=SeriesView(db).observations(identifier,'level')
        return sorted([(pd.Timestamp(r['time']).value,float(r['value'])) for r in rows if r['step']!='day' and r['quality']=='valid' and r['value'] is not None])
    main=points('SLC');shore=points(station)
    if not main or not shore:return pd.DataFrame(columns=['time','difference'])
    mt=np.array([r[0] for r in main],dtype=np.int64);possible=[];tolerance=15*60*10**9
    for j,(t,value) in enumerate(shore):
        k=int(np.searchsorted(mt,t))
        for i in (k-1,k):
            if 0<=i<len(main) and abs(main[i][0]-t)<=tolerance:possible.append((abs(main[i][0]-t),i,j))
    used_main=set();used_shore=set();pairs=[]
    for _,i,j in sorted(possible):
        if i in used_main or j in used_shore:continue
        used_main.add(i);used_shore.add(j)
        pairs.append({'time':pd.Timestamp(shore[j][0]),'difference':shore[j][1]-main[i][1]})
    return pd.DataFrame(pairs).sort_values('time')

def datum_candidates(db):
    def make():
        result=[]
        for station in ('SLC-N','SLC-E','SLC-S','SLC-W'):
            points=_paired_points(db,station)
            if points.empty:
                result.append({'station':station,'status':'insufficient-data','offsetM':None,'trainingDays':0,'months':[],'validation':[]});continue
            day=points.groupby(points.time.dt.floor('D')).difference.median()
            training=day[day.index.year==2025]
            months=[{'month':str(p),'days':len(g),'medianM':float(g.median())} for p,g in training.groupby(training.index.to_period('M'))]
            offset=float(training.median()) if len(training) else None
            validation=[{'month':str(p),'days':len(g),'medianResidualM':float((g-offset).median()) if offset is not None else None} for p,g in day[day.index.year>=2026].groupby(day[day.index.year>=2026].index.to_period('M'))]
            stable=len(training)>=30 and len(months)>=3 and max(m['medianM'] for m in months)-min(m['medianM'] for m in months)<=.05
            drift=any(abs(v['medianResidualM'])>.05 for v in validation if v['days']>=5 and v['medianResidualM'] is not None)
            status='time-varying-under-review' if station=='SLC-S' or drift else 'statistical-candidate' if stable else 'under-review'
            local=points.time.dt.floor('D').map(day);residual=points.difference-local
            mad=float(np.median(np.abs(residual-np.median(residual))))
            threshold=max(.05,6*1.4826*mad);flagged=points.loc[residual.abs()>threshold].copy()
            segments=[]
            if not flagged.empty:
                for _,g in flagged.groupby(flagged.time.diff().gt(pd.Timedelta(hours=6)).cumsum()):
                    segments.append({'start':g.time.min().isoformat(),'end':g.time.max().isoformat(),'matchedPoints':len(g),'minimumDifferenceM':float(g.difference.min()),'maximumDifferenceM':float(g.difference.max())})
            result.append({'station':station,'status':status,'offsetM':offset,'correctionM':-offset if offset is not None and status=='statistical-candidate' else None,'trainingDays':len(training),'trainingYear':2025,'matchedPoints':len(points),'months':months,'validation':validation,'withinDayDiagnostics':{'thresholdM':threshold,'method':'6 robust MAD with 0.05 m floor, relative to same-day paired median; review only','flaggedPoints':len(flagged),'segments':segments,'automaticExclusion':False},'reference':'SLC','automaticApplication':False,'method':'one-to-one-nearest15min; daily-median-equal-day-weight','limitation':'相对主站统计候选，非测量零点；同年拟合结果不是独立验证'})
        source_digest=digest(db.rows('SELECT id,sha256 FROM sources ORDER BY id'))
        payload={'schemaVersion':1,'qualityVersion':QUALITY_VERSION,'observationRevision':revision(db)[1:],'observationSourcesSHA256':source_digest,'candidates':result,'reference':'SLC','datumStatus':'user-retracted','appliedToModel':False}
        payload['id']='OBS-CANDIDATE-'+digest(payload)[:20]
        return payload
    return _cached(db,'datum-candidates',make)

def series(db,station,variable,start=None,end=None,profile='quality-v2'):
    if profile=='raw':rows=raw_points(db,station,variable)
    else:rows=daily_quality(db,station,variable)
    candidate=None
    if profile=='candidate' and station!='SLC' and variable=='level':
        candidate=next((c for c in datum_candidates(db)['candidates'] if c['station']==station),None)
        if not candidate or candidate['status']!='statistical-candidate':
            return {'rows':[],'profile':profile,'message':'该站零点尚不稳定，不生成自动订正候选','candidate':candidate}
        rows=[{**r,'originalValue':r['value'],'value':None if r['value'] is None else r['value']+candidate['correctionM'],'source':datum_candidates(db)['id']} for r in rows]
    lo=pd.Timestamp(start).floor('D') if start else None;hi=pd.Timestamp(end).floor('D')+pd.Timedelta(days=1) if end else None
    if lo is not None and hi is not None and (hi<=lo or (hi-lo).days>36600):raise ValueError('查询日期范围无效')
    selected=[r for r in rows if (lo is None or pd.Timestamp(r['time'])>=lo) and (hi is None or pd.Timestamp(r['time'])<hi)]
    return {'rows':selected,'profile':profile,'qualityVersion':QUALITY_VERSION,'candidate':candidate,'message':''}
