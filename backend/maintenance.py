from __future__ import annotations

import json
import os
import sqlite3
import shutil
import stat
import uuid
import zipfile
import hashlib
from urllib.parse import urlencode
from functools import lru_cache
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
    from .research_schemes import builtin
    standard = builtin(db)
    if standard and standard['payload'].get('resultId') == task_id:
        protected.append('内置方案成果')
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


def _hash_progress(path, progress=None, cancelled=None, start=90, end=99):
    path=Path(path);total=path.stat().st_size;done=0;value=hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            if cancelled and cancelled():raise InterruptedError('用户停止完整性校验')
            block=stream.read(1024*1024)
            if not block:break
            value.update(block);done+=len(block)
            if progress:progress(start+(end-start)*done/max(1,total),f'校验文件摘要 · {done}/{total} 字节')
    return value.hexdigest()


@lru_cache(maxsize=128)
def _restored_artifact_valid(path, size, modified_ns, expected_sha256):
    # Restore creates new filesystem timestamps. Revalidate bytes once for that
    # exact stat signature rather than rejecting an otherwise intact archive.
    return sha_file(Path(path))==expected_sha256


def artifact_file(db, task, name):
    if task.get('status')!='succeeded':raise ValueError('任务尚未成功，产物不可下载')
    artifact=next((a for a in (task.get('result') or {}).get('artifacts',[]) if a.get('name')==name),None)
    if not artifact or not artifact.get('complete'):raise FileNotFoundError('完整任务产物不存在')
    if artifact.get('storage')=='published':
        from .published_results import result_file
        path=result_file(artifact['ownerTaskId'],artifact['filename'])
    elif artifact.get('storage')=='exports':path=within(db.workspace/'exports',artifact['filename'])
    else:path=within(within(db.workspace/'tasks',artifact['ownerTaskId']),artifact['filename'])
    if not path.is_file() or path.stat().st_size!=artifact['bytes']:
        raise ValueError('产物文件已变化，请重新导出并校验')
    signature=path.stat()
    if (signature.st_mtime_ns!=artifact['mtimeNs'] and not _restored_artifact_valid(
            str(path.resolve()),signature.st_size,signature.st_mtime_ns,artifact['sha256'])):
        raise ValueError('产物文件已变化，请重新导出并校验')
    return path


def export_result(db, request, folder, progress=None, cancelled=None):
    from .business_context import result_task
    from .published_results import catalog, result_file
    task=result_task(db,request['taskId'])
    if task.get('status')!='succeeded' or not task.get('result') or task.get('trashed'):
        raise ValueError('只能导出完整成功成果')
    filename=request.get('format','hourly.npz')
    if filename not in ('daily.csv','checkpoint.json','result.json','hourly.npz','request.json'):
        raise ValueError('不支持的导出类型')
    published=any(r['id']==task['id'] for r in catalog())
    owner=task['id'];storage='published' if published else 'task'
    source_filename='final-state.json' if published and filename=='checkpoint.json' else filename
    if published:path=result_file(task['id'],source_filename)
    else:
        source=within(db.workspace/'tasks',task['id'])
        path=source/filename
        if filename=='hourly.npz' and not path.is_file() and task['result'].get('storageFormat')=='zarr-v2':
            from .grid_storage import export_npz
            path=export_npz(source,folder,progress,cancelled)
            owner=folder.name
    if not path.is_file():raise FileNotFoundError('成果中没有该导出文件')
    before=path.stat()
    fingerprint=_hash_progress(path,progress,cancelled)
    after=path.stat()
    if before.st_size!=after.st_size or before.st_mtime_ns!=after.st_mtime_ns:
        raise ValueError('导出过程中源文件发生变化，请重试')
    artifact={'name':filename,'filename':source_filename,'storage':storage,'ownerTaskId':owner,
              'bytes':path.stat().st_size,'sha256':fingerprint,'mtimeNs':path.stat().st_mtime_ns,'complete':True,
              'downloadUrl':'/api/model/task-artifact?'+urlencode({'taskId':folder.name,'name':filename})}
    return {'artifacts':[artifact],'sourceTaskId':task['id'],'sourceIdentitySHA256':digest(task['result'].get('identity',{})),
            'termination':'completed'}


def backup_workspace(db, progress=None, cancelled=None):
    identifier = 'backup-' + now().replace(':', '').replace('+', '-') + '-' + uuid.uuid4().hex[:6]
    output = db.workspace / 'exports' / (identifier + '.zip')
    partial=output.with_suffix('.zip.partial')
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshot = db.workspace / 'exports' / (identifier + '.sqlite3')
    with db.connect() as source:
        target = sqlite3.connect(snapshot)
        try:
            def snapshot_progress(status, remaining, total):
                if cancelled and cancelled():raise InterruptedError('用户停止备份')
                if progress:progress(5*(total-remaining)/max(1,total),'建立一致数据库快照')
            source.backup(target,pages=256,progress=snapshot_progress)
        finally:
            target.close()
    spatial = read_json(ROOT / 'data/derived/spatial/manifest.json')
    manifest = {'schemaVersion': 1, 'projectId': 'selinco', 'createdAt': now(), 'files': [],
                'spatialVersion': (spatial or {}).get('version')}
    from .grid_spatial import load
    versions={(spatial or {}).get('version')}
    for row in db.items('schemes'):versions.add(row.get('payload',{}).get('spatialVersion'))
    for row in db.rows("SELECT result FROM tasks WHERE status='succeeded' AND result IS NOT NULL"):
        versions.add(json.loads(row['result']).get('spatialVersion'))
    manifest['requiredSpatialVersions']={v:digest(load(v)) for v in sorted(v for v in versions if v)}
    files = [p for p in db.workspace.rglob('*') if p.is_file() and not p.is_symlink()
             and p.relative_to(db.workspace).parts[0] not in ('exports', 'numeric-cache')
             and (p.relative_to(db.workspace).parts[0] != 'incoming' or p.suffix == '.nc')
             and not any(part.endswith('.chunks') or part=='.latest-parts' for part in p.relative_to(db.workspace).parts)
             and not p.name.endswith(('-wal', '-shm', '.partial')) and p.name != 'instance.lock' and p != db.path]
    total=sum(p.stat().st_size for p in files+[snapshot]);done=0
    with zipfile.ZipFile(partial, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1,allowZip64=True) as archive:
        for path in files + [snapshot]:
            if cancelled and cancelled():raise InterruptedError('用户停止备份')
            relative = 'database/hydrology.sqlite3' if path == snapshot else path.relative_to(db.workspace).as_posix()
            before=path.stat();hashed=hashlib.sha256();size=0
            with path.open('rb') as source,archive.open(relative,'w',force_zip64=True) as target:
                while True:
                    if cancelled and cancelled():raise InterruptedError('用户停止备份')
                    chunk=source.read(1024*1024)
                    if not chunk:break
                    target.write(chunk);hashed.update(chunk);size+=len(chunk);done+=len(chunk)
                    if progress:progress(5+85*done/max(1,total),f'备份 {relative} · {done}/{total} 字节')
            after=path.stat()
            if before.st_size!=after.st_size or before.st_mtime_ns!=after.st_mtime_ns:
                raise ValueError('备份过程中源文件发生变化，请重试：'+relative)
            manifest['files'].append({'path': relative, 'bytes': size, 'sha256': hashed.hexdigest()})
        archive.writestr('backup-manifest.json', canonical(manifest))
    fingerprint=_hash_progress(partial,progress,cancelled)
    partial.replace(output)
    result={'filename': output.name, 'bytes': output.stat().st_size, 'sha256':fingerprint, 'files': len(files) + 1,
            'mtimeNs':output.stat().st_mtime_ns,'complete':True}
    write_json(output.with_suffix('.complete.json'),result)
    return result


def restore_preview(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read('backup-manifest.json'))
        if manifest.get('projectId') != 'selinco' or manifest.get('schemaVersion') != 1:
            raise ValueError('备份不属于色林错独立平台')
        current = read_json(ROOT / 'data/derived/spatial/manifest.json') or {}
        if manifest.get('spatialVersion') != current.get('version'):
            raise ValueError('备份空间版本与当前安装不一致，请同时迁移对应源码及空间资料包')
        from .grid_spatial import load
        for version,expected in manifest.get('requiredSpatialVersions',{}).items():
            if digest(load(version))!=expected:raise ValueError('备份依赖的空间版本缺失或摘要不同：'+version)
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


def restore_archive(db, request):
    """Resolve only an uploaded basename or this workspace's verified backup."""
    filename, task_id = request.get('filename'), request.get('backupTaskId')
    if bool(filename) == bool(task_id):
        raise ValueError('请仅选择上传备份文件或本工作区备份任务之一')
    if task_id:
        task = db.task(task_id)
        if task.get('kind') != 'backup' or task.get('status') != 'succeeded':
            raise ValueError('只能恢复本工作区成功的完整备份任务')
        artifacts = (task.get('result') or {}).get('artifacts', [])
        if len(artifacts) != 1 or not artifacts[0].get('name', '').lower().endswith('.zip'):
            raise ValueError('备份任务没有唯一完整ZIP产物')
        return artifact_file(db, task, artifacts[0]['name'])
    if not isinstance(filename, str) or not filename or any(c in filename for c in ('/', '\\', ':')) or filename.startswith('.') or not filename.lower().endswith('.zip'):
        raise ValueError('备份文件名须为上传目录内的ZIP名称')
    archive = within(db.workspace/'incoming', filename)
    if not archive.is_file() or archive.is_symlink():
        raise ValueError('已上传备份不存在')
    return archive


def restore_request_preview(db, request):
    manifest = restore_preview(restore_archive(db, request))
    return {'files':len(manifest['files']), 'bytes':sum(row['bytes'] for row in manifest['files']),
            'token':digest(manifest), **({'backupTaskId':request['backupTaskId']} if request.get('backupTaskId') else {'filename':request['filename']}),
            'restartRequired':True, 'message':'恢复到独立目录并逐文件校验；成功后切换下次启动工作区，原工作区保留。恢复排队后暂停接受新任务。'}


def restore_workspace(archive_path, destination, progress=None, cancelled=None):
    manifest = restore_preview(archive_path)
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('恢复目标须为空目录，不覆盖既有工作区')
    destination.mkdir(parents=True, exist_ok=True)
    total = sum(row['bytes'] for row in manifest['files'])
    if shutil.disk_usage(destination).free < total + 16*1024*1024:
        raise ValueError('恢复磁盘可用空间不足：需要至少 '+str(total+16*1024*1024)+' 字节')
    done = 0
    with zipfile.ZipFile(archive_path) as archive:
        for index, row in enumerate(manifest['files']):
            if cancelled and cancelled():raise InterruptedError('用户停止恢复；原工作区未切换')
            output = within(destination, row['path'])
            output.parent.mkdir(parents=True, exist_ok=True)
            hashed = hashlib.sha256()
            with archive.open(row['path']) as source, output.open('xb') as target:
                while chunk := source.read(1024 * 1024):
                    if cancelled and cancelled():raise InterruptedError('用户停止恢复；原工作区未切换')
                    target.write(chunk)
                    hashed.update(chunk);done += len(chunk)
                    if progress:progress(2+92*done/max(1,total),f'恢复 {index+1}/{len(manifest["files"])} 文件 · {done}/{total} 字节 · {row["path"]}')
            if hashed.hexdigest() != row['sha256']:
                raise ValueError('备份摘要校验失败，恢复目录保留供检查，未切换活动工作区')
    identity = read_json(destination / 'workspace-identity.json')
    if not identity or identity.get('projectId') != 'selinco':
        raise ValueError('恢复工作区身份不正确')
    if cancelled and cancelled():raise InterruptedError('用户停止恢复；原工作区未切换')
    if progress:progress(96,'检查恢复数据库完整性与外键')
    database = destination/'database/hydrology.sqlite3'
    from contextlib import closing
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or connection.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('恢复数据库完整性或外键检查失败；未切换工作区')
        tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'stations','observations','input_products','tasks','states','schema_version'} <= tables:
            raise ValueError('恢复数据库缺少色林错必要数据表；未切换工作区')
        counts={name:connection.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('observations','input_products','tasks','states','sources')}
        verified={row['path']:row['sha256'] for row in manifest['files']}
        for identifier,payload in connection.execute('SELECT id,manifest FROM input_products'):
            product=json.loads(payload)
            relative=(Path('inputs/products')/identifier/product['data']).as_posix()
            if verified.get(relative)!=product.get('dataSHA256'):
                raise ValueError('恢复输入登记与已校验文件不一致：'+identifier)
        for identifier,expected,payload in connection.execute('SELECT id,sha256,metadata FROM sources'):
            archived=json.loads(payload).get('archivedPath')
            if archived and verified.get(archived)!=expected:
                raise ValueError('恢复观测来源与已校验文件不一致：'+identifier)
    if cancelled and cancelled():raise InterruptedError('用户停止恢复；原工作区未切换')
    return {'workspace':str(destination),'files':len(manifest['files']),'bytes':total,'validatedSHA256Files':len(manifest['files']),
            'databaseIntegrity':'ok','counts':counts,'restartRequired':True}
