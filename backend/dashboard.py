"""Truthful monitoring/replay products; no changes to the hydrological kernel."""
from __future__ import annotations

from functools import lru_cache
import json
import re
import shutil
import threading
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import numpy as np
import pandas as pd

from .common import ROOT, read_json, digest, sha_file, within
from .observations import daily_series
from .forcing import lake_precipitation_factor, lake_precipitation_series

_observations_lock = threading.RLock()
_forcing_lock = threading.RLock()
_observations = {}

def _map_cache_directory(db, source):
    _, size, modified, expected_sha = source
    return within(db.workspace/'numeric-cache'/'map-forcing', f'{expected_sha}-{size}-{modified}')


def _unpack_map_array(folder, file, variable):
    target = folder/(variable+'.npy')
    if not target.is_file():
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder/(variable+'.'+uuid4().hex+'.partial')
        with ZipFile(file) as archive, archive.open(target.name) as reader, partial.open('xb') as writer:
            shutil.copyfileobj(reader, writer, length=1024*1024)
        partial.replace(target)
    return target


@lru_cache(maxsize=4)
def _map_time_axis(folder, source):
    file, _, _, expected_sha = source
    if not (folder/'time.npy').is_file() and sha_file(file) != expected_sha:
        raise ValueError('输入文件摘要发生变化，拒绝使用')
    path = _unpack_map_array(folder, file, 'time')
    return pd.DatetimeIndex(np.load(path, allow_pickle=False).astype('datetime64[ns]'))


def map_input(db, identifier, spatial_version=None):
    from .grid_spatial import load
    from .land_precipitation import validate_manifest
    folder = within(db.workspace/'inputs/products', identifier)
    manifest = read_json(folder/'manifest.json')
    if not manifest or manifest['id'] != identifier or manifest.get('projectId') != 'selinco':
        raise ValueError('输入产品不存在或不属于色林错')
    source = load(manifest['spatialVersion'])
    spatial = load(spatial_version) if spatial_version and spatial_version != manifest['spatialVersion'] else source
    if not spatial:
        raise ValueError('空间资料未建立')
    if digest(source['grids']) != spatial.get('forcingGridVersion', digest(spatial['grids'])):
        raise ValueError('气象格网坐标或顺序与显示空间不一致')
    rule = manifest.get('lakePrecipitationCorrection')
    if rule and digest(rule) != manifest.get('transformationSHA256'):
        raise ValueError('湖面降水转换定义摘要不一致')
    correction = validate_manifest(manifest)
    file = within(folder, manifest['data'])
    stat = file.stat()
    return manifest, spatial, correction, (file, stat.st_size, stat.st_mtime_ns, manifest['dataSHA256'])


def map_times(db, source):
    with _forcing_lock:
        return _map_time_axis(_map_cache_directory(db, source), source)


def map_values(db, source, variables, shape):
    """Keep weather arrays on disk so the map only reads the hours it needs."""
    folder = _map_cache_directory(db, source)
    with _forcing_lock:
        _map_time_axis(folder, source)
        files = {name: _unpack_map_array(folder, source[0], name) for name in variables}
    arrays = {name: np.load(path, mmap_mode='r', allow_pickle=False) for name, path in files.items()}
    if any(array.shape != shape or array.dtype.kind not in 'iuf' for array in arrays.values()):
        raise ValueError('气象输入数组布局与登记格网不一致')
    return arrays


def replay(db, identifier):
    """Return the daily fields and matching map needed by the lake overview."""
    from .business_context import result_task
    from .result_query import result_spatial
    task = result_task(db, identifier)
    result = task.get('result') or {}
    if task['status'] != 'succeeded' or not result.get('daily') or not result.get('inputId'):
        raise ValueError('该成果没有可回放的日过程')
    version, spatial_base = result_spatial(result)
    fields = ('date', 'lakeLevel', 'observedLevel', 'total', 'duja', 'observedFlow',
              'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'lakeInflowM3',
              'lakePrecipitationM3', 'lakeEvaporationM3', 'lakeDeltaVolumeM3')
    daily = [{key: row[key] for key in fields if key in row} for row in result['daily']]
    summary = {key: result.get(key) for key in ('displayCoverage', 'inputId', 'start', 'end', 'inflowComplete')}
    summary['units'] = [{key: unit[key] for key in ('id', 'name', 'partition', 'basin', 'areaKm2') if key in unit}
                        for unit in result.get('units') or []]
    return {'id': task['id'], 'status': task['status'], 'spatialVersion': version,
            'spatialBase': spatial_base, 'result': {**summary, 'spatialVersion': version, 'daily': daily}}


def monitoring(db):
    # Revisions and coverage confirmations invalidate the observation product.
    from .observation_query import revision
    key = revision(db)
    with _observations_lock:
        if key in _observations:
            return _observations[key]
        stations = db.rows('SELECT * FROM stations ORDER BY id')
        for station in stations:
            station['metadata'] = json.loads(station['metadata'])
            variable = {'lake':'level','river':'flow','rain':'precipitation'}.get(station['kind'])
            station['variable'] = variable
            from .observation_profiles import series as query_series
            try:
                series = query_series(db, station['id'], variable,profile='quality-v2') if variable else {'rows':[]}
                station['coverage'] = series.get('coverage', {})
                station['daily'] = [{'date':r['time'][:10],'value':r['value'],
                                     'quality':r.get('quality'),'reason':r.get('reason')} for r in series['rows']]
                rain_rows=query_series(db,station['id'],'precipitation',profile='quality-v2')['rows']
                station['rainDaily']=[{'date':r['time'][:10],'value':r['value']} for r in rain_rows]
            except (ValueError, OSError, RuntimeError) as exc:
                station.update(daily=[],rainDaily=[],coverage={},error=str(exc))
            valid = [r for r in station['daily'] if r['value'] is not None]
            station['latest'] = valid[-1] if valid else None
            station['located'] = station['lon'] is not None and station['lat'] is not None
        result = {'stations':stations,'source':'SQLite原始观测整编日值','timeZone':'Asia/Shanghai',
                  'liveConnected':False,'forecastConnected':False,'warningThresholdConfigured':False,
                  'note':'主站工作基准；四岸基准待核。','latestDate':max((s['latest']['date'] for s in stations if s['latest']),default=None)}
        _observations.clear()
        _observations[key] = result
        return result


def parse_day(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
        raise ValueError('请选择YYYY-MM-DD格式的北京时间日期')
    return pd.Timestamp(value)


def aggregate_frame(times, array, day, operation='sum'):
    """End-of-hour forcing: (local day 00, next day 00], exactly 24 hours."""
    start = parse_day(day)
    times = pd.DatetimeIndex(times)
    expected = pd.date_range(start+pd.Timedelta(hours=1),start+pd.Timedelta(days=1),freq='h')
    ids = times.get_indexer(expected)
    available = ids >= 0
    if not available.all():
        return None, int(available.sum())
    values = np.asarray(array)[ids]
    if not np.isfinite(values).all():
        return None, 24
    return (values.mean(axis=0) if operation=='mean' else values.sum(axis=0)),24


def map_frame(db, input_id, day, variable='precipitation',spatial_version=None,result_id=None):
    if variable not in ('precipitation','temperature','evaporation'):
        raise ValueError('地图变量须为降水、气温或蒸发能力')
    start=parse_day(day)
    expected=pd.date_range(start+pd.Timedelta(hours=1),start+pd.Timedelta(days=1),freq='h')
    manifest,spatial,correction,source=map_input(db,input_id,spatial_version)
    variables=(variable,)
    lake_rule=(manifest.get('lakePrecipitationCorrection') or {}).get('method')
    if (variable=='temperature' and result_id) or (variable=='precipitation' and correction is not None):
        variables=('precipitation','temperature','evaporation')
    elif variable=='precipitation' and lake_rule=='station-warm-rain-grid-v1':
        variables=('precipitation','temperature')
    times=map_times(db,source)
    positions=times.get_indexer(expected)
    hours=int(np.count_nonzero(positions>=0))
    values=None
    if hours==24:
        raw=map_values(db,source,variables,(len(times),len(spatial['grids'])))
        arrays={key:array[positions] for key,array in raw.items()}
        if np.isfinite(arrays[variable]).all():
            values=arrays[variable].mean(axis=0) if variable=='temperature' else arrays[variable].sum(axis=0)
    base={'day':day,'inputId':input_id,'variable':variable,'hours':hours,'timeZone':'Asia/Shanghai',
          'source':'ERA5-Land已登记真实驱动','resolution':'0.1°','spatialVersion':spatial['version'],
          'unit':'℃' if variable=='temperature' else 'mm/d','complete':values is not None,
          'note':'气温为格网基准，未作模型单元高程修正；蒸发为公开蒸发能力，非实测湖面蒸发。'}
    if values is None:
        return {**base,'cells':[],'unitValues':[],'domainMean':None,'lakeMean':None,'message':'驱动缺少完整日资料','reasonCode':'INCOMPLETE_DAY'}
    areas=np.array([u['areaKm2'] for u in spatial['units']])
    if spatial.get('schemaVersion')==2:
        weights=np.zeros((len(areas),len(spatial['grids'])))
        weights[np.arange(len(areas)),[u['gridIndex'] for u in spatial['units']]]=1.
    else:weights=np.array(spatial['weights'])
    lake_weights=np.array(spatial['lakeWeights'])
    domain_weights=(areas@weights+spatial['lakeAreaKm2']*lake_weights)/(areas.sum()+spatial['lakeAreaKm2'])
    lake_raw=float(lake_weights@values)
    lake_factor=lake_precipitation_factor(manifest) if variable=='precipitation' else 1.
    lake_used=lake_raw*lake_factor
    ids=np.arange(24)
    if variable=='precipitation' and lake_rule in ('station-warm-rain-grid-v1','station-monthly-total-grid-v1'):
        lake_used=float(lake_precipitation_series(manifest,spatial,arrays,expected,ids).sum())
    domain_used=float(domain_weights@values)+(lake_used-lake_raw)*spatial['lakeAreaKm2']/(areas.sum()+spatial['lakeAreaKm2'])
    unit_values=weights@values
    if variable=='temperature' and result_id:
        from .business_context import result_task
        from .grid_simulation import parameter_configuration, unit_forcing
        task=result_task(db,result_id)
        result=task.get('result') or task
        if result.get('inputId') != input_id or result.get('spatialVersion') != spatial['version']:
            raise ValueError('悬停气温的成果、驱动及空间不一致')
        request={key:result.get(key) for key in ('parameters','partitionParameters','parameterTransfer')}
        _,_,parameters,regions,_,_=parameter_configuration(spatial,request)
        _,temperature,_=unit_forcing(spatial,arrays,ids,parameters,regions)
        unit_values=temperature.mean(axis=0)
        base['unitValuesRepresent']='model-elevation-corrected'
    if variable=='precipitation' and correction is not None:
        # Reuse the actual per-member model transformation while retaining raw
        # source-grid values for the weather map and the lake correction once.
        from .grid_simulation import unit_forcing
        rain,_,_=unit_forcing(spatial,arrays,ids,
            {'lapseRate':correction.get('memberLapseRateCPerM',-.0065)},{},correction,expected)
        unit_values=rain.sum(axis=0)
        domain_used=float((areas@unit_values+spatial['lakeAreaKm2']*lake_used)/(areas.sum()+spatial['lakeAreaKm2']))
    return {**base,'cells':[{**g,'value':float(v)} for g,v in zip(spatial['grids'],values)],
            'unitValues':[{'id':u['id'],'value':float(v)} for u,v in zip(spatial['units'],unit_values)],
            'domainMean':domain_used,'lakeMean':lake_used,'lakeRawMean':lake_raw,
            'lakePrecipitationFactor':lake_factor,'cellsRepresentRawGrid':True,
            'displaySupport':'raw-grid',
            'domainAreaKm2':float(areas.sum()+spatial['lakeAreaKm2']),
            'domainIncludesLake':True,'displayLabel':'再分析格网日降水' if variable=='precipitation' else base['variable'],
            'message':''}


def snow_frame(db, task_id, day):
    start=parse_day(day)
    from .published_results import catalog
    published=any(row.get('id')==task_id for row in catalog())
    if published:
        from .published_results import detail, result_file
        task=detail(task_id)
    else:
        task=db.task(task_id)
    if task['status']!='succeeded' or not task.get('result',{}).get('units') or task.get('trashed'):
        raise ValueError('请选择有效的历史计算成果')
    result=task['result']
    if result.get('storageFormat')=='zarr-v2':
        from .result_query import read
        target=start+pd.Timedelta(days=1)
        if target>pd.Timestamp(result['end']):return {'day':day,'complete':False,'units':[],'sweMean':None,'message':'暂无日末状态'}
        frame=read(db,{'resultId':task_id,'variable':'swe','time':target.isoformat()},frame=True)
        units=[{**u,'value':v['value']} for u,v in zip(result['units'],frame['values'])]
        areas=np.array([u['areaKm2'] for u in units]);values=np.array([u['value'] for u in units])
        return {'day':day,'complete':True,'units':units,'sweMean':float(areas@values/areas.sum()),'validAt':target.isoformat(),'unit':'mm','spatialVersion':result['spatialVersion']}
    spatial=read_json(ROOT/'data/derived/spatial/manifest.json')
    if result['spatialVersion']!=spatial['version']:
        raise ValueError('成果空间版本不同，不能叠加当前地图')
    data_path=result_file(task_id,'daily-snow.npz') if published else db.workspace/'tasks'/task_id/'hourly.npz'
    with np.load(data_path,allow_pickle=False) as arrays:
        times=pd.to_datetime(arrays['time'])
        target=start+pd.Timedelta(days=1)
        ids=np.flatnonzero(times==target)
        if not len(ids):
            return {'day':day,'complete':False,'units':[],'sweMean':None,'message':'该成果缺少日末状态，不沿用其他时刻。'}
        values=arrays['swe'][ids[0]] if published else arrays['unitStates'][ids[0],:,0]
    units=[{**u,'value':float(v)} for u,v in zip(result['units'],values)]
    areas=np.array([u['areaKm2'] for u in units])
    return {'day':day,'complete':True,'units':units,'sweMean':float(areas@values/areas.sum()),
            'validAt':target.isoformat(),'source':'模型日末雪水当量（非积雪实测）','unit':'mm','taskId':task_id,
            'spatialVersion':spatial['version'],'message':''}
