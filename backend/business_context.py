"""Small, read-only business views over frozen results and actual input coverage.

No hourly arrays are opened here. Browsing a candidate never changes a release.
"""
from functools import lru_cache
import json
from pathlib import PurePosixPath
import pandas as pd
from .common import ROOT, digest, read_json, within


@lru_cache(maxsize=8)
def _packaged(identifier, stamp):
    from .published_results import detail
    return detail(identifier)


def result_task(db, identifier):
    from .published_results import catalog
    row = next((r for r in catalog() if r['id'] == identifier), None)
    if row:
        path = ROOT / 'data/final-results' / row['directory'] / 'result.json'
        # All callers in this module are read-only; avoid copying daily data on
        # every summary request. The packaged loader verifies file integrity.
        return _packaged(identifier, path.stat().st_mtime_ns)
    return db.task(identifier)


def builtin_result_available(db, identifier):
    """Check that the configured result and its necessary artifacts exist here."""
    if not identifier:
        return False
    from .published_results import catalog
    item = next((row for row in catalog() if row['id'] == identifier), None)
    if item:
        folder = within(ROOT / 'data/final-results', item['directory'])
        return all((folder / name).is_file() for name in ('result.json', 'hourly.npz', 'final-state.json'))
    rows = db.rows('SELECT status,trashed,result FROM tasks WHERE id=?', (identifier,))
    if not rows or rows[0]['status'] != 'succeeded' or rows[0]['trashed'] or not rows[0]['result']:
        return False
    folder = within(db.workspace / 'tasks', identifier)
    if not all((folder / name).is_file() for name in ('result.json', 'checkpoint.json', 'daily.csv')):
        return False
    zarr = folder / 'hourly.zarr'
    if zarr.is_dir():
        try:
            attrs = read_json(zarr / '.zattrs', {})
            manifest = read_json(folder / 'hourly-manifest.json', {})
            result = json.loads(rows[0]['result'])
            artifact = result.get('hourlyArtifact')
            files = manifest.get('files') if isinstance(manifest, dict) else None
            from .grid_storage import FORMAT
            if not (isinstance(attrs, dict) and attrs.get('complete') is True and attrs.get('format') == FORMAT
                    and isinstance(manifest, dict) and manifest.get('complete') is True and manifest.get('format') == FORMAT
                    and isinstance(artifact, dict) and artifact.get('complete') is True and artifact.get('format') == FORMAT
                    and isinstance(files, dict) and files
                    and {'.zattrs', '.zgroup', 'time/.zarray'} <= files.keys()):
                return False
            if manifest.get('sha256') != digest(files) or artifact.get('sha256') != manifest['sha256']:
                return False
            for name, sha in files.items():
                if not (isinstance(name, str) and name and '\\' not in name and
                        PurePosixPath(name).as_posix() == name and not PurePosixPath(name).is_absolute() and
                        '..' not in PurePosixPath(name).parts and isinstance(sha, str) and len(sha) == 64 and
                        all(c in '0123456789abcdef' for c in sha)):
                    return False
                if not within(zarr, name).is_file():
                    return False
            return True
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
            return False
    return (folder / 'hourly.npz').is_file()


def result_summary(db, identifier):
    from .release_registry import current
    task = result_task(db, identifier)
    result = task.get('result') or {}
    if task.get('status') != 'succeeded' or not result:
        raise ValueError('成果尚未完整完成，不能作为业务成果查看')
    request = task.get('request') or {}
    scheme = result.get('scheme') or request.get('schemeSnapshot') or {}
    initialization = result.get('initialization') or scheme.get('initialization') or {}
    continuous = result.get('modelFamily') == 'grid-continuous-v1'
    window = initialization.get('lakeLevel') == 'first-reliable-value-per-observation-window'
    mode = 'continuous' if continuous else 'window' if window else 'unverified'
    daily = result.get('daily') or []
    limitations = [
        '主站仅作为工作基准；四岸零点待核，统计候选不等于测量校正。',
        '固定湖面与冰川面积、裸冰外部水源等简化仍然存在。',
        '2025、2026资料已在开发中查看，不能称为未触碰的外部验证。',
    ]
    limitations.append('未核实区独立计算，未计入已核实入湖合计。' if result.get('inflowComplete') is False
                       else '旧计水假定包含原残余区，不代表排水归属已经核实。')
    if window:
        limitations.append('窗口初始化指标不能与固定锚点连续指标直接排序。')
    elif not continuous:
        limitations.append('该成果的湖位初始化口径未完成核验，不参与默认准入排序。')
    identity = result.get('identity') or {}
    releases_exist = db.rows("SELECT name FROM sqlite_master WHERE type='table' AND name='operational_releases'")
    was_published = bool(releases_exist and db.rows('SELECT 1 FROM operational_releases WHERE result_id=? LIMIT 1', (identifier,)))
    return {
        'id': identifier, 'name': task.get('name') or '计算成果', 'status': task['status'],
        'role': 'current-default' if identifier == current(db)['resultId'] else
                'historical-release' if task.get('builtin') or was_published else 'research-candidate' if
                request.get('researchSchemeId') or continuous else 'user-result',
        'scheme': scheme, 'identity': identity, 'identitySHA256': digest(identity),
        'inputId': result.get('inputId') or request.get('inputId'),
        'spatialVersion': result.get('spatialVersion'), 'unitCount': len(result.get('units') or []),
        'calculationCoverage': {'start': result.get('start'), 'end': result.get('end')},
        'displayCoverage': {'start': daily[0]['date'] if daily else None,
                            'end': daily[-1]['date'] if daily else None},
        'evaluation': {'mode': mode,
                       'label': {'continuous': '固定锚点连续评价', 'window': '窗口初始化评价',
                                 'unverified': '初始化口径待核验'}[mode],
                       'initialization': initialization,
                       'annual': result.get('metrics') or {}, 'source': 'frozen-result',
                       'qualityProfile': result.get('observationProfile') or request.get('_observations', {}).get('profile', 'frozen-legacy'),
                       'commonDatesCompared': False,
                       'formalScoresFollowChartZoom': False},
        'inflowComplete': result.get('inflowComplete'), 'drainage': result.get('drainage'),
        'limitations': limitations,
    }


def business_context(db, identifier=None):
    from .release_registry import current
    release = current(db)
    products = db.items('input_products')
    # Coverage is reported for each frozen input, without mixing three variables
    # from different products or silently choosing a new input for an old result.
    coverages = []
    for product in products:
        m = product['manifest']
        end = pd.Timestamp(m['end'])
        coverages.append({'inputId': product['id'], 'name': product['name'],
                          'start': m['start'], 'end': m['end'],
                          'lastCompleteDay': (end.normalize()-pd.Timedelta(days=1)).date().isoformat(),
                          'spatialVersion': m['spatialVersion']})
    latest = max(coverages, key=lambda r: r['end'], default=None)
    from .observation_profiles import daily_quality, QUALITY_VERSION
    quality = daily_quality(db, 'SLC')
    valid = [r for r in quality if r['value'] is not None]
    last = valid[-1] if valid else None
    default = result_summary(db, release['resultId'])
    selected = default if not identifier or identifier == release['resultId'] else result_summary(db, identifier)
    return {'projectId': 'selinco', 'timezone': 'Asia/Shanghai', 'release': release,
            'selectedResult': selected, 'defaultResult': default,
            'defaultResultInputId': default['inputId'],
            'forcing': {'latest': latest, 'products': coverages,
                        'selectedResult': next((r for r in coverages if r['inputId'] == selected['inputId']), None)},
            'observations': {'mainStation': {'stationId': 'SLC', 'lastValidDay': last['time'][:10] if last else None,
                                           'lastValidValue': last['value'] if last else None,
                                           'qualityProfile': QUALITY_VERSION}},
            'capabilities': {'forecast': False, 'releaseRequiresEvidence': True,
                             'evaluationArchiveWriterAvailable': True,
                             'arbitraryRevisionRuntimeAvailable': False,
                             'publicationLimit': '支持共同日期评价归档；科学诊断与任意修订运行派发仍需独立验收，归档不会升级默认。'}}


def simulation_plan(db, query):
    """Conservative preview. Extension requires the full numerical prefix checks.

    A matching name, parameter set or newer input alone never authorizes reuse.
    """
    if query.get('researchSchemeId'):
        return _scheme_simulation_plan(db, query)
    if query.get('expectedReleaseId'):
        from .release_runtime import simulation_plan as released_plan
        return released_plan(db,query)
    from .operational_scheme import prepare_request
    from .release_registry import current
    release = current(db)
    task = result_task(db, release['resultId'])
    result = task.get('result') or {}
    query = dict(query)
    # URL query values are strings, while the POST request uses an integer.
    # Normalize here so preview and actual submission validate the same revision.
    if 'schemeRevision' in query:
        revision = query['schemeRevision']
        if isinstance(revision, bool) or not str(revision).isdigit():
            raise ValueError('方案修订必须为正整数')
        query['schemeRevision'] = int(revision)
        if query['schemeRevision'] < 1:
            raise ValueError('方案修订必须为正整数')
    query.setdefault('inputId', result.get('inputId'))
    query.setdefault('schemeId', (result.get('scheme') or {}).get('id'))
    request = prepare_request(db, query)
    start, end = pd.Timestamp(query['start']), pd.Timestamp(query['end'])
    period = result.get('evaluationPeriod') or {}
    same = request['inputId'] == result.get('inputId') and request['schemeSnapshot'] == result.get('scheme')
    coverage_start = pd.Timestamp(period.get('start') or result['start'])
    coverage_end = pd.Timestamp(period.get('end') or result['end'])
    reasons = []
    if same and start >= coverage_start and end <= coverage_end:
        action = 'reuse'
        reasons.append('所选日期已包含在同一冻结方案、输入与初始化的发布成果中，可直接查看。')
    else:
        action = 'rerun'
        if not same:
            reasons.append('输入或方案修订与已发布成果不同，禁止直接复用。')
        if end > coverage_end:
            reasons.append('所选结束日期超过已发布成果覆盖。')
        reasons.append('尚未完成历史输入前缀和完整状态兼容核验，按统一预热起点重算。')
    return {'action': action, 'resultId': release['resultId'] if action == 'reuse' else None,
            'releaseId': release['id'], 'reasons': reasons,
            'requestedCoverage': {'start': start.isoformat(), 'endExclusive': end.isoformat()},
            'calculationStart': request['start'], 'inputId': request['inputId'],
            'schemeRevision': request['schemeRevision'], 'createsTask': action != 'reuse'}


def _scheme_simulation_plan(db, query):
    """Reuse only a complete result with the same actual numerical identity."""
    from .research_schemes import builtin, prepare_request, resolved_initialization, plan_token
    from .grid_simulation import model_identity, observations
    request = prepare_request(db, dict(query), kind='simulation')
    observed = observations(db)
    observed['sourceDigests'] = db.rows("SELECT DISTINCT s.id,s.sha256 FROM sources s JOIN observations o ON o.source_id=s.id WHERE s.rowid IN (SELECT MAX(rowid) FROM sources GROUP BY path) AND ((o.station_id='SLC' AND o.variable='level') OR (o.station_id='DUJA' AND o.variable='flow')) ORDER BY s.id")
    initialization, first_lake_day = resolved_initialization(request, observed, 'simulation')
    signature = plan_token(request, initialization, observed)
    expected = {'modelImplementation': model_identity(), 'spatialVersion': request['spatialVersion'],
                'parameterSHA256': digest(request['parameters']),
                'partitionParameterSHA256': digest(request['partitionParameters']),
                'initializationSHA256': digest(initialization), 'observationSnapshotSHA256': digest(observed),
                'routingMode': request['routingMode']}
    rows = db.rows("SELECT id FROM tasks WHERE status='succeeded' AND trashed=0 AND kind IN ('simulation','trial','calibration') ORDER BY created_at DESC")
    standard = builtin(db)
    builtin_result = (standard or {}).get('payload', {}).get('resultId')
    identifiers = ([builtin_result] if builtin_result else []) + [r['id'] for r in rows]
    for identifier in dict.fromkeys(identifiers):
        try:
            task = result_task(db, identifier)
            result = task.get('result') or {}
            identity = result.get('identity') or {}
            if (task.get('status') != 'succeeded' or task.get('trashed') or
                    result.get('inputId') != request['inputId'] or
                    any(identity.get(key) != value for key, value in expected.items()) or
                    pd.Timestamp(result['start']) != pd.Timestamp(request['start']) or
                    pd.Timestamp(result['end']) < pd.Timestamp(request['end'])):
                continue
            # Opening the source also checks that the hourly result exists.
            from .result_query import metadata
            metadata(db, identifier)
            return {'action': 'reuse', 'resultId': identifier, 'reasons': ['已保存成果覆盖所选时段且计算条件一致。'],
                    'requestedCoverage': {'start': request['evaluationStart'], 'endExclusive': request['end']},
                    'calculationStart': request['start'], 'inputId': request['inputId'], 'createsTask': False,
                    'earliestCompleteLakeDay': first_lake_day, 'initialization': initialization, 'planToken': signature}
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return {'action': 'rerun', 'resultId': None, 'reasons': ['将从方案预热起点按所选资料和参数连续计算。'],
            'requestedCoverage': {'start': request['evaluationStart'], 'endExclusive': request['end']},
            'calculationStart': request['start'], 'inputId': request['inputId'], 'createsTask': True,
            'earliestCompleteLakeDay': first_lake_day, 'initialization': initialization, 'planToken': signature}
