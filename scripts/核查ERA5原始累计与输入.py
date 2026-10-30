"""Independently check local CDS downloads against frozen hourly forcing.

Reads original files and the registered input without changing either. Writes
one uniquely named diagnostic report; no download or model calculation.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, now, read_json, sha_file
from backend.database import Database

INPUT_ID = 'INPUT-c00df956d638351ebeb4e96e'
YEARS = (2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026)
POINTS = {'SLC': (89.35702225, 31.84329578),
          'DUJA': (89.15997904, 32.03057137),
          'BLQ': (91.87776607, 32.69167210)}


def main():
    db = Database()
    download = db.workspace / 'downloads/era5-land'
    folder = db.workspace / 'inputs/products' / INPUT_ID
    manifest = read_json(folder / 'manifest.json')
    spatial = read_json(ROOT / 'data/derived/spatial/manifest.json')
    grid = spatial['grids']
    source_digests = {x['sha256'] for x in manifest['sourceDigests']}
    with np.load(folder / manifest['data'], allow_pickle=False) as data:
        times = pd.DatetimeIndex(pd.to_datetime(data['time']))
        precipitation = data['precipitation']
    points = {}
    for name, (lon, lat) in POINTS.items():
        index = min(range(len(grid)), key=lambda i: ((grid[i]['lon']-lon)*np.cos(np.radians(lat)))**2 + (grid[i]['lat']-lat)**2)
        points[name] = {'index': index, 'lon': grid[index]['lon'], 'lat': grid[index]['lat']}
    reports = []
    for year in YEARS:
        candidates = sorted(download.glob(f'era5-land-{year}07*.nc'))
        if len(candidates) != 1:
            raise ValueError(f'{year}年7月原始文件不唯一: {len(candidates)}')
        path = candidates[0]
        source_sha = sha_file(path)
        request_path = path.with_suffix('.request.json')
        request = read_json(request_path, {})
        with xr.open_dataset(path, engine='h5netcdf') as ds:
            tname = 'valid_time' if 'valid_time' in ds.coords else 'time'
            src_time = pd.DatetimeIndex(ds[tname].values)
            tp = ds['tp']
            lon_name = 'longitude' if 'longitude' in ds.coords else 'lon'
            lat_name = 'latitude' if 'latitude' in ds.coords else 'lat'
            checks = {}
            for name, point in points.items():
                raw = np.asarray(tp.sel({lon_name: point['lon'], lat_name: point['lat']}, method='nearest')).squeeze().astype(float)
                if raw.shape != (len(src_time),):
                    raise ValueError('原气象时间维度异常')
                # Independent derivation of the CDS 00UTC forecast cycle.
                previous = np.roll(raw, 1)
                increments = np.where(src_time.hour == 1, raw, raw - previous) * 1000.
                increments[0] = np.nan
                local = src_time + pd.Timedelta(hours=8)
                ix = times.get_indexer(local)
                ok = (ix >= 0) & np.isfinite(increments)
                differences = precipitation[ix[ok], point['index']] - np.maximum(increments[ok], 0.)
                non_reset = np.arange(1, len(raw))[src_time.hour[1:] != 1]
                drops = raw[non_reset] - raw[non_reset-1]
                midnight = np.flatnonzero((src_time.hour == 0) & (np.arange(len(raw)) >= 24))
                last = int(midnight[np.argmax(raw[midnight])])
                sample_indices = [last-23, last-18, last-12, last-6, last-1, last]
                checks[name] = {'grid': point, 'comparedHours': int(ok.sum()),
                    'inputMaxAbsDifferenceMm': float(np.max(np.abs(differences))),
                    'nonResetDecreasesOverTolerance': int((drops < -1.e-7).sum()),
                    'minimumNonResetIncrementMm': float(np.min(drops)*1000.),
                    'sampleRawAccumulationsMm': [{'utc': str(src_time[i]), 'mm': float(raw[i]*1000.)} for i in sample_indices],
                    'sampleAtNext01UTCMm': float(raw[last+1]*1000.) if last+1 < len(raw) else None}
            reports.append({'year': year, 'path': str(path), 'sha256': source_sha,
                'shaIsRegisteredSource': source_sha in source_digests,
                'recordedRequestDataset': request.get('catalog', {}).get('dataset'),
                'requestVariables': request.get('request', {}).get('variable'),
                'globalAttributes': dict(ds.attrs),
                'precipitationAttributes': dict(tp.attrs), 'startUTC': str(src_time[0]),
                'endUTC': str(src_time[-1]), 'points': checks})
    out = db.workspace / 'diagnostics' / ('ERA5原始累计核对-' + uuid.uuid4().hex[:12])
    out.mkdir(parents=True, exist_ok=False)
    result = {'createdAt': now(), 'inputId': INPUT_ID,
        'inputDataSHA256': manifest['dataSHA256'],
        'interpretation': '对CDS网格逐小时累计产品独立复核；UTC 01时直接取累计，其他时次减前一时次并转换为毫米，北京时间为UTC+8。',
        'officialDatasetAndConversion': ['https://confluence.ecmwf.int/pages/viewpage.action?pageId=315578204',
            'https://confluence.ecmwf.int/pages/viewpage.action?pageId=253727898'],
        'files': reports}
    target = out / 'ERA5原始累计与冻结输入.json'
    with target.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, default=str, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'path': str(target), 'sha256': sha_file(target),
        'years': YEARS, 'points': POINTS,
        'maxDifferenceMm': max(x['inputMaxAbsDifferenceMm'] for r in reports for x in r['points'].values()),
        'unregisteredSourceYears': [r['year'] for r in reports if not r['shaIsRegisteredSource']]}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
