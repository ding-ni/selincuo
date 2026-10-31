"""Recoverable scheme-family and result visibility; no files are moved or erased."""
import json
import uuid

from .common import canonical, digest, now
from .business_selection import current, purpose
from .research_schemes import builtin, _family
from .permanent_deletion import ACTIVE, RESULT_KINDS, guard_pending

PREFIX = 'scheme-recycle-batch:'


def _inventory(db):
    families = {}
    rows = [row for row in db.items('schemes') if row['payload'].get('schemaVersion') == 2]
    standard = builtin(db)
    if standard:
        rows.append(standard)
    for row in rows:
        families.setdefault(_family(row), []).append(row)
    tasks = db.rows('''SELECT id,kind,status,trashed,name,updated_at,
        json_extract(request,'$.researchSchemeId') AS schemeId,
        json_extract(request,'$.schemeSnapshot.id') AS snapshotSchemeId FROM tasks''')
    protected = {current(db).get('resultId')}
    if standard:
        protected.add(standard['payload'].get('resultId'))
    if db.rows("SELECT name FROM sqlite_master WHERE type='table' AND name='operational_releases'"):
        protected.update(row['result_id'] for row in db.rows('SELECT result_id FROM operational_releases'))
    return families, tasks, protected - {None}


def _family_entry(db, family, revisions, tasks, protected):
    latest = max(revisions, key=lambda row: (row['payload'].get('revision', 1), row.get('created_at', '')))
    ids = {row['id'] for row in revisions}
    explicit_results = {row['payload'].get('resultId') for row in revisions} - {None}
    related = [row for row in tasks if row['schemeId'] in ids or row['snapshotSchemeId'] in ids
               or row['id'] in explicit_results]
    results = [row for row in related if row['kind'] in RESULT_KINDS or row['id'] in explicit_results]
    reasons = []
    chosen = current(db)
    if family == chosen.get('familyId') or chosen.get('schemeId') in ids:
        reasons.append('当前正在采用')
    if any(row['status'] in ACTIVE for row in related):
        reasons.append('有关联任务正在计算')
    if any(row['id'] in protected for row in results):
        reasons.append('包含当前采用或受保护发布成果')
    return {**latest, 'familyId': family, 'revisionCount': len(revisions),
            'revisionIds': sorted(ids), 'purpose': purpose(db, latest),
            'current': family == chosen.get('familyId') or chosen.get('schemeId') in ids,
            'trashed': bool(db.setting('scheme-hidden:' + family, False)),
            'resultCount': len(results), 'results': results, 'recycleBlocked': reasons}


def catalog(db):
    families, tasks, protected = _inventory(db)
    rows = [_family_entry(db, family, revisions, tasks, protected)
            for family, revisions in families.items()]
    batches = [json.loads(row['value']) for row in db.rows(
        'SELECT value FROM settings WHERE key LIKE ? ORDER BY key DESC', (PREFIX + '%',))]
    return {'schemes': sorted(rows, key=lambda row: (not row['current'], row['name'], row['id'])),
            'recycleBatches': [batch for batch in batches if batch['status'] == 'trashed']}


def preview(db, identifiers):
    if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 100
            or any(not isinstance(value, str) or not value for value in identifiers)):
        raise ValueError('请勾选1—100个方案')
    families, tasks, protected = _inventory(db)
    by_id = {row['id']: family for family, rows in families.items() for row in rows}
    if any(identifier not in by_id for identifier in identifiers):
        raise ValueError('所选方案已变化，请刷新列表')
    selected = sorted({by_id[identifier] for identifier in identifiers})
    items, result_rows = [], {}
    for family in selected:
        item = _family_entry(db, family, families[family], tasks, protected)
        for identifier in item['revisionIds']:
            guard_pending(db, 'scheme', identifier)
        if item['trashed']:
            item['recycleBlocked'].append('方案已经在回收站')
        for task in item['results']:
            guard_pending(db, 'task', task['id'])
            result_rows[task['id']] = task
        items.append({key: item[key] for key in ('id', 'name', 'familyId', 'revisionIds',
                      'revisionCount', 'trashed', 'recycleBlocked')})
    results = sorted(result_rows.values(), key=lambda row: row['id'])
    plan = {'schemeIds': sorted(set(identifiers)), 'items': items, 'results': results,
            'schemeCount': len(items), 'resultCount': sum(not row['trashed'] for row in results),
            'alreadyTrashedResultCount': sum(bool(row['trashed']) for row in results),
            'blocked': any(item['recycleBlocked'] for item in items)}
    return {**plan, 'token': digest(plan)}


def recycle(db, identifiers, token):
    # The server holds Jobs.lock. BEGIN IMMEDIATE also excludes concurrent DB writers.
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        plan = preview(db, identifiers)
        if plan['token'] != token:
            raise ValueError('方案或关联成果已变化，请重新预览')
        if plan['blocked']:
            raise ValueError('所选方案包含正在采用、正在计算或受保护的对象，请重新选择')
        batch_id = uuid.uuid4().hex
        batch = {'id': batch_id, 'createdAt': now(), 'status': 'trashed',
                 'schemes': plan['items'],
                 'resultIds': [row['id'] for row in plan['results'] if not row['trashed']],
                 'token': plan['token']}
        for item in plan['items']:
            connection.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                               ('scheme-hidden:' + item['familyId'], canonical(True)))
        for identifier in batch['resultIds']:
            connection.execute('UPDATE tasks SET trashed=1 WHERE id=?', (identifier,))
        connection.execute('INSERT INTO settings VALUES(?,?)', (PREFIX + batch_id, canonical(batch)))
        connection.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                           (now(), 'schemes.recycle-with-results', canonical(batch)))
    return {'status': 'trashed', 'batchId': batch_id,
            'schemeCount': len(batch['schemes']), 'resultCount': len(batch['resultIds'])}


def restore(db, batch_id):
    if not isinstance(batch_id, str) or len(batch_id) != 32:
        raise ValueError('回收记录编号无效')
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT value FROM settings WHERE key=?', (PREFIX + batch_id,)).fetchone()
        if not row:
            raise ValueError('回收记录不存在')
        batch = json.loads(row[0])
        if batch['status'] == 'restored':
            return {'status': 'restored', 'batchId': batch_id}
        # Verify every object first so a missing or partially deleted object cannot cause partial restoration.
        for item in batch['schemes']:
            for identifier in item['revisionIds']:
                guard_pending(db, 'scheme', identifier)
                if not connection.execute('SELECT 1 FROM schemes WHERE id=?', (identifier,)).fetchone():
                    standard = builtin(db)
                    if not standard or standard['id'] != identifier:
                        raise ValueError('部分方案已被彻底删除，无法整批恢复')
        for identifier in batch['resultIds']:
            guard_pending(db, 'task', identifier)
            if not connection.execute('SELECT 1 FROM tasks WHERE id=?', (identifier,)).fetchone():
                raise ValueError('部分成果已被彻底删除，无法整批恢复')
        for item in batch['schemes']:
            connection.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                               ('scheme-hidden:' + item['familyId'], canonical(False)))
        for identifier in batch['resultIds']:
            connection.execute('UPDATE tasks SET trashed=0 WHERE id=?', (identifier,))
        batch.update(status='restored', restoredAt=now())
        connection.execute('UPDATE settings SET value=? WHERE key=?', (canonical(batch), PREFIX + batch_id))
        connection.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                           (now(), 'schemes.restore-with-results', canonical(batch)))
    return {'status': 'restored', 'batchId': batch_id,
            'schemeCount': len(batch['schemes']), 'resultCount': len(batch['resultIds'])}
