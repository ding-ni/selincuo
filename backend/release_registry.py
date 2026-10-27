"""Append-only default release registry. Scientific evidence is server-owned.

The protected legacy release is retained with its known limitations. It is not
retroactively certified against the new admission protocol.
"""
import json
import math
import sqlite3
from .common import canonical, digest, now

SCHEMA = '''
CREATE TABLE IF NOT EXISTS scheme_revisions(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluation_protocols(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluation_records(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operational_releases(sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,previous_id TEXT,scheme_revision_id TEXT NOT NULL REFERENCES scheme_revisions(id),result_id TEXT NOT NULL,evaluation_id TEXT,published_at TEXT NOT NULL,action TEXT NOT NULL);
'''
PROTOCOL = {'id': 'selinco-admission-v1', 'years': ['2024', '2025'], 'lakeRmseImprovement': 0.10,
            'flowNseImprovement': 0.03, 'tolerance': 1e-10, 'quality': 'common-frozen-days',
            'lakeMode': 'continuous', 'lakeStabilityMaximumM': 0.01,
            'flowStabilityRmseFraction': 0.01}


def migrate(db):
    # SQLite backup API also includes committed WAL pages. Never copy a live DB file.
    backup = db.workspace / 'database/pre-release-registry-v2.sqlite3'
    if not db.rows('SELECT version FROM schema_version WHERE version=2'):
        if not backup.exists():
            with sqlite3.connect(db.path) as source, sqlite3.connect(backup) as target:
                source.backup(target)
        with db.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute('INSERT OR IGNORE INTO evaluation_protocols VALUES(?,?)', (PROTOCOL['id'], canonical(PROTOCOL)))
            connection.execute('INSERT OR IGNORE INTO schema_version VALUES(2,?)', (now(),))
    if db.rows('SELECT id FROM operational_releases LIMIT 1'):
        return
    from .published_results import current_id, detail
    task = detail(current_id())
    result = task['result']
    snapshot = {'scheme': result['scheme'], 'identity': result['identity'],
                'parameters': result['parameters'], 'partitionParameters': result.get('partitionParameters', {}),
                'initialization': result['scheme']['initialization'],
                'status': 'protected-legacy', 'admissionPassed': False}
    revision_id = 'REV-' + digest(snapshot)[:24]
    with db.connect() as connection:
        connection.execute('INSERT OR IGNORE INTO scheme_revisions VALUES(?,?)', (revision_id, canonical(snapshot)))
        # Concurrent initializers must not create two initial releases.
        connection.execute("INSERT INTO operational_releases(id,previous_id,scheme_revision_id,result_id,evaluation_id,published_at,action) SELECT ?,NULL,?,?,NULL,?,'protected-import' WHERE NOT EXISTS(SELECT 1 FROM operational_releases)",
                           ('RELEASE-protected-rev2', revision_id, task['id'], now()))


def current(db):
    # Read-only calls also work on older workspaces, without migrating on GET.
    tables = db.rows("SELECT name FROM sqlite_master WHERE type='table' AND name='operational_releases'")
    rows = db.rows('SELECT * FROM operational_releases ORDER BY sequence DESC LIMIT 1') if tables else []
    if not rows:
        from .published_results import current_id
        return {'id': 'RELEASE-protected-rev2', 'sequence': 0, 'resultId': current_id(),
                'schemeRevisionId': None, 'status': 'protected-legacy', 'admissionPassed': False}
    row = rows[0]
    revisions = db.rows('SELECT payload FROM scheme_revisions WHERE id=?', (row['scheme_revision_id'],))
    protected = bool(revisions and json.loads(revisions[0]['payload']).get('status') == 'protected-legacy')
    return {'id': row['id'], 'sequence': row['sequence'], 'resultId': row['result_id'],
            'schemeRevisionId': row['scheme_revision_id'], 'evaluationId': row['evaluation_id'],
            'previousReleaseId': row['previous_id'], 'publishedAt': row['published_at'],
            'status': 'protected-legacy' if protected else 'published',
            'admissionPassed': bool(row['evaluation_id'])}


def admission(evidence):
    """Fail closed on absent years, samples, protocol or diagnostics."""
    failures, improved = [], False
    if evidence.get('protocolId') != PROTOCOL['id'] or evidence.get('lakeMode') != 'continuous':
        failures.append('必须采用冻结的共同日期连续评价协议')
    for year in PROTOCOL['years']:
        row = evidence.get('years', {}).get(year, {})
        for scope, key, direction in [('lake', 'rmse', -1), ('lake', 'bias', -1), ('flow', 'nse', 1), ('flow', 'pbias', -1)]:
            pair = row.get(scope, {})
            baseline, candidate = pair.get('baseline', {}).get(key), pair.get('candidate', {}).get(key)
            count = pair.get('commonCount', 0)
            if (not isinstance(count, int) or isinstance(count, bool) or not 2 <= count <= 366 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (baseline, candidate))):
                failures.append(f'{year} {scope}.{key} 缺少合格共同样本或数值')
                continue
            if key == 'rmse' and (baseline < 0 or candidate < 0) or key == 'nse' and max(baseline, candidate) > 1 + PROTOCOL['tolerance']:
                failures.append(f'{year} {scope}.{key} 超出指标定义范围')
                continue
            b, c = (abs(baseline), abs(candidate)) if key in ('bias', 'pbias') else (baseline, candidate)
            if (c-b)*direction < -PROTOCOL['tolerance']:
                failures.append(f'{year} {scope}.{key} 退步')
            if scope == 'lake' and key == 'rmse' and b > 0 and c <= b * .9 + 1e-10:
                improved = True
            if scope == 'flow' and key == 'nse' and c-b >= .03 - 1e-10:
                improved = True
    if not improved:
        failures.append('未达到湖位RMSE下降10%或杜加NSE提高0.03的实质改善')
    for key in ('initialStateStable', 'waterBalanceValid', 'parameterBoundsValid', 'inputProvenanceValid',
                'fullArtifactsVerified', 'strictContinuationVerified', 'limitationsArchived', 'commonDatesVerified'):
        if evidence.get('checks', {}).get(key) is not True:
            failures.append('尚未通过：' + key)
    stability = evidence.get('stability') or {}
    lake_difference = stability.get('lakeMaximumDifferenceM')
    flow_difference = stability.get('flowDifferenceRmseM3s')
    observed_mean = stability.get('observedFlowMeanM3s')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in (lake_difference, flow_difference, observed_mean)):
        failures.append('初态稳定性缺少数值证据')
    elif (lake_difference < 0 or flow_difference < 0 or observed_mean <= 0 or
          lake_difference > PROTOCOL['lakeStabilityMaximumM'] + PROTOCOL['tolerance'] or
          flow_difference > observed_mean * PROTOCOL['flowStabilityRmseFraction'] + PROTOCOL['tolerance']):
        failures.append('初态稳定性未达到项目数值门槛')
    return {'passed': not failures, 'failures': failures, 'protocolId': PROTOCOL['id']}


def runtime_supports(snapshot, result):
    """Fail closed until Jobs can dispatch arbitrary immutable revisions.

    Today its business path only executes the packaged legacy definition. A
    registry pointer must not advertise a research revision that path ignores.
    """
    from .operational_scheme import definition
    scheme = definition()
    expected = {key: scheme[key] for key in ('id', 'name', 'revision', 'configurationSHA256',
                                            'initialization', 'spatialVersion')}
    return (not result.get('modelFamily') and snapshot.get('scheme') == expected and
            result.get('scheme') == expected and snapshot.get('parameters') == scheme['parameters'] and
            snapshot.get('partitionParameters', {}) == scheme['partitionParameters'] and
            result.get('parameters') == scheme['parameters'] and
            result.get('partitionParameters', {}) == scheme['partitionParameters'])


def publish(db, request):
    """Only previously archived backend evaluations may authorize a new release.

    Client-provided scores/check flags are deliberately never consulted.
    """
    expected = request.get('expectedReleaseId')
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        previous = connection.execute('SELECT * FROM operational_releases ORDER BY sequence DESC LIMIT 1').fetchone()
        if not previous or expected != previous['id']:
            raise ValueError('默认发布已变化，请刷新后重试')
        action = request.get('action', 'publish')
        if action == 'rollback':
            target = connection.execute('SELECT * FROM operational_releases WHERE id=?', (previous['previous_id'],)).fetchone()
            if not target:
                raise ValueError('没有可回退的上一发布记录')
            revision, result_id, evaluation_id = target['scheme_revision_id'], target['result_id'], target['evaluation_id']
        elif action == 'publish':
            row = connection.execute('SELECT payload FROM evaluation_records WHERE id=?', (request.get('evaluationId'),)).fetchone()
            if not row:
                raise ValueError('没有经过后端归档的评价证据，禁止发布')
            evidence = json.loads(row['payload'])
            verdict = admission(evidence)
            if evidence.get('baselineReleaseId') != previous['id'] or not verdict['passed']:
                raise ValueError('发布准入未通过：' + '；'.join(verdict['failures'] or ['比较基准已变化']))
            revision, result_id, evaluation_id = evidence['schemeRevisionId'], evidence['resultId'], request['evaluationId']
            revision_row = connection.execute('SELECT payload FROM scheme_revisions WHERE id=?', (revision,)).fetchone()
            if not revision_row:
                raise ValueError('不可变方案修订缺失')
            from .business_context import result_task
            task = result_task(db, result_id)
            result = task.get('result') or {}
            snapshot = json.loads(revision_row['payload'])
            if task.get('status') != 'succeeded' or not result or task.get('trashed'):
                raise ValueError('发布成果缺失、未完成或已移入回收站')
            if result.get('modelFamily') != 'grid-continuous-v1':
                raise ValueError('实际成果尚未核验为固定锚点连续计算，禁止套用连续评价准入')
            if (evidence.get('resultIdentitySHA256') != digest(result.get('identity') or {}) or
                    snapshot.get('scheme') != result.get('scheme') or
                    snapshot.get('parameters') != result.get('parameters') or
                    snapshot.get('partitionParameters', {}) != result.get('partitionParameters', {})):
                raise ValueError('评价证据、不可变方案与实际成果身份不一致')
            if not runtime_supports(snapshot, result):
                raise ValueError('该修订的业务计算派发尚未接通，禁止更新默认指针；请保留研究候选身份')
        else:
            raise ValueError('未知发布操作')
        at = now()
        identifier = 'RELEASE-' + digest([previous['id'], revision, result_id, at])[:24]
        connection.execute('INSERT INTO operational_releases(id,previous_id,scheme_revision_id,result_id,evaluation_id,published_at,action) VALUES(?,?,?,?,?,?,?)',
                           (identifier, previous['id'], revision, result_id, evaluation_id, at, action))
        connection.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)', (at, 'operational.'+action, canonical({'id': identifier, 'previousId': previous['id'], 'resultId': result_id})))
    return current(db)
