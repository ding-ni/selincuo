"""Truthful monitoring/replay products; no changes to the hydrological kernel."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import numpy as np
import pandas as pd

from .common import ROOT, read_json
from .observations import daily_series
from .forcing import load_input

_lock = threading.RLock()
_observations = {}
_forcing = {}


def monitoring(db):
    # Revisions and coverage confirmations invalidate the observation product.
    with db.connect() as conn:
        key = (str(db.workspace), tuple(conn.execute('SELECT COUNT(*),MAX(id) FROM audit').fetchone()),
               tuple(conn.execute('SELECT COUNT(*),MAX(id) FROM observations').fetchone()))
    with _lock:
        if key in _observations:
            return _observations[key]
        stations = db.rows('SELECT * FROM stations ORDER BY id')
        for station in stations:
            station['metadata'] = json.loads(station['metadata'])
            variable = {'lake':'level','river':'flow','rain':'precipitation'}.get(station['kind'])
            station['variable'] = variable
            series = daily_series(db, station['id'], variable).dropna() if variable else pd.Series(dtype=float)
            station['daily'] = [{'date':t.strftime('%Y-%m-%d'),'value':float(v)} for t,v in series.items()]
            station['latest'] = station['daily'][-1] if len(series) else None
            station['located'] = station['lon'] is not None and station['lat'] is not None
        result = {'stations':stations,'source':'SQLite原始观测整编日值','timeZone':'Asia/Shanghai',
                  'liveConnected':False,'forecastConnected':False,'warningThresholdConfigured':False,
                  'note':'主站与四岸分别管理；四岸基准未统一，未登记坐标的岸站仅列表展示。'}
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


def map_frame(db, input_id, day, variable='precipitation'):
    if variable not in ('precipitation','temperature','evaporation'):
        raise ValueError('地图变量须为降水、气温或蒸发能力')
    parse_day(day)
    spatial = read_json(ROOT/'data/derived/spatial/manifest.json')
    if not spatial:
        raise ValueError('空间资料未建立')
    key=(str(db.workspace),input_id,spatial['version'])
    with _lock:
        if key not in _forcing:
            manifest,arrays=load_input(db,input_id,spatial)
            _forcing.clear()
            _forcing[key]=(manifest,arrays)
        manifest,arrays=_forcing[key]
    values,hours=aggregate_frame(arrays['time'],arrays[variable],day,'mean' if variable=='temperature' else 'sum')
    base={'day':day,'inputId':input_id,'variable':variable,'hours':hours,'timeZone':'Asia/Shanghai',
          'source':'ERA5-Land已登记真实驱动','resolution':'0.1°','spatialVersion':spatial['version'],
          'unit':'℃' if variable=='temperature' else 'mm/d','complete':values is not None,
          'note':'气温为格网基准，未作模型单元高程修正；蒸发为公开蒸发能力，非实测湖面蒸发。'}
    if values is None:
        return {**base,'cells':[],'domainMean':None,'lakeMean':None,'message':'该日缺少完整24小时，地图不填零、不沿用其他日期。'}
    areas=np.array([u['areaKm2'] for u in spatial['units']])
    weights=np.array(spatial['weights'])
    lake_weights=np.array(spatial['lakeWeights'])
    domain_weights=(areas@weights+spatial['lakeAreaKm2']*lake_weights)/(areas.sum()+spatial['lakeAreaKm2'])
    return {**base,'cells':[{**g,'value':float(v)} for g,v in zip(spatial['grids'],values)],
            'domainMean':float(domain_weights@values),'lakeMean':float(lake_weights@values),'message':''}


def snow_frame(db, task_id, day):
    start=parse_day(day)
    task=db.task(task_id)
    if task['status']!='succeeded' or not task.get('result',{}).get('units') or task.get('trashed'):
        raise ValueError('请选择有效的历史计算成果')
    result=task['result']
    spatial=read_json(ROOT/'data/derived/spatial/manifest.json')
    if result['spatialVersion']!=spatial['version']:
        raise ValueError('成果空间版本不同，不能叠加当前地图')
    with np.load(db.workspace/'tasks'/task_id/'hourly.npz',allow_pickle=False) as arrays:
        times=pd.to_datetime(arrays['time'])
        target=start+pd.Timedelta(days=1)
        ids=np.flatnonzero(times==target)
        if not len(ids):
            return {'day':day,'complete':False,'units':[],'sweMean':None,'message':'该成果缺少日末状态，不沿用其他时刻。'}
        values=arrays['unitStates'][ids[0],:,0]
    units=[{**u,'value':float(v)} for u,v in zip(result['units'],values)]
    areas=np.array([u['areaKm2'] for u in units])
    return {'day':day,'complete':True,'units':units,'sweMean':float(areas@values/areas.sum()),
            'validAt':target.isoformat(),'source':'模型日末雪水当量（非积雪实测）','unit':'mm','taskId':task_id,
            'spatialVersion':spatial['version'],'message':''}
