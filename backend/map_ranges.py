"""Small range summaries; never keep a full state cube in memory."""
from collections import OrderedDict
import numpy as np
import pandas as pd
from .dashboard import aggregate_frame
_cache=OrderedDict()

def range_for(db,q):
    key=(str(db.workspace),q.get('inputId'),q.get('resultId'),q.get('variable'),q.get('start'),q.get('end'))
    if key in _cache:return _cache[key]
    variable=q['variable'];lo=np.inf;hi=-np.inf;count=0
    if variable=='snow':
        from .result_query import source
        task,data,_=source(db,q['resultId']);times=pd.to_datetime(data['time'][:])
        start=pd.Timestamp(q.get('start') or times[0]);end=pd.Timestamp(q.get('end') or times[-1])
        first=int(times.searchsorted(start));last=int(times.searchsorted(end+pd.Timedelta(days=1),side='right'))
        for offset in range(first,last,168):
            stop=min(last,offset+168);ids=np.flatnonzero(times[offset:stop].hour==0)
            if not len(ids):continue
            a=data['unitStates'][offset:stop,:,0][ids]
            values=a[np.isfinite(a)]
            if values.size:lo=min(lo,float(values.min()));hi=max(hi,float(values.max()));count+=values.size
    else:
        if variable not in ('precipitation','temperature','evaporation'):raise ValueError('不支持的地图变量')
        from .grid_simulation import prepare
        from .common import read_json
        manifest=read_json(db.workspace/'inputs/products'/q['inputId']/'manifest.json')
        source=prepare(db,q['inputId'],manifest['spatialVersion']);times=source['times']
        for day in pd.date_range(pd.Timestamp(q.get('start') or times[0]).normalize(),pd.Timestamp(q.get('end') or times[-1]).normalize(),freq='D'):
            a,hours=aggregate_frame(times,source['arrays'][variable],day.strftime('%Y-%m-%d'),'mean' if variable=='temperature' else 'sum')
            if a is not None:lo=min(lo,float(a.min()));hi=max(hi,float(a.max()));count+=a.size
    result={'variable':variable,'minimum':lo if count else None,'maximum':hi if count else None,'count':int(count),'scope':'replay-period'}
    _cache[key]=result
    while len(_cache)>32:_cache.popitem(last=False)
    return result
