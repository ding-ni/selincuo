"""Immutable station-informed warm-rain forcing for a separate comparison.

The original ERA arrays and the existing lake-only transformation are unchanged.
The mask is frozen independently of fitted snow thresholds and protects the
existing aggregate kernel's snow partition over the declared lapse-rate range.
"""
from __future__ import annotations

from copy import deepcopy
import math
import os
import shutil

import numpy as np
import pandas as pd

from .common import canonical, digest, now, read_json, sha_file, within

METHOD = 'station-warm-rain-spatial-v1'
SEASONS = ('DJF', 'MAM', 'JJA', 'SON')
_factor_cache = {}


def validate_profile(profile):
    if not isinstance(profile, dict) or profile.get('method') != METHOD or profile.get('scope') != 'land-warm-rain':
        raise ValueError('陆域暖雨订正规则无效')
    if (profile.get('temperatureThresholdC') != 5.0 or profile.get('memberLapseRateCPerM') != -0.0065
            or profile.get('groupLapseRateBoundsCPerM') != [-0.01, -0.002]
            or profile.get('modelRainTemperatureMaxC') != 5.0):
        raise ValueError('陆域暖雨订正的固定温度条件无效')
    if profile.get('radiusKm') != 100.0 or profile.get('distanceRegularizationKm') != 5.0:
        raise ValueError('陆域暖雨订正的空间权重条件无效')
    stations = profile.get('stations')
    if not isinstance(stations, dict) or not stations:
        raise ValueError('陆域暖雨订正缺少站点')
    for station in stations.values():
        if (not isinstance(station, dict) or not math.isfinite(float(station.get('lon', float('nan'))))
                or not math.isfinite(float(station.get('lat', float('nan'))))):
            raise ValueError('站点坐标无效')
        factors = station.get('seasonFactors')
        if not isinstance(factors, dict) or any(k not in SEASONS for k in factors):
            raise ValueError('站点季节订正无效')
        if any(isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0
               for value in factors.values()):
            raise ValueError('站点季节倍率必须为有限正数')
    return profile


def validate_manifest(manifest):
    profile = manifest.get('landPrecipitationCorrection')
    expected = manifest.get('landTransformationSHA256')
    if profile is None and expected is None:
        return None
    validate_profile(profile)
    if digest(profile) != expected or profile.get('parentDataSHA256') != manifest.get('dataSHA256'):
        raise ValueError('陆域暖雨转换定义或原始气象摘要不一致')
    return profile


def validate_parameters(profile, base, regions=None):
    if profile is None:
        return
    validate_profile(profile)
    lo, hi = profile['groupLapseRateBoundsCPerM']
    for p in [base, *(regions or {}).values()]:
        lapse = p['lapseRate']
        if not lo <= lapse <= hi or p['rainTemperature'] > profile['modelRainTemperatureMaxC']:
            raise ValueError('水文气温递减率或全雨温度超出暖雨订正的物理适用范围')


def season_labels(times):
    # ERA timestamps mark the end of an interval: midnight belongs to the
    # preceding natural day. Station events use their actual occurrence time.
    months = (pd.DatetimeIndex(times) - pd.Timedelta(nanoseconds=1)).month.to_numpy()
    return np.where(np.isin(months, [12, 1, 2]), 0,
           np.where(np.isin(months, [3, 4, 5]), 1,
           np.where(np.isin(months, [6, 7, 8]), 2, 3)))


def _distances_km(lon, lat, station_lon, station_lat):
    lat1, lat2 = np.radians(lat), math.radians(station_lat)
    dlat = lat1-lat2
    dlon = np.radians(lon-station_lon)
    a = np.sin(dlat/2.)**2 + np.cos(lat1)*math.cos(lat2)*np.sin(dlon/2.)**2
    return 6371.0088*2*np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def factors_for_points(profile, points, radius=None, excluded_stations=()):
    """Season-by-point multipliers; radius overrides are diagnostic only."""
    validate_profile(profile)
    radius = float(profile['radiusKm'] if radius is None else radius)
    if radius not in (50., 100., 150.):
        raise ValueError('未登记的空间影响距离')
    xy = np.asarray(points, dtype=float).reshape(-1, 2)
    numerator = np.zeros((len(SEASONS), len(xy)))
    denominator = np.full_like(numerator, 1./radius**2)
    excluded = set(excluded_stations)
    for station_id, station in sorted(profile['stations'].items()):
        if station_id in excluded:
            continue
        distance = _distances_km(xy[:, 0], xy[:, 1], station['lon'], station['lat'])
        weight = np.where(distance < radius,
                          np.maximum(0., 1.-(distance/radius)**2)**2 /
                          (distance**2+profile['distanceRegularizationKm']**2), 0.)
        for i, season in enumerate(SEASONS):
            factor = station['seasonFactors'].get(season)
            if factor is not None:
                numerator[i] += weight*np.log(factor)
                denominator[i] += weight
    return np.exp(numerator/denominator)


def grid_factors(profile, grids):
    key = (digest(profile), digest(grids))
    if key not in _factor_cache:
        _factor_cache.clear()
        _factor_cache[key] = factors_for_points(profile, [(g['lon'], g['lat']) for g in grids])
    return _factor_cache[key]


def corrected_member_precipitation(profile, grids, times, raw_precip, raw_temp, indexes, offsets, weights):
    """Correct eligible members before grouping; guard the group's snow phase."""
    validate_profile(profile)
    indexes = np.asarray(indexes, dtype=int)
    offsets = np.asarray(offsets, dtype=float)
    weights = np.asarray(weights, dtype=float)
    if (raw_precip.shape != raw_temp.shape or raw_precip.shape[1] != len(indexes)
            or len(offsets) != len(indexes) or len(weights) != len(indexes)):
        raise ValueError('暖雨订正的细单元输入不一致')
    group_grid_temperature = raw_temp @ weights
    mean_offset = offsets @ weights
    lo, hi = profile['groupLapseRateBoundsCPerM']
    group_lower = np.minimum(group_grid_temperature + mean_offset*lo,
                             group_grid_temperature + mean_offset*hi)
    member_temp = raw_temp + offsets[None, :]*profile['memberLapseRateCPerM']
    mask = (member_temp >= profile['temperatureThresholdC']) & (group_lower[:, None] >= profile['temperatureThresholdC'])
    factors = grid_factors(profile, grids)[season_labels(times)[:, None], indexes[None, :]]
    return np.where(mask, raw_precip*factors, raw_precip)


def register_product(db, parent_id, profile):
    """Register a new immutable input whose forcing bytes are a hard link/copy."""
    validate_profile(profile)
    rows = [x for x in db.items('input_products') if x['id'] == parent_id]
    if len(rows) != 1:
        raise ValueError('站雨订正来源气象不存在')
    parent = rows[0]['manifest']
    if parent.get('landPrecipitationCorrection') or parent.get('landTransformationSHA256'):
        raise ValueError('不能叠加已有陆域降水订正')
    if profile.get('parentDataSHA256') != parent.get('dataSHA256'):
        raise ValueError('站雨订正与来源气象摘要不一致')
    land_sha = digest(profile)
    identity = {'parentInputId': parent_id, 'parentDataSHA256': parent['dataSHA256'],
                'landTransformationSHA256': land_sha}
    identifier = 'INPUT-' + digest(identity)[:24]
    folder = within(db.workspace / 'inputs/products', identifier)
    previous = [x for x in db.items('input_products') if x['id'] == identifier]
    if previous:
        manifest = previous[0]['manifest']
        if (manifest.get('landTransformationSHA256') != land_sha or
                read_json(folder/'manifest.json') != manifest or sha_file(folder/manifest['data']) != parent['dataSHA256']):
            raise ValueError('已有站雨订正输入与登记身份不一致')
        return manifest
    folder.mkdir(parents=True, exist_ok=True)
    source = within(db.workspace / 'inputs/products', parent_id) / parent['data']
    target = folder / 'forcing.npz'
    if not target.exists():
        try:
            os.link(source, target)
        except OSError:
            # Exclusive creation never replaces an existing input or original.
            with source.open('rb') as src, target.open('xb') as dst:
                shutil.copyfileobj(src, dst)
    if sha_file(source) != parent['dataSHA256'] or sha_file(target) != parent['dataSHA256']:
        raise ValueError('原气象或站雨订正输入字节摘要不一致')
    manifest = {**deepcopy(parent), 'id': identifier, 'name': 'ERA5-Land · 站点暖雨订正对照',
                'createdAt': now(), 'parentInputId': parent_id,
                'landPrecipitationCorrection': deepcopy(profile), 'landTransformationSHA256': land_sha}
    path = folder/'manifest.json'
    if path.exists():
        stored = read_json(path)
        if (not stored or stored.get('id') != identifier or stored.get('landTransformationSHA256') != land_sha):
            raise ValueError('已有站雨订正目录与本次身份不一致')
        manifest = stored
    else:
        with path.open('x', encoding='utf-8', newline='\n') as stream:
            import json
            json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
    with db.connect() as connection:
        connection.execute('INSERT INTO input_products VALUES(?,?,?,?)',
                           (identifier, manifest['name'], manifest['createdAt'], canonical(manifest)))
    db.log('inputs.land-precipitation-comparison', {'id': identifier, **identity})
    return manifest
