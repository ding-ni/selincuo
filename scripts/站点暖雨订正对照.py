"""Register confirmed rain coverage and an independent fixed-parameter comparison.

This script does not start Jobs, change the current forcing, calibrate parameters,
or adopt a result. Every generated product is independently identified.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import sys
import uuid
from urllib.parse import urlencode

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, canonical, digest, now, read_json, sha_file, write_json
from backend.database import Database
from backend.grid_simulation import model_identity, observations, prepare, run
from backend.land_precipitation import METHOD, factors_for_points, register_product
from backend.observations import confirm_coverage
from backend.result_coverage import attach
from backend.simulation import metrics

INPUT_ID='INPUT-c00df956d638351ebeb4e96e'
SPATIAL_ID='AGG-SPATIAL-90fbdd73db960fdda4a6'
BASE_TASK='JOB-88b35b44b27d4e15840c'
PARAMETER_ID='PARAM-bb4c92396eb04820adef'
STATIONS=('SLC','DUJA','BLQ','115DB')
CLUSTERS={'SLC':('SLC','DUJA'),'DUJA':('SLC','DUJA'),
          'BLQ':('BLQ','115DB'),'115DB':('BLQ','115DB')}
EVENT_TEMPERATURE_PAIRING={
    'eventTime':'北京时间实际发生时刻',
    'eventInterval':'[floor(event,h), floor(event,h)+1h)',
    'eraTemperatureEndpoint':'floor(event,h)+1h',
    'dailyInterval':'[当天00:00,次日00:00)，对应ERA结束时刻当天01:00至次日00:00',
}


def confirm_user_coverage(db):
    rows=db.rows('''SELECT c.*,s.sha256,s.path FROM coverage c JOIN sources s ON s.id=c.source_id
        WHERE c.variable='precipitation' AND c.step='irregular'
        AND c.station_id IN ('SLC','DUJA','BLQ')
        AND s.rowid IN (SELECT MAX(rowid) FROM sources GROUP BY path) ORDER BY c.station_id,s.path''')
    selected=[]
    for row in rows:
        # Identify source years from actual source observations, never treat the
        # first/last event as the coverage boundary.
        years=db.rows("SELECT DISTINCT substr(time,1,4) y FROM observations WHERE source_id=?",(row['source_id'],))
        if len(years)!=1 or years[0]['y'] not in ('2025','2026'):
            continue
        year=years[0]['y']
        start=year+'-01-01T00:00:00'
        end='2026-01-01T00:00:00' if year=='2025' else '2026-10-21T00:00:00'
        meta=json.loads(row['metadata'])
        if not (meta.get('coverageConfirmed') and meta.get('zeroForAbsent')
                and not meta.get('stoppedIntervals') and row['start']==start and row['end']==end
                and meta.get('eventTimeMeaning')=='occurrence-time'):
            confirm_coverage(db, {'sourceId':row['source_id'],'station':row['station_id'],
                'variable':'precipitation','start':start,'end':end,'stops':'',
                'eventTimeMeaning':'occurrence-time',
                'note':'2026-10-30用户确认该站该年正常运行、缺行无雨；记录为实际发生时刻，00时属当天；设备主要记录液态降雨。'})
        selected.append({'station':row['station_id'],'year':int(year),'sourceId':row['source_id'],
                         'sourceSHA256':row['sha256'],'start':start,'endExclusive':end})
    if len(selected)!=6 or {(x['station'],x['year']) for x in selected}!={(s,y) for s in ('SLC','DUJA','BLQ') for y in (2025,2026)}:
        raise ValueError('用户确认的六份逐次雨量文件未能逐一对应，停止登记')
    return selected


def season_bounds(year,season):
    if season=='DJF':return pd.Timestamp(year-1,12,1),pd.Timestamp(year,3,1)
    month={'MAM':3,'JJA':6,'SON':9}[season]
    return pd.Timestamp(year,month,1),pd.Timestamp(year,month+3,1)


def event_temperature_endpoint(value):
    """Pair an occurrence with the ERA endpoint of its containing hour."""
    return pd.Timestamp(value).floor('h')+pd.Timedelta(hours=1)


def station_samples(db, source, confirmed):
    times=source['times'];spatial=source['spatial'];arrays=source['arrays']
    locations={r['id']:r for r in db.rows("SELECT id,lon,lat FROM stations WHERE id IN ('SLC','DUJA','BLQ','115DB')")}
    if set(locations)!=set(STATIONS):raise ValueError('站点位置不完整')
    indexes=[]
    for station in STATIONS:
        site=locations[station]
        # 0.1-degree nearest point; longitude distance accounts for latitude.
        distances=[((g['lon']-site['lon'])*np.cos(np.radians(site['lat'])))**2+(g['lat']-site['lat'])**2 for g in spatial['grids']]
        indexes.append(int(np.argmin(distances)))
    selected=(times>=pd.Timestamp('2023-12-01'))&(times<=pd.Timestamp('2026-10-21'))
    dates=times[selected]
    temp=pd.DataFrame(arrays['temperature'][selected][:,indexes],index=dates,columns=STATIONS)
    rain=pd.DataFrame(arrays['precipitation'][selected][:,indexes],index=dates,columns=STATIONS)
    total=rain.resample('D',closed='right',label='left').sum(min_count=24)
    warm=rain.where(temp>=5.,0.).resample('D',closed='right',label='left').sum(min_count=24)
    tmin=temp.resample('D',closed='right',label='left').min()
    count=temp.resample('D',closed='right',label='left').count()
    warmcount=(temp>=5.).resample('D',closed='right',label='left').sum()
    all_dates=pd.date_range('2024-01-01','2026-10-20',freq='D')
    source_by_station=defaultdict(list)
    for c in confirmed:source_by_station[c['station']].append(c)
    result={};snapshot=[];conflicts=[]
    for station,index in zip(STATIONS,indexes):
        daily={}
        for r in db.observations(station,'precipitation',step='day',limit=None):
            snapshot.append({k:r[k] for k in ('source_id','time','step','value','quality')})
            if r['quality'] in ('valid','qualified') and r['value'] is not None:
                daily[r['time'][:10]]=float(r['value'])
        event=defaultdict(list)
        for c in source_by_station[station]:
            for r in db.rows("SELECT source_id,time,step,value,quality FROM observations WHERE source_id=? AND variable='precipitation' AND step='irregular' ORDER BY time,id",(c['sourceId'],)):
                snapshot.append(dict(r));event[r['time'][:10]].append(r)
        rows=[]
        for date in all_dates:
            day=date.date().isoformat();daily_value=daily.get(day)
            if station=='DUJA' and day=='2025-01-01':
                rows.append({'date':date,'coverage':False,'eligible':False,'source':'excluded-suspect'});continue
            coverage=next((c for c in source_by_station[station] if c['start']<=date.isoformat()
                           and (date+pd.Timedelta(days=1)).isoformat()<=c['endExclusive']),None)
            available=daily_value is not None or coverage is not None
            item={'date':date,'coverage':bool(available),'eligible':False,'source':'missing'}
            if not available or date not in count.index or count.at[date,station]!=24:
                rows.append(item);continue
            events=event.get(day,[]) if coverage else []
            valid_events=all(r['quality'] in ('valid','qualified') and r['value'] is not None
                             and float(r['value'])>=0 for r in events)
            event_sum=sum(float(r['value']) for r in events) if valid_events else None
            disagreement=(daily_value is not None and coverage is not None and
                          (event_sum is None or abs(daily_value-event_sum)>1e-6))
            if disagreement:conflicts.append({'station':station,'date':day,'dailyMm':daily_value,'eventMm':event_sum})
            if coverage is None or disagreement or not valid_events:
                # A daily-only or conflicting day can be paired only if the
                # entire day is warm; the daily report keeps precedence.
                if daily_value is not None and tmin.at[date,station]>=5.:
                    item.update(eligible=True,source='daily-all-warm',obsWarm=daily_value,
                                eraWarm=float(total.at[date,station]))
                rows.append(item);continue
            observed=0.;valid_time=True
            for r in events:
                # Event timestamps are actual occurrence times. The ERA
                # endpoint temperature at the containing hour is the proxy.
                hour=event_temperature_endpoint(r['time'])
                if hour not in temp.index or not np.isfinite(temp.at[hour,station]):valid_time=False;break
                if temp.at[hour,station]>=5.:observed+=float(r['value'])
            if valid_time and warmcount.at[date,station]>0:
                item.update(eligible=True,source='confirmed-warm-events',obsWarm=observed,
                            eraWarm=float(warm.at[date,station]))
            rows.append(item)
        result[station]={'location':{'lon':locations[station]['lon'],'lat':locations[station]['lat']},
                         'gridId':spatial['grids'][index]['id'],'days':rows}
    return result,conflicts,digest(sorted(snapshot,key=lambda x:(x['source_id'],x['time'],x['step'])))


def summarize_samples(samples):
    summary={}
    for station,item in samples.items():
        frame=pd.DataFrame(item['days']).set_index('date')
        records=[]
        for year in (2024,2025,2026,2027):
            for season in ('DJF','MAM','JJA','SON'):
                start,end=season_bounds(year,season)
                if start>pd.Timestamp('2026-10-20'):continue
                group=frame[(frame.index>=start)&(frame.index<end)]
                expected=(end-start).days;coverage=int(group.coverage.sum())
                warm=group[group.eligible.fillna(False)]
                observed=float(warm.obsWarm.sum()) if len(warm) else 0.
                era=float(warm.eraWarm.sum()) if len(warm) else 0.
                wet=int((warm.obsWarm>0).sum()) if len(warm) else 0
                usable=coverage/expected>=.8 and wet>=5 and era>=20 and observed>0
                records.append({'seasonYear':year,'season':season,'start':start.date().isoformat(),
                    'endExclusive':end.date().isoformat(),'expectedDays':expected,'coverageDays':coverage,
                    'warmPairedDays':len(warm),'warmObservedRainDays':wet,'gaugeMm':observed,'eraMm':era,
                    'ratio':observed/era if era>0 else None,'usableForFactor':bool(usable)})
        summary[station]=records
    return summary


def make_profile(source, samples, stats, confirmation, snapshot_sha):
    profile={'schemaVersion':1,'method':METHOD,'scope':'land-warm-rain',
        'parentDataSHA256':source['manifest']['dataSHA256'],'parentInputId':INPUT_ID,
        'temperatureThresholdC':5.0,'memberLapseRateCPerM':-.0065,
        'groupLapseRateBoundsCPerM':[-.01,-.002],'modelRainTemperatureMaxC':5.0,
        'radiusKm':100.0,'distanceRegularizationKm':5.0,
        'eligibility':{'minimumCoverageFraction':.8,'minimumObservedWarmRainDays':5,'minimumEraWarmMm':20.,
                       'seasonYears':[2024,2025,2026]},
        'eventTemperaturePairing':deepcopy(EVENT_TEMPERATURE_PAIRING),
        'observationSnapshotSHA256':snapshot_sha,'confirmedEventSources':confirmation,
        'interpretation':'ERA格温暖时近似，缺站温和独立降雪量；2017—2023采用后期站雨估计属于时间外推。',
        'stations':{}}
    for station,item in samples.items():
        factors={}
        for season in ('DJF','MAM','JJA','SON'):
            chosen=[x for x in stats[station] if x['season']==season and x['usableForFactor']]
            obs=sum(x['gaugeMm'] for x in chosen);era=sum(x['eraMm'] for x in chosen)
            if obs>0 and era>0:factors[season]=obs/era
        profile['stations'][station]={**item['location'],'gridId':item['gridId'],'seasonFactors':factors}
    return profile


def station_diagnostics(profile,stats):
    training=deepcopy(profile)
    for station in training['stations']:
        training['stations'][station]['seasonFactors']={row['season']:row['ratio'] for row in stats[station]
            if row['seasonYear']==2025 and row['usableForFactor']}
    comparisons=[]
    for station,site in training['stations'].items():
        for row in stats[station]:
            if row['seasonYear']!=2026 or not row['usableForFactor']:continue
            factor=site['seasonFactors'].get(row['season'])
            if factor is None:continue
            comparisons.append({'station':station,'season':row['season'],'trainingYear':2025,'evaluationYear':2026,
                'factor':factor,'observedWarmMm':row['gaugeMm'],'rawWarmMm':row['eraMm'],
                'correctedWarmMm':factor*row['eraMm']})
    sensitivity=[]
    season_idx={'DJF':0,'MAM':1,'JJA':2,'SON':3}
    for radius in (50.,100.,150.):
        for year in (2025,2026):
            for mode in ('leave-station-out','leave-cluster-out'):
                records=[]
                for station,site in training['stations'].items():
                    for row in stats[station]:
                        if row['seasonYear']!=year or not row['usableForFactor']:continue
                        excluded=(station,) if mode=='leave-station-out' else CLUSTERS[station]
                        factor=float(factors_for_points(training,[(site['lon'],site['lat'])],radius,excluded)[season_idx[row['season']],0])
                        records.append({'station':station,'season':row['season'],'factor':factor,
                            'observedWarmMm':row['gaugeMm'],'rawWarmMm':row['eraMm'],
                            'correctedWarmMm':row['eraMm']*factor,
                            'normalizedAbsoluteError':abs(row['eraMm']*factor-row['gaugeMm'])/row['eraMm']})
                sensitivity.append({'radiusKm':radius,'evaluationYear':year,'mode':mode,'count':len(records),
                    'meanNormalizedAbsoluteError':float(np.mean([x['normalizedAbsoluteError'] for x in records])) if records else None,
                    'records':records})
    return {'timeComparison2025to2026':comparisons,'spatialSensitivity':sensitivity,
            'note':'2025—2026已查看，时间对照属于开发内检验；空间半径固定100 km，不用流量或湖位选择。'}


def comparison_metrics(baseline,corrected,observations):
    left=pd.DataFrame(baseline['daily']).set_index('date');right=pd.DataFrame(corrected['daily']).set_index('date')
    left.index=pd.to_datetime(left.index);right.index=pd.to_datetime(right.index)
    index=left.index.intersection(right.index);left=left.reindex(index);right=right.reindex(index)
    result={'yearly':{}}
    for label,mask in [('all',np.ones(len(index),bool)), *[(str(y),index.year==y) for y in range(2021,2027)]]:
        a=left.loc[mask];b=right.loc[mask]
        periods={}
        for name,obs,sim in [('flow','observedFlow','duja'),('lake','observedLevel','lakeLevel')]:
            paired=a[obs].notna()&a[sim].notna()&b[sim].notna()
            periods[name]={'baseline':metrics(a.loc[paired,obs],a.loc[paired,sim]),
                           'corrected':metrics(a.loc[paired,obs],b.loc[paired,sim])}
        result['yearly'][label]=periods
    result['lakeBalance']={}
    fields=['lakeInflowM3','lakePrecipitationM3','lakeEvaporationM3','lakeDeltaVolumeM3','lakeBalanceErrorM3']
    for name,frame in [('baseline',left),('corrected',right)]:
        frame=frame.loc[frame.index>=pd.Timestamp('2021-04-24')]
        entries={}
        for resolution,rule in [('monthly','MS'),('annual','YS')]:
            rows=frame[fields].resample(rule).sum(min_count=1)
            entries[resolution]=[{'date':date.date().isoformat(),**{k:float(v) if pd.notna(v) else None for k,v in row.items()}}
                                 for date,row in rows.iterrows()]
        result['lakeBalance'][name]=entries
    from backend.lake_calibration import freeze_window,joint_assess
    window=freeze_window(observations['level'],'2021-04-23','2026-09-14',baseline['initialization'])
    result['lakeFourCriteria']={}
    for name,frame in [('baseline',left),('corrected',right)]:
        frame=frame.copy();frame['baseLevel']=frame.lakeLevel;frame['slope']=0.
        result['lakeFourCriteria'][name]=joint_assess(frame,0.,0.,window)
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--verify-task',help='只读验证已生成的对照任务；不写库、不重算完整成果')
    args=parser.parse_args()
    db=Database()
    if args.verify_task:
        from backend.fast_calibration import DujaEvaluator
        task=db.task(args.verify_task);req=task['request'];existing=task['result']
        source=prepare(db,req['inputId'],req['spatialVersion'])
        fast=DujaEvaluator(source,{**req,'search':{}})
        values=fast.daily(req['parameters']);days=fast.days
        rows=pd.DataFrame(existing['daily']).set_index('date');rows.index=pd.to_datetime(rows.index)
        fast_daily=pd.Series(values,index=days).reindex(rows.index)
        valid=fast_daily.notna()&rows.duja.notna()
        diff=(fast_daily[valid]-rows.duja[valid]).abs()
        from backend.grid_storage import open_arrays
        artifact=open_arrays(db.workspace/'tasks'/args.verify_task)
        flow=artifact['flow']
        max_sum=max(float(np.max(np.abs(block[:,6]-np.sum(block[:,1:6],axis=1))))
                    for i in range(0,flow.shape[0],168) for block in [np.asarray(flow[i:i+168])])
        baseline=read_json(db.workspace/'tasks'/args.verify_task/'原气象固定参数日过程.json')
        before=db.task(req['baselineTaskId'])['result'];original=pd.DataFrame(before['daily']).set_index('date')
        original.index=pd.to_datetime(original.index)
        original=original.reindex(rows.index)
        baseline_rows=pd.DataFrame(baseline['daily']).set_index('date');baseline_rows.index=pd.to_datetime(baseline_rows.index)
        baseline_rows=baseline_rows.reindex(rows.index)
        reproduce={}
        for key in ('duja','total','lakeLevel','lakeRainMm','lakeEvaporationMm'):
            pair=pd.concat([baseline_rows[key],original[key]],axis=1).dropna()
            reproduce[key]=float((pair.iloc[:,0]-pair.iloc[:,1]).abs().max()) if len(pair) else None
        assessment=comparison_metrics(baseline,existing,req['_observations'])
        print(json.dumps({'taskId':args.verify_task,'fastDaysCompared':int(valid.sum()),
            'maximumFastVsFullFlow':float(diff.max()),'maximumRiverSumError':max_sum,
            'landBalanceMaxMm':existing['diagnostics']['landGlacierMaxBalanceErrorMm'],
            'routingBalanceMaxM3':existing['diagnostics']['routingMaxBalanceErrorM3'],
            'baselineOriginalReproductionMaxAbs':reproduce,'lakeFourCriteria':assessment['lakeFourCriteria']},ensure_ascii=True),flush=True)
        return
    baseline_task=db.task(BASE_TASK)
    if baseline_task['status']!='succeeded' or baseline_task['result']['inputId']!=INPUT_ID:
        raise ValueError('原采用成果或输入身份不一致')
    confirmation=confirm_user_coverage(db)
    print('已核对六份来源覆盖；加载原气象与空间',flush=True)
    source=prepare(db,INPUT_ID,SPATIAL_ID)
    samples,conflicts,snapshot_sha=station_samples(db,source,confirmation)
    stats=summarize_samples(samples)
    profile=make_profile(source,samples,stats,confirmation,snapshot_sha)
    station_check=station_diagnostics(profile,stats)
    print('站点季节倍率',json.dumps({k:v['seasonFactors'] for k,v in profile['stations'].items()},ensure_ascii=True),flush=True)
    manifest=register_product(db,INPUT_ID,profile)
    derived={'manifest':manifest,'arrays':source['arrays'],'times':source['times'],'spatial':source['spatial']}
    print('独立气象输入',manifest['id'],flush=True)
    if args.prepare_only:
        folder=db.workspace/'diagnostics'/('站点暖雨资料-'+uuid.uuid4().hex[:12]);folder.mkdir(parents=True,exist_ok=False)
        write_json(folder/'站点样本与订正方案.json',{'inputId':manifest['id'],'profile':profile,'statistics':stats,
            'conflictingDailyRecords':conflicts,'diagnostics':station_check})
        print('样本报告',str(folder),flush=True)
        return
    base_result=baseline_task['result']
    params=deepcopy(base_result['parameters'])
    if digest(params)!=digest(read_json(ROOT/'config/operational/湖位联合优选方案-20261029.json')['payload']['parameters']):
        raise ValueError('固定参数与已采用参数快照不一致')
    if db.setting('business-adopted-scheme-v1',{}).get('resultId')!=BASE_TASK:
        raise ValueError('当前采用成果已变化，请先核对后再做固定参数对照')
    if (db.setting('current-model-forcing') or {}).get('inputId')!=INPUT_ID:
        raise ValueError('当前气象设置已变化，请先核对后再做固定参数对照')
    request={'projectId':'selinco','modelFamily':'grid-continuous-v1','purpose':'development',
        'name':'站点暖雨订正固定参数对照','inputId':INPUT_ID,'spatialVersion':SPATIAL_ID,
        'start':base_result['start'],'end':base_result['end'],'parameters':params,'partitionParameters':{},
        'parameterTransfer':deepcopy(base_result['parameterTransfer']),'routingMode':base_result['routingMode'],
        'initialHydrology':deepcopy(base_result['initialHydrology']),
        '_fixedInitialization':deepcopy(base_result['initialization']),'_observations':observations(db),
        '_modelIdentity':model_identity(),'periods':deepcopy(baseline_task['request']['periods']),
        'evaluationStart':'2021-04-23T00:00:00','evaluationEnd':'2026-10-21T00:00:00',
        'parameterId':PARAMETER_ID,'baselineTaskId':BASE_TASK}
    print('开始相同代码、固定参数的原输入计算',flush=True)
    last=[-1]
    def progress(percent,message):
        decile=int(percent//10)
        if decile>last[0]:last[0]=decile;print(f'{percent:.0f}% {message}',flush=True)
    baseline=run(db,request,prepared=source,progress=progress)
    trial_id='JOB-'+uuid.uuid4().hex[:20];folder=db.workspace/'tasks'/trial_id;folder.mkdir(parents=True,exist_ok=False)
    trial_request={**request,'inputId':manifest['id']}
    write_json(folder/'request.json',trial_request)
    last[0]=-1
    print('开始独立暖雨输入完整成果',trial_id,flush=True)
    corrected=run(db,trial_request,folder=folder,prepared=derived,progress=progress)
    comparison=comparison_metrics(baseline,corrected,request['_observations'])
    report={'schemaVersion':1,'createdAt':now(),'comparisonTaskId':trial_id,'baselineTaskId':BASE_TASK,
        'baselineInputId':INPUT_ID,'comparisonInputId':manifest['id'],'parameterId':PARAMETER_ID,
        'parametersSHA256':digest(params),'modelImplementation':request['_modelIdentity'],
        'landTransformationSHA256':manifest['landTransformationSHA256'],
        'lakeTransformationSHA256':manifest['transformationSHA256'],'profile':profile,'stationStatistics':stats,
        'observationMethod':{'seasonDefinition':'DJF由上一年12月至本年2月；MAM为3—5月，JJA为6—8月，SON为9—11月',
            'coverage':'原始日表或用户确认的完整逐次观测日；分母为完整季节自然日',
            'dailyPrecedence':'冲突时优先日表，只有全日暖时进入日值配对',
            'eventTimestamp':'实际发生时刻，00:00属于当日；匹配floor(发生时刻,小时)+1小时的ERA温度',
            'hourlyEraSelection':'仅取同日结束时刻对应ERA气温至少5℃的降水小时',
            'warmSources':'逐次暖时按事件求和；只有日表时须全日24小时均暖',
            'parameterEstimation':'对所有达标的站季年按同日站雨与ERA暖时量分别求和后取比值',
            'excludedObservations':['DUJA 2025-01-01 847.1 mm 待核']},
        'conflictingDailyRecords':conflicts,'stationDiagnostics':station_check,'fixedParameterComparison':comparison,
        'adopted':False,'limitations':['站点无气温，格温暖时是液态雨近似','2017—2023站雨因子由后期资料外推',
            '2025、2026已用于开发和最终系数，不是未触碰的外部验证','未重新率定，也未自动采用']}
    report_path=folder/'站点暖雨对照报告.json';write_json(report_path,report)
    baseline_path=folder/'原气象固定参数日过程.json';write_json(baseline_path,{'identity':baseline['identity'],
        'initialization':baseline['initialization'],'diagnostics':baseline['diagnostics'],'daily':baseline['daily']})
    attach(corrected,trial_request)
    corrected.update(taskId=trial_id,checkpointSHA256=digest(corrected['checkpoint']))
    report_url='/api/model/task-artifact?'+urlencode({'taskId':trial_id,'name':report_path.name})
    corrected['artifacts']=[{'name':p.name,'filename':p.name,'storage':'task','ownerTaskId':trial_id,
        'bytes':p.stat().st_size,'sha256':sha_file(p),'mtimeNs':p.stat().st_mtime_ns,'complete':True,
        'downloadUrl':'/api/model/task-artifact?'+urlencode({'taskId':trial_id,'name':p.name})}
        for p in (report_path,baseline_path)]
    state_id='STATE-'+trial_id[4:];corrected['stateId']=state_id
    write_json(folder/'result.json',corrected)
    created=now()
    with db.connect() as connection:
        connection.execute('''INSERT INTO tasks(id,kind,status,created_at,updated_at,progress,message,request,result,name,notes,collection,user_saved)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(trial_id,'trial','succeeded',created,created,100.,'完成',canonical(trial_request),
                canonical(corrected),'站点暖雨订正固定参数对照','独立对照，未采用','开发试验',0))
        connection.execute('INSERT INTO states VALUES(?,?,?,?,?,?,?)',
            (state_id,trial_id,created,corrected['checkpoint']['validAt'],canonical(corrected['checkpoint']),'',0))
        setting={'inputId':manifest['id'],'createdAt':created,
            'reportFile':report_path.relative_to(db.workspace).as_posix(),'reportUrl':report_url,
            'stations':{key:value['seasonFactors'] for key,value in profile['stations'].items()},
            'scope':'land-warm-rain','adopted':False,'baselineTaskId':BASE_TASK,'comparisonTaskId':trial_id}
        connection.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                           ('rainfall-correction-comparison',canonical(setting)))
    db.log('station-rainfall-comparison.completed',{'taskId':trial_id,'inputId':manifest['id'],'reportSHA256':sha_file(report_path)})
    print(json.dumps({'inputId':manifest['id'],'taskId':trial_id,'reportFile':setting['reportFile'],
        'metrics':comparison['yearly']['all'],'adopted':False},ensure_ascii=True),flush=True)


if __name__=='__main__':main()
