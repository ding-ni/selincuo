"""Small range summaries; never keep a full state cube in memory."""
from collections import OrderedDict
import json
import numpy as np
import pandas as pd
from .dashboard import aggregate_frame
from .common import digest, read_json
_cache=OrderedDict()

def range_for(db,q):
    product=db.workspace/'inputs/products'/str(q.get('inputId') or '')
    def stamp(path):
        try:
            s=path.stat()
            return [s.st_size,s.st_mtime_ns]
        except FileNotFoundError:return None
    variable=q['variable']
    # The inputs are immutable versions; include their actual manifest/data
    # descriptors as well as the result manifest for cache invalidation.
    identity={'version':'raw-grid-v2','workspace':str(db.workspace),'inputId':q.get('inputId'),
              'spatialVersion':q.get('spatialVersion'),'variable':variable,'start':q.get('start'),
              'end':q.get('end'),'manifest':stamp(product/'manifest.json'),
              'forcing':stamp(product/'forcing.npz')}
    if variable=='snow':
        from .result_query import _descriptor
        identity['result']=_descriptor(db,q['resultId'])
    key=digest(identity)
    if key in _cache:return _cache[key]
    cache_path=db.workspace/'cache'/'map-ranges'/f'{key}.json'
    try:
        saved=read_json(cache_path)
        if saved and saved.get('identity')==key:
            _cache[key]=saved['range']
            return saved['range']
    except (OSError,ValueError,KeyError,TypeError):
        pass
    lo=np.inf;hi=-np.inf;count=0;samples=[]
    def collect(a):
        nonlocal lo,hi,count
        values=np.asarray(a).ravel();values=values[np.isfinite(values)]
        if not values.size:return
        lo=min(lo,float(values.min()));hi=max(hi,float(values.max()));count+=values.size
        samples.extend(values[::max(1,int(np.ceil(values.size/64)))].tolist())
        if len(samples)>65536:samples[:]=samples[::2]
    if variable=='snow':
        from .result_query import source
        task,data,_=source(db,q['resultId']);times=pd.to_datetime(data['time'][:])
        start=pd.Timestamp(q.get('start') or times[0]);end=pd.Timestamp(q.get('end') or times[-1])
        first=int(times.searchsorted(start+pd.Timedelta(days=1)));last=int(times.searchsorted(end+pd.Timedelta(days=1),side='right'))
        for offset in range(first,last,168):
            stop=min(last,offset+168);ids=np.flatnonzero(times[offset:stop].hour==0)
            if not len(ids):continue
            a=data['unitStates'][offset:stop,:,0][ids]
            collect(a)
    else:
        if variable not in ('precipitation','temperature','evaporation'):raise ValueError('不支持的地图变量')
        from .grid_simulation import prepare
        manifest=read_json(db.workspace/'inputs/products'/q['inputId']/'manifest.json')
        source=prepare(db,q['inputId'],q.get('spatialVersion') or manifest['spatialVersion']);times=source['times']
        for day in pd.date_range(pd.Timestamp(q.get('start') or times[0]).normalize(),pd.Timestamp(q.get('end') or times[-1]).normalize(),freq='D'):
            a,hours=aggregate_frame(times,source['arrays'][variable],day.strftime('%Y-%m-%d'),'mean' if variable=='temperature' else 'sum')
            if a is not None:collect(a)
    positive=np.asarray([v for v in samples if v>0])
    result={'variable':variable,'minimum':lo if count else None,'maximum':hi if count else None,'count':int(count),'scope':'replay-period',
            'positiveQuantiles':np.quantile(positive,[.2,.4,.6,.8,.95]).tolist() if positive.size else [],
            'sampling':'bounded-systematic-max65536','sampleCount':len(samples),
            'displaySupport':'model-units' if variable=='snow' else 'raw-grid'}
    _cache[key]=result
    while len(_cache)>32:_cache.popitem(last=False)
    # Cache files are append-only: concurrent calculations may race to create
    # the same identity, but never overwrite a result or remove a file.
    try:
        cache_path.parent.mkdir(parents=True,exist_ok=True)
        with cache_path.open('x',encoding='utf-8',newline='\n') as stream:
            json.dump({'identity':key,'range':result},stream,ensure_ascii=False,allow_nan=False)
    except (FileExistsError,OSError):
        pass
    return result
