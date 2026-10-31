"""Explicit, previewed deletion of recoverable business objects.

This is separate from the Windows Recycle Bin maintenance operation.  The
caller must hold Jobs.lock while previewing/executing; execution is only for
objects a user has already placed in the platform recycle bin.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from .common import ROOT, canonical, digest, now, read_json

KINDS = {'task', 'scheme', 'parameter'}
RESULT_KINDS = {'simulation', 'continuation', 'trial', 'calibration',
                'calibration-workflow', 'two-stage-calibration', 'calibration-replay', 'sensitivity'}
ACTIVE = {'queued', 'running', 'stopping'}
JOURNAL = 'permanent_deletion_operations'
IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,119}$')


def _exists_table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS permanent_deletion_operations(
        token TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        status TEXT NOT NULL, plan TEXT NOT NULL, error TEXT NOT NULL DEFAULT '')''')


def _parse(value, default=None):
    if value is None:
        return {} if default is None else default
    return json.loads(value) if isinstance(value, str) else value


def _requests(objects):
    if not isinstance(objects, list) or not objects:
        raise ValueError('请选择回收站对象')
    rows = []
    for obj in objects:
        if not isinstance(obj, dict) or set(obj) != {'kind', 'id'}:
            raise ValueError('删除请求只接受对象类型和编号，不接受文件路径')
        kind, identifier = obj['kind'], obj['id']
        if kind not in KINDS or not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise ValueError('删除对象类型或编号无效')
        rows.append({'kind': kind, 'id': identifier})
    if len({(r['kind'], r['id']) for r in rows}) != len(rows):
        raise ValueError('删除对象不能重复')
    return sorted(rows, key=lambda r: (r['kind'], r['id']))


def _key(kind, identifier):
    return kind + ':' + identifier


def _blocked(message, action='', related=None):
    result = {'message': message, 'action': action}
    if related:
        result['related'] = related
    return result


def _is_reparse(st):
    return bool(getattr(st, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def _safe_entry(path, expected):
    """Do not follow symlinks, junctions, or non-regular filesystem entries."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise ValueError('成果目录包含链接或重解析点，不能自动彻底删除')
    if expected == 'dir' and not stat.S_ISDIR(info.st_mode):
        raise ValueError('成果目录已变化，请重新预览')
    if expected == 'file' and not stat.S_ISREG(info.st_mode):
        raise ValueError('成果中存在不支持的文件类型，不能自动彻底删除')
    return info


def _task_path(workspace, task_id):
    if not IDENTIFIER.fullmatch(task_id):
        raise ValueError('成果编号不合法')
    root = workspace / 'tasks'
    if root.exists() or root.is_symlink():
        _safe_entry(root, 'dir')
    return root / task_id


def _enumerate_task(workspace, task_id):
    """Return server-derived files only. Original inputs are outside this root."""
    folder = _task_path(workspace, task_id)
    if not folder.exists() and not folder.is_symlink():
        return [], []
    _safe_entry(folder, 'dir')
    files, directories = [], []
    stack = [folder]
    while stack:
        directory = stack.pop()
        _safe_entry(directory, 'dir')
        directories.append(directory.relative_to(workspace).as_posix())
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.name in ('', '.', '..') or '\\' in entry.name or ':' in entry.name:
                    raise ValueError('成果中包含不支持的文件名，不能自动彻底删除')
                path = directory / entry.name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                    raise ValueError('成果目录包含链接或重解析点，不能自动彻底删除')
                if stat.S_ISDIR(info.st_mode):
                    stack.append(path)
                elif stat.S_ISREG(info.st_mode):
                    files.append({'path': path.relative_to(workspace).as_posix(),
                                  'bytes': info.st_size, 'mtimeNs': info.st_mtime_ns,
                                  'device': info.st_dev, 'inode': info.st_ino,
                                  'links': info.st_nlink})
                else:
                    raise ValueError('成果中存在不支持的文件类型，不能自动彻底删除')
    return sorted(files, key=lambda row: row['path']), sorted(directories)


def _reclaim_bytes(files):
    # Count bytes only when every hard link is among the selected files.
    # Filesystems without stable inode/link metadata get a conservative zero.
    groups = {}
    for file in files:
        if not file['inode'] or not file['links']:
            continue
        groups.setdefault((file['device'], file['inode']), []).append(file)
    return sum(group[0]['bytes'] for group in groups.values()
               if len(group) == group[0]['links'] and
               all(row['links'] == group[0]['links'] and row['bytes'] == group[0]['bytes'] for row in group))


def _current_config():
    return read_json(ROOT / 'config/operational/continuous-builtin-v1.json', {}) or {}


def _snapshot(conn):
    settings = {r['key']: _parse(r['value']) for r in conn.execute(
        "SELECT key,value FROM settings WHERE key LIKE 'scheme-hidden:%' OR key LIKE 'parameter-hidden:%' OR key='business-adopted-scheme-v1'")}
    schemes = []
    for row in conn.execute('SELECT id,name,created_at,payload FROM schemes'):
        row = dict(row)
        row['payload'] = _parse(row['payload'])
        row['family'] = row['payload'].get('familyId') or row['id']
        schemes.append(row)
    parameters = []
    for row in conn.execute('SELECT id,name,payload FROM parameter_versions'):
        row = dict(row)
        row['payload'] = _parse(row['payload'])
        parameters.append(row)
    tasks = []
    for row in conn.execute('''SELECT id,kind,status,trashed,favorite,name,
        json_extract(request,'$.stateId') state_id,
        json_extract(request,'$.taskId') source_task_id,
        json_extract(request,'$.parentTaskId') parent_task_id,
        json_extract(request,'$.baseTaskId') base_task_id,
        json_extract(request,'$.lakeOnlyBaseTaskId') lake_base_task_id,
        json_extract(request,'$.twoStage.candidateSourceTaskId') candidate_source_task_id,
        json_extract(request,'$.twoStage.baselineTaskId') selection_baseline_task_id,
        json_extract(request,'$.replay.sourceTaskId') replay_source_task_id,
        json_extract(request,'$.baselineResultId') baseline_result_id,
        json_extract(request,'$.candidateResultId') candidate_result_id,
        json_extract(request,'$.researchSchemeId') scheme_id,
        json_extract(request,'$.candidateParameterId') parameter_id,
        json_extract(result,'$.sourceTaskId') result_source_task_id,
        json_extract(result,'$.artifacts') artifacts FROM tasks'''):
        row = dict(row)
        row['artifacts'] = _parse(row['artifacts'], [])
        tasks.append(row)
    states = [dict(row) for row in conn.execute('SELECT id,task_id,name,favorite FROM states')]
    releases = []
    if _exists_table(conn, 'operational_releases'):
        current = conn.execute('SELECT id,previous_id,result_id FROM operational_releases ORDER BY sequence DESC LIMIT 1').fetchone()
        if current:
            releases.append(dict(current))
            if current['previous_id']:
                previous = conn.execute('SELECT id,previous_id,result_id FROM operational_releases WHERE id=?',
                                        (current['previous_id'],)).fetchone()
                if previous:
                    releases.append(dict(previous))
    catalog = read_json(ROOT / 'data/final-results/catalog.json', {'results': []}) or {'results': []}
    return {'settings': settings, 'schemes': schemes, 'parameters': parameters,
            'tasks': tasks, 'states': states, 'releases': releases,
            'builtin': _current_config(), 'published': sorted(r['id'] for r in catalog.get('results', []))}


def _build_plan(db, conn, requested, include_files=True, recyclable_parameters=()):
    snapshot = _snapshot(conn)
    schemes = {row['id']: row for row in snapshot['schemes']}
    parameters = {row['id']: row for row in snapshot['parameters']}
    tasks = {row['id']: row for row in snapshot['tasks']}
    states = snapshot['states']
    by_family = {}
    for row in schemes.values():
        by_family.setdefault(row['family'], []).append(row)
    latest = {family: max(rows, key=lambda row: (row['payload'].get('revision', 1), row['created_at'], row['id']))
              for family, rows in by_family.items()}
    requested_keys = {_key(r['kind'], r['id']) for r in requested}
    items, family_keys = {}, {}
    for request in requested:
        kind, identifier = request['kind'], request['id']
        key = _key(kind, identifier)
        row = (tasks if kind == 'task' else schemes if kind == 'scheme' else parameters).get(identifier)
        builtin = snapshot['builtin']
        if kind == 'scheme' and identifier == builtin.get('id'):
            item = {'kind': kind, 'id': identifier, 'name': builtin.get('name') or '内置方案',
                    'blocked': [_blocked('当前内置方案受到保护', '切换采用方案后再处理')],
                    'members': [], 'family': builtin.get('familyId') or identifier}
        elif not row:
            raise ValueError('所选对象不存在或已删除，请刷新列表')
        else:
            item = {'kind': kind, 'id': identifier, 'name': row['name'] or
                    {'task': '计算成果', 'scheme': '方案', 'parameter': '参数'}[kind],
                    'blocked': [], 'members': [identifier]}
            adopted=snapshot['settings'].get('business-adopted-scheme-v1',{})
            selected_id=adopted.get({'task':'resultId','parameter':'parameterId','scheme':'schemeId'}[kind])
            if identifier==selected_id or kind=='scheme' and row['payload'].get('familyId')==adopted.get('familyId'):
                item['blocked'].append(_blocked('该对象正在采用','先采用其他方案'))
            if kind == 'task':
                item['stateIds'] = sorted(state['id'] for state in states if state['task_id'] == identifier)
                if row['kind'] not in RESULT_KINDS:
                    item['blocked'].append(_blocked('该任务不是可在成果中心删除的模型成果'))
                if not row['trashed']:
                    item['blocked'].append(_blocked('成果不在回收站', '先移入回收站'))
                if row['status'] in ACTIVE:
                    item['blocked'].append(_blocked('成果任务正在运行', '等待任务完成'))
                if row['favorite']:
                    item['blocked'].append(_blocked('成果仍被收藏', '先取消收藏'))
            elif kind == 'parameter':
                if not snapshot['settings'].get('parameter-hidden:' + identifier, False) and identifier not in recyclable_parameters:
                    item['blocked'].append(_blocked('参数不在回收站', '先移入回收站'))
            else:
                family = row['family']
                if family in family_keys:
                    raise ValueError('同一方案的多个修订不能重复选择，请刷新列表')
                family_keys[family] = key
                item['family'] = family
                item['members'] = sorted(r['id'] for r in by_family[family])
                if not snapshot['settings'].get('scheme-hidden:' + family, False):
                    item['blocked'].append(_blocked('方案不在回收站', '先移入回收站'))
                if family == (builtin.get('familyId') or builtin.get('id')):
                    item['blocked'].append(_blocked('当前内置方案受到保护', '切换采用方案后再处理'))
        items[key] = item

    # A backup reads the entire workspace, while other running jobs only need
    # their explicit inputs. An unrelated forcing download must not lock the
    # entire recycle bin for hours.
    active_tasks = [row for row in tasks.values() if row['status'] in ACTIVE]
    state_owners = {row['id']: row['task_id'] for row in states}
    for active in active_tasks:
        active_name = active['name'] or active['id']
        action = '等待任务结束后重新预览'
        if active['kind'] == 'backup':
            for item in items.values():
                item['blocked'].append(_blocked('工作区备份“' + active_name + '”正在运行', action))
            continue
        referenced_tasks = {active[field] for field in ('source_task_id', 'parent_task_id', 'base_task_id', 'lake_base_task_id',
                                                        'baseline_result_id', 'candidate_result_id',
                                                        'candidate_source_task_id', 'selection_baseline_task_id', 'replay_source_task_id') if active[field]}
        if active['state_id'] in state_owners:
            referenced_tasks.add(state_owners[active['state_id']])
        for identifier in referenced_tasks:
            key = _key('task', identifier)
            if key in items:
                items[key]['blocked'].append(_blocked('任务“' + active_name + '”正在使用该成果', action))
        parameter_key = _key('parameter', active['parameter_id']) if active['parameter_id'] else ''
        if parameter_key in items:
            items[parameter_key]['blocked'].append(_blocked('任务“' + active_name + '”正在使用该参数', action))
        for item in items.values():
            if item['kind'] == 'scheme' and active['scheme_id'] in item['members']:
                item['blocked'].append(_blocked('任务“' + active_name + '”正在使用该方案', action))

    dependents = {key: [] for key in items}
    def dependency(target_key, dependent_key, message, action, related=None):
        if target_key not in items:
            return
        if dependent_key in requested_keys:
            dependents[target_key].append((dependent_key, message, action, related))
        else:
            items[target_key]['blocked'].append(_blocked(message, action, related))

    task_keys = {row['id']: _key('task', row['id']) for row in tasks.values()
                 if _key('task', row['id']) in items}
    for identifier, key in task_keys.items():
        if identifier in snapshot['published']:
            items[key]['blocked'].append(_blocked('内置成果受到保护', '切换采用成果后再处理'))
        if identifier == snapshot['builtin'].get('resultId'):
            items[key]['blocked'].append(_blocked('当前方案采用此成果', '先切换采用成果'))
        for index, release in enumerate(snapshot['releases']):
            if identifier == release['result_id']:
                items[key]['blocked'].append(_blocked('当前采用成果受到保护' if index == 0 else '上一采用成果仍可用于回退',
                                                       '先切换采用成果'))
        for state in states:
            if state['task_id'] == identifier and (state['favorite'] or (state['name'] or '').strip()):
                items[key]['blocked'].append(_blocked('末状态“' + (state['name'] or '已收藏状态') + '”受到保护',
                                                       '先取消该状态收藏并清除名称'))
    for row in parameters.values():
        source = row['payload'].get('sourceTaskId')
        if source in task_keys:
            dependency(task_keys[source], _key('parameter', row['id']),
                       '参数“' + row['name'] + '”仍需读取该成果', '先回收该参数并选择一同删除',
                       {'kind': 'parameter', 'id': row['id'], 'name': row['name'],
                        'trashed': bool(snapshot['settings'].get('parameter-hidden:' + row['id'], False))})
    for family, row in latest.items():
        parameter = row['payload'].get('adoptedParameterId')
        key = _key('parameter', parameter) if parameter else ''
        if key in items:
            dependency(key, family_keys.get(family, ''), '方案“' + row['name'] + '”正在使用该参数',
                       '先改用其他参数，或将该方案与参数一同回收并删除',
                       {'kind': 'scheme', 'id': row['id'], 'name': row['name'],
                        'trashed': bool(snapshot['settings'].get('scheme-hidden:' + family, False))})
    for row in tasks.values():
        if row['kind'] in ('two-stage-calibration', 'calibration-replay'):
            for field in ('candidate_source_task_id', 'selection_baseline_task_id', 'replay_source_task_id'):
                reference = row[field]
                if reference in task_keys and row['id'] != reference:
                    dependency(task_keys[reference], _key('task', row['id']),
                               '率定“' + (row['name'] or row['id']) + '”保留对此参考成果的引用',
                               '先回收引用的率定成果并选择一同删除',
                               {'kind': 'task', 'id': row['id'], 'name': row['name'] or '率定成果',
                                'trashed': bool(row['trashed'])})
        source = state_owners.get(row['state_id'])
        if source in task_keys and row['id'] != source and row['status'] == 'succeeded':
            dependency(task_keys[source], _key('task', row['id']),
                       '成果“' + (row['name'] or row['id']) + '”引用该末状态',
                       '先回收该成果并选择一同删除',
                       {'kind': 'task', 'id': row['id'], 'name': row['name'] or '计算成果',
                        'trashed': bool(row['trashed']) and row['kind'] in RESULT_KINDS})

    # Export tasks are reproducible, object-owned caches unless explicitly
    # saved/used elsewhere. Include their records and directories in preview.
    owned = {identifier: [] for identifier in task_keys}
    export_owner = {}
    for row in tasks.values():
        source = row['source_task_id']
        if row['kind'] == 'export-result' and source in task_keys:
            if row['favorite']:
                items[task_keys[source]]['blocked'].append(_blocked('关联导出“' + (row['name'] or row['id']) + '”被收藏',
                                                                    '先取消导出收藏'))
            elif any(a.get('storage') == 'exports' and a.get('ownerTaskId') == row['id'] for a in row['artifacts']):
                items[task_keys[source]]['blocked'].append(_blocked('关联导出使用独立导出目录', '先单独处理该导出'))
            else:
                owned[source].append(row['id'])
                export_owner[row['id']] = source
    for row in tasks.values():
        for artifact in row['artifacts']:
            if artifact.get('storage') != 'task':
                continue
            owner = artifact.get('ownerTaskId')
            source = owner if owner in task_keys else export_owner.get(owner)
            if source and row['id'] not in {source, *owned[source]}:
                dependency(task_keys[source], _key('task', row['id']),
                           '成果“' + (row['name'] or row['id']) + '”仍引用其文件',
                           '先回收该成果并选择一同删除',
                           {'kind': 'task', 'id': row['id'], 'name': row['name'] or '计算成果',
                            'trashed': bool(row['trashed']) and row['kind'] in RESULT_KINDS})

    # Reject an unsafe filesystem target before resolving the dependency graph.
    scans = {}
    if include_files:
        for identifier, key in task_keys.items():
            if items[key]['blocked']:
                continue
            try:
                files, directories = [], []
                for task_id in [identifier, *owned[identifier]]:
                    part_files, part_dirs = _enumerate_task(db.workspace, task_id)
                    files.extend(part_files)
                    directories.extend(part_dirs)
                scans[identifier] = (files, directories)
            except (OSError, ValueError) as exc:
                items[key]['blocked'].append(_blocked('成果文件无法安全处理：' + str(exc), '检查文件后重新预览'))

    eligible = {key for key, item in items.items() if not item['blocked']}
    while True:
        removed = set()
        for key in eligible:
            for dependent, message, action, related in dependents[key]:
                if dependent not in eligible:
                    items[key]['blocked'].append(_blocked(message, action, related))
                    removed.add(key)
        if not removed:
            break
        eligible -= removed

    included_tasks = sorted(identifier for identifier, key in task_keys.items() if key in eligible)
    owned_tasks = sorted({other for identifier in included_tasks for other in owned[identifier]})
    files, directories = [], []
    for identifier in included_tasks:
        part_files, part_dirs = scans.get(identifier, ([], []))
        files.extend(part_files)
        directories.extend(part_dirs)
    files.sort(key=lambda row: row['path'])
    directories = sorted(set(directories))
    for key, item in items.items():
        item['deletable'] = key in eligible
        item['blocked'] = sorted(item['blocked'], key=lambda row: (row['message'], row['action']))
    context = {'items': [items[key] for key in sorted(items)], 'eligible': sorted(eligible),
               'ownedTasks': owned_tasks,
               'builtin': {'id': snapshot['builtin'].get('id'), 'resultId': snapshot['builtin'].get('resultId'),
                           'familyId': snapshot['builtin'].get('familyId')},
               'releases': snapshot['releases']}
    result = {'requested': requested, 'items': context['items'], 'eligible': context['eligible'],
              'taskIds': sorted(included_tasks + owned_tasks), 'ownedTasks': owned_tasks,
              'files': files, 'directories': directories,
              'contextHash': digest(context)}
    result['token'] = digest(result)
    return result


def _public(plan, *, resume=False, status='preview', error=''):
    files = plan['files']
    scopes = sorted({('/'.join(row['path'].split('/')[:2])) for row in files} |
                    {('/'.join(path.split('/')[:2])) for path in plan['directories']})
    return {'token': plan['token'], 'objects': plan['requested'], 'items': plan['items'],
            'deletableCount': len(plan['eligible']), 'blockedCount': len(plan['items']) - len(plan['eligible']),
            'fileCount': len(files), 'fileBytes': sum(row['bytes'] for row in files),
            'estimatedReclaimBytes': _reclaim_bytes(files), 'fileScopes': scopes,
            'ownedExportCount': len(plan['ownedTasks']), 'irreversible': True,
            'status': status, 'resume': resume, 'error': error}


def _pending(conn):
    if not _exists_table(conn, JOURNAL):
        return []
    rows = conn.execute("SELECT token,status,plan,error FROM permanent_deletion_operations WHERE status='pending'")
    return [{**dict(row), 'plan': _parse(row['plan'])} for row in rows]


def _plan_targets(plan):
    targets = set(plan['eligible'])
    targets |= {_key('scheme', member) for item in plan['items'] if item['deletable'] and item['kind'] == 'scheme'
                for member in item.get('members', [])}
    targets |= {_key('task', value) for value in plan['ownedTasks']}
    return targets


def preview(db, objects):
    requested = _requests(objects)
    wanted = {_key(row['kind'], row['id']) for row in requested}
    with db.connect() as conn:
        pending = [row for row in _pending(conn) if wanted & _plan_targets(row['plan'])]
        if len(pending) > 1:
            raise ValueError('所选对象属于多个未完成删除，请分别重试')
        if pending:
            row = pending[0]
            plan = row['plan']
            result = _public(plan, resume=True, status='partial', error=row['error'])
            try:
                remaining = len(_file_progress(db.workspace, plan))
                result.update(deletedFileCount=len(plan['files']) - remaining, remainingFileCount=remaining)
            except (OSError, ValueError):
                result.update(deletedFileCount=None, remainingFileCount=None)
            return result
        return _public(_build_plan(db, conn, requested))


def guard_pending(db, kind, identifier):
    """Protect incomplete objects from restoration or new runtime references."""
    if kind not in KINDS or not identifier:
        return
    from .windows_recycle import guard_available
    guard_available(db, kind, identifier)
    with db.connect() as conn:
        for row in _pending(conn):
            plan = row['plan']
            if _key(kind, identifier) in _plan_targets(plan):
                raise ValueError('该对象的彻底删除尚未完成，请在回收站重新预览并重试')


def guard_state(db, identifier):
    if not identifier:
        return
    rows = db.rows('SELECT task_id FROM states WHERE id=?', (identifier,))
    if rows:
        guard_pending(db, 'task', rows[0]['task_id'])


def guard_parameter_source(db, identifier):
    if not identifier:
        return
    guard_pending(db, 'parameter', identifier)
    rows = db.rows("SELECT json_extract(payload,'$.sourceTaskId') source FROM parameter_versions WHERE id=?", (identifier,))
    if rows and rows[0]['source']:
        guard_pending(db, 'task', rows[0]['source'])


def guard_no_pending(db):
    from .windows_recycle import guard_no_pending as guard_windows
    guard_windows(db)
    with db.connect() as conn:
        if _pending(conn):
            raise ValueError('有未完成的彻底删除，请先在回收站重试，确保资料和索引一致')


def _same_file(current, expected):
    return all(current[key] == expected[key] for key in ('path', 'bytes', 'mtimeNs', 'device', 'inode'))


def _file_progress(workspace, plan):
    expected = {row['path']: row for row in plan['files']}
    task_ids = plan['taskIds']
    current_files, current_dirs = [], []
    for identifier in task_ids:
        files, dirs = _enumerate_task(workspace, identifier)
        current_files.extend(files)
        current_dirs.extend(dirs)
    if not set(current_dirs).issubset(set(plan['directories'])):
        raise ValueError('删除范围内出现了新目录，已停止处理')
    for file in current_files:
        if file['path'] not in expected or not _same_file(file, expected[file['path']]):
            raise ValueError('删除范围内文件发生变化，已停止处理：' + file['path'])
    return current_files


def _resolve_planned(workspace, relative, expected):
    parts = relative.split('/')
    if len(parts) < 2 or parts[0] != 'tasks' or not IDENTIFIER.fullmatch(parts[1]) or any(
            part in ('', '.', '..') or '\\' in part or ':' in part for part in parts):
        raise ValueError('删除计划包含不合法的内部路径')
    folder = _task_path(workspace, parts[1])
    path = folder
    for part in parts[2:]:
        if path.exists() or path.is_symlink():
            _safe_entry(path, 'dir')
        path = path / part
    if path.exists() or path.is_symlink():
        _safe_entry(path, expected)
    return path


def _remove_file(path):
    os.unlink(path)


def _remove_directory(path):
    os.rmdir(path)


def _result(plan, status, error='', remaining=None):
    file_count = plan.get('fileCount', len(plan.get('files', [])))
    remaining = file_count if remaining is None else remaining
    return {'status': status, 'token': plan['token'],
            'deletedObjects': [{'kind': item['kind'], 'id': item['id'], 'name': item['name']}
                               for item in plan['items'] if item['deletable']] if status == 'deleted' else [],
            'deletedFileCount': file_count - remaining, 'remainingFileCount': remaining,
            'error': error, 'estimatedReclaimBytes': plan.get('reclaimBytes', _reclaim_bytes(plan.get('files', [])))}


def execute(db, objects, token):
    """Execute an approved plan. The server must hold the shared Jobs.lock."""
    requested = _requests(objects)
    from .windows_recycle import guard_available
    for obj in requested:
        guard_available(db, obj['kind'], obj['id'])
    if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{64}', token):
        raise ValueError('删除预览令牌无效')
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _schema(conn)
        existing = conn.execute('SELECT status,plan,error FROM permanent_deletion_operations WHERE token=?', (token,)).fetchone()
        if existing:
            plan = _parse(existing['plan'])
            if plan['requested'] != requested:
                raise ValueError('确认对象与删除预览不一致')
            if existing['status'] == 'done':
                return _result(plan, 'deleted', remaining=0)
            if existing['status'] != 'pending':
                raise ValueError('该删除计划已失效，请重新预览')
        else:
            plan = _build_plan(db, conn, requested)
            if plan['token'] != token:
                raise ValueError('对象、文件或依赖已变化，请重新预览')
            if not plan['eligible']:
                raise ValueError('所选对象均有保护或依赖，请先处理预览中的原因')
            if any(_plan_targets(plan) & _plan_targets(row['plan']) for row in _pending(conn)):
                raise ValueError('所选对象有未完成的删除，请先重新预览并重试')
            # Blocked objects are not part of this operation. Their later
            # recovery or deletion must not prevent retrying an interrupted
            # operation on the explicitly approved deletable subset.
            execution_requested = [row for row in requested if _key(row['kind'], row['id']) in plan['eligible']]
            subset = _build_plan(db, conn, execution_requested)
            if (len(subset['eligible']) != len(execution_requested) or
                    subset['files'] != plan['files'] or subset['directories'] != plan['directories']):
                raise ValueError('可删除范围已变化，请重新预览')
            plan['executionRequested'] = execution_requested
            plan['contextHash'] = subset['contextHash']
            conn.execute('INSERT INTO permanent_deletion_operations VALUES(?,?,?,?,?,?)',
                         (token, now(), now(), 'pending', canonical(plan), ''))

    from .result_query import clear_source_cache
    clear_source_cache()
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        finalizing = False
        try:
            fresh = _build_plan(db, conn, plan['executionRequested'])
            if fresh['contextHash'] != plan['contextHash']:
                raise ValueError('对象状态或依赖已变化，请处理后重试')
            remaining = _file_progress(db.workspace, plan)
            for file in remaining:
                path = _resolve_planned(db.workspace, file['path'], 'file')
                info = _safe_entry(path, 'file')
                actual = {'path': file['path'], 'bytes': info.st_size, 'mtimeNs': info.st_mtime_ns,
                          'device': info.st_dev, 'inode': info.st_ino}
                if not _same_file(actual, file):
                    raise ValueError('文件在处理期间发生变化：' + file['path'])
                _remove_file(path)
            for relative in sorted(plan['directories'], key=lambda value: (-value.count('/'), value)):
                path = _resolve_planned(db.workspace, relative, 'dir')
                if path.exists():
                    _remove_directory(path)
            # Database rows are removed only after every planned file is gone.
            conn.execute('SAVEPOINT finalize_deletion')
            finalizing = True
            for identifier in plan['ownedTasks'] + [item['id'] for item in plan['items']
                                                     if item['deletable'] and item['kind'] == 'task']:
                conn.execute('DELETE FROM states WHERE task_id=?', (identifier,))
                conn.execute('DELETE FROM tasks WHERE id=?', (identifier,))
                conn.execute("DELETE FROM settings WHERE key LIKE 'task-request:%' AND json_extract(value,'$.taskId')=?", (identifier,))
            for item in plan['items']:
                if not item['deletable']:
                    continue
                if item['kind'] == 'parameter':
                    conn.execute('DELETE FROM parameter_versions WHERE id=?', (item['id'],))
                    conn.execute('DELETE FROM settings WHERE key=?', ('parameter-hidden:' + item['id'],))
                elif item['kind'] == 'scheme':
                    for identifier in item['members']:
                        conn.execute('DELETE FROM schemes WHERE id=?', (identifier,))
                    conn.execute('DELETE FROM settings WHERE key=?', ('scheme-hidden:' + item['family'],))
            receipt = _result(plan, 'deleted', remaining=0)
            conn.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                         (now(), 'objects.permanent-delete', canonical({'objects': receipt['deletedObjects'],
                                                                       'fileCount': receipt['deletedFileCount']})))
            compact = {'token': plan['token'], 'requested': plan['requested'],
                       'items': [{key: item[key] for key in ('kind', 'id', 'name', 'deletable')}
                                 for item in plan['items']],
                       'fileCount': len(plan['files']), 'reclaimBytes': _reclaim_bytes(plan['files'])}
            conn.execute("UPDATE permanent_deletion_operations SET status='done',updated_at=?,plan=?,error='' WHERE token=?",
                         (now(), canonical(compact), token))
            conn.execute('RELEASE finalize_deletion')
            finalizing = False
        except Exception as exc:
            # Commit the durable pending record and keep all business indices.
            # Files already removed can be counted from the saved plan on retry.
            if finalizing:
                conn.execute('ROLLBACK TO finalize_deletion')
                conn.execute('RELEASE finalize_deletion')
            conn.execute('UPDATE permanent_deletion_operations SET updated_at=?,error=? WHERE token=?',
                         (now(), str(exc)[:1000], token))
            try:
                remaining = len(_file_progress(db.workspace, plan))
            except (OSError, ValueError):
                remaining = 0
                for file in plan['files']:
                    try:
                        remaining += int(_resolve_planned(db.workspace, file['path'], 'file').exists())
                    except (OSError, ValueError):
                        remaining += 1
            return _result(plan, 'partial', str(exc), remaining)
    clear_source_cache()
    return receipt
