"""Non-destructive launcher-only installation of a missing frozen seed input."""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sqlite3
import uuid
from .common import ROOT, canonical, digest, now, read_json, sha_file, within


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


def _package_business(seed):
    seed=Path(seed).resolve()
    report=read_json(seed/'seed-report.json') or {}
    business=report.get('businessDefault')
    if report.get('schemaVersion')!=2 or report.get('projectId')!='selinco' or not business:
        return None
    if (not business.get('schemeId') or not business.get('parameterId') or not business.get('resultId') or
            not business.get('inputId') or not business.get('spatialVersion')):
        raise ValueError('发行包的业务默认项清单不完整')
    return seed,report,business


def _copy_input(seed, workspace, identifier, manifest):
    source=within(seed/'inputs/products',identifier)
    if read_json(source/'manifest.json')!=manifest:
        raise ValueError('发行输入manifest与种子清单不同')
    data=within(source,manifest.get('data',''))
    if data.is_symlink() or not data.is_file() or sha_file(data)!=manifest.get('dataSHA256'):
        raise ValueError('发行输入数据摘要校验失败')
    destination=within(workspace/'inputs/products',identifier)
    if destination.exists():
        if (read_json(destination/'manifest.json')!=manifest or
                not within(destination,manifest['data']).is_file() or
                sha_file(within(destination,manifest['data']))!=manifest['dataSHA256']):
            raise ValueError('既有输入目录与发行输入冲突，未覆盖')
        return destination,False
    stage=workspace/'upgrade-staging'/uuid.uuid4().hex
    stage.mkdir(parents=True)
    for relative in ('manifest.json',manifest['data']):
        original=within(source,relative)
        if original.is_symlink():raise ValueError('发行输入文件含链接')
        target=within(stage,relative);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(original,target)
    if sha_file(within(stage,manifest['data']))!=manifest['dataSHA256']:
        raise ValueError('复制后的默认输入摘要不同，原工作区保留')
    destination.parent.mkdir(parents=True,exist_ok=True)
    stage.rename(destination)
    return destination,True


def supplement_business_default(workspace, seed, app_root):
    """Add one packaged business scheme without replacing any existing adoption."""
    workspace=Path(workspace).resolve();app_root=Path(app_root).resolve()
    packaged=_package_business(seed)
    if not packaged:return {'status':'no-business-default','changed':False}
    seed,report,business=packaged
    source_db=seed/'database/hydrology.sqlite3'
    if not source_db.is_file():raise ValueError('发行种子缺少只读数据库')
    with sqlite3.connect(source_db.as_uri()+'?mode=ro',uri=True) as origin:
        origin.row_factory=sqlite3.Row
        scheme_row=origin.execute('SELECT id,name,created_at,payload FROM schemes WHERE id=?',(business['schemeId'],)).fetchone()
        parameter_row=origin.execute('SELECT id,name,created_at,payload FROM parameter_versions WHERE id=?',(business['parameterId'],)).fetchone()
        selected=origin.execute('SELECT value FROM settings WHERE key=?',('business-adopted-scheme-v1',)).fetchone()
        product_row=origin.execute('SELECT id,name,created_at,manifest FROM input_products WHERE id=?',(business['inputId'],)).fetchone()
        if not all((scheme_row,parameter_row,selected,product_row)):
            raise ValueError('发行种子缺少业务方案、参数、输入或采用关系')
        scheme={key:scheme_row[key] for key in scheme_row.keys()}
        parameter={key:parameter_row[key] for key in parameter_row.keys()}
        selection=json.loads(selected['value'])
        manifest=json.loads(product_row['manifest'])
        product={key:product_row[key] for key in product_row.keys()}
    scheme_payload=json.loads(scheme['payload']);parameter_payload=json.loads(parameter['payload'])
    if (digest(scheme_payload)!=business.get('schemePayloadSHA256') or
            digest(parameter_payload)!=business.get('parameterPayloadSHA256') or
            digest(json.loads(product['manifest']))!=business.get('inputManifestSHA256') or
            selection.get('schemeId')!=business['schemeId'] or selection.get('parameterId')!=business['parameterId'] or
            selection.get('resultId')!=business['resultId'] or scheme_payload.get('inputId')!=business['inputId'] or
            scheme_payload.get('spatialVersion')!=business['spatialVersion']):
        raise ValueError('发行包方案/参数/输入和清单摘要不一致')
    spatial=business['spatialVersion']
    spatial_manifest=read_json(within(app_root/'data/derived/spatial/versions',spatial)/'manifest.json')
    if not spatial_manifest or spatial_manifest.get('version')!=spatial:
        raise ValueError('发行默认方案所需空间版本未安装')
    database=workspace/'database/hydrology.sqlite3'
    identity=read_json(workspace/'workspace-identity.json') or {}
    if identity.get('projectId')!='selinco' or not database.is_file():
        raise ValueError('仅能补充合法色林错工作区')

    backup=None
    destination=within(workspace/'inputs/products',business['inputId'])
    added_input=False
    changed=False
    with sqlite3.connect(database,timeout=60) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        for table,row,payload_key in (('schemes',scheme,'payload'),('parameter_versions',parameter,'payload')):
            found=connection.execute(f'SELECT name,created_at,{payload_key} FROM {table} WHERE id=?',(row['id'],)).fetchone()
            if found:
                if tuple(found)!=(row['name'],row['created_at'],row[payload_key]):
                    raise ValueError('既有同ID '+table+' 与发行方案冲突，未覆盖')
            else:
                changed=True
        existing_product=connection.execute('SELECT name,created_at,manifest FROM input_products WHERE id=?',(business['inputId'],)).fetchone()
        if existing_product:
            if (existing_product[0]!=product['name'] or existing_product[1]!=product['created_at'] or
                    json.loads(existing_product[2])!=manifest):
                raise ValueError('既有同ID输入登记与发行输入冲突，未覆盖')
        else:
            changed=True
        input_file=within(destination,manifest['data'])
        if destination.exists():
            existing_manifest=read_json(destination/'manifest.json')
            if (existing_manifest!=manifest or input_file.is_symlink() or not input_file.is_file() or
                    sha_file(input_file)!=manifest['dataSHA256']):
                raise ValueError('既有输入目录与发行输入冲突或损坏，未覆盖')
        else:
            changed=True
        current=connection.execute('SELECT value FROM settings WHERE key=?',('business-adopted-scheme-v1',)).fetchone()
        if current and json.loads(current[0])!=selection:
            current_selection=json.loads(current[0])
        else:
            current_selection=None
            if not current:changed=True
        current_forcing=connection.execute('SELECT value FROM settings WHERE key=?',('current-model-forcing',)).fetchone()
        preserve_forcing=False
        if current_forcing:
            try:
                current_input_id=json.loads(current_forcing[0]).get('inputId')
                if current_input_id==business['inputId']:
                    preserve_forcing=True
                elif current_input_id:
                    current_product_row=connection.execute('SELECT manifest FROM input_products WHERE id=?',
                                                           (current_input_id,)).fetchone()
                    current_manifest=json.loads(current_product_row[0]) if current_product_row else {}
                    update=current_manifest.get('precipitationCorrectionUpdate') or {}
                    preserve_forcing=update.get('method')=='automatic-warm-rain-reestimate-v1'
            except (TypeError,ValueError,AttributeError):
                preserve_forcing=False
        set_forcing=not preserve_forcing
        if set_forcing:changed=True
        if not changed:
            return {'status':'already-present','schemeId':business['schemeId'],'inputId':business['inputId'],
                    'adoptionPreserved':bool(current_selection),'changed':False}
        backup=workspace/'database'/('pre-business-default-upgrade-'+uuid.uuid4().hex[:12]+'.sqlite3')
        with sqlite3.connect(backup) as target:connection.backup(target)
        _,added_input=_copy_input(seed,workspace,business['inputId'],manifest)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('INSERT OR IGNORE INTO schemes VALUES(?,?,?,?)',
                               (scheme['id'],scheme['name'],scheme['created_at'],scheme['payload']))
            connection.execute('INSERT OR IGNORE INTO parameter_versions VALUES(?,?,?,?)',
                               (parameter['id'],parameter['name'],parameter['created_at'],parameter['payload']))
            connection.execute('INSERT OR IGNORE INTO input_products VALUES(?,?,?,?)',
                               (product['id'],product['name'],product['created_at'],product['manifest']))
            if not current:
                connection.execute('INSERT INTO settings VALUES(?,?)',
                                   ('business-adopted-scheme-v1',canonical(selection)))
            if set_forcing:
                connection.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                                   ('current-model-forcing',canonical({'inputId':business['inputId'],
                                    'source':'packaged-business-default'})))
            connection.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',
                               ('scheme-purpose:'+selection['familyId'],canonical('business')))
            connection.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                               (now(),'workspace.business-default-supplement',canonical({
                                   'schemeId':business['schemeId'],'parameterId':business['parameterId'],
                                   'resultId':business['resultId'],'inputId':business['inputId'],
                                   'adoptionSet':not current,'previousAdoptionPreserved':bool(current_selection),
                                   'forcingSet':set_forcing,'previousForcingPreserved':preserve_forcing,
                                   'backup':str(backup)})))
    return {'status':'supplemented','schemeId':business['schemeId'],'inputId':business['inputId'],
            'adoptionPreserved':bool(current_selection),'currentForcingPreserved':preserve_forcing,
            'changed':True,'backup':str(backup),
            'inputCopied':added_input,'inputDirectory':str(destination)}


def prepare_package_workspace(workspace, seed=None, app_root=None):
    """Seed a fresh install or append the packaged business default on upgrade."""
    workspace=Path(workspace).resolve();seed=Path(seed or ROOT/'seed/workspace').resolve()
    app_root=Path(app_root or ROOT).resolve()
    packaged=_package_business(seed)
    if not packaged:return {'status':'no-business-default','changed':False}
    if workspace==seed or workspace in seed.parents:
        raise ValueError('工作区不能指向发行种子目录')
    database=workspace/'database/hydrology.sqlite3'
    if not workspace.exists():
        workspace.parent.mkdir(parents=True,exist_ok=True)
        shutil.copytree(seed,workspace)
        return {'status':'seeded','changed':True,'workspace':str(workspace)}
    contents=list(workspace.iterdir())
    if not contents:
        shutil.copytree(seed,workspace,dirs_exist_ok=True)
        return {'status':'seeded','changed':True,'workspace':str(workspace)}
    if not database.is_file():
        raise ValueError('目标工作区已有文件但无数据库，拒绝与发行种子混合')
    with idle_workspace(workspace):
        return supplement_business_default(workspace,seed,app_root)
