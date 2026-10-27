"""Explicit immutable overlay of newly retrieved official values, with lineage."""
import numpy as np
import pandas as pd
from .common import digest,read_json,write_json,sha_file,now,canonical
from .forcing import load_input

def merge_products(db,base_id,new_id):
    base,a=load_input(db,base_id);fresh,b=load_input(db,new_id)
    if base['spatialVersion']!=fresh['spatialVersion'] or base.get('conversionVersion')!=fresh.get('conversionVersion'):
        raise ValueError('气象修订的格网或时区、累计量转换规则不同，不能拼接')
    times=a['time'].union(b['time']).sort_values()
    if len(times)>1 and not np.all(np.diff(times.asi8)==3600*10**9):raise ValueError('新旧资料之间缺少小时，不能填零拼接')
    common=a['time'].intersection(b['time']);ia=a['time'].get_indexer(common);ib=b['time'].get_indexer(common)
    changed=np.zeros(len(common),dtype=bool);merged={}
    for key in ('precipitation','temperature','evaporation'):
        if a[key].shape[1]!=b[key].shape[1]:raise ValueError('气象格网数或顺序不同')
        changed|=(a[key][ia]!=b[key][ib]).any(axis=1)
        values=np.empty((len(times),a[key].shape[1]),dtype=np.float64)
        values[times.get_indexer(a['time'])]=a[key];values[times.get_indexer(b['time'])]=b[key]
        if not np.isfinite(values).all():raise ValueError('拼接资料含缺测，拒绝登记')
        merged[key]=values
    policy={'method':'new-official-product-overrides-overlap-v1','baseInputId':base_id,'baseDataSHA256':base['dataSHA256'],'newInputId':new_id,'newDataSHA256':fresh['dataSHA256'],'changedHistoricalHours':int(changed.sum()),'firstChangedTime':common[changed][0].isoformat() if changed.any() else None,'lastChangedTime':common[changed][-1].isoformat() if changed.any() else None}
    identifier='INPUT-'+digest(policy)[:24]
    prior=next((r['manifest'] for r in db.items('input_products') if r['id']==identifier),None)
    if prior:
        _,saved=load_input(db,identifier)
        if not np.array_equal(saved['time'],times) or any(not np.array_equal(saved[k],v) for k,v in merged.items()):raise ValueError('已有修订输入内容不一致，拒绝覆盖')
        return prior
    folder=db.workspace/'inputs/products'/identifier;folder.mkdir(parents=True,exist_ok=True);path=folder/'forcing.npz'
    if path.exists():raise ValueError('修订输入残留文件待核查，拒绝覆盖')
    np.savez_compressed(path,time=times.asi8,**merged)
    sources={s['sha256']:s for s in base.get('sources',[])+fresh.get('sources',[])}
    superseded=set(base.get('supersededSourceDigests',[]))
    for source in base.get('sources',[]):
        from pathlib import Path
        proof=read_json(db.workspace/'downloads/era5-land'/Path(source['name']).with_suffix('.provenance.json'))
        if not proof:continue
        coverage=list(proof.get('verification',{}).get('variables',{}).values())
        if coverage and all(pd.Timestamp(c['startUTC'])+pd.Timedelta(hours=8)>=b['time'][0] and pd.Timestamp(c['endUTC'])+pd.Timedelta(hours=8)<=b['time'][-1] for c in coverage):superseded.add(source['sha256'])
    source_digests=[{k:s[k] for k in ('sha256','variables','bytes')} for _,s in sorted(sources.items())]
    manifest={**base,'id':identifier,'parentInputId':base_id,'name':'ERA5-Land连续驱动 · 官方延伸与修订','createdAt':now(),'start':times[0].isoformat(),'end':times[-1].isoformat(),'hours':len(times),'sources':list(sources.values()),'sourceDigests':source_digests,'data':'forcing.npz','dataSHA256':sha_file(path),'revisionSelection':policy,'supersededSourceDigests':sorted(superseded)}
    write_json(folder/'manifest.json',manifest)
    with db.connect() as c:c.execute('INSERT INTO input_products VALUES(?,?,?,?)',(identifier,manifest['name'],manifest['createdAt'],canonical(manifest)))
    db.log('inputs.official-revision',{'inputId':identifier,**policy});return manifest
