"""Read-only time and space queries for frozen and workspace results."""
from collections import OrderedDict
from threading import RLock
from copy import deepcopy
import numpy as np
import pandas as pd
from .common import ROOT,read_json,model_identity,within
from .simulation import RIVERS,PARTITIONS
STATE=[('swe','雪水当量','mm'),('snowLiquid','雪层液水','mm'),('WU','上层土壤含水量','mm'),('WL','下层土壤含水量','mm'),('WD','深层土壤含水量','mm'),('freeWater','自由水蓄量','mm'),('FR','产流面积比例',''),('glacierStorage','冰川融水调蓄','mm')]
DIAG=[('RS','地表产流','mm'),('RI','壤中产流','mm'),('RG','地下产流','mm'),('glacierRunoff','冰川出流','mm'),('snowmelt','融雪','mm'),('icemelt','融冰','mm'),('actualEt','实际蒸散发','mm'),('balanceError','水量平衡误差','mm'),('iceSublimation','冰面升华','mm')]
_lock=RLock();_arrays=OrderedDict();_sources=OrderedDict();_source_cache_limit=8


def _stat(path):
    try:
        value=path.stat()
        return (str(path.resolve()),value.st_size,value.st_mtime_ns,value.st_ctime_ns,getattr(value,'st_ino',None))
    except FileNotFoundError:return (str(path.resolve()),None)


def _descriptor(db,identifier):
    """Cheap invalidation descriptors; numerical chunks remain immutable.

    A changed frozen Zarr artifact must publish a changed hourly manifest.
    Arbitrary in-place data-chunk tampering without that manifest is outside
    this cache contract, as it is outside frozen-result storage semantics.
    """
    from .published_results import catalog
    row=next((r for r in catalog() if r['id']==identifier),None)
    if row:
        directory=within(ROOT/'data/final-results',row['directory'])
        key=('published',str(ROOT.resolve()),identifier,_stat(ROOT/'data/final-results/catalog.json'),
             tuple(_stat(directory/name) for name in ('result.json','hourly.npz','final-state.json')))
        return key
    rows=db.rows("""SELECT status,updated_at,name,notes,favorite,trashed,collection,
                    json_extract(result,'$.identity') identity,
                    json_extract(result,'$.hourlyArtifact.sha256') artifact_sha,
                    json_extract(result,'$.checkpointSHA256') checkpoint_sha,
                    LENGTH(request) request_bytes,LENGTH(result) result_bytes FROM tasks WHERE id=?""",(identifier,))
    if not rows:raise FileNotFoundError('计算记录不存在或已移走')
    folder=within(db.workspace/'tasks',identifier)
    stamps=[_stat(folder/name) for name in ('checkpoint.json','hourly.npz','hourly-manifest.json','hourly.zarr/.zattrs','hourly.zarr/.zgroup','hourly.zarr/.zmetadata','hourly.zarr/time/.zarray')]
    time_folder=folder/'hourly.zarr/time'
    if time_folder.is_dir():stamps.extend(_stat(path) for path in sorted(time_folder.iterdir()) if path.is_file() and not path.name.startswith('.'))
    return ('workspace',str(db.workspace.resolve()),str(db.path.resolve()),identifier,tuple(sorted(rows[0].items())),tuple(stamps))


def _entry(db,identifier):
    # Hold the lock during a cold build so simultaneous metadata/series/frame
    # requests do not deserialize the same result/checkpoint/time axis twice.
    # Hot keys do no array reads. Capacity bounds handles and time axes, while
    # legacy NPZ arrays retain their separate existing two-entry bound.
    with _lock:
        key=_descriptor(db,identifier)
        if key in _sources:
            _sources.move_to_end(key)
            return _sources[key]
        task,data,state=_open_source(db,identifier)
        times=pd.DatetimeIndex(data['time'][:].astype('datetime64[ns]'))
        if not len(times):raise ValueError('成果时间轴为空')
        # Protect this shared axis from accidental query-side writes.
        times.values.flags.writeable=False
        entry={'key':key,'task':task,'data':data,'state':state,'times':times,'initializationDays':None,'initializationDaysBuilt':False}
        # Remove previous signatures for this exact result, not other workspaces.
        prefix=key[:4] if key[0]=='workspace' else key[:3]
        for prior in list(_sources):
            if prior[:len(prefix)]==prefix:_sources.pop(prior)
        _sources[key]=entry
        while len(_sources)>_source_cache_limit:_sources.popitem(last=False)
        legacy=[cached for cached,value in _sources.items() if isinstance(value['data'],dict)]
        for cached in legacy[:-2]:_sources.pop(cached)
        return entry


def clear_source_cache():
    """Test/maintenance hook; no source files are modified."""
    with _lock:_sources.clear();_arrays.clear()


def source_cache_info():
    with _lock:
        return {'entries':len(_sources),'capacity':_source_cache_limit,
                'timeAxisBytes':sum(entry['times'].nbytes for entry in _sources.values()),
                'zarrHandles':sum(not isinstance(entry['data'],dict) for entry in _sources.values()),
                'legacyArrayEntries':sum(isinstance(entry['data'],dict) for entry in _sources.values()),
                'legacyArrayBytes':sum(sum(value.nbytes for value in entry['data'].values()) for entry in _sources.values() if isinstance(entry['data'],dict))}

def _open_source(db,identifier):
    from .published_results import catalog,detail,result_file
    if any(r['id']==identifier for r in catalog()):
        task=detail(identifier);path=result_file(identifier,'hourly.npz');checkpoint=read_json(result_file(identifier,'final-state.json'))
    else:
        task=db.task(identifier)
        if task['status']!='succeeded' or not task.get('result',{}).get('units'):raise ValueError('请选择已完成的模型成果')
        path=db.workspace/'tasks'/identifier/'hourly.npz';checkpoint=read_json(path.parent/'checkpoint.json')
        if (path.parent/'hourly.zarr').is_dir():
            from .grid_storage import open_arrays
            return task,open_arrays(path.parent),checkpoint
    if not path.is_file():raise FileNotFoundError('该成果未保存逐时状态')
    # The enclosing source cache owns at most two legacy array bundles. Do not
    # retain a second independent NPZ cache, which could double retained memory.
    with np.load(path,allow_pickle=False) as archive:data={k:archive[k] for k in archive.files}
    return task,data,checkpoint


def source(db,identifier):
    entry=_entry(db,identifier)
    return entry['task'],entry['data'],entry['state']

def variables():
    rows=[{'id':k,'label':n,'unit':u,'scope':'unit','kind':'state','aggregation':'日末'} for k,n,u in STATE]
    rows +=[{'id':k,'label':n,'unit':u,'scope':'unit','kind':'flux','aggregation':'日累计'} for k,n,u in DIAG]
    rows +=[{'id':'flow','label':'分区流量','unit':'m³/s','scope':'river','kind':'flow','aggregation':'日均'},{'id':'routingStorage','label':'汇流总储水量','unit':'m³','scope':'partition','kind':'state','aggregation':'日末'},{'id':'interflowStorage','label':'壤中调蓄储水量','unit':'m³','scope':'partition','kind':'state','aggregation':'日末'},{'id':'baseflowStorage','label':'地下调蓄储水量','unit':'m³','scope':'partition','kind':'state','aggregation':'日末'}]
    rows +=[{'id':k,'label':n,'unit':u,'scope':'lake','kind':kind,'aggregation':'日累计' if kind=='flux' else '日末'} for k,n,u,kind in [('lakeLevel','湖泊水位','m','state'),('volumeChange','起算相对蓄水量','m³','state'),('continuousLakeVolumeChange','连续相对水量','m³','state'),('lakeFlux','湖泊净水量','m³','flux')]]
    return rows

def compatible(state):
    if not state:return False
    if state.get('schemaVersion')==3:
        from .grid_simulation import model_identity as grid_identity,PARTITIONS as grid_partitions
        return (state['identity'].get('modelImplementation')==grid_identity() and
                set(state.get('routing',{}))==set(grid_partitions) and
                all('queue' in r.get('channel',{}) for r in state['routing'].values()))
    return state['identity'].get('modelImplementation')==model_identity()


def spatial_base(version):
    """Resolve map assets from the application root, including frozen builds.

    A missing archived geometry must never silently attach current geometry to
    a different result. Only an exact current legacy manifest is compatible.
    """
    if not version:return '/spatial/'
    archive=within(ROOT/'data/derived/spatial/versions',version)
    archived=read_json(archive/'manifest.json')
    if archived:
        if archived.get('version')!=version:
            raise ValueError('成果空间版本与归档空间身份不一致：'+version)
        return '/spatial/versions/'+version+'/'
    current=read_json(ROOT/'data/derived/spatial/manifest.json') or {}
    if current.get('version')==version and current.get('schemaVersion',1)==1:
        return '/spatial/'
    raise FileNotFoundError('成果对应的空间资料未安装，不能使用其他版本替代：'+version)


def frozen_spatial_version(result, state=None):
    """Accept only consistent identities frozen with the result/checkpoint."""
    candidates=[result.get('spatialVersion'),(result.get('scheme') or {}).get('spatialVersion'),
                (result.get('identity') or {}).get('spatialVersion'),
                ((state or {}).get('identity') or {}).get('spatialVersion')]
    versions={v for v in candidates if v}
    if len(versions)>1:raise ValueError('成果、方案与检查点的冻结空间身份互相冲突')
    return next(iter(versions),None)

def metadata(db,identifier):
    entry=_entry(db,identifier);task,data,state,times=(entry[k] for k in ('task','data','state','times'))
    with _lock:
        if not entry['initializationDaysBuilt']:
            windows=task['result'].get('diagnostics',{}).get('lakeWindows')
            if isinstance(windows,list):initialization_days=sorted({w['start'][:10] for w in windows})
            elif task.get('kind')=='continuation' or task.get('request',{}).get('kind')=='continuation':initialization_days=None
            else:
                finite=np.isfinite(data['lakeLevel'][:]);starts=np.flatnonzero(finite & ~np.r_[False,finite[:-1]])
                initialization_days=sorted(set(times[starts].strftime('%Y-%m-%d')))
            entry['initializationDays']=initialization_days;entry['initializationDaysBuilt']=True
        initialization_days=deepcopy(entry['initializationDays'])
    result=task['result'];version=frozen_spatial_version(result,state)
    if not version:
        legacy=read_json(ROOT/'data/derived/spatial/manifest.json') or {}
        if (legacy.get('schemaVersion',1)!=1 or not result.get('units') or
                legacy.get('units')!=result['units']):
            raise FileNotFoundError('成果缺少冻结空间版本，且无法证明与当前旧版计算单元一致')
        version=legacy.get('version')
        if not version:raise FileNotFoundError('旧版空间资料缺少版本身份')
    from .grid_spatial import load
    spatial=load(version) if version else {}
    spatial_path=spatial_base(version)
    return {'id':identifier,'start':times[0].isoformat(),'end':times[-1].isoformat(),'hours':len(times),'units':result['units'],'variables':variables(),'rivers':result.get('rivers',RIVERS),'partitions':result.get('partitions',PARTITIONS),'canContinue':not task.get('builtin',False) and compatible(state),'warmupEnd':task.get('request',{}).get('periods',{}).get('trainingStart','2024-01-01T00:00:00'),'lakeInitializationDays':initialization_days,
            'spatialVersion':version,'spatialBase':spatial_path,'initialization':result.get('initialization'),'storageFormat':result.get('storageFormat','npz'),'inflowComplete':result.get('inflowComplete',True),'drainage':result.get('drainage'),'modelFamily':result.get('modelFamily','legacy-v0.8')}


def range_for(db, query):
    """True saved-period bounds for the same variable and temporal convention.

    Only one variable is read, in bounded blocks. The frozen source descriptor
    owns this cache, so switching objects or dates never scans the period again.
    """
    entry = _entry(db, query['resultId'])
    key = query.get('variable', 'swe')
    definition = next((v for v in variables() if v['id'] == key), None)
    if not definition:
        raise ValueError('状态变量不存在')
    step = query.get('step', 'day')
    if step not in ('day', 'hour'):
        raise ValueError('查询尺度须为逐日或逐小时')
    with _lock:
        cache = entry.setdefault('ranges', {})
        if (key, step) in cache:
            return deepcopy(cache[key, step])
        times, data = entry['times'], entry['data']
        minimum, maximum, count = np.inf, -np.inf, 0

        def collect(block):
            nonlocal minimum, maximum, count
            finite = np.asarray(block).ravel()
            finite = finite[np.isfinite(finite)]
            if finite.size:
                minimum = min(minimum, float(finite.min()))
                maximum = max(maximum, float(finite.max()))
                count += finite.size

        if step == 'hour' or definition['kind'] == 'state':
            for first in range(0, len(times), 744):
                last = min(first + 744, len(times))
                block = values(data, key, slice(first, last))
                if step == 'day':
                    block = block[(times[first:last].hour == 0) & (times[first:last] > times[0])]
                collect(block)
        else:
            # End each day at its actual local midnight. Missing hours must
            # not shift subsequent 24-row groups into another calendar day.
            ends = np.flatnonzero((times.hour == 0) & (times > times[0]))
            valid = [int(i) for i in ends if i >= 23 and
                     np.all(np.diff(times.asi8[i-23:i+1]) == 3600 * 10**9)]
            for first in range(0, len(valid), 31):
                batch = valid[first:first+31]
                offset, last = batch[0]-23, batch[-1]+1
                block = np.asarray(values(data, key, slice(offset, last)))
                shaped = np.stack([block[i-23-offset:i+1-offset] for i in batch])
                aggregate = shaped.mean(axis=1) if definition['kind'] == 'flow' else shaped.sum(axis=1)
                collect(aggregate)
        result = {'variable': key, 'unit': definition['unit'], 'step': step,
                  'minimum': minimum if count else None, 'maximum': maximum if count else None,
                  'count': int(count), 'scope': 'result-period',
                  'start': times[0].isoformat(), 'end': times[-1].isoformat(),
                  'aggregation': definition['aggregation'] if step == 'day' else '逐时保存值',
                  'spatialVersion': entry['task']['result'].get('spatialVersion'),
                  'source': '所选成果全部对象、完整已保存时段', 'exact': True}
        cache[key, step] = result
        return deepcopy(result)

def values(data,key,rows=slice(None),column=slice(None)):
    if key in [r[0] for r in STATE]:return data['unitStates'][rows,column,[r[0] for r in STATE].index(key)]
    if key in [r[0] for r in DIAG]:return data['unitDiagnostics'][rows,column,[r[0] for r in DIAG].index(key)]
    if key in ('interflowStorage','baseflowStorage'):return data['sourceRoutingStorage'][rows,column,0 if key=='interflowStorage' else 1]
    if key not in [r['id'] for r in variables()]:raise ValueError('状态变量不存在')
    return data[key][rows,column] if data[key].ndim>1 else data[key][rows]

def read(db,q,frame=False):
    entry=_entry(db,q['resultId']);task,data,state,times=(entry[k] for k in ('task','data','state','times'));key=q.get('variable','swe');definition=next((v for v in variables() if v['id']==key),None)
    if definition is None:raise ValueError('状态变量不存在')
    if frame:
        t=pd.Timestamp(q.get('time') or times[-1]);idx=times.get_indexer([t])[0]
        if idx<0:raise ValueError('所选时刻没有保存状态')
        vals=np.atleast_1d(values(data,key,idx));ids=[u['id'] for u in task['result']['units']] if definition['scope']=='unit' else task['result'].get('rivers',RIVERS) if definition['scope']=='river' else task['result'].get('partitions',PARTITIONS) if definition['scope']=='partition' else ['lake']
        aggregation={'state':'小时末','flow':'小时均','flux':'小时累计'}[definition['kind']]
        if q.get('step')=='day':
            if t!=t.normalize():raise ValueError('日空间帧请选择北京时间日末（次日00:00）')
            aggregation=definition['aggregation']
            if definition['kind']!='state':
                lo=idx-23
                if lo<0 or times[idx]-times[lo]!=pd.Timedelta(hours=23):vals=np.full(len(ids),np.nan)
                else:
                    block=values(data,key,slice(lo,idx+1));vals=np.atleast_1d(block.mean(axis=0) if definition['kind']=='flow' else block.sum(axis=0))
        return {'time':t.isoformat(),'variable':definition,'aggregation':aggregation,'values':[{'id':i,'value':float(v) if np.isfinite(v) else None} for i,v in zip(ids,vals)],'completeCheckpoint':False,
                'resultId':q['resultId'],'spatialVersion':task['result'].get('spatialVersion'),
                'timeZone':'Asia/Shanghai','scope':definition['scope']}
    idx=int(q.get('unit',0));cols=len(task['result']['units']) if definition['scope']=='unit' else len(task['result'].get('rivers',RIVERS)) if definition['scope']=='river' else len(task['result'].get('partitions',PARTITIONS)) if definition['scope']=='partition' else 1
    if not 0<=idx<cols:raise ValueError('查询单元不存在')
    start=pd.Timestamp(q.get('start') or times[0]);end=pd.Timestamp(q.get('end') or times[-1])
    if start>end or start<times[0] or end>times[-1]:raise ValueError('查询时段超出成果覆盖')
    lo=int(times.searchsorted(start.floor('D') if q.get('step','day')=='day' else start));hi=int(times.searchsorted(end.ceil('D') if q.get('step','day')=='day' else end,side='right'))
    series=pd.Series(values(data,key,slice(lo,hi),idx),index=times[lo:hi])
    if q.get('step','day')=='day':
        resampler=series.resample('D',closed='right',label='left')
        if definition['kind']=='flux':series=resampler.sum(min_count=24)
        elif definition['kind']=='flow':series=resampler.mean().where(resampler.count()==24)
        else:
            series=series[series.index.hour==0].copy();series.index=series.index-pd.Timedelta(days=1)
        series=series[(series.index>=start.floor('D'))&(series.index<end.ceil('D'))]
    else:series=series[(series.index>=start)&(series.index<=end)]
    return {'variable':definition,'rows':[{'time':t.isoformat(),'value':float(v) if np.isfinite(v) else None} for t,v in series.items()]}

def final(db,identifier):
    task,_,state=source(db,identifier)
    return checkpoint_detail(state,task.get('result',{}))


def checkpoint_detail(state,result=None):
    result=result or {}
    return {'state':state,'completeCheckpoint':True,'compatible':compatible(state),
            'units':result.get('units',[]),'variables':[v for v in variables() if v['scope']=='unit' and v['kind']=='state'],
            'spatialVersion':state.get('identity',{}).get('spatialVersion')}
