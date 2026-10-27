"""User-triggered official coverage and immutable sequential acquisition."""
import calendar,time
from pathlib import Path
import numpy as np
import pandas as pd
import requests
from .common import ROOT,read_json,write_json,sha_file,digest,now
from .acquisition import AREA,VARIABLES,classify_error

CATALOG='https://cds.climate.copernicus.eu/api/catalogue/v1/collections/reanalysis-era5-land'

def provenance_summary(db,manifest):
    records=[]
    active=[s for s in manifest.get('sources',[]) if s['sha256'] not in manifest.get('supersededSourceDigests',[])]
    for source in active:
        path=db.workspace/'downloads/era5-land'/Path(source['name']).with_suffix('.provenance.json')
        record=read_json(path)
        if not record or record.get('verification',{}).get('sha256')!=source['sha256']:continue
        raw=record['verification'].get('expver');values=np.asarray(raw).ravel().tolist() if raw is not None else []
        versions=sorted({str(v).zfill(4) for v in values})
        records.append({'name':source['name'],'sha256':source['sha256'],'experimentVersions':versions,
                        'status':'provisional' if versions==['0005'] else 'final' if versions==['0001'] else 'mixed' if versions else 'undeclared',
                        'coverage':record['verification']['variables'],'catalogUpdated':record.get('catalog',{}).get('catalogUpdated')})
    return {'hasProvisional':any('0005' in r['experimentVersions'] for r in records),'sources':records,
            'unclassifiedSourceCount':len(active)-len(records),'archivedSourceCount':len(manifest.get('sources',[]))-len(active),'method':'raw-NetCDF-expver; 0001 final / 0005 provisional',
            'policy':'frozen files and input retained; revisions require a new input and prefix check'}

def availability():
    response=requests.get(CATALOG,timeout=(10,30));response.raise_for_status();value=response.json()
    interval=value['extent']['temporal']['interval'][0]
    return {'dataset':'reanalysis-era5-land','source':CATALOG,'checkedAt':now(),'catalogUpdated':value.get('updated'),'availableStartUTC':interval[0],'availableEndUTC':interval[1],
            'policy':'latest-available; provisional files kept immutable; final complete local day verified from all three variables',
            'variables':['2m_temperature','total_precipitation','potential_evaporation'],'provisionalAllowed':True}

def inspect(path):
    import xarray as xr
    result={}
    with xr.open_dataset(path,engine='h5netcdf') as ds:
        for variable,aliases in VARIABLES.items():
            name=next((n for n in aliases if n in ds),None)
            if not name:raise ValueError('下载缺少'+variable)
            a=ds[name]
            if 'expver' in a.dims:
                merged=a.isel(expver=0,drop=True)
                for index in range(1,a.sizes['expver']):merged=merged.combine_first(a.isel(expver=index,drop=True))
                a=merged
            t=next((v for v in ('valid_time','time') if v in a.coords),None)
            if not t:raise ValueError('下载缺少逐时坐标')
            times=pd.DatetimeIndex(a[t].values.ravel())
            if times.has_duplicates or times.hasnans:raise ValueError('下载时间重复或无效')
            if len(times)>1 and not np.all(np.diff(times.asi8)==3600*10**9):raise ValueError('下载缺少连续逐小时资料')
            a.load()
            if not np.isfinite(a.values).all():raise ValueError('下载变量存在缺测，不能登记为完整资料')
            result[variable]={'startUTC':times.min().isoformat(),'endUTC':times.max().isoformat(),'hours':len(times),'units':str(a.attrs.get('units')),'accumulation':a.attrs.get('GRIB_stepType')}
        exp=ds.coords.get('expver')
        version=np.asarray(exp).tolist() if exp is not None else ds.attrs.get('expver')
    return {'variables':result,'sha256':sha_file(path),'bytes':Path(path).stat().st_size,'expver':version}


def acquisition_window(first, meta, start_date=None, end_date=None):
    """Plan whole UTC download days for inclusive Beijing business dates."""
    from datetime import date
    def day(value):
        if value is None or value == '':
            return None
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError()
            return pd.Timestamp(parsed)
        except (TypeError, ValueError):
            raise ValueError('气象更新日期须为YYYY-MM-DD') from None
    start_day, end_day = day(start_date), day(end_date)
    if start_day is not None and end_day is not None and start_day > end_day:
        raise ValueError('气象更新结束日期不能早于开始日期')
    base_start, base_end = pd.Timestamp(first['start']), pd.Timestamp(first['end'])
    catalog_start = pd.Timestamp(meta['availableStartUTC']).tz_localize(None)
    catalog_end = pd.Timestamp(meta['availableEndUTC']).tz_localize(None)
    latest = catalog_end.floor('D') + pd.Timedelta(hours=23)
    if end_day is not None:
        local_end = end_day + pd.Timedelta(days=1)
        if local_end - pd.Timedelta(hours=8) > latest:
            raise ValueError('所选结束日期超过官方气象资料可用范围')
        if local_end < base_start - pd.Timedelta(hours=1):
            raise ValueError('所选时段与已有驱动之间有缺口，请将结束日期延伸至现有资料起点')
        last = (local_end - pd.Timedelta(hours=8)).floor('D') + pd.Timedelta(hours=23)
    else:
        last = latest
    if start_day is not None:
        if start_day > base_end + pd.Timedelta(hours=1):
            raise ValueError('所选时段与已有驱动之间有缺口，请从现有资料末尾开始补充')
        # One UTC day supplies the accumulator/reset boundary; data are never fabricated.
        start = (start_day - pd.Timedelta(hours=8)).floor('D') - pd.Timedelta(days=1)
        if start < catalog_start.floor('D'):
            raise ValueError('所选开始日期及累计量边界早于官方可用范围')
    else:
        start = (base_end - pd.Timedelta(hours=8)).floor('D')
    if (start_date or end_date) and start > last:
        raise ValueError('所选时段没有需要更新的小时，请调整日期')
    return start, last


def acquire_latest(db,progress=None,cancelled=lambda:False,start_date=None,end_date=None):
    import cdsapi
    folder=db.workspace/'downloads/era5-land';folder.mkdir(parents=True,exist_ok=True)
    meta=availability();write_json(folder/'latest-catalog.json',meta)
    products=db.items('input_products')
    if not products:raise ValueError('先接入包含预热期的基础驱动，再补齐最新资料')
    from .operational_scheme import definition
    fixed=definition();eligible=[r for r in products if r['manifest']['start']<=fixed['initialization']['origin'] and r['manifest']['spatialVersion']==fixed['spatialVersion']]
    if not eligible:raise ValueError('缺少覆盖统一预热起点的基础驱动，不能用独立片段替代连续历史')
    first=max(eligible,key=lambda r:(r['manifest']['end'],bool(r['manifest'].get('lakePrecipitationCorrection')),r.get('created_at','')))['manifest']
    start,last=acquisition_window(first,meta,start_date,end_date)
    previous=read_json(folder/'latest-status.json',{})
    if not start_date and not end_date and last<=pd.Timestamp(first['end'])-pd.Timedelta(hours=8) and meta['catalogUpdated']==previous.get('catalog',{}).get('catalogUpdated'):return {'status':'up-to-date','catalog':meta,'inputId':first['id']}
    provisional=[pd.Timestamp(v['startUTC']).floor('D')-pd.Timedelta(days=1) for s in provenance_summary(db,first)['sources'] if '0005' in s['experimentVersions'] for v in s['coverage'].values()]
    if provisional and not start_date and not end_date:start=min(start,min(provisional))
    status={'status':'running','at':now(),'catalog':meta,'completed':[],'startUTC':start.isoformat(),'requestedEndUTC':last.isoformat(),
            'requestedStartDate':start_date,'requestedEndDate':end_date}
    def record(**kw):status.update(kw,at=now());write_json(folder/'latest-status.json',status)
    def check():
        if cancelled():raise InterruptedError('用户停止更新，已完成文件与请求保留')
    downloaded=[]
    try:
        client=cdsapi.Client(wait_until_complete=False,quiet=True,timeout=90,retry_max=3)
        client.client.get_jobs(limit=1)
        periods=pd.period_range(start,last,freq='M')
        for i,period in enumerate(periods):
            check();lo=max(start,period.start_time);hi=min(last,period.end_time.floor('h'))
            request={'variable':meta['variables'],'year':str(period.year),'month':f'{period.month:02}',
                     'day':[f'{d:02}' for d in range(lo.day,hi.day+1)],'time':[f'{h:02}:00' for h in range(24)],'area':AREA,'data_format':'netcdf','download_format':'unarchived'}
            identity=digest({'request':request,'catalogUpdated':meta['catalogUpdated']})[:16]
            target=folder/f'era5-land-{period.year}{period.month:02}-{lo.day:02}-{hi.day:02}-{identity}.nc'
            if target.is_file():
                verified=inspect(target);downloaded.append(target);status['completed'].append(target.name);continue
            record(stage='request',currentMonth=str(period))
            if progress:progress(i/len(periods)*75,f'{period}：补齐官方逐时资料')
            saved=read_json(target.with_suffix('.request.json'))
            remote=client.client.get_remote(saved['requestId']) if saved else None
            if remote is None:remote=client.retrieve('reanalysis-era5-land',request)
            write_json(target.with_suffix('.request.json'),{'requestId':remote.request_id,'request':request,'catalog':meta})
            while remote.status in ('accepted','running'):
                check()
                for _ in range(5):check();time.sleep(1)
            check();result=remote.get_results();size=result.content_length
            parts=folder/'.latest-parts'/identity;parts.mkdir(parents=True,exist_ok=True);step=1024*1024
            record(stage='transfer',bytesExpected=size)
            for offset in range(0,size,step):
                check();end=min(size-1,offset+step-1);part=parts/str(offset);checksum=part.with_suffix('.sha256')
                if part.is_file() and part.stat().st_size==end-offset+1 and checksum.is_file() and sha_file(part)==checksum.read_text():continue
                for attempt in range(3):
                    try:
                        response=requests.get(result.location,headers={'Range':f'bytes={offset}-{end}','Accept-Encoding':'identity'},timeout=(15,40))
                        response.raise_for_status();break
                    except requests.RequestException:
                        if attempt==2:raise
                        for _ in range(2*(attempt+1)):check();time.sleep(1)
                response.raise_for_status()
                if response.status_code!=206 or response.headers.get('Content-Range')!=f'bytes {offset}-{end}/{size}' or len(response.content)!=end-offset+1:raise ValueError('下载字节范围不完整')
                part.write_bytes(response.content);checksum.write_text(sha_file(part))
            partial=target.with_suffix('.partial')
            with partial.open('wb') as stream:
                for offset in range(0,size,step):stream.write((parts/str(offset)).read_bytes())
            verified=inspect(partial)
            if any(pd.Timestamp(v['startUTC'])>lo or pd.Timestamp(v['endUTC'])<hi for v in verified['variables'].values()):raise ValueError('官方返回资料未完整覆盖所请求时段，请重新检查可用日期')
            partial.replace(target)
            write_json(target.with_suffix('.provenance.json'),{'request':request,'catalog':meta,'verification':verified,'revisionStatus':'provisional-or-final-as-delivered','immutable':True})
            downloaded.append(target);status['completed'].append(target.name);record(stage='verified')
        check()
        from .forcing import register_era5,register_lake_correction
        from .operational_scheme import definition
        # Include historical raw files for accumulated UTC boundary hours; never synthesize a halo.
        if progress:progress(80,'核验新增时段与官方修订，原输入保持冻结')
        fresh=register_era5(downloaded,db,name='ERA5-Land本次官方资料',progress=lambda p,s:progress(80+p*.12,s) if progress else None)
        from .forcing_revisions import merge_products
        product=merge_products(db,first['id'],fresh['id'])
        correction=definition().get('inputRequirement',{}).get('lakePrecipitationCorrection')
        if correction and not product.get('lakePrecipitationCorrection'):product=register_lake_correction(db,product['id'],correction)
        record(status='succeeded',stage='registered',inputId=product['id'],verifiedEnd=product['end'])
        return {**status,'manifest':product,'downloadedFiles':[p.name for p in downloaded]}
    except InterruptedError:
        record(status='cancelled');raise
    except Exception as exc:
        code,message=classify_error(exc)
        if isinstance(exc,ValueError):message=str(exc).split('https://')[0][:300]
        record(status=code,message=message)
        raise ValueError(message) from None
