"""Previewed relocation of exclusive result folders to the verified Windows bin.

Business records remain in SQLite so restoring the original files from the
Windows bin can restore their scheme/result links as well. No unlink/rmdir.
"""
import json

from .common import canonical, digest, now
from . import permanent_deletion as planner

TABLE = 'windows_recycle_operations'


def _operations(db):
    with db.connect() as conn:
        if not planner._exists_table(conn, TABLE):
            return []
        return [{**dict(row), 'plan': json.loads(row['plan']), 'receipts': json.loads(row['receipts'])}
                for row in conn.execute('SELECT * FROM windows_recycle_operations')]


def _restored(db, plan):
    expected = {row['path']: row for row in plan['files']}
    actual = {}
    try:
        for identifier in plan['taskIds']:
            if 'tasks/' + identifier in plan['directories'] and not planner._task_path(db.workspace, identifier).is_dir():
                return False
        for identifier in plan['taskIds']:
            files, _ = planner._enumerate_task(db.workspace, identifier)
            actual.update({row['path']: row for row in files})
        return set(actual) == set(expected) and all(
            all(actual[path][key] == row[key] for key in ('bytes', 'mtimeNs'))
            for path, row in expected.items())
    except (OSError, ValueError):
        return False


def guard_available(db, kind, identifier):
    for row in _operations(db):
        if planner._key(kind, identifier) not in planner._plan_targets(row['plan']):
            continue
        if row['status'] == 'pending':
            raise ValueError('文件移入Windows回收站尚未完成，请在回收站继续处理')
        if row['status'] == 'recycled' and not _restored(db, row['plan']):
            raise ValueError('请先从Windows回收站将该批文件还原到原位置，再恢复方案与结果')


def guard_no_pending(db):
    if any(row['status'] == 'pending' for row in _operations(db)):
        raise ValueError('有未完成的Windows回收站操作，请先在平台回收站继续处理')


def states(db):
    values = {}
    for row in _operations(db):
        if row['status'] not in ('pending', 'recycled'):
            continue
        if row['status'] == 'recycled' and _restored(db, row['plan']):
            continue
        for key in planner._plan_targets(row['plan']):
            values[key] = row['status']
    return values


def _expand(db, objects):
    requested = planner._requests(objects)
    selected = {(row['kind'], row['id']): row for row in requested}
    # Selecting an old scheme includes the results from all of its saves.
    from .scheme_management import _inventory, _family_entry
    families, tasks, protected = _inventory(db)
    for kind, identifier in list(selected):
        if kind != 'scheme':
            continue
        group = next(((family, rows) for family, rows in families.items()
                      if any(row['id'] == identifier for row in rows)), None)
        if group:
            item = _family_entry(db, group[0], group[1], tasks, protected)
            for task in item['results']:
                selected['task', task['id']] = {'kind': 'task', 'id': task['id']}
    task_ids = {identifier for kind, identifier in selected if kind == 'task'}
    # Derived parameter records travel with their owning result when they are
    # exclusively used by the selected schemes. The preview lists them too.
    selected_schemes = {identifier for kind, identifier in selected if kind == 'scheme'}
    selected_families = {family for family, rows in families.items()
                         if any(row['id'] in selected_schemes for row in rows)}
    automatic = []
    for row in db.items('parameter_versions'):
        if row['payload'].get('sourceTaskId') not in task_ids:
            continue
        used_elsewhere = any(family not in selected_families and any(
            scheme['payload'].get('adoptedParameterId') == row['id'] for scheme in rows)
            for family, rows in families.items())
        if not used_elsewhere:
            selected['parameter', row['id']] = {'kind': 'parameter', 'id': row['id']}
            automatic.append(row['id'])
    return sorted(selected.values(), key=lambda row: (row['kind'], row['id'])), sorted(automatic)


def _public(plan, status='preview', receipts=(), error=''):
    result = planner._public(plan, resume=status == 'pending', status=status, error=error)
    return {**result, 'objects': plan['selection'], 'irreversible': False, 'destination': 'windows-recycle-bin',
            'automaticParameterCount': len(plan['automaticParameters']),
            'recycledFolderCount': len(receipts), 'recordsRetained': True,
            'recycledObjects': [{'kind': item['kind'], 'id': item['id'], 'name': item['name']}
                                for item in plan['items'] if item['deletable']]}


def preview(db, objects):
    selection = planner._requests(objects)
    requested, automatic = _expand(db, selection)
    wanted = {planner._key(row['kind'], row['id']) for row in requested}
    for row in _operations(db):
        if wanted & planner._plan_targets(row['plan']):
            if row['status'] == 'pending':
                if row['plan']['selection'] != selection:
                    raise ValueError('这些对象有未完成的操作，请按原选择继续处理')
                return _public(row['plan'], 'pending', row['receipts'], row['error'])
            if row['status'] == 'recycled' and not _restored(db, row['plan']):
                raise ValueError('所选文件已送入Windows回收站')
    with db.connect() as conn:
        plan = planner._build_plan(db, conn, requested, recyclable_parameters=automatic)
    plan.update(selection=selection, automaticParameters=automatic, destination='windows-recycle-bin')
    plan['token'] = digest({key: value for key, value in plan.items() if key != 'token'})
    return _public(plan)


def execute(db, objects, token):
    selection = planner._requests(objects)
    prior = next((row for row in _operations(db) if row['token'] == token), None)
    if prior:
        plan, receipts = prior['plan'], prior['receipts']
        if selection != plan['selection']:
            raise ValueError('确认对象与预览不一致')
        if prior['status'] == 'recycled':
            return _public(plan, 'recycled', receipts)
    else:
        public = preview(db, selection)
        if public['token'] != token:
            raise ValueError('方案、文件或依赖已变化，请重新预览')
        requested, automatic = _expand(db, selection)
        with db.connect() as conn:
            plan = planner._build_plan(db, conn, requested, recyclable_parameters=automatic)
        plan.update(selection=selection, automaticParameters=automatic, destination='windows-recycle-bin', token=token)
        if not plan['eligible']:
            raise ValueError('请先处理预览中仍在使用的对象')
        if any(planner._plan_targets(plan) & planner._plan_targets(row['plan']) for row in _operations(db)
               if row['status'] == 'pending'):
            raise ValueError('这些对象有未完成的回收操作，请重新预览')
        receipts = []
        with db.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS windows_recycle_operations(
                token TEXT PRIMARY KEY,created_at TEXT NOT NULL,status TEXT NOT NULL,
                plan TEXT NOT NULL,receipts TEXT NOT NULL,error TEXT NOT NULL DEFAULT '')''')
            conn.execute('INSERT INTO windows_recycle_operations VALUES(?,?,?,?,?,?)',
                         (token, now(), 'pending', canonical(plan), '[]', ''))
    from .result_query import clear_source_cache
    from .maintenance import recycle_native
    clear_source_cache()
    try:
        with db.connect() as conn:
            fresh = planner._build_plan(db, conn, plan['requested'], include_files=False,
                                        recyclable_parameters=plan['automaticParameters'])
        if fresh['contextHash'] != plan['contextHash']:
            raise ValueError('对象的使用情况已变化，请处理后继续')
        planner._file_progress(db.workspace, plan)
        completed = {row['taskId'] for row in receipts}
        for identifier in plan['taskIds']:
            if identifier in completed:
                continue
            folder = planner._task_path(db.workspace, identifier)
            if not folder.exists():
                if 'tasks/' + identifier in plan['directories']:
                    raise ValueError('未确认回收记录的成果目录缺失，请核对Windows回收站')
                continue
            receipt = {'taskId': identifier, **recycle_native(folder)}
            receipts.append(receipt)
            with db.connect() as conn:
                conn.execute('UPDATE windows_recycle_operations SET receipts=? WHERE token=?',
                             (canonical(receipts), token))
        from pathlib import Path
        if any(not Path(row['metadata']).is_file() or not Path(row['recoverable']).exists() for row in receipts):
            raise ValueError('Windows回收站条目核验未通过，平台记录保留')
        with db.connect() as conn:
            for item in plan['items']:
                if item['deletable'] and item['kind'] == 'parameter':
                    conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                                 ('parameter-hidden:' + item['id'], 'true'))
            conn.execute("UPDATE windows_recycle_operations SET status='recycled',error='' WHERE token=?", (token,))
            conn.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                         (now(), 'objects.windows-recycle', canonical({'token': token, 'receipts': receipts})))
        return _public(plan, 'recycled', receipts)
    except Exception as exc:
        with db.connect() as conn:
            conn.execute('UPDATE windows_recycle_operations SET error=? WHERE token=?', (str(exc)[:1000], token))
        return _public(plan, 'pending', receipts, str(exc))
