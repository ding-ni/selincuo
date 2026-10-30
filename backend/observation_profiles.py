"""Versioned observation views. Raw rows are never corrected or resampled in place."""
import json
from collections import OrderedDict
from threading import RLock
import numpy as np
import pandas as pd
from .common import ROOT,digest,read_json
from .observation_query import SeriesView,revision
from .observations import daily_series

QUALITY_VERSION='time-weighted-level-v2-gap6h-cover18h'
_cache=OrderedDict();_lock=RLock();_flights={}

def _cached(db,key,make):
    identity=(revision(db),key)
    with _lock:
        if identity in _cache:
            _cache.move_to_end(identity)
            return _cache[identity]
        guard=_flights.setdefault(identity,RLock())
    with guard:
        with _lock:
            if identity in _cache:return _cache[identity]
        try:
            value=make()
            with _lock:
                _cache[identity]=value
                while len(_cache)>48:_cache.popitem(last=False)
            return value
        finally:
            with _lock:
                if _flights.get(identity) is guard:_flights.pop(identity,None)

def daily_quality(db,station,variable='level'):
    def make():
        if variable=='precipitation':
            # The aggregation owns the valid daily axis. Confirmed sparse
            # rain coverage can contain no events or end after the last event.
            old=daily_series(SeriesView(db),station,variable)
            if old.empty:return []
            days=pd.date_range(old.index.min(),old.index.max(),freq='D')
            return [{'time':t.isoformat(),'value':None if pd.isna(v) else float(v),'coverageHours':None,'maximumGapHours':None,'source':'legacy-variable-rule','quality':'valid' if pd.notna(v) else 'missing'} for t,v in old.reindex(days).items()]
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

def daily_reference(db,station,variable='level'):
    """Cache registered-reference views separately from unchanged daily values."""
    from .stations import datum_at
    registry=read_json(ROOT/'config/station-datums.json',{})
    def make():
        converted=[]
        for row in daily_quality(db,station,variable):
            day=pd.Timestamp(row['time']).floor('D')
            datum=datum_at(station,day.isoformat(),registry,(day+pd.Timedelta(days=1)).isoformat())
            record=datum['period'] or {};value=row['value']
            converted.append({**row,'referenceValue':value+datum['referenceOffsetM'] if value is not None and datum['absoluteConversionAvailable'] else None,
                              'referenceDatum':{'available':datum['absoluteConversionAvailable'],'reason':datum['reason'],
                                  'referenceStation':datum['referenceStation'],'method':datum['method'],
                                  'offsetM':datum['referenceOffsetM'],'surveySource':record.get('surveySource'),
                                  'effectiveFrom':record.get('effectiveFrom'),'effectiveTo':record.get('effectiveTo')}})
        return converted
    return _cached(db,('reference-quality',station,variable,QUALITY_VERSION,digest(registry)),make)

def raw_points(db,station,variable,start=None,end=None):
    return [{'time':r['time'],'value':r['value'] if r['quality'] in ('valid','qualified') else None,'rawValue':r['value'],'quality':r['quality'],'step':r['step'],'sourceRecordId':r['id']} for r in SeriesView(db).observations(station,variable,start=start,end=end)]

def _point_samples(db,station,accepted=('valid','qualified')):
    rows=SeriesView(db).observations(station,'level')
    return sorted([(pd.Timestamp(r['time']).value,float(r['value'])) for r in rows if r['step']!='day' and r['quality'] in accepted and r['value'] is not None])

def _paired_points(db,station,main=None,accepted=('valid','qualified'),tolerance_minutes=15):
    if main is None:main=_point_samples(db,'SLC',accepted)
    shore=_point_samples(db,station,accepted)
    if not main or not shore:return pd.DataFrame(columns=['time','difference'])
    mt=np.array([r[0] for r in main],dtype=np.int64);possible=[];tolerance=tolerance_minutes*60*10**9
    for j,(t,value) in enumerate(shore):
        k=int(np.searchsorted(mt,t))
        for i in (k-1,k):
            if 0<=i<len(main) and abs(main[i][0]-t)<=tolerance:possible.append((abs(main[i][0]-t),i,j))
    used_main=set();used_shore=set();pairs=[]
    for _,i,j in sorted(possible):
        if i in used_main or j in used_shore:continue
        used_main.add(i);used_shore.add(j)
        pairs.append({'time':pd.Timestamp(shore[j][0]),'difference':shore[j][1]-main[i][1]})
    return pd.DataFrame(pairs,columns=['time','difference']).sort_values('time')

def datum_candidates(db):
    rules=read_json(ROOT/'config/observation-analysis.json')
    if not rules or rules.get('schemaVersion')!=1:raise ValueError('四岸差值分析规则未配置')
    def make():
        result=[]
        accepted=tuple(rules['acceptedQuality'])
        main=_point_samples(db,'SLC',accepted)
        est_start=pd.Timestamp(rules['estimationStart']);est_end=pd.Timestamp(rules['estimationEndExclusive'])
        check_start=pd.Timestamp(rules['validationStart']);check_end=pd.Timestamp(rules['validationEndExclusive']) if rules.get('validationEndExclusive') else None
        for station in ('SLC-N','SLC-E','SLC-S','SLC-W'):
            points=_paired_points(db,station,main,accepted,rules['pairToleranceMinutes'])
            if points.empty:
                has_shore=bool(SeriesView(db).observations(station,'level'))
                reason='所选站点无记录' if not has_shore else '无同期配对' if main else '主站无可配对时刻记录'
                result.append({'station':station,'status':'insufficient-data','reasonCode':'NO_RECORDS' if not has_shore else 'NO_MATCHED_PAIRS',
                               'reason':reason,'offsetM':None,'correctionM':None,'trainingDays':0,'matchedPoints':0,'months':[],'validation':[],
                               'estimationPeriod':{'start':rules['estimationStart'],'endExclusive':rules['estimationEndExclusive']},
                               'validationPeriod':{'start':rules['validationStart'],'endExclusive':rules.get('validationEndExclusive')},
                               'acceptedQuality':list(accepted),'reference':'SLC','automaticApplication':False});continue
            day=points.groupby(points.time.dt.floor('D')).difference.median()
            training=day[(day.index>=est_start)&(day.index<est_end)]
            months=[{'month':str(p),'days':len(g),'medianM':float(g.median())} for p,g in training.groupby(training.index.to_period('M'))]
            offset=float(training.median()) if len(training) else None
            checked=day[day.index>=check_start]
            if check_end is not None:checked=checked[checked.index<check_end]
            validation=[{'month':str(p),'days':len(g),'medianResidualM':float((g-offset).median()) if offset is not None else None} for p,g in checked.groupby(checked.index.to_period('M'))]
            enough=len(training)>=rules['minimumEstimationDays'] and len(months)>=rules['minimumEstimationMonths']
            stable=enough and max(m['medianM'] for m in months)-min(m['medianM'] for m in months)<=rules['maximumMonthlySpreadM']
            drift=any(abs(v['medianResidualM'])>rules['maximumValidationResidualM'] for v in validation if v['days']>=rules['minimumValidationDaysPerMonth'] and v['medianResidualM'] is not None)
            status='time-varying-under-review' if drift else 'statistical-candidate' if stable else 'under-review'
            reason='差值随时段变化' if drift or (enough and not stable) else '稳定偏移统计候选' if stable else '配对样本不足'
            reason_code='TIME_VARYING' if drift or (enough and not stable) else 'STATISTICAL_CANDIDATE' if stable else 'INSUFFICIENT_SAMPLES'
            local=points.time.dt.floor('D').map(day);residual=points.difference-local
            mad=float(np.median(np.abs(residual-np.median(residual))))
            threshold=max(.05,6*1.4826*mad);flagged=points.loc[residual.abs()>threshold].copy()
            segments=[]
            if not flagged.empty:
                for _,g in flagged.groupby(flagged.time.diff().gt(pd.Timedelta(hours=6)).cumsum()):
                    segments.append({'start':g.time.min().isoformat(),'end':g.time.max().isoformat(),'matchedPoints':len(g),'minimumDifferenceM':float(g.difference.min()),'maximumDifferenceM':float(g.difference.max())})
            result.append({'station':station,'status':status,'reasonCode':reason_code,'reason':reason,'offsetM':offset,'correctionM':-offset if offset is not None and status=='statistical-candidate' else None,'trainingDays':len(training),'matchedPoints':len(points),'months':months,'validation':validation,
                           'estimationPeriod':{'start':rules['estimationStart'],'endExclusive':rules['estimationEndExclusive']},'validationPeriod':{'start':rules['validationStart'],'endExclusive':rules.get('validationEndExclusive')},'acceptedQuality':list(accepted),
                           'withinDayDiagnostics':{'thresholdM':threshold,'method':'6 robust MAD with 0.05 m floor, relative to same-day paired median; review only','flaggedPoints':len(flagged),'segments':segments,'automaticExclusion':False},'reference':'SLC','automaticApplication':False,'method':'one-to-one-nearest15min; daily-median-equal-day-weight','limitation':'相对主站统计候选，非测量零点；估计期拟合结果不是独立验证'})
        source_digest=digest(db.rows('SELECT id,sha256 FROM sources ORDER BY id'))
        payload={'schemaVersion':1,'qualityVersion':QUALITY_VERSION,'analysisRules':rules,'observationRevision':revision(db)[1:],'observationSourcesSHA256':source_digest,'candidates':result,'reference':'SLC','datumStatus':'user-retracted','appliedToModel':False}
        payload['id']='OBS-CANDIDATE-'+digest(payload)[:20]
        return payload
    return _cached(db,('datum-candidates',digest(rules)),make)

def series(db,station,variable,start=None,end=None,profile='quality-v2'):
    if profile=='raw':
        rows=raw_points(db,station,variable,start,end)
        from .observation_query import catalog
        station_data=next((s for s in catalog(db)['stations'] if s['id']==station),{})
        item=next((v for v in station_data.get('variables',[]) if v['id']==variable),{})
        coverage={'start':item.get('recordStart'),'end':item.get('recordEnd'),'validStart':item.get('validStart'),'validEnd':item.get('validEnd')}
    else:
        rows=(daily_reference(db,station,variable) if variable=='level' and station in ('SLC','SLC-N','SLC-E','SLC-S','SLC-W')
              else daily_quality(db,station,variable))
        valid=[r for r in rows if r['value'] is not None]
        coverage={'start':rows[0]['time'] if rows else None,'end':rows[-1]['time'] if rows else None,
                  'validStart':valid[0]['time'] if valid else None,'validEnd':valid[-1]['time'] if valid else None}
    candidate=None
    message=''
    if profile=='candidate' and station!='SLC' and variable=='level':
        analysis=datum_candidates(db)
        candidate=next((c for c in analysis['candidates'] if c['station']==station),None)
        correction=candidate.get('correctionM') if candidate else None
        if correction is None:message=(candidate or {}).get('reason','无可用固定偏移')+'；保留原始日值，不生成平移对照。'
        rows=[{**r,'originalValue':r['value'],'adjustedValue':None if r['value'] is None or correction is None else r['value']+correction,'analysisId':analysis['id']} for r in rows]
    lo=pd.Timestamp(start).floor('D') if start else None;hi=pd.Timestamp(end).floor('D')+pd.Timedelta(days=1) if end else None
    if lo is not None and hi is not None and (hi<=lo or (hi-lo).days>36600):raise ValueError('查询日期范围无效')
    selected=[r for r in rows if (lo is None or pd.Timestamp(r['time'])>=lo) and (hi is None or pd.Timestamp(r['time'])<hi)]
    return {'rows':selected,'profile':profile,'qualityVersion':QUALITY_VERSION,'candidate':candidate,'coverage':coverage,'message':message}
