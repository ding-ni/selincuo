"""Non-destructive launcher-only installation of a missing frozen seed input."""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sqlite3
import uuid
from .common import canonical, now, read_json, sha_file, within


@contextmanager
def idle_workspace(workspace):
    """Use the same instance lock as Platform; never migrate a live workspace."""
    lock=Path(workspace)/'instance.lock'
    stream=lock.open('a+b')
    try:
        try:
            stream.seek(0)
            if not stream.read(1):stream.write(b'0');stream.flush()
            stream.seek(0)
            if __import__('os').name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:raise ValueError('工作区已有服务运行或锁不可访问；请先退出原服务，再升级。不会自动关闭服务。')
        yield
    finally:stream.close()


def supplement_default_input(workspace, seed, app_root):
    """Caller holds idle_workspace. No Database() construction or release edits."""
    workspace=Path(workspace).resolve();seed=Path(seed).resolve();app_root=Path(app_root).resolve()
    identity=read_json(workspace/'workspace-identity.json') or {}
    database=workspace/'database/hydrology.sqlite3'
    if identity.get('projectId')!='selinco' or not database.is_file():
        raise ValueError('仅能升级有合法身份与数据库的既有色林错工作区')
    report=read_json(seed/'seed-report.json') or {}
    manifest=report.get('inputManifest') or {};identifier=report.get('defaultInputId')
    if (report.get('schemaVersion')!=2 or report.get('projectId')!='selinco' or not identifier or
        manifest.get('id')!=identifier or manifest.get('projectId')!='selinco' or
        report.get('spatialVersion')!=manifest.get('spatialVersion') or
        report.get('resultIdentity',{}).get('inputId')!=identifier or
        report.get('resultIdentity',{}).get('inputSHA256')!=manifest.get('dataSHA256')):
        raise ValueError('发行默认输入绑定清单不完整或身份冲突')
    source=within(seed/'inputs/products',identifier)
    if read_json(source/'manifest.json')!=manifest:raise ValueError('发行输入manifest与种子清单不同')
    spatial=manifest['spatialVersion']
    archived=read_json(within(app_root/'data/derived/spatial/versions',spatial)/'manifest.json')
    current=read_json(app_root/'data/derived/spatial/manifest.json')
    if not any(s and s.get('version')==spatial for s in (archived,current)):
        raise ValueError('发行默认输入的空间版本未安装')
    data=within(source,manifest['data'])
    if data.is_symlink() or not data.is_file() or sha_file(data)!=manifest.get('dataSHA256'):
        raise ValueError('发行输入数据摘要校验失败')
    destination=within(workspace/'inputs/products',identifier)
    with sqlite3.connect(database) as connection:
        rows=connection.execute('SELECT manifest FROM input_products WHERE id=?',(identifier,)).fetchall()
        if rows:
            if json.loads(rows[0][0])!=manifest:raise ValueError('既有同ID输入摘要或manifest冲突，未覆盖')
            if (read_json(destination/'manifest.json')!=manifest or
                not within(destination,manifest['data']).is_file() or
                sha_file(within(destination,manifest['data']))!=manifest['dataSHA256']):
                raise ValueError('既有同ID输入文件缺失或冲突，未覆盖；请先独立核查恢复')
            return {'status':'already-present','inputId':identifier,'changed':False}
        if destination.exists():raise ValueError('默认输入目标目录已存在但未登记，禁止覆盖；请独立核查')
        # The caller holds Platform's instance lock. Old running/queued rows may
        # be crash leftovers; preserve them for Jobs' normal interrupted-task
        # recovery rather than preventing the upgraded service from starting.
        backup=workspace/'database'/('pre-default-input-upgrade-'+uuid.uuid4().hex[:12]+'.sqlite3')
        with sqlite3.connect(backup) as target:connection.backup(target)
        stage=workspace/'upgrade-staging'/uuid.uuid4().hex
        stage.mkdir(parents=True)
        for name in ('manifest.json',manifest['data']):
            original=within(source,name);target=within(stage,name);target.parent.mkdir(parents=True,exist_ok=True)
            if original.is_symlink():raise ValueError('发行输入文件含链接')
            shutil.copy2(original,target)
        if sha_file(within(stage,manifest['data']))!=manifest['dataSHA256']:
            raise ValueError('复制后的默认输入摘要不同，原工作区保留')
        destination.parent.mkdir(parents=True,exist_ok=True)
        connection.execute('BEGIN IMMEDIATE')
        stage.rename(destination)
        connection.execute('INSERT INTO input_products(id,name,created_at,manifest) VALUES(?,?,?,?)',
                           (identifier,manifest.get('name',identifier),manifest.get('createdAt',now()),canonical(manifest)))
        connection.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                           (now(),'workspace.default-input-supplement',canonical({'inputId':identifier,'backup':str(backup),'dataSHA256':manifest['dataSHA256']})))
    return {'status':'supplemented','inputId':identifier,'changed':True,'backup':str(backup)}
