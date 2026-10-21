"""Download registered historical forcing; credentials stay in the user's CDS configuration."""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import now, sha_file, write_json, read_json


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--years', nargs='+', type=int, default=[2023, 2024, 2025])
    p.add_argument('--months', nargs='+', type=int, default=list(range(1, 13)))
    p.add_argument('--prepare-only', action='store_true')
    p.add_argument('--direct', action='store_true', help='本次下载进程直连，不修改系统代理设置')
    p.add_argument('--workers', type=int, default=1)
    p.add_argument('--transfer-workers', type=int, default=4, help='同一已完成结果的分段传输并发；CDS任务仍严格串行')
    p.add_argument('--group-months', type=int, default=2, help='串行合并相邻月份；服务成本限制拒绝时自动拆分')
    p.add_argument('--halo', action='store_true', help='增加首年前一月，确保时区转换与累计量差分完整')
    p.add_argument('--output', type=Path, default=ROOT / 'workspace/downloads/era5-land')
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    requests = []
    for year in args.years:
        for month in args.months:
            request = {'variable': ['2m_temperature', 'total_precipitation', 'potential_evaporation'],
                       'year': str(year), 'month': f'{month:02}',
                       'day': [f'{d:02}' for d in range(1, calendar.monthrange(year, month)[1] + 1)],
                       'time': [f'{h:02}:00' for h in range(24)],
                       'area': [33.5, 87.2, 29.9, 92.4],
                       'data_format': 'netcdf', 'download_format': 'unarchived'}
            requests.append({'dataset': 'reanalysis-era5-land', 'request': request,
                             'filename': f'era5-land-{year}{month:02}.nc'})
    if args.halo:
        year = min(args.years) - 1
        halo = dict(requests[0]['request'])
        halo.update(year=str(year), month='12', day=[f'{d:02}' for d in range(1, 32)])
        requests.insert(0, {'dataset': 'reanalysis-era5-land', 'request': halo, 'filename': f'era5-land-{year}12.nc'})
    if args.group_months > 1:
        covered = set()
        for path in args.output.glob('*.nc'):
            match = re.fullmatch(r'era5-land-(\d{4})(\d{2})(?:-(\d{4})(\d{2}))?\.nc', path.name)
            if match:
                y, m = map(int, match.group(1, 2))
                end_y, end_m = map(int, match.group(3, 4)) if match.group(3) else (y, m)
                while (y, m) <= (end_y, end_m):
                    covered.add((y, m))
                    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        pending = []
        outstanding = set()
        for path in args.output.glob('*.request.json'):
            target = path.with_name(path.name.replace('.request.json', '.nc'))
            remembered = read_json(path)
            r = (remembered or {}).get('request', {})
            if not target.is_file() and isinstance(r.get('month'), str) and int(r.get('year', 0)) in args.years:
                key = (int(r['year']), int(r['month']))
                outstanding.add(key)
                pending.append({'dataset': 'reanalysis-era5-land', 'request': r, 'filename': target.name})
        remaining = [r for r in requests if (int(r['request']['year']), int(r['request']['month'])) not in covered | outstanding]
        grouped = []
        for row in remaining:
            r = row['request']
            if grouped and grouped[-1][0]['request']['year'] == r['year'] and int(grouped[-1][-1]['request']['month']) + 1 == int(r['month']) and len(grouped[-1]) < min(5, args.group_months):
                grouped[-1].append(row)
            else:
                grouped.append([row])
        for group in grouped:
            if len(group) == 1:
                pending.append(group[0])
            else:
                r = {**group[0]['request'], 'month': [row['request']['month'] for row in group], 'day': [f'{d:02}' for d in range(1, 32)]}
                pending.append({'dataset': 'reanalysis-era5-land', 'request': r,
                                'filename': group[0]['filename'][:-3] + '-' + group[-1]['filename'][10:]})
        requests = pending
    write_json(args.output / 'download-requests.json', {'createdAt': now(), 'requests': requests})
    if args.prepare_only:
        print(json.dumps({'status': 'prepared', 'months': len(requests)}, ensure_ascii=False))
        return
    if args.direct:
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            os.environ.pop(name, None)
    import cdsapi
    # Never print credentials or the parsed configuration.
    manifest = read_json(args.output / 'source-manifest.json', {'dataset': 'reanalysis-era5-land', 'source': 'Copernicus Climate Change Service', 'files': []})

    def fetch(row):
        client = cdsapi.Client(quiet=True, debug=False, timeout=120, retry_max=5, sleep_max=20, progress=False, wait_until_complete=False)
        output = args.output / row['filename']
        if not output.is_file():
            print(f"Downloading {row['filename']}", flush=True)
            partial = output.with_suffix('.partial')
            request_file = output.with_suffix('.request.json')
            remembered = read_json(request_file)
            remote = None
            if remembered and remembered.get('request') == row['request']:
                try:
                    remote = client.client.get_remote(remembered['requestId'])
                    if remote.status in ('rejected', 'failed', 'dismissed', 'deleted'):
                        remote = None
                except Exception as exc:
                    if '404' not in str(exc) and 'dismissed' not in str(exc):
                        raise
            if remote is None:
                # Recover the exact outstanding request after a local restart.
                for job in client.client.get_jobs(limit=20, status=['accepted', 'running']).json['jobs']:
                    if job['processID'] == row['dataset']:
                        candidate = client.client.get_remote(job['jobID'])
                        if candidate.request == row['request']:
                            remote = candidate
                            break
                if remote is None:
                    try:
                        remote = client.retrieve(row['dataset'], row['request'])
                    except Exception as exc:
                        months = row['request']['month']
                        if isinstance(months, list) and len(months) > 1 and 'cost limits exceeded' in str(exc):
                            middle = len(months) // 2
                            children = []
                            for part in (months[:middle], months[middle:]):
                                request = {**row['request'], 'month': part if len(part) > 1 else part[0]}
                                year = request['year']
                                filename = f'era5-land-{year}{part[0]}' + (f'-{year}{part[-1]}' if len(part) > 1 else '') + '.nc'
                                children.extend(fetch({'dataset': row['dataset'], 'request': request, 'filename': filename}))
                            return children
                        raise
                write_json(request_file, {'requestId': remote.request_id, 'request': row['request']})
            result = remote.get_results()
            # Retain partial bytes and request bounded byte ranges. The standard
            # client restarts the entire file after some interrupted transfers.
            import requests as http
            expected = result.content_length
            chunks = output.parent / (output.stem + '.chunks')
            chunks.mkdir(exist_ok=True)
            size = 512 * 1024
            def transfer(offset):
                end = min(expected - 1, offset + size - 1)
                destination = chunks / str(offset)
                if destination.is_file() and destination.stat().st_size == end - offset + 1:
                    return
                session = http.Session()
                session.trust_env = not args.direct
                for attempt in range(8):
                    try:
                        response = session.get(result.location, headers={'Range': f'bytes={offset}-{end}', 'Accept-Encoding': 'identity'}, timeout=(30, 90))
                        response.raise_for_status()
                        if response.status_code != 206 or not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-{end}/') or len(response.content) != end - offset + 1:
                            raise RuntimeError('下载服务未正确响应字节分段')
                        destination.write_bytes(response.content)
                        return
                    except Exception:
                        if attempt == 7:
                            raise RuntimeError(f'{output.name}分段{offset}传输失败，已完成分段保留供重试')
                        time.sleep(min(10, (attempt + 1) * 2))
                session.close()
            with ThreadPoolExecutor(max_workers=min(4, max(1, args.transfer_workers))) as transfers:
                list(transfers.map(transfer, range(0, expected, size)))
            with partial.open('wb') as stream:
                for offset in range(0, expected, size):
                    stream.write((chunks / str(offset)).read_bytes())
            if partial.stat().st_size != expected:
                raise RuntimeError('分段合并大小校验失败')
            partial.replace(output)
        import xarray as xr
        with xr.open_dataset(output, engine='h5netcdf') as ds:
            if not any(name in ds for name in ('t2m', '2m_temperature')):
                raise RuntimeError(f'下载文件缺少气温：{output.name}')
        return [{'path': output.name, 'sha256': sha_file(output), 'request': row['request']}]

    failed = []
    errors = []
    halt = threading.Event()
    def guarded_fetch(row):
        if halt.is_set():
            return []
        try:
            return fetch(row)
        except Exception as exc:
            if any(text in str(exc) for text in ('User is disabled', 'User has been disabled', '401 Client Error', 'Maximum number queued requests', 'required licences')):
                halt.set()
            raise
    # CDS currently limits this dataset to sequential submissions per account.
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {pool.submit(guarded_fetch, row): row for row in requests}
        for future in as_completed(futures):
            try:
                records = future.result()
            except Exception as exc:
                failed.append(futures[future]['filename'])
                errors.append({'file': futures[future]['filename'], 'error': str(exc)[:1000]})
                write_json(args.output / 'download-status.json', {'status': 'account-disabled' if halt.is_set() else 'incomplete', 'errors': errors, 'at': now()})
                print(json.dumps({'file': futures[future]['filename'], 'status': 'failed', 'error': str(exc)[:1000]}, ensure_ascii=False), flush=True)
                continue
            for record in records:
                manifest['files'] = [r for r in manifest['files'] if r['path'] != record['path']] + [record]
                write_json(args.output / 'source-manifest.json', manifest)
                print(f"Completed {record['path']} ({len(manifest['files'])} files registered)", flush=True)
    print(json.dumps({'status': 'incomplete' if failed else 'downloaded', 'months': len(manifest['files']), 'failed': failed}, ensure_ascii=False))
    write_json(args.output / 'download-status.json', {'status': 'account-disabled' if halt.is_set() else ('incomplete' if failed else 'downloaded'), 'errors': errors, 'at': now()})
    if failed:
        raise SystemExit(2)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # API errors do not include local configuration in the diagnostic.
        print(json.dumps({'status': 'failed', 'errorType': type(exc).__name__, 'message': str(exc)[:1500]}, ensure_ascii=False), flush=True)
        raise SystemExit(1)
