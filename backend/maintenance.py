from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
import zipfile
from pathlib import Path

from .common import ROOT, canonical, digest, now, read_json, sha_file, within, write_json


def cleanup_preview(db, task_id):
    task = db.task(task_id)
    if not task['trashed']:
        raise ValueError('请先移入平台回收站')
    if task['status'] in ('queued', 'running', 'stopping'):
        raise ValueError('活动任务不能移走')
    if task['favorite']:
        raise ValueError('收藏成果受到保护')
    states = db.rows('SELECT * FROM states WHERE task_id=?', (task_id,))
    ids = [task_id, *[r['id'] for r in states]]
    protected = []
    for state in states:
        if state['name'].strip() or state['favorite']:
            protected.append('已命名或收藏状态/' + state['id'])
    for table, field in [('parameter_versions', 'payload'), ('schemes', 'payload')]:
        for row in db.rows(f'SELECT id,{field} FROM {table}'):
            if any(identifier in row[field] for identifier in ids):
                protected.append(table + '/' + row['id'])
    for row in db.rows('SELECT id,request FROM tasks WHERE id<>?', (task_id,)):
        public_request = {k: v for k, v in json.loads(row['request']).items() if not k.startswith('_')}
        if any(identifier in canonical(public_request) for identifier in ids):
            protected.append('后续任务/' + row['id'])
    folder = within(db.workspace / 'tasks', task_id)
    files = []
    for p in sorted(folder.rglob('*')):
        if p.is_symlink() or getattr(p.lstat(), 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
            raise ValueError('清理目录中含链接，不能自动移走')
        if p.is_file():
            files.append({'path': p.relative_to(db.workspace).as_posix(), 'bytes': p.stat().st_size, 'sha256': sha_file(p)})
    preview = {'taskId': task_id, 'files': files, 'bytes': sum(r['bytes'] for r in files), 'protected': protected}
    preview['token'] = digest(preview)
    return preview


def recycle_native(path):
    """Recycle only via the Shell undo service; failure never falls back to unlink."""
    if os.name != 'nt':
        raise RuntimeError('仅Windows回收站支持最终移走；文件仍保留在平台回收站')
    import ctypes
    from ctypes import wintypes
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT), ('pFrom', wintypes.LPCWSTR),
                    ('pTo', wintypes.LPCWSTR), ('fFlags', wintypes.WORD), ('fAnyOperationsAborted', wintypes.BOOL),
                    ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', wintypes.LPCWSTR)]
    path = Path(path).resolve()
    drive = path.drive
    bin_root = Path(drive + '\\') / '$Recycle.Bin'
    if not drive or not bin_root.is_dir():
        raise RuntimeError('目标磁盘未检测到Windows回收站，未执行移走')
    # Require readable per-user recycle metadata before invoking Shell. Verify
    # the matching $I original path and recoverable $R entry afterwards.
    readable = []
    for folder in bin_root.iterdir():
        if not folder.is_dir():
            continue
        try:
            list(folder.iterdir())
            readable.append(folder)
        except PermissionError:
            pass
    if not readable:
        raise RuntimeError('无法核验当前用户回收站，未执行移走')
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3
    operation.pFrom = str(path) + '\0\0'
    # ALLOWUNDO + WANTNUKEWARNING. Never suppress a warning when a device
    # cannot recycle an object; never request a permanent-delete fallback.
    operation.fFlags = 0x0040 | 0x4000
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if code or operation.fAnyOperationsAborted or path.exists():
        raise RuntimeError(f'Windows未确认移入回收站（代码{code}），索引仍保留')
    import struct
    for folder in readable:
        for info in folder.glob('$I*'):
            try:
                raw = info.read_bytes()
                version = struct.unpack_from('<Q', raw)[0]
                offset = 28 if version == 2 else 24
                original = raw[offset:].decode('utf-16-le').rstrip('\0')
                recoverable = info.with_name('$R' + info.name[2:])
                if original.casefold() == str(path).casefold() and recoverable.exists():
                    return {'metadata': str(info), 'recoverable': str(recoverable), 'original': str(path)}
            except (OSError, UnicodeError, struct.error):
                continue
    raise RuntimeError('Shell已返回，但未找到匹配的可恢复条目；平台索引保留，请核对系统回收站')


def cleanup_execute(db, task_id, token):
    preview = cleanup_preview(db, task_id)
    if preview['protected']:
        raise ValueError('记录仍被引用：' + '、'.join(preview['protected']))
    if token != preview['token']:
        raise ValueError('文件或依赖已变化，请重新预览')
    journal = db.workspace / 'cleanup-journal' / (uuid.uuid4().hex + '.json')
    write_json(journal, {'at': now(), 'preview': preview, 'status': 'started'})
    recycled = recycle_native(within(db.workspace / 'tasks', task_id))
    with db.connect() as conn:
        conn.execute('DELETE FROM states WHERE task_id=?', (task_id,))
        conn.execute('DELETE FROM tasks WHERE id=?', (task_id,))
    write_json(journal, {'at': now(), 'preview': preview, 'status': 'recycled', 'recycleVerification': recycled})
    return {'status': 'recycled', 'taskId': task_id}


def backup_workspace(db):
    identifier = 'backup-' + now().replace(':', '').replace('+', '-') + '-' + uuid.uuid4().hex[:6]
    output = db.workspace / 'exports' / (identifier + '.zip')
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshot = db.workspace / 'exports' / (identifier + '.sqlite3')
    with db.connect() as source:
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
    spatial = read_json(ROOT / 'data/derived/spatial/manifest.json')
    manifest = {'schemaVersion': 1, 'projectId': 'selinco', 'createdAt': now(), 'files': [],
                'spatialVersion': (spatial or {}).get('version')}
    files = [p for p in db.workspace.rglob('*') if p.is_file() and not p.is_symlink()
             and p.relative_to(db.workspace).parts[0] not in ('exports', 'numeric-cache')
             and (p.relative_to(db.workspace).parts[0] != 'incoming' or p.suffix == '.nc')
             and not any(part.endswith('.chunks') for part in p.relative_to(db.workspace).parts)
             and not p.name.endswith(('-wal', '-shm', '.partial')) and p.name != 'instance.lock' and p != db.path]
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for path in files + [snapshot]:
            relative = 'database/hydrology.sqlite3' if path == snapshot else path.relative_to(db.workspace).as_posix()
            archive.write(path, relative)
            manifest['files'].append({'path': relative, 'bytes': path.stat().st_size, 'sha256': sha_file(path)})
        archive.writestr('backup-manifest.json', canonical(manifest))
    return {'filename': output.name, 'bytes': output.stat().st_size, 'sha256': sha_file(output), 'files': len(files) + 1}


def restore_preview(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read('backup-manifest.json'))
        if manifest.get('projectId') != 'selinco' or manifest.get('schemaVersion') != 1:
            raise ValueError('备份不属于色林错独立平台')
        current = read_json(ROOT / 'data/derived/spatial/manifest.json') or {}
        if manifest.get('spatialVersion') != current.get('version'):
            raise ValueError('备份空间版本与当前安装不一致，请同时迁移对应源码及空间资料包')
        names = [info.filename for info in archive.infolist()]
        if len(names) != len(set(names)):
            raise ValueError('备份含重复文件名')
        expected = {r['path'] for r in manifest['files']} | {'backup-manifest.json'}
        if set(names) != expected:
            raise ValueError('备份清单与文件不一致')
        if sum(info.file_size for info in archive.infolist()) > 50 * 1024 ** 3:
            raise ValueError('备份展开体积超过50GB')
        for row in manifest['files']:
            name = row['path']
            if '\\' in name or name.startswith('/') or '..' in Path(name).parts or ':' in name:
                raise ValueError('备份路径不安全')
            info = archive.getinfo(name)
            if info.file_size != row['bytes']:
                raise ValueError('备份文件大小与清单不一致')
    return manifest


def restore_workspace(archive_path, destination):
    manifest = restore_preview(archive_path)
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('恢复目标须为空目录，不覆盖既有工作区')
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for row in manifest['files']:
            output = within(destination, row['path'])
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(row['path']) as source, output.open('xb') as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
            if sha_file(output) != row['sha256']:
                raise ValueError('备份摘要校验失败，恢复目录保留供检查，未切换活动工作区')
    identity = read_json(destination / 'workspace-identity.json')
    if not identity or identity.get('projectId') != 'selinco':
        raise ValueError('恢复工作区身份不正确')
    return {'workspace': str(destination), 'files': len(manifest['files'])}
