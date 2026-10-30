"""Truthful monitoring/replay products; no changes to the hydrological kernel."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import numpy as np
import pandas as pd

from .common import ROOT, read_json, digest
from .observations import daily_series
from .forcing import load_input, lake_precipitation_factor, lake_precipitation_series

_lock = threading.RLock()
_observations = {}
_forcing = {}


def monitoring(db):
    # Revisions and coverage confirmations invalidate the observation product.
    from .observation_query import revision
    key = revision(db)
    with _lock:
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


def map_frame(db, input_id, day, variable='precipitation',spatial_version=None):
    if variable not in ('precipitation','temperature','evaporation'):
        raise ValueError('地图变量须为降水、气温或蒸发能力')
    parse_day(day)
    from .grid_spatial import load
    native=read_json(db.workspace/'inputs/products'/input_id/'manifest.json')
    source_spatial=load(native['spatialVersion'])
    spatial=load(spatial_version) if spatial_version else source_spatial
    if not spatial:
        raise ValueError('空间资料未建立')
    if digest(source_spatial['grids'])!=spatial.get('forcingGridVersion',digest(spatial['grids'])):
        raise ValueError('气象格网坐标或顺序与显示空间不一致')
    product=db.workspace/'inputs/products'/input_id
    key=(str(db.workspace),input_id,spatial['version'],(product/'manifest.json').stat().st_mtime_ns,
         (product/'forcing.npz').stat().st_mtime_ns)
    with _lock:
        if key not in _forcing:
            manifest,arrays=load_input(db,input_id,source_spatial)
            _forcing.clear()
            _forcing[key]=(manifest,arrays)
        manifest,arrays=_forcing[key]
    values,hours=aggregate_frame(arrays['time'],arrays[variable],day,'mean' if variable=='temperature' else 'sum')
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
    if variable=='precipitation' and (manifest.get('lakePrecipitationCorrection') or {}).get('method')=='station-warm-rain-grid-v1':
        times=pd.DatetimeIndex(arrays['time']); start=parse_day(day)
        ids=times.get_indexer(pd.date_range(start+pd.Timedelta(hours=1),start+pd.Timedelta(days=1),freq='h'))
        if np.any(ids<0):raise ValueError('湖面暖雨订正缺少完整日气象')
        lake_used=float(lake_precipitation_series(manifest,spatial,arrays,times,ids).sum())
    domain_used=float(domain_weights@values)+(lake_used-lake_raw)*spatial['lakeAreaKm2']/(areas.sum()+spatial['lakeAreaKm2'])
    unit_values=weights@values
    if variable=='precipitation' and manifest.get('landPrecipitationCorrection') is not None:
        # Reuse the actual per-member model transformation while retaining raw
        # source-grid values for the weather map and the lake correction once.
        from .grid_simulation import unit_forcing
        from .land_precipitation import validate_manifest
        correction=validate_manifest(manifest)
        times=pd.DatetimeIndex(arrays['time'])
        start=parse_day(day)
        ids=times.get_indexer(pd.date_range(start+pd.Timedelta(hours=1),start+pd.Timedelta(days=1),freq='h'))
        if np.any(ids<0):raise ValueError('陆域降水订正缺少完整日气象')
        rain,_,_=unit_forcing(spatial,arrays,ids,{'lapseRate':correction['memberLapseRateCPerM']},{},correction,times)
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
    published=task_id.startswith('BUILTIN-')
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
