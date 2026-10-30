from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from .common import ROOT, canonical, digest, now, read_json, sha_file, within, write_json
from .database import Database

VARIABLES = {'precipitation': ('tp', 'total_precipitation'),
             'temperature': ('t2m', '2t', '2m_temperature'),
             'evaporation': ('pev', 'potential_evaporation')}


def lake_precipitation_factor(manifest):
    """Explicit input-version correction; never alter catchment precipitation."""
    correction = manifest.get('lakePrecipitationCorrection')
    if correction is None:
        return 1.
    if correction.get('method') != 'station-point-annual-ratio-v1' or correction.get('scope') != 'lake-only':
        raise ValueError('不支持的湖面降水订正定义')
    factor = correction.get('factor')
    if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not np.isfinite(factor) or not 0 < factor <= 2:
        raise ValueError('湖面降水订正比例无效')
    return float(factor)


def register_lake_correction(db, baseline_id, correction):
    """Create an immutable derived input; original downloaded arrays are kept."""
    parent, _ = load_input(db, baseline_id)
    if parent.get('lakePrecipitationCorrection'):
        raise ValueError('不得对已经订正的输入叠加同类订正')
    lake_precipitation_factor({'lakePrecipitationCorrection': correction})
    identity = {'parentInput': baseline_id, 'parentDataSHA256': parent['dataSHA256'],
                'lakePrecipitationCorrection': correction}
    identifier = 'INPUT-' + digest(identity)[:24]
    folder = db.workspace / 'inputs/products' / identifier
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'forcing.npz'
    if not path.exists():
        source = db.workspace / 'inputs/products' / baseline_id / parent['data']
        try:
            # The derived manifest carries the lake-only rule; the immutable
            # raw arrays are identical, so same-volume products can share bytes.
            os.link(source, path)
        except FileExistsError:
            pass
        except OSError:
            # Cross-volume or unsupported filesystems keep a byte-identical,
            # exclusively created copy. Existing targets are never overwritten.
            try:
                with source.open('rb') as src, path.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
            except FileExistsError:
                pass
    if sha_file(path) != parent['dataSHA256']:
        raise ValueError('派生输入数据与原始驱动摘要不一致')
    manifest = {**parent, 'id': identifier, 'name': '标准历史驱动 · 湖面降水校正',
                'createdAt': now(), 'parentInputId': baseline_id,
                'lakePrecipitationCorrection': correction,
                'transformationSHA256': digest(correction), 'data': 'forcing.npz',
                'assumptions': parent.get('assumptions', []) + [
                    '仅湖面降水乘已冻结的主站/同点格网年总量比；流域降水与温度蒸发原值保留',
                    '单站代表湖面的空间转移假设；不视为实测湖面平均降水']}
    with db.connect() as connection:
        prior = connection.execute('SELECT manifest FROM input_products WHERE id=?', (identifier,)).fetchone()
        if prior:
            previous = json.loads(prior['manifest'])
            if previous.get('transformationSHA256') != manifest['transformationSHA256']:
                raise ValueError('已有派生输入定义不一致')
            return previous
        write_json(folder/'manifest.json', manifest)
        connection.execute('INSERT INTO input_products VALUES(?,?,?,?)',
                           (identifier, manifest['name'], manifest['createdAt'], canonical(manifest)))
    db.log('inputs.lake-correction', {'id': identifier, **identity})
    return manifest


def deaccumulate(values, times):
    """ERA5-Land: 01UTC starts the daily accumulation; 00UTC ends it."""
    array = np.asarray(values, dtype=float)
    times = pd.DatetimeIndex(times)
    result = np.full_like(array, np.nan)
    for i, t in enumerate(times):
        if t.hour == 1:
            result[i] = array[i]
        elif i and t - times[i - 1] == pd.Timedelta(hours=1):
            result[i] = array[i] - array[i - 1]
    return result


def normalize_variable(ds, name, grids):
    import xarray as xr
    arr = ds[name]
    attrs = dict(arr.attrs)
    for dim in ('expver',):
        if dim in arr.dims:
            merged = arr.isel({dim: 0}, drop=True)
            for i in range(1, arr.sizes[dim]):
                merged = merged.combine_first(arr.isel({dim: i}, drop=True))
            arr = merged
    lat_name = next((n for n in ('latitude', 'lat') if n in arr.coords), None)
    lon_name = next((n for n in ('longitude', 'lon') if n in arr.coords), None)
    if not lat_name or not lon_name:
        raise ValueError(f'{name} 缺少规则经纬度坐标')
    for coordinate in (lat_name, lon_name):
        if len(ds[coordinate]) > 1 and not np.allclose(np.abs(np.diff(ds[coordinate].values)), .1, atol=.00001, rtol=0):
            raise ValueError('历史输入须使用原生规则0.1° ERA5-Land网格')
    selected = arr.sel({lat_name: xr.DataArray([g['lat'] for g in grids], dims='cell'),
                        lon_name: xr.DataArray([g['lon'] for g in grids], dims='cell')}, method='nearest')
    if np.max(np.abs(selected[lat_name].values - np.array([g['lat'] for g in grids]))) > .001 or np.max(np.abs(selected[lon_name].values - np.array([g['lon'] for g in grids]))) > .001:
        raise ValueError('气象经纬度未完整覆盖登记格网，禁止重复使用边缘格点')
    time_name = next((n for n in ('valid_time', 'time') if n in selected.coords), None)
    if time_name is None:
        raise ValueError(f'{name}缺少时间坐标')
    time_coord = selected[time_name]
    time_dims = time_coord.dims
    for dim in list(selected.dims):
        if dim not in (*time_dims, 'cell'):
            if selected.sizes[dim] != 1:
                raise ValueError(f'不支持的历史输入维度 {dim}')
            selected = selected.isel({dim: 0}, drop=True)
    if len(time_dims) == 1:
        values = selected.transpose(time_dims[0], 'cell').values
        times = pd.DatetimeIndex(time_coord.values)
    else:
        values = selected.stack(sample=time_dims).transpose('sample', 'cell').values
        times = pd.DatetimeIndex(time_coord.stack(sample=time_dims).values)
    return times, np.asarray(values, dtype=np.float64), attrs


def register_era5(paths, database=None, name='ERA5-Land历史输入', progress=None):
    import xarray as xr
    db = database or Database()
    spatial = read_json(ROOT / 'data/derived/spatial/manifest.json')
    if not spatial:
        raise ValueError('请先建立空间产品')
    files = sorted({Path(p).resolve() for p in paths})
    if not files:
        raise ValueError('未找到真实ERA5-Land文件')
    data, sources, attrs_map = {key: [] for key in VARIABLES}, [], {}
    for i, path in enumerate(files):
        if progress:
            progress(i / len(files) * 75, f'检查 {path.name}')
        with xr.open_dataset(path, engine='h5netcdf') as ds:
            text = json.dumps(dict(ds.attrs), default=str).lower()
            if any(word in text for word in ('synthetic', 'demonstration', 'frontend-fixture')):
                raise ValueError(f'拒绝登记演示或合成驱动：{path.name}')
            found = []
            for key, aliases in VARIABLES.items():
                source_name = next((n for n in aliases if n in ds), None)
                if source_name:
                    times, values, attrs = normalize_variable(ds, source_name, spatial['grids'])
                    data[key].append(pd.DataFrame(values, index=times))
                    attrs_map[key] = attrs
                    found.append(source_name)
            if found:
                sources.append({'name': path.name, 'sha256': sha_file(path), 'variables': found, 'bytes': path.stat().st_size})
    if not all(data.values()):
        raise ValueError('历史计算需要完整的tp、t2m与pev三类真实输入')
    merged = {}
    for key, frames in data.items():
        frame = pd.concat(frames).sort_index()
        if frame.index.duplicated().any():
            duplicates = frame[frame.index.duplicated(keep=False)]
            # Equal overlapping downloads are permitted, conflicting revisions
            # must be registered separately instead of silently taking one.
            if any(np.ptp(group.values, axis=0).max() > 1e-8 for _, group in duplicates.groupby(level=0)):
                raise ValueError(f'{key}同一时次存在不同资料值，须分别建立输入版本')
            frame = frame[~frame.index.duplicated(keep='first')]
        merged[key] = frame
    times = merged['temperature'].index.intersection(merged['precipitation'].index).intersection(merged['evaporation'].index)
    if not len(times):
        raise ValueError('气象变量没有共同时间覆盖')
    times = times.sort_values()
    if len(times) > 1 and not ((times[1:] - times[:-1]) == pd.Timedelta(hours=1)).all():
        raise ValueError('气象资料存在小时断档，请补齐后登记')
    arrays = {key: value.reindex(times).values for key, value in merged.items()}
    for key in ('precipitation', 'evaporation'):
        if attrs_map[key].get('GRIB_stepType') != 'accum':
            raise ValueError(f'{key}未声明累计量含义；需带GRIB_stepType=accum的原始ERA5-Land文件')
        units = str(attrs_map[key].get('units', '')).lower().strip()
        if units not in ('m', 'm of water equivalent', 'm of water equivalent per hour'):
            raise ValueError(f'{key}原始单位必须明确为m，当前为{units}')
        arrays[key] = deaccumulate(arrays[key], times) * 1000
    p = arrays['precipitation']
    # GRIB packing produces tiny non-monotone accumulated values (Jan 2023:
    # minimum -0.0000298 mm). Only the documented 0.0001 mm tolerance is clipped.
    negative_noise = p < 0
    minimum_increment = float(np.nanmin(p))
    if minimum_increment < -1e-4:
        raise ValueError('降水差分出现负值，请核对累计量类型和日界')
    arrays['precipitation'] = np.maximum(p, 0)
    arrays['evaporation'] = np.maximum(-arrays['evaporation'], 0)
    units = str(attrs_map['temperature'].get('units', '')).lower().strip()
    if units not in ('k', 'kelvin'):
        raise ValueError('t2m必须明确为Kelvin，禁止猜测温度单位')
    arrays['temperature'] -= 273.15
    valid = np.logical_and.reduce([np.isfinite(a).all(axis=1) for a in arrays.values()])
    positions = np.flatnonzero(valid)
    if not len(positions):
        raise ValueError('没有完整有效的逐小时气象')
    a, b = positions[0], positions[-1] + 1
    if not valid[a:b].all():
        raise ValueError('气象资料中段含缺测，不能填零后计算')
    times = times[a:b] + pd.Timedelta(hours=8)
    arrays = {k: v[a:b] for k, v in arrays.items()}
    unique_sources = {s['sha256']: {'sha256': s['sha256'], 'variables': s['variables'], 'bytes': s['bytes']} for s in sources}
    source_identity = {'spatialVersion': spatial['version'], 'sourceDigests': [unique_sources[k] for k in sorted(unique_sources)],
                       'conversionVersion': 'era5land-utc01-reset-mm-celsius-local-end-hour-v1'}
    identifier = 'INPUT-' + digest(source_identity)[:24]
    folder = db.workspace / 'inputs/products' / identifier
    folder.mkdir(parents=True, exist_ok=True)
    data_path = folder / 'forcing.npz'
    if not data_path.is_file():
        np.savez_compressed(data_path, time=times.values.astype('datetime64[ns]').astype('int64'), **arrays)
    else:
        with np.load(data_path, allow_pickle=False) as prior:
            if not np.array_equal(prior['time'], times.values.astype('datetime64[ns]').astype('int64')) or any(not np.array_equal(prior[k], v) for k, v in arrays.items()):
                raise ValueError('已登记输入内容发生变化，拒绝覆盖或重新认可损坏产品')
    manifest = {**source_identity, 'id': identifier, 'name': name, 'createdAt': now(), 'projectId': 'selinco',
                'sources': sources,
                'source': 'Copernicus ERA5-Land', 'resultSource': 'registered-real-data',
                'timeZone': 'Asia/Shanghai', 'timeMeaning': 'end of hourly accumulation interval',
                'start': times[0].isoformat(), 'end': times[-1].isoformat(), 'hours': len(times),
                'gridCount': len(spatial['grids']), 'forcingResolution': '0.1°',
                'units': {'precipitation': 'mm/h', 'temperature': 'degC', 'evaporation': 'mm/h potential open-water evaporation'},
                'data': 'forcing.npz', 'dataSHA256': sha_file(data_path),
                'quality': {'precipitationNegativeToleranceMm': .0001,
                            'negativePackingNoiseCount': int(negative_noise.sum()),
                            'minimumPrecipitationIncrementMm': minimum_increment},
                'assumptions': ['未经站点偏差订正的公开格网基线', 'PEV为蒸发能力而非实测湖面蒸发',
                                '原始正向凝结通量不作为负蒸发能力输入'],
                'futureInterface': {'issueTime': None, 'leadHours': None, 'member': None}}
    write_json(folder / 'manifest.json', manifest)
    from .quality import forcing_report
    forcing_report(db, identifier, times, arrays['precipitation'], spatial)
    with db.connect() as conn:
        conn.execute('INSERT OR IGNORE INTO input_products VALUES(?,?,?,?)', (identifier, name, now(), canonical(manifest)))
    db.log('inputs.register', {'id': identifier, 'hours': len(times), 'sources': len(sources)})
    if progress:
        progress(100, f'真实输入登记完成：{len(times)}小时')
    return manifest


def load_input(db, identifier, spatial=None):
    folder = within(db.workspace / 'inputs/products', identifier)
    manifest = read_json(folder / 'manifest.json')
    if not manifest or manifest['id'] != identifier or manifest.get('projectId') != 'selinco':
        raise ValueError('输入产品不存在或不属于色林错')
    spatial = spatial or read_json(ROOT / 'data/derived/spatial/manifest.json')
    if manifest['spatialVersion'] != spatial['version']:
        raise ValueError('输入产品与当前空间单元不一致，请重新登记')
    if manifest.get('lakePrecipitationCorrection') and digest(manifest['lakePrecipitationCorrection']) != manifest.get('transformationSHA256'):
        raise ValueError('湖面降水转换定义摘要不一致')
    from .land_precipitation import validate_manifest
    validate_manifest(manifest)
    path = folder / manifest['data']
    if sha_file(path) != manifest['dataSHA256']:
        raise ValueError('输入文件摘要发生变化，拒绝使用')
    with np.load(path, allow_pickle=False) as source:
        arrays = {key: source[key].copy() for key in source.files}
    arrays['time'] = pd.DatetimeIndex(arrays['time'].astype('datetime64[ns]'))
    return manifest, arrays
