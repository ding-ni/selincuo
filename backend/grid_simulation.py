"""Grid-forced cold-region XAJ with one continuous, auditable lake anchor."""
from copy import deepcopy
import hashlib
import numpy as np
import pandas as pd
from .common import ROOT,digest,sha_file,read_json,write_json,within
from .model_core import parameters,PARAMETER_NAMES,STATE_NAMES,initial_states
from .parameter_regions import resolve_regions,region_initial_states,run_region_kernel
from .routing import route_partition
from .forcing import load_input,lake_precipitation_factor
from .simulation import metrics,RIVERS
from .observation_profiles import quality_series,QUALITY_VERSION
from . import grid_spatial

PARTITIONS=['duja','zajia_rest','zagen','boqu','ali','fringe','external','unresolved']
_cache={};_prefixes={}

def model_identity():
    # Reuse and continuation also depend on forcing interpretation, spatial
    # loading and the shared evaluation routines used below.
    files = ('model_core.py', 'routing.py', 'parameter_regions.py',
             'grid_simulation.py', 'forcing.py', 'grid_spatial.py', 'simulation.py')
    return digest({'format': 'grid-continuous-v1',
                   'files': {name: sha_file(ROOT / 'backend' / name) for name in files}})

def observations(db):
    result={'profile':QUALITY_VERSION,'level':{},'flow':{}}
    for key,station,variable in [('level','SLC','level'),('flow','DUJA','flow')]:
        series=quality_series(db,station,variable).dropna();result[key]={t.isoformat():float(v) for t,v in series.items()}
    return result

def prepare(db,input_id,spatial_version):
    key=(str(db.workspace),input_id,spatial_version)
    if key not in _cache:
        spatial=grid_spatial.load(spatial_version)
        input_manifest=read_json(db.workspace/'inputs/products'/input_id/'manifest.json')
        source_spatial=grid_spatial.load(input_manifest['spatialVersion'])
        manifest,arrays=load_input(db,input_id,source_spatial)
        if digest(source_spatial['grids'])!=spatial.get('forcingGridVersion',digest(spatial['grids'])):raise ValueError('气象格网坐标或顺序与计算格网不一致')
        _cache.clear();_cache[key]={'spatial':spatial,'manifest':manifest,'arrays':arrays,'times':arrays['time']}
    return _cache[key]

def prefix_identity(source,end,start=None):
    """Hash the legacy prefix, optionally starting at an earlier input's first hour."""
    times=source['times'];begin=0 if start is None else int(times.searchsorted(pd.Timestamp(start),side='left'))
    stop=int(times.searchsorted(pd.Timestamp(end),side='right'))
    if (begin>=len(times) or stop<=begin or times[stop-1]!=pd.Timestamp(end) or
            (start is not None and times[begin]!=pd.Timestamp(start))):
        raise ValueError('当前气象资料未完整覆盖父状态的历史前缀')
    n=stop-begin
    key=(source['manifest']['dataSHA256'],source['manifest'].get('transformationSHA256'),source['spatial']['forcingGridVersion'],begin,stop)
    if key in _prefixes:return _prefixes[key]
    h=hashlib.sha256();h.update(digest({'grids':source['spatial']['forcingGridVersion'],'transformation':source['manifest'].get('transformationSHA256'),'n':n}).encode())
    h.update(times[begin:stop].asi8.tobytes())
    for k in ('precipitation','temperature','evaporation'):
        h.update(k.encode())
        for offset in range(begin,stop,744):h.update(np.ascontiguousarray(source['arrays'][k][offset:min(stop,offset+744)],dtype='<f8').tobytes())
    value=h.hexdigest();_prefixes[key]=value
    return value

def verify_parent_forcing(db,source,parent):
    """Do not count new leading hours as a revision of the parent's history."""
    identifier=parent.get('inputId')
    if identifier and identifier==source['manifest'].get('id'):
        if prefix_identity(source,parent['validAt'])!=parent.get('forcingPrefixSHA256'):
            raise ValueError('历史气象资料已修订，请从有效起点重新模拟')
        return
    rows=[r for r in db.items('input_products') if r['id']==identifier]
    if len(rows)!=1:raise ValueError('父状态对应的历史气象资料不存在')
    manifest=read_json(within(db.workspace/'inputs/products',identifier)/'manifest.json')
    if not manifest or manifest.get('id')!=identifier or digest(manifest)!=digest(rows[0]['manifest']):
        raise ValueError('父状态对应的历史气象资料登记不一致')
    if prefix_identity(source,parent['validAt'],manifest['start'])!=parent.get('forcingPrefixSHA256'):
        raise ValueError('历史气象资料已修订，请从有效起点重新模拟')

def anchor(request,observation,start,end):
    manual=request.get('initialLakeLevel')
    if manual is not None:
        level=float(manual);t=pd.Timestamp(request.get('initialLakeTime') or start)
        if not np.isfinite(level) or not 4000<=level<=6500 or t<start or t>end or t.minute or t.second or t.tzinfo or not str(request.get('initialLakeSource','')).strip():raise ValueError('手填湖位须有有效时刻、高程及来源')
        return {'time':t.isoformat(),'level':level,'method':'user-known-level','source':request['initialLakeSource'],'reference':'SLC-working-datum'}
    values=sorted((pd.Timestamp(t),v) for t,v in observation['level'].items() if start.normalize()<=pd.Timestamp(t)<=end)
    if not values:return None
    t,level=values[0];t=t+pd.Timedelta(hours=12)
    if t>end:return None
    return {'time':t.isoformat(),'level':level,'method':'first-quality-daily-level-at-noon','source':observation['profile'],'reference':'SLC-working-datum'}

def unit_forcing(spatial,arrays,ids,base,regions):
    """Apply the temperature correction before optional HRU area aggregation."""
    units=spatial['units']
    if spatial.get('unitStrategy') == grid_spatial.AGGREGATE_STRATEGY:
        groups=spatial.get('aggregation',[])
        if len(groups)!=len(units):raise ValueError('聚合单元的成员清单不完整')
        columns={'precipitation':[],'temperature':[],'evaporation':[]}
        for unit,group in zip(units,groups):
            if group.get('unitId')!=unit['id'] or not group.get('members'):
                raise ValueError('聚合单元与成员身份不一致')
            members=group['members'];indexes=np.array([m['gridIndex'] for m in members],dtype=int)
            weights=np.array([m['areaWeight'] for m in members],dtype=float)
            dz=np.array([m['elevationOffsetM'] for m in members],dtype=float)
            if (np.any(indexes<0) or np.any(indexes>=len(spatial['grids'])) or
                    not np.isfinite(weights).all() or np.any(weights<=0) or abs(weights.sum()-1)>1e-12 or
                    not np.isfinite(dz).all()):
                raise ValueError('聚合成员的气象索引、高程或面积权重无效')
            lapse=regions.get(unit['partition'],base)['lapseRate']
            for key in columns:
                values=arrays[key][ids][:,indexes]
                if key=='temperature':values=values+dz[None,:]*lapse
                columns[key].append(values@weights)
        return tuple(np.ascontiguousarray(np.stack(columns[k],axis=1)) for k in ('precipitation','temperature','evaporation'))
    grid_ids=np.array([u['gridIndex'] for u in units])
    dz=np.array([u['elevation']-spatial['grids'][u['gridIndex']]['referenceElevation'] for u in units])
    lapse=np.array([regions.get(u['partition'],base)['lapseRate'] for u in units])
    return (np.ascontiguousarray(arrays['precipitation'][ids][:,grid_ids]),
            np.ascontiguousarray(arrays['temperature'][ids][:,grid_ids]+dz[None,:]*lapse[None,:]),
            np.ascontiguousarray(arrays['evaporation'][ids][:,grid_ids]))

def run(db,request,folder=None,progress=None,cancelled=None,prepared=None):
    source=prepared or prepare(db,request['inputId'],request['spatialVersion'])
    spatial=source['spatial'];arrays=source['arrays'];all_times=source['times'];units=spatial['units']
    p=parameters(request.get('parameters'));regions_patch,regions=resolve_regions(units,p,request.get('partitionParameters'))
    start=pd.Timestamp(request.get('start') or '2023-01-01');end=pd.Timestamp(request.get('end') or all_times[-1])
    if start.tzinfo or end.tzinfo or start.minute or end.minute or start.second or end.second or start>end or start<all_times[0] or end>all_times[-1]:raise ValueError('计算起止必须是驱动覆盖内的北京时间整小时')
    indexes=np.flatnonzero((all_times>=start)&(all_times<=end));times=all_times[indexes]
    if not len(times) or times[0]!=start or times[-1]!=end or not np.all(np.diff(times.asi8)==3600*10**9):raise ValueError('计算时段缺少连续小时')
    parent=request.get('_parentState');observation=request.get('_observations') or observations(db)
    initial=(parent or {}).get('initialization') if parent else anchor(request,observation,start,end)
    identity={'projectId':'selinco','modelFamily':'grid-continuous-v1','modelImplementation':request.get('_modelIdentity') or model_identity(),'spatialVersion':spatial['version'],'parameterSHA256':digest(p),'partitionParameterSHA256':digest(regions_patch),
              'initializationSHA256':digest(initial),'observationProfile':observation['profile'],'observationSnapshotSHA256':digest(observation),'forcingGridSHA256':spatial['forcingGridVersion'],'forcingTransformationSHA256':source['manifest'].get('transformationSHA256'),'routingMode':request.get('routingMode','source-separated')}
    if parent:
        # Observations are evaluation inputs, not physical state. Keep both
        # snapshots in their respective results, but allow newly imported
        # observations without resetting an already established lake anchor.
        observation_fields={'observationProfile','observationSnapshotSHA256'}
        numerical_identity=lambda value:{k:v for k,v in value.items() if k not in observation_fields}
        if (parent.get('schemaVersion')!=3 or
                numerical_identity(parent.get('identity',{}))!=numerical_identity(identity)):
            raise ValueError('完整父状态的模型、空间、参数、初始化或汇流身份不一致')
        if pd.Timestamp(parent['validAt'])+pd.Timedelta(hours=1)!=start:raise ValueError('续算必须从完整末状态下一小时开始')
        verify_parent_forcing(db,source,parent)
        if set(parent.get('routing',{}))!=set(PARTITIONS):raise ValueError('父状态缺少分区传播队列')
        if parent.get('unitIds') != [u['id'] for u in units]:raise ValueError('父状态单元顺序不一致')
        for key,route in parent['routing'].items():
            channel=route.get('channel',{});sources=route.get('sources',{})
            rp=regions.get(key,p)
            if set(sources)!= {'interflow','baseflow'} or 'storage' not in channel or len(channel.get('queue',[]))!=int(rp['lagHours'])+2:
                raise ValueError('父状态缺少完整汇流储水或传播队列')
            numbers=np.asarray([channel['storage'],*channel['queue'],*sources.values()],dtype=float)
            if not np.isfinite(numbers).all() or np.min(numbers)<-1e-8:raise ValueError('父汇流状态数值无效')
        state=np.asarray(parent['unitStates'],dtype=float)
        if state.shape!=(len(units),8) or not np.isfinite(state).all() or np.min(state)<-1e-8:raise ValueError('父状态单元形状或数值错误')
    else:state=region_initial_states(units,p,regions)
    areas=np.array([u['areaKm2'] for u in units]);glaciers=np.array([u['kind']=='glacier' for u in units])
    lake_weights=np.array(spatial['lakeWeights']);lake_area=spatial['lakeAreaKm2']*1e6;lake_factor=lake_precipitation_factor(source['manifest'])
    old_routing=deepcopy((parent or {}).get('routing',{}));lake=deepcopy((parent or {}).get('lake',{}));continuous=float((parent or {}).get('continuousLakeVolume',0.))
    total_hours=len(times);output={k:np.full(total_hours,np.nan) for k in [*RIVERS,'lakeLevel','volumeChange','continuousLakeVolumeChange','lakeFlux','lakeRainMm','lakeEvaporationMm','excludedRunoff','snowmeltMm','icemeltMm','etMm','RS M3','RI M3','RG M3','glacierRunoffM3']}
    writer=None
    if folder is not None and not request.get('_metricsOnly'):
        from .grid_storage import Writer
        writer=Writer(folder,total_hours,len(units),len(PARTITIONS))
    balance_max=0.;routing_error_max=0.;partition_totals={key:{'generatedM3':0.,'routedM3':0.} for key in PARTITIONS}
    anchor_time=pd.Timestamp(initial['time']) if initial else None
    for offset in range(0,total_hours,168):
        if cancelled and cancelled():raise InterruptedError('用户停止逐格网计算')
        stop=min(total_hours,offset+168);ids=indexes[offset:stop];local_times=times[offset:stop]
        rain,temperature,evap=unit_forcing(spatial,arrays,ids,p,regions)
        diagnostic,history,state=run_region_kernel(rain,temperature,evap,glaciers,state,units,p,regions)
        balance_max=max(balance_max,float(np.max(np.abs(diagnostic[:,:,7]))))
        if balance_max>1e-6:raise ValueError('逐格网产流水量守恒失败')
        volumes=diagnostic[:,:,:4]*areas[None,:,None]*1000;routed={};stores=[];source_stores=[]
        for partition in PARTITIONS:
            uids=[i for i,u in enumerate(units) if u['partition']==partition]
            volume=volumes[:,uids,:].sum(axis=1) if uids else np.zeros((stop-offset,4))
            flow,stored,final,src,errors=route_partition(volume,old_routing.get(partition,{}),regions.get(partition,p),identity['routingMode'])
            routed[partition]=flow;old_routing[partition]=final;stores.append(stored);source_stores.append(src)
            routing_error_max=max(routing_error_max,max(map(abs,errors.values())))
            partition_totals[partition]['generatedM3']+=float(volume.sum());partition_totals[partition]['routedM3']+=float(flow.sum()*3600)
        q={'duja':routed['duja'],'zajia':routed['duja']+routed['zajia_rest'],**{k:routed[k] for k in ('zagen','boqu','ali','fringe')}}
        q['total']=sum(q[k] for k in ('zajia','zagen','boqu','ali','fringe'))
        lake_rain=arrays['precipitation'][ids]@lake_weights*lake_factor;lake_evap=arrays['evaporation'][ids]@lake_weights*p['lakeCoefficient']
        flux=q['total']*3600+(lake_rain-lake_evap)/1000*lake_area
        levels=np.full(len(ids),np.nan);lake_vol=np.full(len(ids),np.nan);cv=np.empty(len(ids))
        for j,t in enumerate(local_times):
            continuous+=float(flux[j]);cv[j]=continuous
            if anchor_time is not None and t>=anchor_time:
                if not lake:
                    if t!=anchor_time:raise ValueError('缺少连续湖位锚点状态，不能在窗口中途伪造湖位')
                    lake={'level':float(initial['level']),'volumeChange':0.,'validAt':t.isoformat()}
                else:
                    if pd.Timestamp(lake['validAt'])+pd.Timedelta(hours=1)!=t:raise ValueError('连续湖泊状态时次不连续')
                    lake['level']+=float(flux[j]/lake_area);lake['volumeChange']+=float(flux[j]);lake['validAt']=t.isoformat()
                levels[j]=lake['level'];lake_vol[j]=lake['volumeChange']
        local={**q,'lakeLevel':levels,'volumeChange':lake_vol,'continuousLakeVolumeChange':cv,'lakeFlux':flux,'lakeRainMm':lake_rain,'lakeEvaporationMm':lake_evap,'excludedRunoff':(routed['unresolved']+routed['external'])*3600}
        for key,col in [('snowmeltMm',4),('icemeltMm',5),('etMm',6)]:local[key]=(diagnostic[:,:,col]*areas).sum(axis=1)/areas.sum()
        for key,col in [('RS M3',0),('RI M3',1),('RG M3',2),('glacierRunoffM3',3)]:local[key]=(diagnostic[:,:,col]*areas*1000).sum(axis=1)
        for key,value in local.items():output[key][offset:stop]=value
        if writer:writer.put(offset,stop,{'time':local_times.asi8,'flow':np.stack([q[k] for k in RIVERS],axis=1),'unitStates':history,'unitDiagnostics':diagnostic,'routingStorage':np.stack(stores,axis=1),'sourceRoutingStorage':np.stack(source_stores,axis=1),**{k:local[k] for k in ('lakeLevel','volumeChange','continuousLakeVolumeChange','lakeFlux','excludedRunoff')}})
        if progress:progress(5+90*stop/total_hours,f'连续计算 {local_times[-1]} · {len(units)}单元')
    hourly=pd.DataFrame(output,index=times);counts=hourly.total.resample('D',closed='right',label='left').count()
    daily=hourly[list(RIVERS)+['lakeLevel']].resample('D',closed='right',label='left').mean().where(counts.eq(24),axis=0)
    daily['lakeLevel']=hourly.lakeLevel.resample('D',closed='right',label='left').mean().where(hourly.lakeLevel.resample('D',closed='right',label='left').count().eq(24))
    for key in ('lakeRainMm','lakeEvaporationMm','snowmeltMm','icemeltMm','etMm','excludedRunoff','RS M3','RI M3','RG M3','glacierRunoffM3'):
        daily[key.replace(' ','')]=hourly[key].resample('D',closed='right',label='left').sum(min_count=24)
    daily['forcingHours']=counts
    daily['volumeChange']=hourly.volumeChange.resample('D',closed='right',label='left').last()
    for key,value in {'lakeInflowM3':hourly.total*3600,'lakePrecipitationM3':hourly.lakeRainMm/1000*lake_area,'lakeEvaporationM3':hourly.lakeEvaporationMm/1000*lake_area,'lakeDeltaVolumeM3':hourly.lakeFlux}.items():daily[key]=value.resample('D',closed='right',label='left').sum(min_count=24)
    daily['lakeBalanceErrorM3']=daily.lakeDeltaVolumeM3-daily.lakeInflowM3-daily.lakePrecipitationM3+daily.lakeEvaporationM3
    for key,field in [('level','observedLevel'),('flow','observedFlow')]:
        values=observation[key];daily[field]=pd.Series(list(values.values()),index=pd.to_datetime(list(values))).reindex(daily.index)
    daily=daily.loc[(daily.index>=start.normalize())&(daily.index<end.ceil('D'))]
    scored=daily.lakeLevel.copy()
    if anchor_time is not None:scored.loc[scored.index==anchor_time.normalize()]=np.nan
    yearly={str(y):{'purpose':'预热' if y==2023 else '研究评价','flow':metrics(g.observedFlow,g.duja),'lake':metrics(g.observedLevel,scored.reindex(g.index))} for y,g in daily.groupby(daily.index.year)}
    checkpoint={'schemaVersion':3,'identity':identity,'validAt':times[-1].isoformat(),'unitStates':state.tolist(),'unitIds':[u['id'] for u in units],'routing':old_routing,'lake':lake,'initialization':initial,'continuousLakeVolume':continuous,'parameters':p,'partitionParameters':regions_patch,'inputId':source['manifest']['id'],'forcingPrefixSHA256':prefix_identity(source,end),'observationSnapshot':observation}
    result={'projectId':'selinco','resultSource':'grid-continuous-model','identity':identity,'modelFamily':'grid-continuous-v1','start':times[0].isoformat(),'end':times[-1].isoformat(),'hours':len(times),'parameters':p,'partitionParameters':regions_patch,'inputId':source['manifest']['id'],'spatialVersion':spatial['version'],'units':units,'rivers':RIVERS,'partitions':PARTITIONS,'routingMode':identity['routingMode'],'metrics':yearly,'daily':__import__('json').loads(daily.reset_index(names='date').to_json(orient='records',date_format='iso')),
            'initialization':initial,'observationProfile':QUALITY_VERSION,'inflowComplete':spatial['drainage']['inflowComplete'],'drainage':spatial['drainage'],'scheme':request.get('schemeSnapshot'),
            'diagnostics':{'landGlacierMaxBalanceErrorMm':balance_max,'routingMaxBalanceErrorM3':routing_error_max,'lakeAreaKm2':spatial['lakeAreaKm2'],'partitions':partition_totals,'lakeWindows':[] if initial is None else [{'start':initial['time'],'initialization':initial['method']}]},'checkpoint':checkpoint,'storageFormat':'zarr-v2' if writer else 'metrics-only'}
    if writer:
        result['hourlyArtifact']=writer.finish();daily.to_csv(folder/'daily.csv',index_label='date',encoding='utf-8-sig');write_json(folder/'checkpoint.json',checkpoint);write_json(folder/'result.json',result)
    return result
