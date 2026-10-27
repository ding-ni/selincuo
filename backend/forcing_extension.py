"""Extend a registered drive without changing any overlapping historical hour."""
from pathlib import Path
import numpy as np
import pandas as pd
from .common import digest,canonical,now,sha_file,write_json
from .forcing import load_input

def extend(db,current):
    candidates=[r['manifest'] for r in db.items('input_products') if r['id']!=current['id'] and r['manifest']['spatialVersion']==current['spatialVersion'] and r['manifest']['start']<=current['start']]
    candidates.sort(key=lambda m:(m['end'],m['hours']),reverse=True)
    if not candidates:return current
    prior=candidates[0]
    if prior['end']<current['start'] and pd.Timestamp(current['start'])-pd.Timestamp(prior['end'])>pd.Timedelta(hours=1):
        raise ValueError('新增气象与现有驱动之间存在缺测，请补充衔接月份')
    old,a=load_input(db,prior['id']);new,b=load_input(db,current['id'])
    old_t=pd.DatetimeIndex(a['time']);new_t=pd.DatetimeIndex(b['time'])
    common=old_t.intersection(new_t)
    for key in ('precipitation','temperature','evaporation'):
        if len(common) and not np.array_equal(a[key][old_t.get_indexer(common)],b[key][new_t.get_indexer(common)]):
            raise ValueError('新旧气象资料重叠时段数值不一致，未替换标准驱动')
    if old_t[0]<=new_t[0] and old_t[-1]>=new_t[-1]:return prior
    later=new_t>old_t[-1]
    times=old_t.append(new_t[later])
    if len(times)>1 and not np.all(np.diff(times.values).astype('timedelta64[h]').astype(int)==1):
        raise ValueError('扩展后的气象资料不连续')
    identifier='INPUT-'+digest({'parents':[prior['id'],current['id']],'conversion':'registered-hour-extension-v1'})[:24]
    folder=db.workspace/'inputs/products'/identifier;folder.mkdir(parents=True,exist_ok=True)
    path=folder/'forcing.npz'
    if not path.exists():
        np.savez_compressed(path,time=times.values.astype('datetime64[ns]').astype('int64'),**{k:np.concatenate([a[k],b[k][later]],axis=0) for k in ('precipitation','temperature','evaporation')})
    manifest={**current,'id':identifier,'name':'ERA5-Land连续气象驱动','createdAt':now(),'start':times[0].isoformat(),'end':times[-1].isoformat(),'hours':len(times),'data':'forcing.npz','dataSHA256':sha_file(path),'parentInputIds':[prior['id'],current['id']],'sources':prior.get('sources',[])+current.get('sources',[])}
    for key in ('lakePrecipitationCorrection','transformationSHA256'):manifest.pop(key,None)
    write_json(folder/'manifest.json',manifest)
    with db.connect() as c:c.execute('INSERT OR IGNORE INTO input_products VALUES(?,?,?,?)',(identifier,manifest['name'],manifest['createdAt'],canonical(manifest)))
    db.log('inputs.extend',{'id':identifier,'parents':manifest['parentInputIds']})
    return manifest
