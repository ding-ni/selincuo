"""User-triggered official coverage and immutable sequential acquisition."""
import json,os,re,time,uuid
from pathlib import Path
import numpy as np
import pandas as pd
import requests
from .common import ROOT,read_json,write_json,sha_file,digest,now
from .acquisition import AREA,VARIABLES,classify_error,inspect_file

CATALOG='https://cds.climate.copernicus.eu/api/catalogue/v1/collections/reanalysis-era5-land'

def provenance_summary(db,manifest):
    records=[]
    proof_cache = None
    active=[s for s in manifest.get('sources',[]) if s['sha256'] not in manifest.get('supersededSourceDigests',[])]
    for source in active:
        path=db.workspace/'downloads/era5-land'/Path(source['name']).with_suffix('.provenance.json')
        try:record=read_json(path)
        except (OSError,ValueError):record=None
        if not record or record.get('verification',{}).get('sha256')!=source['sha256']:
            if proof_cache is None:
                proof_cache = {}
                for root in (db.workspace/'downloads',db.workspace/'incoming'):
                    for candidate in root.rglob('*.provenance.json'):
                        try:
                            proof=read_json(candidate,{})
                            checksum=proof.get('verification',{}).get('sha256')
                            if checksum:proof_cache.setdefault(checksum,[]).append(proof)
                        except (OSError,ValueError):
                            continue
            found=proof_cache.get(source['sha256'],[])
            # Duplicate receipts with different official catalogue dates are
            # conservatively treated as the oldest known version.
            record=min(found,key=lambda item:str(item.get('catalog',{}).get('catalogUpdated') or '')) if found else None
        if not record:continue
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
            if not len(times) or times.has_duplicates or times.hasnans:raise ValueError('下载时间重复或无效')
            if len(times)>1 and not np.all(np.diff(times.asi8)==3600*10**9):raise ValueError('下载缺少连续逐小时资料')
            a.load()
            if not np.isfinite(a.values).all():raise ValueError('下载变量存在缺测，不能登记为完整资料')
            result[variable]={'startUTC':times.min().isoformat(),'endUTC':times.max().isoformat(),'hours':len(times),'units':str(a.attrs.get('units')),'accumulation':a.attrs.get('GRIB_stepType')}
        exp=ds.coords.get('expver')
        version=np.asarray(exp).tolist() if exp is not None else ds.attrs.get('expver')
    return {'variables':result,'sha256':sha_file(path),'bytes':Path(path).stat().st_size,'expver':version}


def verified_coverage(path, lo, hi):
    """Verify raw source semantics, domain, all variables and every requested hour."""
    source = inspect_file(path)
    checked = inspect(path)
    if source['sha256'] != checked['sha256']:
        raise ValueError('资料在核验期间发生变化')
    if any(pd.Timestamp(v['startUTC']) > lo or pd.Timestamp(v['endUTC']) < hi
           for v in checked['variables'].values()):
        raise ValueError('资料未完整覆盖所请求时段')
    return checked


def _candidate_files(workspace):
    return sorted(path for name in ('downloads', 'incoming')
                  for path in (workspace / name).rglob('*.nc') if path.is_file())


def _local_candidate(target, candidates, period, lo, hi, explicit, cache, meta=None):
    """Reuse historical files only after checking the requested coverage and meaning."""
    hint = re.compile(r'(?<!\d)(19\d{2}|20\d{2})[-_]?(0[1-9]|1[0-2])(?!\d)')
    ordered = [target] + [p for p in candidates if p != target]
    provisional_skipped = False
    for path in ordered:
        if not path.is_file():
            continue
        hints = {(int(y), int(m)) for y, m in hint.findall(path.name)}
        if hints and (period.year, period.month) not in hints:
            continue
        if path not in cache:
            try:
                # Coverage depends on this request, while the expensive source scan does not.
                source, checked = inspect_file(path), inspect(path)
                if source['sha256'] != checked['sha256']:
                    raise ValueError('资料在核验期间发生变化')
                cache[path] = checked
            except Exception:
                cache[path] = None
        checked = cache[path]
        if not checked:
            continue
        if any(pd.Timestamp(v['startUTC']) > lo or pd.Timestamp(v['endUTC']) < hi
               for v in checked['variables'].values()):
            continue
        raw = checked.get('expver')
        versions = {str(v).zfill(4) for v in np.asarray(raw).ravel().tolist()} if raw is not None else set()
        if '0005' in versions:
            try:
                proof = read_json(path.with_suffix('.provenance.json'), {})
            except (OSError, ValueError):
                proof = {}
            verified_catalog = (proof.get('catalog', {}).get('catalogUpdated')
                                if proof.get('verification', {}).get('sha256') == checked['sha256'] else None)
            if (not meta or not meta.get('catalogUpdated') or
                    verified_catalog != meta['catalogUpdated']):
                provisional_skipped = True
                continue
        # Refreshes must not silently choose an unclassified old source.
        if not explicit and path != target and not versions:
            provisional_skipped = True
            continue
        return (path, checked), provisional_skipped
    return None, provisional_skipped


def _saved_remote(client, folder, target, request, remote_cache, meta, refresh_provisional=False):
    """Recover an identical request across catalogue updates without resubmitting it."""
    preferred = target.with_suffix('.request.json')
    records = [preferred] + [p for p in sorted(folder.glob('*.request.json')) if p != preferred]
    seen = set()
    matches = []
    def local_choice():
        for desired in (('accepted', 'running'), ('successful',)):
            for candidate in matches:
                if candidate.status in desired:
                    return candidate
        return None
    catalogs = remote_cache.setdefault('catalogs', {})
    for path in records:
        try:
            saved = read_json(path, {})
        except (OSError, ValueError):
            continue
        if saved.get('request') != request or not saved.get('requestId') or saved['requestId'] in seen:
            continue
        seen.add(saved['requestId'])
        try:
            remote = client.client.get_remote(saved['requestId'])
            if saved['requestId'] not in catalogs or catalogs[saved['requestId']].get('catalogUpdated') is None:
                catalogs[saved['requestId']] = saved.get('catalog') or {}
            outdated = (refresh_provisional and remote.status == 'successful' and
                        saved.get('catalog', {}).get('catalogUpdated') != meta.get('catalogUpdated'))
            if not outdated and remote.status in ('accepted', 'running', 'successful'):
                matches.append(remote)
        except Exception as exc:
            if '404' not in str(exc) and '410' not in str(exc):
                raise
    # A durable receipt establishes the request identity. Check active peers
    # before choosing a completed result, without repeatedly scanning history.
    if matches:
        if local_choice().status == 'successful':
            if 'requests' in remote_cache:
                active = remote_cache['requests'].get(digest(request), [])
            else:
                active = []
                jobs = client.client.get_jobs(limit=100, status=['accepted', 'running']).json['jobs']
                for job in jobs:
                    if job.get('processID') != 'reanalysis-era5-land' or job.get('jobID') in seen:
                        continue
                    try:
                        active.append(client.client.get_remote(job['jobID']))
                    except Exception as exc:
                        if '404' not in str(exc) and '410' not in str(exc):
                            raise
            matches.extend(candidate for candidate in active if candidate.request == request
                           and candidate.request_id not in seen and candidate.status in ('accepted', 'running'))
        return local_choice()
    # Also recover a request made by an earlier client before its local receipt was saved.
    if 'requests' not in remote_cache:
        remote_cache['requests'] = {}
        jobs = client.client.get_jobs(limit=100, status=['accepted', 'running', 'successful']).json['jobs']
        for job in jobs:
            if job.get('processID') != 'reanalysis-era5-land':
                continue
            try:
                candidate = client.client.get_remote(job['jobID'])
                if candidate.status not in ('rejected', 'failed', 'dismissed', 'deleted'):
                    remote_cache['requests'].setdefault(digest(candidate.request), []).append(candidate)
            except Exception as exc:
                if '404' not in str(exc) and '410' not in str(exc):
                    raise
    for candidate in remote_cache['requests'].get(digest(request), []):
        if (candidate.request == request and candidate.request_id not in seen and
                candidate.status in ('accepted', 'running', 'successful') and
                not (refresh_provisional and candidate.status == 'successful')):
            matches.append(candidate)
    # Never let an earlier completed request hide an active matching request.
    return local_choice()


def _write_new_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def _remember_remote(target, remote, request, meta):
    path = target.with_suffix('.request.json')
    try:
        existing = read_json(path, {})
    except (OSError, ValueError):
        existing = {}
    if existing.get('requestId') == remote.request_id and existing.get('request') == request:
        return
    if path.exists():
        path = target.with_name(target.stem + '-' + uuid.uuid4().hex + '.request.json')
    _write_new_json(path, {'requestId': remote.request_id, 'request': request, 'catalog': meta})


def _usable_chunk(folder, offset, length):
    original = folder / str(offset)
    for path in [original, *sorted(folder.glob(str(offset) + '-*'))]:
        if not path.is_file() or path.name.endswith('.sha256'):
            continue
        checksum = path.with_suffix('.sha256')
        if (path.stat().st_size == length and checksum.is_file() and
                sha_file(path) == checksum.read_text(encoding='ascii').strip()):
            return path
    return None


def _save_chunk(folder, offset, data):
    # Retain any invalid or incomplete prior bytes; new chunks are exclusive and versioned.
    path = folder / (str(offset) + '-' + uuid.uuid4().hex)
    with path.open('xb') as stream:
        stream.write(data)
    with path.with_suffix('.sha256').open('x', encoding='ascii') as stream:
        stream.write(sha_file(path))
    return path


def _publish_partial(partial, target):
    """Publish without overwriting another file, retaining the assembled partial."""
    while True:
        if target.with_suffix('.provenance.json').exists():
            target = target.with_name(target.stem + '-' + uuid.uuid4().hex + '.nc')
            continue
        try:
            os.link(partial, target)
            return target
        except FileExistsError:
            target = target.with_name(target.stem + '-' + uuid.uuid4().hex + '.nc')


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


def acquire_latest(db,progress=None,cancelled=lambda:False,start_date=None,end_date=None,commit=None,task_id=None):
    folder=db.workspace/'downloads/era5-land';folder.mkdir(parents=True,exist_ok=True)
    try:previous=read_json(folder/'latest-status.json',{})
    except (OSError,ValueError):previous={}
    status={'status':'running','stage':'preflight','at':now(),'completed':[],
            'requestedStartDate':start_date,'requestedEndDate':end_date,'updateId':uuid.uuid4().hex,'taskId':task_id}
    def record(**kw):status.update(kw,at=now());write_json(folder/'latest-status.json',status)
    def check():
        if cancelled():raise InterruptedError('用户停止更新，已完成文件与请求保留')
    downloaded=[]
    record()
    try:
        check()
        import cdsapi
        meta=availability();write_json(folder/'latest-catalog.json',meta)
        products=db.items('input_products')
        if not products:raise ValueError('先接入包含预热期的基础驱动，再补齐最新资料')
        from .operational_scheme import definition
        fixed=definition();eligible=[r for r in products if r['manifest']['start']<=fixed['initialization']['origin'] and r['manifest']['spatialVersion']==fixed['spatialVersion']]
        if not eligible:raise ValueError('缺少覆盖统一预热起点的基础驱动，不能用独立片段替代连续历史')
        first=max(eligible,key=lambda r:(r['manifest']['end'],bool(r['manifest'].get('lakePrecipitationCorrection')),r.get('created_at','')))['manifest']
        start,last=acquisition_window(first,meta,start_date,end_date)
        provenance=provenance_summary(db,first)['sources']
        provisional_sources=[s for s in provenance if '0005' in s['experimentVersions']]
        stale_provisional=any(s.get('catalogUpdated') != meta.get('catalogUpdated') for s in provisional_sources)
        if (not start_date and not end_date and not stale_provisional and last<=pd.Timestamp(first['end'])-pd.Timedelta(hours=8)
                and previous.get('status') in ('succeeded','up-to-date')
                and meta['catalogUpdated']==previous.get('catalog',{}).get('catalogUpdated')):
            result={**status,'status':'up-to-date','stage':'complete','catalog':meta,'inputId':first['id']}
            if commit:commit(result)
            else:check();record(**result)
            return result
        provisional=[pd.Timestamp(v['startUTC']).floor('D')-pd.Timedelta(days=1) for s in provisional_sources for v in s['coverage'].values()]
        if provisional and not start_date and not end_date:start=min(start,min(provisional))
        record(catalog=meta,startUTC=start.isoformat(),requestedEndUTC=last.isoformat())
        candidates = _candidate_files(db.workspace)
        verified_cache, remote_cache = {}, {}
        client=cdsapi.Client(wait_until_complete=False,quiet=True,timeout=90,retry_max=3)
        client.client.get_jobs(limit=1)
        periods=pd.period_range(start,last,freq='M')
        for i,period in enumerate(periods):
            check();lo=max(start,period.start_time);hi=min(last,period.end_time.floor('h'))
            request={'variable':meta['variables'],'year':str(period.year),'month':f'{period.month:02}',
                     'day':[f'{d:02}' for d in range(lo.day,hi.day+1)],'time':[f'{h:02}:00' for h in range(24)],'area':AREA,'data_format':'netcdf','download_format':'unarchived'}
            identity=digest({'request':request,'catalogUpdated':meta['catalogUpdated']})[:16]
            target=folder/f'era5-land-{period.year}{period.month:02}-{lo.day:02}-{hi.day:02}-{identity}.nc'
            existing,refresh_provisional=_local_candidate(target,candidates,period,lo,hi,bool(start_date or end_date),verified_cache,meta)
            if existing:
                path,_=existing;downloaded.append(path);status['completed'].append(path.name)
                record(stage='verified-local',currentMonth=str(period))
                if progress:progress((i+1)/len(periods)*75,f'{period}：复用已核验的官方资料')
                continue
            record(stage='request',currentMonth=str(period))
            if progress:progress(i/len(periods)*75,f'{period}：补齐官方逐时资料')
            while True:
                remote=_saved_remote(client,folder,target,request,remote_cache,meta,refresh_provisional)
                if remote is None:
                    remote=client.retrieve('reanalysis-era5-land',request)
                    request_catalog=meta
                else:
                    request_catalog=remote_cache.get('catalogs',{}).get(remote.request_id,{})
                _remember_remote(target,remote,request,request_catalog)
                while remote.status in ('accepted','running'):
                    check()
                    for _ in range(5):check();time.sleep(1)
                check();result=remote.get_results();size=result.content_length
                if not isinstance(size,int) or size<=0:raise ValueError('官方返回文件大小无效')
                # Bytes are bound to the actual request ID and size, not only the calendar month.
                part_identity=digest({'requestId':remote.request_id,'size':size,'request':request})[:24]
                parts=folder/'.latest-parts'/part_identity;parts.mkdir(parents=True,exist_ok=True);step=1024*1024
                record(stage='transfer',bytesExpected=size)
                selected=[]
                for offset in range(0,size,step):
                    check();end=min(size-1,offset+step-1)
                    part=_usable_chunk(parts,offset,end-offset+1)
                    if part:
                        selected.append(part);continue
                    for attempt in range(3):
                        try:
                            response=requests.get(result.location,headers={'Range':f'bytes={offset}-{end}','Accept-Encoding':'identity'},timeout=(15,40))
                            response.raise_for_status();break
                        except requests.RequestException:
                            if attempt==2:raise
                            for _ in range(2*(attempt+1)):check();time.sleep(1)
                    response.raise_for_status()
                    if response.status_code!=206 or response.headers.get('Content-Range')!=f'bytes {offset}-{end}/{size}' or len(response.content)!=end-offset+1:raise ValueError('下载字节范围不完整')
                    selected.append(_save_chunk(parts,offset,response.content))
                partial=target.with_name(target.stem+'-'+uuid.uuid4().hex+'.partial')
                with partial.open('xb') as stream:
                    for part in selected:
                        check();stream.write(part.read_bytes())
                verified=verified_coverage(partial,lo,hi)
                published=_publish_partial(partial,target)
                _write_new_json(published.with_suffix('.provenance.json'),{'request':request,'catalog':request_catalog,'catalogChecked':meta,'verification':verified,'revisionStatus':'provisional-or-final-as-delivered','immutable':True})
                raw=verified.get('expver')
                versions={str(v).zfill(4) for v in np.asarray(raw).ravel().tolist()} if raw is not None else set()
                # A recovered old in-flight request must finish before a new one
                # is submitted, but its provisional data cannot satisfy a refresh.
                if (refresh_provisional and '0005' in versions and
                        request_catalog.get('catalogUpdated') != meta.get('catalogUpdated')):
                    record(stage='verified-prior',message='保留旧请求结果，继续获取官方新版')
                    continue
                downloaded.append(published);status['completed'].append(published.name)
                record(stage='verified',message='')
                break
        check()
        from .forcing import register_era5,register_lake_correction
        from .operational_scheme import definition
        # Include historical raw files for accumulated UTC boundary hours; never synthesize a halo.
        if progress:progress(80,'核验新增时段与官方修订，原输入保持冻结')
        # register_era5 publishes its input after processing and then emits its
        # final 100% progress callback. Do not raise there after publication;
        # record the input ID and honor cancellation immediately afterwards.
        def registration_progress(value,message):
            if progress and value < 100:
                progress(80+value*.12,message)
        fresh=register_era5(downloaded,db,name='ERA5-Land本次官方资料',progress=registration_progress)
        record(stage='source-registered',sourceInputId=fresh['id'],inputId=fresh['id'])
        check()
        from .forcing_revisions import merge_products
        product=merge_products(db,first['id'],fresh['id'])
        record(stage='merged',inputId=product['id'],verifiedEnd=product['end'])
        check()
        correction=definition().get('inputRequirement',{}).get('lakePrecipitationCorrection')
        if correction and not product.get('lakePrecipitationCorrection'):
            product=register_lake_correction(db,product['id'],correction)
            record(stage='corrected',inputId=product['id'],verifiedEnd=product['end'])
            check()
        result={**status,'status':'succeeded','stage':'registered','inputId':product['id'],
                'verifiedEnd':product['end'],'manifest':product,'downloadedFiles':[p.name for p in downloaded]}
        if commit:commit(result)
        else:check();record(**{k:v for k,v in result.items() if k not in ('manifest','downloadedFiles')})
        return result
    except InterruptedError:
        record(status='cancelled',message='更新已停止；已完成资料及已登记输入均保留');raise
    except Exception as exc:
        code,message=classify_error(exc)
        if isinstance(exc,ValueError):message=str(exc).split('https://')[0][:300]
        if cancelled():
            record(status='cancelled',message='更新已停止；已完成资料及已登记输入均保留')
            raise InterruptedError('用户停止更新，已完成资料及已登记输入保留') from None
        record(status=code,message=message)
        raise ValueError(message) from None
