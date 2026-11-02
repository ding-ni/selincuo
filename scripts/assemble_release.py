import sys
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import VERSION, WORKSPACE, write_json, sha_file, now, read_json, within, digest
from backend.database import Database


class ReadOnlySeed:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.path = self.workspace / 'database/hydrology.sqlite3'

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    rows = Database.rows
    items = Database.items


def inspect_seed(seed):
    from backend.release_registry import current
    from backend.published_results import catalog, detail
    db = ReadOnlySeed(seed)
    identity = read_json(db.workspace / 'workspace-identity.json', {})
    if identity.get('projectId') != 'selinco':
        raise ValueError('交付种子不是色林错工作区')
    with db.connect() as connection:
        counts = {name:connection.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                  for name in ('tasks','states','parameter_versions','schemes','input_products','observations')}
        if counts['tasks'] or counts['states'] or not counts['input_products']:
            raise ValueError('交付种子含开发任务、状态，或缺少真实输入')
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('交付种子数据库完整性检查失败')
    release = current(db)
    selected = next((row for row in catalog() if row['id']==release['resultId']),None)
    if not selected:
        raise ValueError('种子的当前发布成果不在不可变发行目录')
    task = detail(release['resultId'])
    result = task['result']
    report = read_json(db.workspace/'seed-report.json',{})
    if (report.get('schemaVersion')!=2 or report.get('resultId')!=release['resultId'] or
        report.get('release',{}).get('id')!=release['id'] or report.get('resultIdentity')!=result.get('identity')):
        raise ValueError('种子缺少本轮发布绑定清单，或清单与冻结成果不一致')
    from backend.release_runtime import _packaged_observations
    frozen_observation=_packaged_observations(release['resultId'],result.get('identity',{}).get('observationSHA256'))
    if frozen_observation is not None:
        required=frozen_observation['sources']
        if (report.get('frozenObservationSHA256')!=digest(frozen_observation) or
                type(report.get('frozenObservationRequiredSourceCount')) is not int or
                report['frozenObservationRequiredSourceCount']!=len(required)):
            raise ValueError('种子冻结观测摘要或必需来源数量与发行快照不同')
        for source in required:
            rows=db.rows('SELECT sha256 FROM sources WHERE id=?',(source['id'],))
            if not rows or rows[0]['sha256']!=source['sha256']:
                raise ValueError('种子缺少冻结观测必需来源或摘要不同：'+source['id'])
    identifier = result.get('inputId') or result.get('identity',{}).get('inputId')
    products = db.items('input_products')
    release_product = next((row for row in products if row['id']==identifier),None)
    if not release_product or report.get('defaultInputId')!=identifier:
        raise ValueError('发行种子缺少当前发布成果实际使用的冻结输入')
    business_report = report.get('businessDefault')
    business = None
    expected_products = {identifier}
    if business_report:
        from backend.common import canonical
        selections = db.rows('SELECT value FROM settings WHERE key=?',('business-adopted-scheme-v1',))
        schemes = db.rows('SELECT id,name,created_at,payload FROM schemes')
        parameters = db.rows('SELECT id,name,created_at,payload FROM parameter_versions')
        if len(selections)!=1 or len(schemes)!=1 or len(parameters)!=1:
            raise ValueError('正式种子必须只有一套业务方案和参数，并有采用关系')
        selection = __import__('json').loads(selections[0]['value'])
        scheme_payload = __import__('json').loads(schemes[0]['payload'])
        parameter_payload = __import__('json').loads(parameters[0]['payload'])
        if (selection.get('schemeId')!=business_report.get('schemeId') or
                selection.get('parameterId')!=business_report.get('parameterId') or
                selection.get('resultId')!=business_report.get('resultId') or
                schemes[0]['id']!=business_report.get('schemeId') or
                parameters[0]['id']!=business_report.get('parameterId') or
                digest(scheme_payload)!=business_report.get('schemePayloadSHA256') or
                digest(parameter_payload)!=business_report.get('parameterPayloadSHA256') or
                scheme_payload.get('name')!=business_report.get('name') or
                scheme_payload.get('inputId')!=business_report.get('inputId') or
                scheme_payload.get('spatialVersion')!=business_report.get('spatialVersion')):
            raise ValueError('业务方案、参数与采用关系和冻结清单不一致')
        business_product=next((row for row in products if row['id']==business_report.get('inputId')),None)
        if not business_product or digest(business_product['manifest'])!=business_report.get('inputManifestSHA256'):
            raise ValueError('业务方案绑定的输入缺失或摘要不同')
        business_data=within(db.workspace/'inputs/products',business_product['id'])/business_product['manifest']['data']
        if (not business_data.is_file() or sha_file(business_data)!=business_product['manifest'].get('dataSHA256')):
            raise ValueError('业务方案绑定的输入文件缺失或摘要不符')
        if (selection.get('inputId') not in (None,business_report['inputId']) or
                selection.get('spatialVersion') not in (None,business_report['spatialVersion'])):
            raise ValueError('业务采用指针与输入/空间版本不一致')
        if business_report['resultId']==release['resultId']:
            raise ValueError('业务结果不能覆盖受保护的历史发布结果')
        if release.get('status')!='protected-legacy' or release.get('admissionPassed') is not False:
            raise ValueError('保护历史发布必须继续保持未通过科学准入的原状态')
        release_rows=db.rows('SELECT result_id,scheme_revision_id,evaluation_id FROM operational_releases WHERE id=?',
                             (release['id'],))
        if (len(release_rows)!=1 or release_rows[0]['result_id']!=release['resultId'] or
                release_rows[0]['scheme_revision_id']!=release.get('schemeRevisionId') or
                release_rows[0]['evaluation_id'] is not None):
            raise ValueError('种子保护发布的结果、修订或评价关系与冻结登记不符')
        release_revision=db.rows('SELECT payload FROM scheme_revisions WHERE id=?',(release_rows[0]['scheme_revision_id'],))
        if (len(release_revision)!=1 or __import__('json').loads(release_revision[0]['payload']).get('status')!='protected-legacy' or
                __import__('json').loads(release_revision[0]['payload']).get('admissionPassed') is not False):
            raise ValueError('保护发布修订已改变科学状态，禁止作为历史状态打包')
        expected_products.add(business_report['inputId'])
        business={**business_report,'payload':scheme_payload,'parameterPayload':parameter_payload,
                  'inputManifest':business_product['manifest']}
    elif counts['schemes'] or counts['parameter_versions']:
        raise ValueError('未登记业务默认的种子不能包含人工方案或参数')
    if {row['id'] for row in products}!=expected_products:
        raise ValueError('种子包含默认历史与正式业务方案之外的气象版本')
    product = release_product
    scheme = {**result['scheme'],'inputId':identifier,'available':True,'release':release,'resultIdentity':result['identity'],
              'businessDefault':business}
    if product['manifest']!=report.get('inputManifest') or product['manifest'].get('spatialVersion')!=scheme['spatialVersion']:
        raise ValueError('种子输入清单或空间身份不同')
    revisions = db.rows('SELECT payload FROM scheme_revisions WHERE id=?',(release.get('schemeRevisionId'),))
    if not revisions or __import__('json').loads(revisions[0]['payload']).get('identity')!=result['identity']:
        raise ValueError('发布修订与默认成果身份不一致')
    data = within(db.workspace / 'inputs/products', product['id']) / product['manifest']['data']
    if not data.is_file() or sha_file(data) != product['manifest']['dataSHA256']:
        raise ValueError('内置方案输入文件缺失或摘要不一致')
    for row in db.rows('SELECT sha256,metadata FROM sources'):
        metadata=__import__('json').loads(row['metadata'])
        raw=within(db.workspace,metadata.get('archivedPath',''))
        if not raw.is_file() or sha_file(raw)!=row['sha256']:
            raise ValueError('种子观测来源文件缺失或摘要不同')
    if any((db.workspace/name).exists() for name in ('tasks','states','downloads','incoming')):
        raise ValueError('发行种子目录含研究任务、下载或临时文件')
    return counts, scheme


def selected_resources(scheme, published):
    """Bundle the adopted spatial lineage, source boundaries and model DEM."""
    spatial = ROOT / 'data/derived/spatial'
    business = scheme.get('businessDefault') or {}
    selected_version = business.get('spatialVersion') or scheme['spatialVersion']
    versions = {selected_version}
    for item in published:
        version=item.get('spatialVersion')
        if not version:
            directory=item.get('directory')
            record=read_json(ROOT/'data/final-results'/directory/'result.json',{}) if directory else {}
            version=(record.get('result') or record).get('spatialVersion')
        if version:
            versions.add(version)
    files = [p for p in (spatial/'manifest.json', spatial/'grid-current.json') if p.is_file()]
    pending=list(sorted(versions));seen=set()
    while pending:
        version=pending.pop()
        if version in seen:
            continue
        if not isinstance(version,str) or not version:
            raise ValueError('空间父版本身份无效')
        seen.add(version)
        folder=within(spatial/'versions',version)
        manifest=read_json(folder/'manifest.json')
        if not isinstance(manifest,dict) or manifest.get('version')!=version:
            raise ValueError('缺少所选空间版本或版本清单不符：'+version)
        files += [p for p in folder.rglob('*') if p.is_file()]
        parent=manifest.get('parentSpatialVersion')
        if parent:
            pending.append(parent)

    if business:
        for dirname in ('full_basin_shp','subbasin_shp','lake_shp'):
            folder=ROOT/'data/raw/spatial/source-package'/dirname
            groups=list(folder.glob('*.shp')) if folder.is_dir() else []
            if not groups:
                raise ValueError('缺少原始流域/湖泊边界：'+dirname)
            files += [p for p in folder.iterdir() if p.is_file()]

        dem=ROOT/'slc_dem/slc_dem'
        required_dem={'dblbnd.adf','hdr.adf','prj.adf','sta.adf','vat.adf','w001001.adf','w001001x.adf'}
        if not dem.is_dir() or not required_dem.issubset({p.name for p in dem.iterdir() if p.is_file()}):
            raise ValueError('缺少原始90米DEM的完整ADF文件组')
        files += [p for p in dem.iterdir() if p.is_file()]
    return sorted(set(files))


def bundle_business_result(source_workspace, business, destination):
    """Freeze one adopted task as a curated static result, excluding its search history."""
    import json
    from backend.common import canonical
    from backend.grid_storage import FORMAT
    source=ReadOnlySeed(source_workspace)
    rows=source.rows('SELECT id,kind,status,created_at,updated_at,name,request,result,trashed FROM tasks WHERE id=?',
                     (business['resultId'],))
    if len(rows)!=1 or rows[0]['status']!='succeeded' or rows[0]['trashed']:
        raise ValueError('正式业务结果不存在、未成功或已回收')
    row=rows[0]
    result=json.loads(row['result'] or '{}')
    request=json.loads(row['request'])
    if (digest(result.get('identity') or {})!=business['resultIdentitySHA256'] or
            result.get('inputId')!=business['inputId'] or result.get('spatialVersion')!=business['spatialVersion'] or
            result.get('parameters')!=business['parameterPayload'].get('parameters') or
            result.get('partitionParameters',{})!=business['parameterPayload'].get('partitionParameters',{})):
        raise ValueError('正式业务结果与参数/驱动身份不一致')
    source_folder=within(source.workspace/'tasks',business['resultId'])
    task_file=read_json(source_folder/'result.json')
    if task_file!=result:
        raise ValueError('任务结果JSON与SQLite冻结结果不一致')
    zarr_folder=source_folder/'hourly.zarr'
    hourly_manifest=read_json(source_folder/'hourly-manifest.json')
    artifact=result.get('hourlyArtifact') or {}
    files=hourly_manifest.get('files') if isinstance(hourly_manifest,dict) else None
    if (result.get('storageFormat')!='zarr-v2' or not zarr_folder.is_dir() or
            hourly_manifest.get('format')!=FORMAT or hourly_manifest.get('complete') is not True or
            artifact.get('format')!=FORMAT or artifact.get('complete') is not True or
            not isinstance(files,dict) or not files or artifact.get('files')!=files or
            hourly_manifest.get('sha256')!=digest(files) or artifact.get('sha256')!=hourly_manifest['sha256']):
        raise ValueError('正式业务Zarr成果清单或数值身份不完整')
    for name, expected in files.items():
        if not isinstance(name,str) or not isinstance(expected,str):
            raise ValueError('正式业务Zarr清单含无效路径或摘要')
        path=within(zarr_folder,name)
        if not path.is_file() or path.is_symlink() or sha_file(path)!=expected:
            raise ValueError('正式业务Zarr分块缺失或摘要不同：'+name)
    for name in ('daily.csv','checkpoint.json'):
        path=source_folder/name
        if not path.is_file() or path.is_symlink():
            raise ValueError('正式业务结果缺少工件：'+name)
    checkpoint=source_folder/'checkpoint.json'
    expected_checkpoint=result.get('checkpointSHA256')
    if expected_checkpoint and digest(read_json(checkpoint))!=expected_checkpoint:
        raise ValueError('正式业务末状态摘要不一致')

    directory=business['resultId']
    target=within(destination/'data/final-results',directory)
    target.mkdir(parents=True)
    wrapper={'id':business['resultId'],'name':business['name'],'status':'succeeded','kind':row['kind'],
             'created_at':row['created_at'],'updated_at':row['updated_at'],'request':request,'result':result}
    write_json(target/'result.json',wrapper)
    shutil.copy2(source_folder/'daily.csv',target/'daily.csv')
    shutil.copy2(checkpoint,target/'final-state.json')
    shutil.copy2(source_folder/'hourly-manifest.json',target/'hourly-manifest.json')
    for name in sorted(files):
        source_file=within(zarr_folder,name)
        target_file=within(target/'hourly.zarr',name)
        target_file.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source_file,target_file)
    packaged_files={path.relative_to(target).as_posix():{'sha256':sha_file(path),'bytes':path.stat().st_size}
                    for path in sorted(target.rglob('*')) if path.is_file()}
    return {'id':business['resultId'],'name':business['name'],'period':(result.get('displayCoverage') or {}).get('start','')[:4]+'—'+(result.get('displayCoverage') or {}).get('endExclusive','')[:4],
            'directory':directory,'files':packaged_files,'displayName':business['name'],
            'spatialVersion':business['spatialVersion'],'inputId':business['inputId'],'businessDefault':True}

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path, default=ROOT / 'build/verified-dist/SelincoPlatform')
    parser.add_argument('--seed-workspace', type=Path,
                        help='本次独立构建生成的冻结种子；省略时沿用仓库当前种子')
    args = parser.parse_args()
    dest = args.destination.resolve()
    version_manifest=ROOT/('releases/build-manifest-v'+VERSION+'.json')
    if version_manifest.exists():
        raise ValueError('此发行版本已有构建清单，必须提升版本号或选择独立发布编号')
    if not dest.is_relative_to(ROOT / 'build'):
        raise ValueError('发布组装仅允许本项目build目录')
    if any((dest / name).exists() for name in ('backend','config','docs','scripts','seed','workspace')):
        raise ValueError('目标包含既有平台文件，请选择新构建目录，避免混入旧版内容')
    seed = (args.seed_workspace.resolve() if args.seed_workspace else
            within(ROOT / 'seed', read_json(ROOT / 'seed/current.json', {}).get('directory', 'workspace')))
    counts, scheme = inspect_seed(seed)
    for name in ['backend', 'frontend/dist']:
        origin = ROOT / name
        if origin.is_dir():
            shutil.copytree(origin, dest / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.nbi', '*.nbc', 'active-workspace.json'))
    from backend.published_results import catalog as published_catalog, result_file
    protected_result_id = scheme['release']['resultId']
    published = list(published_catalog())
    protected = [r for r in published if r['id']==protected_result_id]
    if not protected:
        raise ValueError('缺少选定的最终历史成果')
    published=protected[:]
    business = scheme.get('businessDefault')
    current_result_id=business.get('resultId') if business else protected_result_id
    business_result = None
    if business:
        business_result = bundle_business_result(WORKSPACE,business,dest)
        published = [row for row in published if row['id']!=business_result['id']]
        published.append(business_result)
    resources = selected_resources(scheme, published)
    for source in resources:
        target = dest / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for item in published:
        if item.get('businessDefault'):
            continue
        for filename in item['files']:
            source = result_file(item['id'], filename)
            target = dest/'data/final-results'/item['directory']/filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    write_json(dest/'data/final-results/catalog.json', {'currentResultId':protected_result_id, 'results':published})
    for name in ('现场使用手册-现行.md','整改执行台账-现行.md','E3同新域聚合顺序受控诊断-20261027.md','数据接入接口-v1.md','实现与来源说明.md','逐格网连续模拟修订与验收-20261026.md'):
        origin = ROOT / 'docs' / name
        if not origin.is_file():
            raise ValueError('缺少交付说明：'+name)
        (dest / 'docs').mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, dest / 'docs' / name)
    if (ROOT/'docs/images').is_dir():shutil.copytree(ROOT/'docs/images',dest/'docs/images',dirs_exist_ok=True)
    (dest / 'config').mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'config/platform.json', dest / 'config/platform.json')
    # The release has one clean business workspace.  Keep it separate from
    # any historical workspace left by an earlier installation.
    write_json(dest / 'config/active-workspace.json', {
        'workspace': 'workspace-business',
        'purpose': 'business-default',
        'seed': 'seed/workspace',
    })
    shutil.copy2(ROOT / 'config/station-locations.json', dest / 'config/station-locations.json')
    shutil.copy2(ROOT / 'config/station-datums.json', dest / 'config/station-datums.json')
    shutil.copy2(ROOT / 'config/observation-analysis.json', dest / 'config/observation-analysis.json')
    (dest/'config/operational').mkdir(parents=True,exist_ok=True)
    for name in ('historical-standard-v1.json','continuous-builtin-v1.json','published-observations.json'):
        source=ROOT/'config/operational'/name
        if not source.is_file():raise ValueError('缺少运行必需配置：'+name)
        shutil.copy2(source,dest/'config/operational'/name)
    if business:
        continuous_path=dest/'config/operational/continuous-builtin-v1.json'
        continuous=read_json(continuous_path,{}) or {}
        product_manifest=business['inputManifest']
        continuous['inputId']=business['inputId']
        continuous['spatialVersion']=business['spatialVersion']
        requirement=continuous.setdefault('inputRequirement',{})
        requirement['transformationSHA256']=product_manifest.get('transformationSHA256')
        if product_manifest.get('lakePrecipitationCorrection') is not None:
            requirement['lakePrecipitationCorrection']=product_manifest['lakePrecipitationCorrection']
        write_json(continuous_path,continuous)
    # A saved calibration references its historical comparison curve. Keep only
    # that immutable daily dependency, outside all user-facing result catalogs.
    if business:
        baseline_id=(business['payload'].get('twoStage') or {}).get('baselineTaskId')
        if baseline_id and baseline_id not in {row['id'] for row in published}:
            import json
            origin=ReadOnlySeed(WORKSPACE)
            rows=origin.rows('SELECT id,status,result FROM tasks WHERE id=?',(baseline_id,))
            if len(rows)!=1 or rows[0]['status']!='succeeded':
                raise ValueError('正式方案缺少原率定对照基准：'+baseline_id)
            original=json.loads(rows[0]['result'])
            if not original.get('daily'):
                raise ValueError('率定对照基准缺少日过程')
            baseline={'id':baseline_id,'status':'succeeded','kind':'calibration-comparison',
                      'result':{'identity':original['identity'],'daily':original['daily']}}
            relative='calibration-baselines/'+baseline_id+'.json'
            path=within(dest/'config/operational',relative)
            write_json(path,baseline)
            write_json(dest/'config/operational/calibration-baselines.json',
                       {'schemaVersion':1,'results':{baseline_id:{'file':relative,'sha256':sha_file(path)}}})
    observation_index=read_json(ROOT/'config/operational/published-observations.json',{}) or {}
    for snapshot in observation_index.get('snapshots',{}).values():
        relative=snapshot.get('file')
        if not relative:raise ValueError('历史发布观测快照登记缺少路径')
        source=within(ROOT/'config/operational',relative)
        target=within(dest/'config/operational',relative)
        if not source.is_file() or sha_file(source)!=snapshot.get('fileSHA256'):
            raise ValueError('历史发布观测快照缺失或摘要不符：'+relative)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,target)
    source_manifest = ROOT / 'docs/source-manifest.json'
    if source_manifest.is_file():
        shutil.copy2(source_manifest, dest / 'data/source-manifest.json')
    shutil.copytree(seed, dest / 'seed/workspace', dirs_exist_ok=True)
    seed_report = read_json(seed / 'seed-report.json', {})
    shutil.copy2(ROOT / 'README.md', dest / 'README.md')
    write_json(dest / 'release-info.json', {'projectId':'selinco','version':VERSION,
                'schemeId':business.get('schemeId') if business else scheme['id'],
                'schemeRevision':business.get('payload',{}).get('revision') if business else scheme['revision'],
                'spatialVersion':business.get('spatialVersion') if business else scheme['spatialVersion'],
                'release':scheme['release'],'standardResultId':protected_result_id,
                'defaultResultId':business.get('resultId') if business else protected_result_id,
                'defaultInputId':business.get('inputId') if business else scheme['inputId'],
                'businessDefault':business,
                'resultIdentity':scheme['resultIdentity'],'seedReport':seed_report,'verifiedSeedCounts':counts,
                'manual':'docs/现场使用手册-现行.md','scientificStatus':seed_report.get('scientificStatus'),
                'installationAcceptance':'pending-actual-installed-verification'})
    def included(path):
        relative = path.relative_to(dest)
        return relative.parts[0] not in ('workspace', 'workspace-business', 'restored-workspaces')
    write_json(dest/'source-manifest.json', {'schemaVersion':1,'createdAt':now(),'projectId':'selinco','version':VERSION,
                'releaseId':scheme['release']['id'],'defaultResultId':current_result_id,
                'files':[{'path':p.relative_to(ROOT).as_posix(),'sha256':sha_file(p)}
                         for folder in ('backend','scripts','frontend/src') for p in sorted((ROOT/folder).rglob('*'))
                         if p.is_file() and p.suffix in ('.py','.vue','.mjs','.js','.css') and '__pycache__' not in p.parts]})
    manifest = {'createdAt': now(), 'projectId': 'selinco','version':VERSION,'releaseId':scheme['release']['id'],
                'files': [{'path': p.relative_to(dest).as_posix(), 'bytes': p.stat().st_size, 'sha256': sha_file(p)}
                          for p in sorted(dest.rglob('*')) if p.is_file() and included(p)]}
    write_json(dest/'release-manifest.json',manifest)
    write_json(version_manifest,manifest)
    print(dest)

if __name__ == '__main__':
    main()
