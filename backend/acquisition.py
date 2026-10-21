"""Bounded, sequential CDS acquisition with verified local coverage and resume."""
from __future__ import annotations

import calendar
import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from .common import now, read_json, write_json, sha_file, digest

AREA = [33.5, 87.2, 29.9, 92.4]
VARIABLES = {'temperature': ('t2m', '2t', '2m_temperature'),
             'precipitation': ('tp', 'total_precipitation'),
             'evaporation': ('pev', 'potential_evaporation')}
_cache = {}


def inspect_file(path):
    import numpy as np
    import pandas as pd
    import xarray as xr
    path = Path(path)
    stamp = (str(path.resolve()), path.stat().st_size, path.stat().st_mtime_ns)
    if stamp in _cache:
        return _cache[stamp]
    with xr.open_dataset(path, engine='h5netcdf') as ds:
        if any(s in json.dumps(dict(ds.attrs), default=str).lower() for s in ('synthetic', 'demonstration', 'frontend-fixture')):
            raise ValueError('演示资料不能作为真实下载')
        lat = next((n for n in ('latitude', 'lat') if n in ds.coords), None)
        lon = next((n for n in ('longitude', 'lon') if n in ds.coords), None)
        if not lat or not lon:
            raise ValueError('缺少经纬度')
        for coord, lo, hi in ((lat, AREA[2], AREA[0]), (lon, AREA[1], AREA[3])):
            a = np.sort(ds[coord].values)
            if len(a) < 2 or not np.allclose(np.diff(a), .1, atol=1e-5, rtol=0) or a[0] > lo + .001 or a[-1] < hi - .001:
                raise ValueError('非原生0.1°或未覆盖色林错下载控制域')
        coverage = {}
        for variable, aliases in VARIABLES.items():
            name = next((n for n in aliases if n in ds), None)
            if not name:
                continue
            arr = ds[name]
            time_name = next((n for n in ('valid_time', 'time') if n in arr.coords), None)
            if not time_name:
                raise ValueError(name + '缺少时间轴')
            times = pd.DatetimeIndex(arr[time_name].values.ravel())
            if times.hasnans or times.has_duplicates or not times.is_monotonic_increasing:
                raise ValueError(name + '时间轴重复或无序')
            if variable != 'temperature' and (arr.attrs.get('GRIB_stepType') != 'accum' or arr.attrs.get('units') != 'm'):
                raise ValueError(name + '累计量含义或单位不符')
            if variable == 'temperature' and str(arr.attrs.get('units', '')).lower() not in ('k', 'kelvin'):
                raise ValueError('气温单位须为K')
            # Materialize each variable once: truncated HDF5 data must not pass a header-only check.
            arr.load()
            months = []
            for period in times.to_period('M').unique():
                expected = pd.date_range(period.start_time, period.end_time.floor('h'), freq='h')
                if expected.isin(times).all():
                    months.append(str(period))
            coverage[variable] = months
        if not coverage:
            raise ValueError('没有所需ERA5-Land变量')
        result = {'bytes': path.stat().st_size, 'sha256': sha_file(path), 'variables': coverage}
    _cache[stamp] = result
    return result


def requested_months(years=(2023, 2024, 2025), months=range(1, 13), halo=True):
    if not years or not months or any(type(y) is not int or not 1950 <= y <= 2100 for y in years) or any(type(m) is not int or not 1 <= m <= 12 for m in months):
        raise ValueError('年份或月份不合法')
    values = {f'{y:04}-{m:02}' for y in years for m in months}
    if halo:
        y, m = min((int(v[:4]), int(v[5:])) for v in values)
        values.add(f'{y - 1 if m == 1 else y:04}-{12 if m == 1 else m - 1:02}')
    return sorted(values)


def inventory(workspace, years=(2023, 2024, 2025), months=range(1, 13), halo=True):
    workspace = Path(workspace)
    files, issues = [], []
    for folder in ('downloads', 'incoming'):
        for path in sorted((workspace / folder).rglob('*.nc')):
            try:
                files.append({'path': path.relative_to(workspace).as_posix(), **inspect_file(path)})
            except Exception as exc:
                issues.append({'path': path.relative_to(workspace).as_posix(), 'error': str(exc).split('https://')[0][:240]})
    rows = []
    for month in requested_months(years, months, halo):
        available = {v: [f['path'] for f in files if month in f['variables'].get(v, [])] for v in VARIABLES}
        rows.append({'month': month, 'variables': available, 'complete': all(available.values())})
    return {'dataset': 'reanalysis-era5-land', 'area': AREA, 'resolution': '0.1°', 'timeZone': 'UTC（登记时转北京时间）',
            'months': rows, 'completeMonths': sum(r['complete'] for r in rows), 'requiredMonths': len(rows),
            'files': files, 'issues': issues,
            'download': read_json(workspace / 'downloads/era5-land/download-status.json', {'status': 'not-started'})}


def classify_error(exc):
    s = str(exc).lower()
    if 'disabled' in s:
        return 'account-disabled', 'CDS账户已被停用。请联系CDS支持恢复账户，或在本机配置另一个已授权账户；恢复后点击继续下载。'
    if '401' in s or 'unauthoriz' in s:
        return 'authentication', 'CDS认证失败，请检查本机 .cdsapirc；不要在聊天或日志中粘贴密钥。'
    if 'licen' in s:
        return 'license-required', '请登录CDS并接受ERA5-Land数据许可后继续。'
    if 'queued requests' in s or '429' in s:
        return 'queue-limit', 'CDS请求配额已满，请等待已有请求结束后继续；本平台不并行提交任务。'
    if 'cost limits' in s:
        return 'cost-limit', '单月请求超出CDS成本限制，保留请求和已下载资料，需调整请求范围。'
    return 'incomplete', type(exc).__name__ + '：连接或传输未完成，已保存断点，可稍后继续。'


def acquire(workspace, years=(2023, 2024, 2025), months=range(1, 13), halo=True,
            direct=False, prepare_only=False, progress=None, cancelled=lambda: False):
    import requests
    workspace = Path(workspace)
    output = workspace / 'downloads/era5-land'
    output.mkdir(parents=True, exist_ok=True)
    report = inventory(workspace, years, months, halo)
    pending = [r['month'] for r in report['months'] if not r['complete']]
    plan = []
    for value in pending:
        y, m = int(value[:4]), int(value[5:])
        request = {'variable': ['2m_temperature', 'total_precipitation', 'potential_evaporation'],
                   'year': str(y), 'month': f'{m:02}',
                   'day': [f'{d:02}' for d in range(1, calendar.monthrange(y, m)[1] + 1)],
                   'time': [f'{h:02}:00' for h in range(24)], 'area': AREA,
                   'data_format': 'netcdf', 'download_format': 'unarchived'}
        plan.append({'month': value, 'request': request})
    write_json(output / 'download-requests.json', {'createdAt': now(), 'requests': plan, 'sequential': True})
    if prepare_only or not pending:
        return {'status': 'prepared' if prepare_only else 'downloaded', 'pendingMonths': pending, 'coverage': report}
    status = {'status': 'running', 'at': now(), 'pendingMonths': pending, 'completedThisRun': []}
    def record(**kwargs):
        status.update(kwargs, at=now())
        write_json(output / 'download-status.json', status)
    def check():
        if cancelled():
            raise InterruptedError('用户停止下载，已完成文件与断点保留')
    session = requests.Session()
    session.trust_env = not direct
    try:
        check()
        import cdsapi
        # No environment mutation: direct mode applies only to this task's sessions.
        client = cdsapi.Client(session=session, quiet=True, debug=False, timeout=30, retry_max=0,
                               progress=False, wait_until_complete=False)
        # One bounded readiness call. A disabled account stops the entire run immediately.
        client.client.get_jobs(limit=1)
        for index, row in enumerate(plan):
            check()
            value, request = row['month'], row['request']
            # A recovered grouped request may already have completed this month.
            current = inventory(workspace, years, months, halo)
            if next(r for r in current['months'] if r['month'] == value)['complete']:
                continue
            record(currentMonth=value, stage='request')
            if progress:
                progress(index / len(plan) * 95, value + '：检查或恢复CDS请求')
            target = output / ('era5-land-' + value.replace('-', '') + '.nc')
            request_file = target.with_suffix('.request.json')
            remembered = read_json(request_file)
            remote = None
            if not remembered:
                # Preserve useful requests created by earlier grouped-download versions.
                required = {r['month'] for r in report['months']}
                for legacy in output.glob('*.request.json'):
                    saved = read_json(legacy, {})
                    old = saved.get('request', {})
                    old_months = old.get('month', [])
                    old_months = [old_months] if isinstance(old_months, str) else old_months
                    covered = {str(old.get('year')) + '-' + str(m).zfill(2) for m in old_months}
                    if value in covered and covered <= required and old.get('area') == AREA and set(old.get('variable', [])) == set(request['variable']):
                        remembered = saved
                        request = old
                        break
            if remembered and remembered.get('request') == request:
                try:
                    remote = client.client.get_remote(remembered['requestId'])
                    if remote.status in ('rejected', 'failed', 'dismissed', 'deleted'):
                        remote = None
                except Exception as exc:
                    if '404' not in str(exc):
                        raise
            if remote is None:
                request = row['request']
                for job in client.client.get_jobs(limit=100, status=['accepted', 'running', 'successful']).json['jobs']:
                    if job['processID'] == 'reanalysis-era5-land':
                        candidate = client.client.get_remote(job['jobID'])
                        if candidate.request == request:
                            remote = candidate
                            break
            if remote is None:
                remote = client.retrieve('reanalysis-era5-land', request)
            write_json(request_file, {'requestId': remote.request_id, 'request': request})
            while remote.status in ('accepted', 'running'):
                check()
                if progress:
                    progress(index / len(plan) * 95, value + '：CDS排队或处理，任务编号已保留')
                import time
                for _ in range(15):
                    check()
                    time.sleep(1)
            check()
            result = remote.get_results()
            size = result.content_length
            # Bind chunks to immutable request ID and result size, never reuse unrelated byte ranges.
            identity = digest({'requestId': remote.request_id, 'size': size, 'request': request})[:24]
            chunks = output / '.parts' / identity
            chunks.mkdir(parents=True, exist_ok=True)
            record(stage='transfer', bytesExpected=size)
            step = 512 * 1024
            def transfer(offset):
                check()
                end = min(size - 1, offset + step - 1)
                path = chunks / str(offset)
                checksum = path.with_suffix('.sha256')
                if path.is_file() and path.stat().st_size == end - offset + 1 and checksum.is_file() and sha_file(path) == checksum.read_text():
                    return
                with requests.Session() as transport:
                    transport.trust_env = not direct
                    for attempt in range(3):
                        check()
                        try:
                            with transport.get(result.location, headers={'Range': f'bytes={offset}-{end}', 'Accept-Encoding': 'identity'}, timeout=(20, 40)) as response:
                                response.raise_for_status()
                                if response.status_code != 206 or response.headers.get('Content-Range') != f'bytes {offset}-{end}/{size}' or len(response.content) != end - offset + 1:
                                    raise ValueError('服务端字节范围响应不完整')
                                data = response.content
                            path.write_bytes(data)
                            checksum.write_text(hashlib.sha256(data).hexdigest())
                            return
                        except Exception:
                            if attempt == 2:
                                raise
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(transfer, range(0, size, step)))
            check()
            partial = target.with_suffix('.partial')
            with partial.open('wb') as stream:
                for offset in range(0, size, step):
                    stream.write((chunks / str(offset)).read_bytes())
            verified = inspect_file(partial)
            requested = request['month'] if isinstance(request['month'], list) else [request['month']]
            verified_months = [request['year'] + '-' + m for m in requested]
            if any(m not in verified['variables'].get(v, []) for v in VARIABLES for m in verified_months):
                raise ValueError('下载结果没有完整覆盖请求月和三个变量')
            if target.exists():
                # Preserve incomplete prior originals; never overwrite or delete them silently.
                target = target.with_name(target.stem + '-' + identity[:8] + '.nc')
            partial.replace(target)
            manifest = read_json(output / 'source-manifest.json', {'dataset': 'reanalysis-era5-land', 'files': []})
            manifest['files'] = [f for f in manifest['files'] if f.get('path') != target.name] + [{'path': target.name, **verified, 'request': request}]
            write_json(output / 'source-manifest.json', manifest)
            record(completedThisRun=status['completedThisRun'] + [value])
        final = inventory(workspace, years, months, halo)
        record(status='downloaded' if all(r['complete'] for r in final['months']) else 'incomplete', stage='finished')
        return {'status': status['status'], 'completeMonths': final['completeMonths'], 'requiredMonths': final['requiredMonths']}
    except InterruptedError:
        record(status='cancelled', message='下载已停止；已完成资料、远端请求和分段保留')
        raise
    except Exception as exc:
        code, message = classify_error(exc)
        record(status=code, message=message)
        # Avoid signed result URLs or credentials in task logs and error messages.
        raise ValueError(message) from None
    finally:
        session.close()
