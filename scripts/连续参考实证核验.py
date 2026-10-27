"""C1 localhost queue acceptance using frozen real resources; all outputs retained."""
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import canonical, digest, read_json, sha_file, write_json
from backend.server import Platform
from backend.release_registry import current


def independent_vector_check(record):
    """Cross-check with direct pandas daily means, without the producer helper."""
    import numpy as np
    import pandas as pd
    parent_path = ROOT / 'data/final-results/historical-rev2-39192cc713c5/hourly.npz'
    spatial = read_json(ROOT / record['sourceLineage']['parentSpatial']['path'])
    area = spatial['lakeAreaKm2'] * 1e6
    observation = read_json(ROOT / 'config/scientific/冻结共同质量观测-20261027.json')['observations']
    anchor = pd.Timestamp('2023-04-20T12:00:00')
    with np.load(parent_path, allow_pickle=False) as arrays:
        times = pd.to_datetime(arrays['time'])
        volume = arrays['continuousLakeVolumeChange']
        level = 4544.03 + (volume - volume[times.get_loc(anchor)]) / area
        level[times < anchor] = np.nan
        hourly = pd.DataFrame({'lake': level, 'flow': arrays['flow'][:, 0]}, index=times)
        maximum_cumulative_difference = float(np.max(np.abs(volume - np.cumsum(arrays['lakeFlux']))))
    daily = hourly.resample('D', closed='right', label='left').mean()
    counts = hourly.resample('D', closed='right', label='left').count()
    daily = daily.where(counts == 24)
    daily.loc[anchor.normalize(), 'lake'] = np.nan
    maximum_level_difference = 0.
    for row in record['daily']:
        expected = daily.loc[pd.Timestamp(row['date']), 'lake']
        if pd.isna(expected):
            assert row['lakeLevel'] is None
        else:
            maximum_level_difference = max(maximum_level_difference, abs(row['lakeLevel'] - expected))
            assert math.isclose(row['lakeLevel'], expected, rel_tol=0, abs_tol=1e-9)
    metrics = {}
    for scope, key in [('lake', 'level'), ('flow', 'flow')]:
        observed = pd.Series(observation[key], dtype=float)
        observed.index = pd.to_datetime(observed.index)
        pairs = pd.concat([observed.rename('observed'), daily[scope].rename('simulated')], axis=1).dropna()
        for year in record['protocol']['years']:
            selected = pairs[pairs.index.year == int(year)]
            difference = selected.simulated - selected.observed
            score = {'count': len(selected), 'rmse': float(np.sqrt(np.mean(difference ** 2))),
                     'mae': float(np.mean(np.abs(difference))), 'bias': float(np.mean(difference))}
            actual = record['years'][year][scope]
            assert actual['count'] == score['count']
            for name in ('rmse', 'mae', 'bias'):
                assert math.isclose(actual[name], score[name], rel_tol=0, abs_tol=1e-10)
            metrics.setdefault(year, {})[scope] = score
    old_root = ROOT / 'build/grid-revision-20261026'
    old_report_path = old_root / 'comparison-evidence.json'
    old_report = read_json(old_report_path)
    old_evidence = read_json(old_root / 'baseline-evidence.json')
    with sqlite3.connect((old_root / 'workspace/database/hydrology.sqlite3').as_uri() + '?mode=ro', uri=True) as connection:
        old_request, old_result = map(json.loads, connection.execute('SELECT request,result FROM tasks WHERE id=?',
                                                                    (old_evidence['taskId'],)).fetchone())
    assert digest(old_request['_observations']) == record['observationSnapshotSHA256']
    old_area = old_result['diagnostics']['lakeAreaKm2'] * 1e6
    old_level = 4544.03 + (volume - volume[times.get_loc(anchor)]) / old_area
    old_level[times < anchor] = np.nan
    old_hourly = pd.Series(old_level, index=times)
    old_daily = old_hourly.resample('D', closed='right', label='left').mean().where(
        old_hourly.resample('D', closed='right', label='left').count() == 24)
    historical_csv = old_root / 'same-input-comparison.csv'
    historical = pd.read_csv(historical_csv, index_col=0, parse_dates=True)
    explained = {}
    for year in record['protocol']['years']:
        selected = historical.loc[year].dropna(subset=['lakeLevel', 'oldContinuousLevel', 'observedLevel'])
        pairs = [row for row in record['pairs']['lake'] if row['date'].startswith(year)]
        assert list(selected.index.strftime('%Y-%m-%d')) == [row['date'] for row in pairs]
        np.testing.assert_allclose(selected.observedLevel, [row['observed'] for row in pairs], rtol=0, atol=1e-10)
        np.testing.assert_allclose(selected.oldContinuousLevel, old_daily.reindex(selected.index), rtol=0, atol=1e-9)
        difference = old_daily.reindex(selected.index) - selected.observedLevel
        reproduced = float(np.sqrt(np.mean(difference ** 2)))
        frozen_score = old_report['scores'][year]['oldContinuousSameAnchorQuality']['rmse']
        assert math.isclose(reproduced, frozen_score, rel_tol=0, abs_tol=1e-10)
        explained[year] = {'sameDates': True, 'sameObservations': True, 'count': len(pairs),
                           'historicalE1RmseM': frozen_score, 'reproducedWithHistoricalAreaRmseM': reproduced,
                           'parentAreaReferenceRmseM': metrics[year]['lake']['rmse'],
                           'differenceFromE1RmseM': metrics[year]['lake']['rmse'] - frozen_score}
    return {'method': 'independent-direct-NPZ-pandas-right-closed-full-day-means',
            'parentLakeAreaM2': area, 'maximumDailyLevelDifferenceM': maximum_level_difference,
            'maximumCumulativeDifferenceM3': maximum_cumulative_difference, 'years': metrics,
            'historicalE1Comparison': {'reportSHA256': sha_file(old_report_path), 'csvSHA256': sha_file(historical_csv),
                                     'historicalCandidateLakeAreaM2': old_area,
                                     'parentMinusHistoricalAreaM2': area - old_area,
                                     'sameFrozenQualitySnapshotSHA256': digest(old_request['_observations']),
                                     'explanation': 'C1 uses the protected parent area; E1 used the grid candidate area. Dates and frozen quality observations agree.',
                                     'years': explained}}


def main():
    batch = ROOT / 'build' / ('连续参考实证-' + uuid.uuid4().hex[:12])
    batch.mkdir(parents=True, exist_ok=False)
    source = ROOT / 'build/grid-revision-20261026/workspace'
    parent = read_json(ROOT / 'data/final-results/historical-rev2-39192cc713c5/result.json')['result']
    identifier = parent['inputId']
    with sqlite3.connect((source / 'database/hydrology.sqlite3').as_uri() + '?mode=ro', uri=True) as connection:
        source_manifest = json.loads(connection.execute('SELECT manifest FROM input_products WHERE id=?', (identifier,)).fetchone()[0])
    source_folder = source / 'inputs/products' / identifier
    assert read_json(source_folder / 'manifest.json') == source_manifest
    destination = batch / 'workspace/inputs/products' / identifier
    copies = []
    for name in ('manifest.json', source_manifest['data']):
        path = source_folder / name
        copies.append({'source': str(path), 'target': str(destination / name),
                       'bytes': path.stat().st_size, 'sha256': sha_file(path)})
    assert copies[1]['sha256'] == source_manifest['dataSHA256'] == parent['identity']['inputSHA256']
    write_json(batch / '复制计划与摘要.json', {'projectId': 'selinco', 'files': copies})
    destination.mkdir(parents=True, exist_ok=False)
    for item in copies:
        target = Path(item['target'])
        with Path(item['source']).open('rb') as source_stream, target.open('xb') as target_stream:
            shutil.copyfileobj(source_stream, target_stream, 1024 * 1024)
        assert sha_file(target) == item['sha256'] == sha_file(item['source'])
    write_json(batch / '复制校验.json', {'passed': True, 'files': copies})
    server = Platform(('127.0.0.1', 0), batch / 'workspace')
    with server.db.connect() as connection:
        connection.execute('INSERT INTO input_products VALUES(?,?,?,?)',
                           (identifier, source_manifest['name'], source_manifest['createdAt'], canonical(source_manifest)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = 'http://127.0.0.1:' + str(server.server_port)

    def api(path, payload=None):
        request = Request(origin + '/api/model/' + path,
                          canonical(payload).encode('utf-8') if payload is not None else None,
                          {'Content-Type': 'application/json'} if payload is not None else {})
        with urlopen(request, timeout=60) as response:
            return json.load(response)

    def wait(identifier):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            task = api('tasks/' + identifier)
            if task['status'] not in ('queued', 'running', 'stopping'):
                return task
            time.sleep(.1)
        raise TimeoutError('连续参考后台任务超过180秒')

    try:
        release = current(server.db)
        catalog_sha = sha_file(ROOT / 'data/final-results/catalog.json')
        config = api('continuous-reference-config')
        payload = {'kind': 'continuous-reference', 'name': 'C1真实冻结资源核验',
                   'baselineReleaseId': release['id'], 'protocolId': config['protocolId'],
                   'anchorConfigId': config['anchor']['id'], 'clientRequestId': 'c1-acceptance'}
        rejected = []
        for key, value in [('checks', {'passed': True}), ('initialLakeLevel', 4544.1), ('path', 'arbitrary.csv')]:
            try:
                api('tasks', {**payload, 'clientRequestId': key, key: value})
            except HTTPError as error:
                assert error.code == 400
                rejected.append(key)
            else:
                raise AssertionError('客户端自由字段未拒绝：' + key)
        started = time.monotonic()
        submitted = api('tasks', payload)
        finished = wait(submitted['id'])
        write_json(batch / '后台任务.json', finished)
        assert finished['status'] == 'succeeded', finished['message']
        result = finished['result']
        record = api('continuous-reference?' + urlencode({'id': result['referenceId']}))
        write_json(batch / '连续参考核验记录.json', record)
        assert record['archiveComplete']
        independent = independent_vector_check(record)
        write_json(batch / '独立逐时向量核对.json', independent)
        downloaded = []
        for artifact in result['artifacts']:
            with urlopen(origin + artifact['downloadUrl'], timeout=60) as response:
                raw = response.read()
            assert hashlib.sha256(raw).hexdigest() == artifact['sha256']
            assert len(raw) == artifact['bytes']
            downloaded.append({'name': artifact['name'], 'bytes': len(raw), 'sha256': artifact['sha256']})
        assert api('tasks', payload)['id'] == submitted['id']
        reused = wait(api('tasks', {**payload, 'clientRequestId': 'c1-reuse'})['id'])
        assert reused['status'] == 'succeeded', reused['message']
        assert reused['result']['referenceId'] == result['referenceId'] and reused['result']['reused']
        compact = next(row for row in api('tasks')['tasks'] if row['id'] == submitted['id'])
        assert compact['result']['referenceId'] == result['referenceId']
        assert len(api('continuous-references')['references']) == 1
        assert current(server.db) == release
        assert sha_file(ROOT / 'data/final-results/catalog.json') == catalog_sha
        assert read_json(ROOT / 'data/final-results/historical-rev2-39192cc713c5/result.json')['result']['metrics'] == parent['metrics']
        receipt = {'passed': True, 'projectId': 'selinco', 'sourceVersion': '0.10.0+C1-development',
                   'origin': origin, 'workspace': str(server.db.workspace), 'taskId': submitted['id'],
                   'referenceId': result['referenceId'], 'seconds': time.monotonic() - started,
                   'independentVectorCheck': independent,
                   'rejectedClientFields': rejected, 'downloaded': downloaded,
                   'idempotent': True, 'defaultUnchanged': True, 'protectedWindowMetricsUnchanged': True,
                   'scientificUpgradePassed': False, 'evaluationReaderIntegrated': False,
                   'newInstallerBuilt': False}
        write_json(batch / 'HTTP验收.json', receipt)
        print(canonical({'batch': str(batch), **receipt}), flush=True)
    finally:
        server.shutdown()
        server.jobs.executor.shutdown(wait=True)
        server.server_close()
        thread.join(3)


if __name__ == '__main__':
    main()
