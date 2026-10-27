"""Backend-owned fixed-anchor derivation of the protected legacy lake result.

This archive is a derived reference, never a new model run, scientific admission
or publication. Parent bytes and the original window scores remain unchanged.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from threading import RLock
import uuid

import numpy as np
import pandas as pd

from .common import ROOT, canonical, digest, now, read_json, within
from .release_registry import PROTOCOL, current

REQUEST_KEYS = {'baselineReleaseId', 'protocolId', 'anchorConfigId'}
METHOD = 'legacy-hourly-volume-fixed-anchor-derivation-v1'
ANCHOR_CONFIG = {
    'id': 'selinco-fixed-anchor-20230420-v1',
    'time': '2023-04-20T12:00:00', 'level': 4544.03,
    'reference': 'SLC-working-datum', 'timeZone': 'Asia/Shanghai',
    'method': 'first-quality-daily-level-at-noon',
    'source': 'time-weighted-level-v2-gap6h-cover18h',
}
QUALITY_CONFIG = {
    'file': 'config/scientific/冻结共同质量观测-20261027.json',
    'fileSHA256': '599e9efc142d5d69914a4e4cd20c603d2fdbd5a9ba0b780263e583229922a50d',
    'observationSnapshotSHA256': 'a9a49990a15603b9aed1fdb0cc1830b4a2caa55d4e92ecf3967e1c5c9ec18032',
    'qualityProfile': 'time-weighted-level-v2-gap6h-cover18h',
    'sourceTaskId': 'JOB-f5ccc18ad51e4bdf9480',
    'rule': {'maximumSubdailyGapHours': 6, 'minimumDailyCoverageHours': 18,
             'sourceDailyAccepted': True, 'modelDailyRequiredHours': 24,
             'modelDay': 'end-hour; right-closed; left-labelled',
             'missingInterpolation': False, 'excludeOnlyFixedAnchorDay': True},
}
# The first implementation deliberately supports this one protected parent.
# A self-reported catalog SHA cannot redefine which original bytes it denotes.
PARENT_RESULT_ID = 'BUILTIN-historical-rev2-39192cc713c5'
PARENT_CATALOG_SHA256 = '695aea026ca611c7dfcf0320ea2c739deee208792cd1000334478bebdeaccca4'
PARENT_FILES = {'daily-snow.npz', 'daily.csv', 'final-state.json',
                'result.json', 'summary.json', 'hourly.npz'}
CUMULATIVE_RULE = {'absoluteToleranceM3': 1e-5, 'relativeTolerance': 1e-12,
                   'initialCumulativeVolumeM3': 0.0}
UNITS = {'time': 'datetime64[ns] stored as int64; naive Asia/Shanghai labels',
         'timeMeaning': 'end of hourly accumulation interval',
         'flow': 'm3/s', 'lakeLevel': 'm; SLC-working-datum',
         'lakeFlux': 'm3 per hourly interval', 'continuousLakeVolumeChange': 'm3',
         'lakeArea': 'm2', 'daily': '24 end-hours; right-closed; left-labelled'}
ARTIFACT_NAMES = {'连续参考.json', '日过程.csv', '月净水量账.csv', '湖位配对.csv',
                  '杜加流量配对.csv', '逐时连续参考.npz', '来源摘要.json'}
_migration_lock = RLock()


def _cancel(cancelled):
    if cancelled and cancelled():
        raise InterruptedError('用户停止连续参考生成；已写文件保留，不登记完整参考')


def _file(path, cancelled=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('冻结来源或归档文件缺失、不是普通文件：' + str(path))
    h = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        while True:
            _cancel(cancelled)
            block = stream.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            h.update(block)
    return {'bytes': size, 'sha256': h.hexdigest()}


def implementation_identity():
    source = ROOT / 'backend/continuous_reference.py' if getattr(sys, 'frozen', False) else Path(__file__)
    if not source.is_file():
        raise FileNotFoundError('连续参考实现源码未随程序安装，不能冻结真实实现摘要')
    return _file(source)['sha256']


def migrate(db):
    """Add the append-only table only after a SQLite online backup, including WAL."""
    with _migration_lock:
        if db.rows("SELECT name FROM sqlite_master WHERE type='table' AND name='continuous_reference_records'"):
            return
        backup = db.workspace / 'database' / ('pre-continuous-reference-v1-' + uuid.uuid4().hex[:12] + '.sqlite3')
        # Exclusive creation also preserves any earlier interrupted migration.
        with backup.open('xb'):
            pass
        with sqlite3.connect(str(db.path)) as source, sqlite3.connect(str(backup)) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('连续参考迁移前在线备份完整性检查失败')
        with db.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS continuous_reference_records(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            connection.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                               (now(), 'continuous-reference.migrate', canonical({'schemaVersion': 1, 'backup': str(backup)})))


def _request(request):
    if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
        raise ValueError('连续参考请求只接受baselineReleaseId、protocolId和anchorConfigId三个ID；禁止提交数值、分数、checks或路径')
    if any(not isinstance(request[key], str) or not request[key].strip() for key in REQUEST_KEYS):
        raise ValueError('连续参考请求必须完整指定三个身份ID')
    if request['protocolId'] != PROTOCOL['id'] or request['anchorConfigId'] != ANCHOR_CONFIG['id']:
        raise ValueError('未知冻结协议或已登记固定锚点配置')


def _quality(cancelled=None):
    path = within(ROOT, QUALITY_CONFIG['file'])
    meta = _file(path, cancelled)
    if meta['sha256'] != QUALITY_CONFIG['fileSHA256']:
        raise ValueError('冻结共同质量观测资源文件SHA变化')
    value = read_json(path)
    observation = value.get('observations') if isinstance(value, dict) else None
    if (not isinstance(observation, dict) or value.get('schemaVersion') != 1 or
            value.get('projectId') != 'selinco' or
            value.get('sourceTaskId') != QUALITY_CONFIG['sourceTaskId'] or
            value.get('qualityProfile') != QUALITY_CONFIG['qualityProfile'] or
            value.get('observationSnapshotSHA256') != QUALITY_CONFIG['observationSnapshotSHA256'] or
            digest(observation) != QUALITY_CONFIG['observationSnapshotSHA256'] or
            observation.get('profile') != QUALITY_CONFIG['qualityProfile']):
        raise ValueError('冻结共同质量观测、配置与原任务身份不一致')
    identity = value.get('sourceResultIdentity') or {}
    if (identity.get('observationProfile') != observation['profile'] or
            identity.get('observationSnapshotSHA256') != digest(observation)):
        raise ValueError('共同质量观测与冻结来源成果身份不一致')
    for key in ('level', 'flow'):
        if not isinstance(observation.get(key), dict):
            raise ValueError('冻结共同质量观测缺少日序列：' + key)
        for time, number in observation[key].items():
            stamp = pd.Timestamp(time)
            if (stamp.tzinfo or stamp != stamp.normalize() or
                    isinstance(number, bool) or not isinstance(number, (int, float)) or not np.isfinite(number)):
                raise ValueError('冻结质量观测须为有限数值及无时区北京时间日标签')
    if observation['level'].get(pd.Timestamp(ANCHOR_CONFIG['time']).normalize().isoformat()) != ANCHOR_CONFIG['level']:
        raise ValueError('登记锚点与冻结共同质量观测的原始日值不同')
    return observation, {'configuration': QUALITY_CONFIG, 'configurationSHA256': digest(QUALITY_CONFIG),
                         'resource': {'path': QUALITY_CONFIG['file'], **meta},
                         'sourceRequestSHA256': value.get('sourceRequestSHA256'),
                         'sourceResultSHA256': value.get('sourceResultSHA256'),
                         'sourceResultIdentity': identity}


def source_snapshot(db, request, cancelled=None):
    """Verify registered parent sources without loading any land-state arrays.

    Internal result keys are lineage (JSON), task, hourlyPath, observations and
    areaM2. Every call freshly hashes the parent catalog, six files and input.
    """
    _request(request)
    _cancel(cancelled)
    release = current(db)
    if request['baselineReleaseId'] != release['id']:
        raise ValueError('默认发布已变化，请按当前受保护基准重新提交')
    if release.get('status') != 'protected-legacy' or release.get('resultId') != PARENT_RESULT_ID:
        raise ValueError('C1当前仅支持已登记的受保护旧窗口成果')
    rows = db.rows('SELECT payload FROM evaluation_protocols WHERE id=?', (request['protocolId'],))
    if not rows or json.loads(rows[0]['payload']) != PROTOCOL:
        raise ValueError('数据库协议与冻结连续参考协议不一致')
    rows = db.rows('SELECT payload FROM scheme_revisions WHERE id=?', (release.get('schemeRevisionId'),))
    if not rows:
        raise ValueError('当前发布缺少不可变方案修订')
    revision = json.loads(rows[0]['payload'])
    catalog_path = ROOT / 'data/final-results/catalog.json'
    catalog = read_json(catalog_path)
    item = next((row for row in (catalog or {}).get('results', []) if row.get('id') == release['resultId']), None)
    if (not item or digest(item) != PARENT_CATALOG_SHA256 or set(item.get('files', {})) != PARENT_FILES):
        raise ValueError('受保护父成果的登记六文件catalog身份变化')
    directory = within(ROOT / 'data/final-results', item['directory'])
    files = []
    for name in sorted(PARENT_FILES):
        path = within(directory, name)
        meta = _file(path, cancelled)
        if meta != item['files'][name]:
            raise ValueError('受保护父成果文件SHA或大小变化：' + name)
        files.append({'path': path.relative_to(ROOT).as_posix(), **meta})
    task = read_json(directory / 'result.json')
    result = task.get('result') or {}
    parent_request = task.get('request') or {}
    identity = result.get('identity') or {}
    if (task.get('id') != release['resultId'] or task.get('status') != 'succeeded' or
            task.get('trashed') or result.get('modelFamily') or revision.get('status') != 'protected-legacy' or
            revision.get('identity') != identity or revision.get('scheme') != result.get('scheme') or
            revision.get('parameters') != result.get('parameters') or
            revision.get('partitionParameters', {}) != result.get('partitionParameters', {})):
        raise ValueError('父发布、不可变修订与真实成功成果身份不一致')
    if ((result.get('scheme') or {}).get('initialization', {}).get('lakeLevel') !=
            'first-reliable-value-per-observation-window'):
        raise ValueError('当前父成果不是已登记的旧窗口初始化成果')
    for key, payload in [('parameterSHA256', result.get('parameters')),
                         ('partitionParameterSHA256', result.get('partitionParameters', {}))]:
        if not payload or digest(payload) != identity.get(key):
            raise ValueError('父成果参数摘要不一致：' + key)
    for key in ('parameters', 'partitionParameters', 'routingMode', 'schemeSnapshot'):
        target = result.get('scheme') if key == 'schemeSnapshot' else result.get(key, {})
        if parent_request.get(key, {}) != target:
            raise ValueError('父实际请求与成果定义不同：' + key)
    from .operational_scheme import definition
    scheme = definition()
    if (scheme['configurationSHA256'] != result['scheme']['configurationSHA256'] or
            scheme['parameters'] != result['parameters'] or
            scheme.get('partitionParameters', {}) != result.get('partitionParameters', {})):
        raise ValueError('父发布参数或转换配置与受保护配置不同')
    # Resolve the parent's own old spatial version, never a candidate's area.
    version = result.get('spatialVersion')
    spatial_path = ROOT / 'data/derived/spatial/versions' / str(version) / 'manifest.json'
    if not spatial_path.is_file():
        spatial_path = ROOT / 'data/derived/spatial/manifest.json'
    spatial = read_json(spatial_path)
    if (not spatial or spatial.get('version') != version or identity.get('spatialVersion') != version or
            version != 'SPATIAL-' + digest({k: v for k, v in spatial.items() if k not in ('createdAt', 'version')})[:20] or
            result.get('units') != spatial.get('units')):
        raise ValueError('父自身空间版本、内容身份或计算单元变化')
    area = spatial.get('lakeAreaKm2')
    if isinstance(area, bool) or not isinstance(area, (int, float)) or not np.isfinite(area) or area <= 0 or spatial.get('areaCrs') != 'EPSG:6933':
        raise ValueError('父自身湖面积或面积坐标基准无效')
    from .release_runtime import _product
    manifest, input_path = _product(db, result['inputId'])
    if (manifest.get('dataSHA256') != identity.get('inputSHA256') or
            manifest.get('spatialVersion') != version or parent_request.get('inputId') != result['inputId'] or
            identity.get('inputId') != result['inputId'] or
            manifest.get('transformationSHA256') != scheme.get('inputRequirement', {}).get('transformationSHA256') or
            manifest.get('timeZone') != 'Asia/Shanghai' or
            manifest.get('timeMeaning') != 'end of hourly accumulation interval'):
        raise ValueError('父实际输入、转换、空间或小时口径不一致')
    if not manifest.get('sources') or not manifest.get('conversionVersion'):
        raise ValueError('父输入缺少冻结来源和转换血缘')
    from .release_runtime import _packaged_observations
    parent_observation = _packaged_observations(task['id'], identity.get('observationSHA256'))
    if parent_observation is None:
        raise ValueError('父成果原始冻结观测资源未登记，禁止改用当前观测')
    index_path = ROOT / 'config/operational/published-observations.json'
    index = read_json(index_path)['snapshots'][task['id']]
    parent_observation_path = within(ROOT / 'config/operational', index['file'])
    observations, quality = _quality(cancelled)
    final = read_json(directory / 'final-state.json')
    if final.get('identity') != identity or final.get('validAt') != result.get('end') or final.get('parameters') != result.get('parameters'):
        raise ValueError('父完整末状态与成果身份或结束时刻不同')
    lineage = {
        'release': release, 'schemeRevisionSHA256': digest(revision),
        'parentResultId': task['id'], 'parentIdentity': identity,
        'parentIdentitySHA256': digest(identity), 'parentRequestSHA256': digest(parent_request),
        'parentResultPayloadSHA256': digest(result), 'parentScheme': result['scheme'],
        'parentParameterSHA256': digest(result['parameters']),
        'parentPartitionParameterSHA256': digest(result.get('partitionParameters', {})),
        'parentModelImplementation': identity.get('modelImplementation'),
        'parentCatalogEntrySHA256': digest(item),
        'parentCatalogFile': {'path': catalog_path.relative_to(ROOT).as_posix(), **_file(catalog_path, cancelled)},
        'parentFiles': files,
        'parentSpatial': {'version': version, 'path': spatial_path.relative_to(ROOT).as_posix(),
                          **_file(spatial_path, cancelled), 'contentSHA256': digest(spatial),
                          'lakeAreaKm2': float(area), 'areaCrs': spatial['areaCrs'],
                          'unitsSHA256': digest(spatial['units']), 'sourceFiles': spatial.get('sourceFiles', {})},
        'parentConfiguration': {'path': 'config/operational/historical-standard-v1.json',
                                **_file(ROOT / 'config/operational/historical-standard-v1.json', cancelled),
                                'configurationSHA256': scheme['configurationSHA256']},
        'parentInput': {'id': result['inputId'], 'manifestSHA256': digest(manifest),
                        'manifestFile': _file(input_path.parent / 'manifest.json', cancelled),
                        'dataSHA256': manifest['dataSHA256'], 'dataBytes': input_path.stat().st_size,
                        'conversionVersion': manifest['conversionVersion'],
                        'transformationSHA256': manifest.get('transformationSHA256'),
                        'lakePrecipitationCorrection': manifest.get('lakePrecipitationCorrection'),
                        'sourceDigests': manifest.get('sourceDigests'), 'sources': manifest['sources']},
        'parentObservation': {'snapshotSHA256': digest(parent_observation),
                              'path': parent_observation_path.relative_to(ROOT).as_posix(),
                              **_file(parent_observation_path, cancelled),
                              'indexFile': _file(index_path, cancelled)},
        'commonQuality': quality, 'protocol': PROTOCOL, 'protocolSHA256': digest(PROTOCOL),
        'anchorConfig': ANCHOR_CONFIG, 'anchorConfigSHA256': digest(ANCHOR_CONFIG),
        'coverage': {'start': result['start'], 'end': result['end'], 'hours': result['hours']},
        'parentFinalContinuousVolumeM3': final.get('continuousLakeVolume'),
    }
    _cancel(cancelled)
    return {'lineage': lineage, 'task': task, 'hourlyPath': directory / 'hourly.npz',
            'observations': observations, 'areaM2': float(area) * 1e6}


def derive_hourly(time, flow, continuous_volume, net_flux, area_m2, anchor_time, anchor_level):
    """Pure small-vector calculation; times denote ends of hourly intervals.

    Flow columns are the seven legacy rivers, or (duja,total) for independent
    small examples. No interpolation and no yearly observation-window anchors.
    """
    times = pd.DatetimeIndex(pd.to_datetime(np.asarray(time)))
    if (not len(times) or times.hasnans or times.tz is not None or
            np.any(times.asi8 % (3600 * 10**9)) or
            (len(times) > 1 and not np.all(np.diff(times.asi8) == 3600 * 10**9))):
        raise ValueError('源time必须为无时区北京时间连续整小时，不能有缺口、重复或倒序')
    q = np.asarray(flow, dtype=np.float64)
    volume = np.asarray(continuous_volume, dtype=np.float64)
    flux = np.asarray(net_flux, dtype=np.float64)
    if (q.ndim != 2 or q.shape[0] != len(times) or q.shape[1] not in (2, 7) or
            volume.shape != (len(times),) or flux.shape != (len(times),) or
            not np.isfinite(q).all() or not np.isfinite(volume).all() or not np.isfinite(flux).all()):
        raise ValueError('源逐时flow、净量和连续累计体积shape或有限数值无效')
    if np.min(q) < -1e-10:
        raise ValueError('源逐时流量为负，不能派生参考')
    if q.shape[1] == 7 and not np.allclose(q[:, -1], q[:, 1:6].sum(axis=1), rtol=1e-12, atol=1e-10):
        raise ValueError('父总入湖流量与分区不一致；杜加不得重复累加')
    if (isinstance(area_m2, bool) or not np.isfinite(area_m2) or area_m2 <= 0 or
            isinstance(anchor_level, bool) or not np.isfinite(anchor_level)):
        raise ValueError('父自身湖面积与固定锚点高程必须为有效有限值')
    anchor = pd.Timestamp(anchor_time)
    if anchor.tzinfo or anchor.value % (3600 * 10**9) or anchor not in times:
        raise ValueError('源小时序列缺少固定锚点或锚点不是北京时间整小时')
    increments = np.diff(volume, prepend=CUMULATIVE_RULE['initialCumulativeVolumeM3'])
    differences = increments - flux
    tolerance = CUMULATIVE_RULE['absoluteToleranceM3'] + CUMULATIVE_RULE['relativeTolerance'] * np.abs(flux)
    if np.any(np.abs(differences) > tolerance):
        raise ValueError('连续累计体积差分与完整小时净水量不一致；禁止用窗口活动小时量替代')
    at = int(times.get_loc(anchor))
    levels = anchor_level + (volume - volume[at]) / area_m2
    levels[:at] = np.nan
    labels = (times - pd.Timedelta(nanoseconds=1)).normalize()
    daily = []
    for day in sorted(set(labels)):
        if day < times[0].normalize():
            continue
        ids = np.flatnonzero(labels == day)
        complete = len(ids) == 24
        lake_hours = int(np.isfinite(levels[ids]).sum())
        excluded = day == anchor.normalize()
        daily.append({'date': day.isoformat(), 'forcingHours': len(ids), 'lakeHours': lake_hours,
                      'lakeLevel': float(np.mean(levels[ids])) if complete and lake_hours == 24 and not excluded else None,
                      'duja': float(q[ids, 0].mean()) if complete else None,
                      'total': float(q[ids, -1].mean()) if complete else None,
                      'initializationDayExcluded': bool(excluded)})
    monthly = []
    months = labels.to_period('M')
    for month in sorted(set(months)):
        ids = np.flatnonzero((months == month) & (labels >= times[0].normalize()))
        if not len(ids):
            continue
        first, last = int(ids[0]), int(ids[-1])
        opening = float(volume[first - 1]) if first else CUMULATIVE_RULE['initialCumulativeVolumeM3']
        net = float(flux[ids].sum())
        inflow = float(q[ids, -1].sum() * 3600)
        delta = float(volume[last] - opening)
        monthly.append({'month': str(month), 'hours': len(ids), 'complete': len(ids) == month.days_in_month * 24,
                        'firstHourEnd': times[first].isoformat(), 'lastHourEnd': times[last].isoformat(),
                        'inflowM3': inflow, 'atmosphericNetM3': net - inflow,
                        'hourlyNetM3': net, 'openingContinuousVolumeM3': opening,
                        'closingContinuousVolumeM3': float(volume[last]), 'cumulativeChangeM3': delta,
                        'cumulativeMinusHourlyNetM3': delta - net})
    return {'lakeLevel': levels, 'daily': daily, 'monthly': monthly,
            'consistency': {**CUMULATIVE_RULE, 'checkedHours': len(times),
                            'maximumDifferenceM3': float(np.max(np.abs(differences))),
                            'maximumDifferenceAt': times[int(np.argmax(np.abs(differences)))].isoformat(),
                            'anchorIndex': at, 'anchorCumulativeVolumeM3': float(volume[at])}}


def _arrays(source):
    # np.load on an NPZ is lazy: these four members alone are decompressed.
    with np.load(source['hourlyPath'], allow_pickle=False) as archive:
        arrays = {key: archive[key] for key in ('time', 'flow', 'continuousLakeVolumeChange', 'lakeFlux')}
    result = source['task']['result']
    if (arrays['time'].dtype.kind != 'i' or arrays['time'].shape != (result['hours'],) or
            result.get('rivers', ['duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total']) !=
            ['duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total']):
        raise ValueError('父真实小时数组覆盖、time类型或流量列定义不同')
    times = pd.to_datetime(arrays['time'])
    if not len(times) or times[0].isoformat() != result['start'] or times[-1].isoformat() != result['end']:
        raise ValueError('父小时数组与冻结成果起止不一致')
    final = source['lineage'].get('parentFinalContinuousVolumeM3')
    if final is None or not np.isfinite(final) or arrays['continuousLakeVolumeChange'][-1] != final:
        raise ValueError('父小时累计体积末值与完整末状态不同')
    return arrays


def _array_identity(value):
    a = np.ascontiguousarray(value)
    return {'shape': list(a.shape), 'dtype': a.dtype.str,
            'bytes': int(a.nbytes), 'sha256': hashlib.sha256(a.tobytes()).hexdigest()}


def _pairs(daily, observations):
    from .evaluation_archive import metrics
    pairs = {'lake': [], 'flow': []}
    for row in daily:
        if row['date'][:4] not in PROTOCOL['years']:
            continue
        for scope, sim, obs in [('lake', 'lakeLevel', 'level'), ('flow', 'duja', 'flow')]:
            number = observations[obs].get(row['date'])
            if number is not None and row[sim] is not None:
                pairs[scope].append({'date': row['date'][:10], 'observed': float(number), 'simulated': row[sim]})
    years = {year: {scope: metrics([r for r in rows if r['date'].startswith(year)])
                    for scope, rows in pairs.items()} for year in PROTOCOL['years']}
    return pairs, years


def _identifier(request, lineage, producer):
    return 'CREF-' + digest({'request': request, 'lineage': lineage,
                             'producerImplementationSHA256': producer, 'method': METHOD})[:24]


def _write_json(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def _write_csv(path, rows, fields):
    with Path(path).open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _report(record):
    return {k: v for k, v in record.items() if k not in ('artifacts', 'archiveComplete', 'recordSHA256')}


def _return(record, reused):
    return {'referenceId': record['id'], 'reused': reused,
            'archiveDirectory': record['archiveDirectory'], 'method': record['method']}


def generate(db, request, folder, progress=None, cancelled=None):
    """Heavy-queue operation. Only a complete, reverified archive gets a DB row."""
    _request(request)
    _cancel(cancelled)
    migrate(db)
    if progress:
        progress(3, '固定当前发布、协议与父六文件身份')
    source = source_snapshot(db, request, cancelled)
    lineage = source['lineage']
    producer = implementation_identity()
    identifier = _identifier(request, lineage, producer)
    existing = db.rows('SELECT payload FROM continuous_reference_records WHERE id=?', (identifier,))
    if existing:
        record = read_verified_reference(db, identifier)
        _cancel(cancelled)
        return _return(record, True)
    if progress:
        progress(25, '只读提取父小时小向量，核验累计差与完整净水量')
    arrays = _arrays(source)
    _cancel(cancelled)
    derived = derive_hourly(arrays['time'], arrays['flow'], arrays['continuousLakeVolumeChange'],
                            arrays['lakeFlux'], source['areaM2'], ANCHOR_CONFIG['time'], ANCHOR_CONFIG['level'])
    daily = derived['daily']
    for row in daily:
        row['observedLevel'] = source['observations']['level'].get(row['date'])
        row['observedFlow'] = source['observations']['flow'].get(row['date'])
    pairs, years = _pairs(daily, source['observations'])
    folder = Path(folder).resolve()
    archive_directory = folder.relative_to(db.workspace.resolve()).as_posix()
    if not archive_directory or archive_directory == '.':
        raise ValueError('连续参考必须使用独立归档目录')
    folder.mkdir(parents=True, exist_ok=True)
    if any((folder / name).exists() for name in ARTIFACT_NAMES):
        raise FileExistsError('目标已有连续参考文件；请用独立任务目录，禁止覆盖或清理旧输出')
    record = {
        'schemaVersion': 1, 'id': identifier, 'referenceId': identifier, 'request': dict(request),
        'baselineReleaseId': request['baselineReleaseId'], 'parentResultId': lineage['parentResultId'],
        'schemeRevisionId': lineage['release']['schemeRevisionId'],
        'protocolId': request['protocolId'], 'protocol': PROTOCOL, 'protocolSHA256': digest(PROTOCOL),
        'anchorConfig': ANCHOR_CONFIG, 'anchorConfigSHA256': digest(ANCHOR_CONFIG),
        'method': METHOD, 'referenceFamily': 'legacy-derived-continuous-reference-v1',
        'lakeMode': 'continuous-derived', 'initialization': dict(ANCHOR_CONFIG),
        'producerImplementationSHA256': producer, 'createdAt': now(),
        'qualityProfile': QUALITY_CONFIG['qualityProfile'],
        'observationSnapshotSHA256': digest(source['observations']),
        'qualityRuleSHA256': digest(QUALITY_CONFIG['rule']),
        'sourceLineage': lineage, 'sourceLineageSHA256': digest(lineage),
        'sourceArrays': {key: _array_identity(value) for key, value in arrays.items()},
        'loadedMembers': list(arrays), 'maximumLoadedArrayBytes': max(a.nbytes for a in arrays.values()),
        'parentLakeAreaM2': source['areaM2'], 'consistency': derived['consistency'],
        'units': UNITS,
        'daily': daily, 'monthly': derived['monthly'], 'pairs': pairs, 'years': years,
        'pairedDataSHA256': {scope: digest(rows) for scope, rows in pairs.items()},
        'commonDateSHA256': {scope: digest([r['date'] for r in rows]) for scope, rows in pairs.items()},
        'initializationExclusions': {'rule': 'exclude-only-fixed-anchor-calendar-day-v1',
                                     'dates': [ANCHOR_CONFIG['time'][:10]],
                                     'datesSHA256': digest([ANCHOR_CONFIG['time'][:10]])},
        'archiveDirectory': archive_directory, 'archiveComplete': False, 'artifacts': [],
        'scientificAdmission': 'not-evaluated',
        'limitations': ['本成果为旧产汇流结果固定锚点派生连续湖位，未重新率定、未独立重跑原模型。',
                        '父原窗口成果、六文件、模型身份和窗口评分原样保留；本归档不更新默认发布。',
                        '湖面使用父空间版本的定面积；SLC为工作高程基准，未建立独立测量零点。',
                        '月账大气净交换由完整小时净量减入湖量得到，未独立拆解降水和蒸发，非正式物理诊断通过证明。',
                        '输入文件和已冻结来源摘要得到核对，不等同原始气象、严格续算、长期初态或科学准入已验证。',
                        '2025及2026资料已用于开发，不能称未触碰的外部验证。'],
    }
    if progress:
        progress(55, '归档24小时完整日、冻结质量配对与月净水量账')
    _cancel(cancelled)
    _write_csv(folder / '日过程.csv', daily, list(daily[0]) if daily else
               ['date', 'forcingHours', 'lakeHours', 'lakeLevel', 'duja', 'total', 'initializationDayExcluded', 'observedLevel', 'observedFlow'])
    _write_csv(folder / '月净水量账.csv', derived['monthly'], list(derived['monthly'][0]) if derived['monthly'] else ['month', 'hours'])
    for scope, name in [('lake', '湖位配对.csv'), ('flow', '杜加流量配对.csv')]:
        _cancel(cancelled)
        _write_csv(folder / name, pairs[scope], ['date', 'observed', 'simulated'])
    with (folder / '逐时连续参考.npz').open('xb') as stream:
        np.savez_compressed(stream, time=arrays['time'], flow=arrays['flow'], lakeLevel=derived['lakeLevel'],
                            continuousLakeVolumeChange=arrays['continuousLakeVolumeChange'], lakeFlux=arrays['lakeFlux'])
    _write_json(folder / '来源摘要.json', {'lineage': lineage, 'sourceArrays': record['sourceArrays']})
    _write_json(folder / '连续参考.json', _report(record))
    for name in sorted(ARTIFACT_NAMES):
        record['artifacts'].append({'path': name, **_file(folder / name, cancelled)})
    record['archiveComplete'] = True
    record['recordSHA256'] = digest({k: v for k, v in record.items() if k != 'recordSHA256'})
    if progress:
        progress(95, '结束前重新核验默认发布及全部来源身份')
    _cancel(cancelled)
    if implementation_identity() != producer or source_snapshot(db, request, cancelled)['lineage'] != lineage:
        raise ValueError('生成期间默认发布、实现或冻结来源变化，连续参考归档未提交')
    for artifact in record['artifacts']:
        if _file(within(folder, artifact['path']), cancelled) != {k: artifact[k] for k in ('bytes', 'sha256')}:
            raise ValueError('生成期间连续参考归档产物变化，归档未提交：' + artifact['path'])
    inserted = False
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        latest = connection.execute('SELECT * FROM operational_releases ORDER BY sequence DESC LIMIT 1').fetchone()
        if (not latest or latest['id'] != lineage['release']['id'] or
                latest['result_id'] != lineage['parentResultId'] or
                latest['scheme_revision_id'] != record['schemeRevisionId']):
            raise ValueError('生成期间默认发布变化，连续参考归档未提交')
        protocol = connection.execute('SELECT payload FROM evaluation_protocols WHERE id=?', (request['protocolId'],)).fetchone()
        revision = connection.execute('SELECT payload FROM scheme_revisions WHERE id=?', (record['schemeRevisionId'],)).fetchone()
        if (not protocol or json.loads(protocol[0]) != PROTOCOL or not revision or
                digest(json.loads(revision[0])) != lineage['schemeRevisionSHA256']):
            raise ValueError('生成期间协议或不可变方案修订变化，归档未提交')
        _cancel(cancelled)
        if not connection.execute('SELECT 1 FROM continuous_reference_records WHERE id=?', (identifier,)).fetchone():
            connection.execute('INSERT INTO continuous_reference_records VALUES(?,?)', (identifier, canonical(record)))
            connection.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                               (now(), 'continuous-reference.archive', canonical({'referenceId': identifier,
                                'baselineReleaseId': request['baselineReleaseId'], 'sourceLineageSHA256': digest(lineage)})))
            inserted = True
    if not inserted:
        return _return(read_verified_reference(db, identifier), True)
    if progress:
        progress(100, '连续参考与完整血缘归档完成；科学发布准入未评价')
    return _return(record, False)


def read_verified_reference(db, identifier):
    """Read-only C1 verification; this does not open C2 evaluation admission."""
    if not isinstance(identifier, str) or not identifier.startswith('CREF-'):
        raise ValueError('连续参考须使用后端登记referenceId')
    if not db.rows("SELECT name FROM sqlite_master WHERE type='table' AND name='continuous_reference_records'"):
        raise FileNotFoundError('没有已登记的连续参考')
    rows = db.rows('SELECT payload FROM continuous_reference_records WHERE id=?', (identifier,))
    if not rows:
        raise FileNotFoundError('连续参考不存在或尚未完整归档')
    record = json.loads(rows[0]['payload'])
    _request(record.get('request'))
    if (record.get('schemaVersion') != 1 or record.get('id') != identifier or
            record.get('referenceId') != identifier or record.get('archiveComplete') is not True or
            record.get('method') != METHOD or record.get('recordSHA256') !=
            digest({k: v for k, v in record.items() if k != 'recordSHA256'})):
        raise ValueError('连续参考记录未完成、方法身份或自身摘要不一致')
    producer = implementation_identity()
    source = source_snapshot(db, record['request'])
    if (record.get('producerImplementationSHA256') != producer or
            source['lineage'] != record.get('sourceLineage') or
            record.get('sourceLineageSHA256') != digest(source['lineage']) or
            _identifier(record['request'], source['lineage'], producer) != identifier or
            record.get('anchorConfig') != ANCHOR_CONFIG or record.get('protocol') != PROTOCOL or
            record.get('units') != UNITS or
            record.get('observationSnapshotSHA256') != digest(source['observations']) or
            record.get('parentLakeAreaM2') != source['areaM2']):
        raise ValueError('连续参考的父发布、来源、协议、锚点、湖面积或生产实现血缘不同')
    directory = within(db.workspace, record['archiveDirectory'])
    artifacts = record.get('artifacts')
    if (not isinstance(artifacts, list) or len(artifacts) != len(ARTIFACT_NAMES) or
            {item.get('path') for item in artifacts} != ARTIFACT_NAMES):
        raise ValueError('连续参考完整产物清单缺项或重复')
    for item in artifacts:
        if _file(within(directory, item['path'])) != {k: item[k] for k in ('bytes', 'sha256')}:
            raise ValueError('连续参考归档产物SHA或大小变化：' + item['path'])
    if read_json(directory / '连续参考.json') != _report(record):
        raise ValueError('连续参考文件与登记内容不一致')
    return record
