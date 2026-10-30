"""Resolve the completed, verified model forcing without exposing product choices."""
from copy import deepcopy
from pathlib import Path
import json
import threading
import zipfile

import numpy as np
import pandas as pd

from .common import ROOT, digest, read_json, sha_file, within
from . import grid_spatial

CURRENT_SETTING = 'current-model-forcing'
CONVERSION = 'era5land-utc01-reset-mm-celsius-local-end-hour-v1'
UNITS = {'precipitation': 'mm/h', 'temperature': 'degC',
         'evaporation': 'mm/h potential open-water evaporation'}
_verified = {}
_lock = threading.RLock()


def _required_correction():
    scheme = read_json(ROOT / 'config/operational/continuous-builtin-v1.json') or {}
    required = scheme.get('inputRequirement') or {}
    correction = required.get('lakePrecipitationCorrection')
    if not correction or digest(correction) != required.get('transformationSHA256'):
        raise ValueError('模型湖面降水处理设置无效，请检查方案配置')
    return required['transformationSHA256']


def _product(db, identifier):
    return next((row for row in db.items('input_products') if row['id'] == identifier), None)


def _verify_variable(archive, name, expected_shape):
    """Stream each array, checking values as well as its declared dimensions."""
    with archive.open(name + '.npy') as stream:
        version = np.lib.format.read_magic(stream)
        if version == (1, 0):
            shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
        elif version == (2, 0):
            shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
        else:
            raise ValueError('气象数组格式不受支持')
        if shape != expected_shape or dtype.kind not in 'iuf':
            raise ValueError('气象资料变量维数或类型不完整：' + name)
        size = int(np.prod(shape)) * dtype.itemsize
        block = (8 * 1024 * 1024 // dtype.itemsize) * dtype.itemsize
        while size:
            part = stream.read(min(block, size))
            if not part or len(part) % dtype.itemsize:
                raise ValueError('气象资料变量字节不完整：' + name)
            if not np.isfinite(np.frombuffer(part, dtype=dtype)).all():
                raise ValueError('气象资料变量存在缺测：' + name)
            size -= len(part)
        if stream.read(1):
            raise ValueError('气象资料变量字节数量不一致：' + name)


def verify_product(db, product, spatial=None):
    """Verify immutable registered bytes and their hourly/shape contract once per file state."""
    if not product:
        raise ValueError('当前模型气象资料尚未登记，请先更新资料')
    m = product['manifest']
    if m.get('id') != product['id'] or m.get('projectId') != 'selinco':
        raise ValueError('当前气象资料身份无效')
    if (m.get('conversionVersion') != CONVERSION or m.get('timeZone') != 'Asia/Shanghai'
            or m.get('timeMeaning') != 'end of hourly accumulation interval'
            or m.get('units') != UNITS):
        raise ValueError('气象资料的变量、单位、时区或累计量处理不一致')
    lake_rule=m.get('lakePrecipitationCorrection') or {}
    if (digest(lake_rule) != m.get('transformationSHA256') or
            (m.get('transformationSHA256') != _required_correction() and
             lake_rule.get('method') != 'station-warm-rain-grid-v1')):
        raise ValueError('气象资料缺少当前方案的湖面降水处理规则')
    from .land_precipitation import validate_manifest
    validate_manifest(m)
    from .forcing import lake_precipitation_factor
    lake_precipitation_factor(m)
    source_spatial = grid_spatial.load(m['spatialVersion'])
    if spatial is not None and digest(source_spatial['grids']) != spatial.get('forcingGridVersion', digest(spatial['grids'])):
        raise ValueError('模型气象格网与方案计算格网不一致')
    count = m.get('gridCount')
    if not isinstance(count, int) or count != len(source_spatial['grids']):
        raise ValueError('气象资料的格网数与空间定义不一致')
    folder = within(db.workspace / 'inputs/products', product['id'])
    stored = read_json(folder / 'manifest.json')
    if not stored or digest(stored) != digest(m):
        raise ValueError('气象资料登记信息与文件不一致')
    file = within(folder, m.get('data', ''))
    if not file.is_file():
        raise ValueError('气象资料文件缺失，请重新接入')
    stat = file.stat()
    key = (str(file), stat.st_size, stat.st_mtime_ns, m.get('dataSHA256'), digest(m))
    with _lock:
        if key in _verified:
            return deepcopy(_verified[key])
        if sha_file(file) != m.get('dataSHA256'):
            raise ValueError('气象资料文件摘要不一致，请重新核验')
        try:
            with zipfile.ZipFile(file) as archive:
                if set(archive.namelist()) != {'time.npy', 'precipitation.npy', 'temperature.npy', 'evaporation.npy'}:
                    raise ValueError('气象资料缺少完整的降水、气温或蒸发变量')
                for variable in UNITS:
                    _verify_variable(archive, variable, (m['hours'], count))
                with archive.open('time.npy') as stream:
                    times = np.load(stream, allow_pickle=False)
        except (OSError, zipfile.BadZipFile, KeyError, TypeError) as exc:
            raise ValueError('气象资料文件无法读取，请重新核验') from exc
        if times.ndim != 1 or len(times) != m['hours'] or len(times) == 0 or times.dtype.kind != 'i':
            raise ValueError('气象资料的逐时时间坐标无效')
        if len(times) > 1 and not np.all(np.diff(times) == 3600 * 10**9):
            raise ValueError('气象资料存在逐小时缺口，请补充缺失时段')
        start, end = pd.Timestamp(times[0]), pd.Timestamp(times[-1])
        if start != pd.Timestamp(m['start']) or end != pd.Timestamp(m['end']):
            raise ValueError('气象资料覆盖与实际时间坐标不一致')
        result = {'start': start.isoformat(), 'end': end.isoformat(), 'hours': len(times),
                  'firstCompleteDay': start.ceil('D').date().isoformat(),
                  'lastCompleteDay': (end.normalize() - pd.Timedelta(days=1)).date().isoformat(),
                  'variables': ['降水', '气温', '蒸发能力'], 'complete': True}
        _verified.clear()
        _verified[key] = result
        return deepcopy(result)


def current_product(db):
    """A successful update is the sole current library; incomplete products are ignored."""
    setting = db.setting(CURRENT_SETTING)
    identifier = setting.get('inputId') if isinstance(setting, dict) else None
    if not identifier:
        rows = db.rows("SELECT result FROM tasks WHERE kind='update-forcing' AND status='succeeded' ORDER BY updated_at DESC")
        for row in rows:
            try:
                result = json.loads(row['result'] or '{}')
            except (TypeError, ValueError):
                continue
            if result.get('inputId'):
                identifier = result['inputId']
                break
    if not identifier:
        builtin = read_json(ROOT / 'config/operational/continuous-builtin-v1.json') or {}
        identifier = builtin.get('inputId')
    product = _product(db, identifier)
    if not product:
        raise ValueError('当前模型气象资料不存在，请先更新资料')
    return product


def resolve(db, start=None, end=None, spatial=None):
    product = current_product(db)
    coverage = verify_product(db, product, spatial)
    if start is not None and pd.Timestamp(start) < pd.Timestamp(coverage['start']):
        raise ValueError(f'气象资料从{coverage["firstCompleteDay"]}起有完整日，预热起点{pd.Timestamp(start):%Y-%m-%d}缺资料，请补充该时段')
    if end is not None and pd.Timestamp(end) > pd.Timestamp(coverage['end']):
        raise ValueError(f'气象资料完整日截至{coverage["lastCompleteDay"]}，所选结束日期之后缺少逐时资料，请更新')
    return product, coverage


def current_summary(db):
    try:
        _, coverage = resolve(db)
        return coverage
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'complete': False, 'message': str(exc)}
