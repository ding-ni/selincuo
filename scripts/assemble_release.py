import sys
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import VERSION, write_json, sha_file, now, read_json, within, digest
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
        if any(counts[k] for k in ('tasks','states','parameter_versions','schemes')) or not counts['input_products']:
            raise ValueError('交付种子含开发任务、状态或人工方案，或缺少真实输入')
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
    if len(products)!=1 or products[0]['id']!=identifier or report.get('defaultInputId')!=identifier:
        raise ValueError('发行种子必须仅包含当前发布成果实际使用的冻结输入')
    product = products[0]
    scheme = {**result['scheme'],'inputId':identifier,'available':True,'release':release,'resultIdentity':result['identity']}
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
    """Only standard/reference data and the selected results' own spatial versions."""
    spatial = ROOT / 'data/derived/spatial'
    if read_json(spatial / 'manifest.json', {}).get('version') != scheme['spatialVersion']:
        raise ValueError('默认空间与发行标准方案不同，禁止混入研究空间')
    versions = {scheme['spatialVersion']}
    for item in published:
        record = read_json(ROOT / 'data/final-results' / item['directory'] / 'result.json', {})
        result = record.get('result',record)
        if result.get('spatialVersion'):
            versions.add(result['spatialVersion'])
    # Observation originals are already snapshotted in the seed. Keep source
    # spatial evidence but do not bundle unrelated raw or NetCDF downloads.
    files = [p for p in (ROOT / 'data/raw/spatial').rglob('*') if p.is_file()]
    files += [p for p in spatial.iterdir() if p.is_file() and p.name != 'grid-current.json']
    for version in sorted(versions):
        folder = within(spatial / 'versions', version)
        files += [p for p in folder.rglob('*') if p.is_file()]
    return sorted(set(files))

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path, default=ROOT / 'build/verified-dist/SelincoPlatform')
    args = parser.parse_args()
    dest = args.destination.resolve()
    if not dest.is_relative_to(ROOT / 'build'):
        raise ValueError('发布组装仅允许本项目build目录')
    if any((dest / name).exists() for name in ('backend','config','docs','scripts','seed','workspace')):
        raise ValueError('目标包含既有平台文件，请选择新构建目录，避免混入旧版内容')
    seed = within(ROOT / 'seed', read_json(ROOT / 'seed/current.json', {}).get('directory', 'workspace'))
    counts, scheme = inspect_seed(seed)
    for name in ['backend', 'frontend/dist']:
        origin = ROOT / name
        if origin.is_dir():
            shutil.copytree(origin, dest / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.nbi', '*.nbc', 'active-workspace.json'))
    from backend.published_results import catalog as published_catalog, result_file
    current_result_id = scheme['release']['resultId']
    published = [r for r in published_catalog() if r['id'] == current_result_id]
    if not published:
        raise ValueError('缺少选定的最终历史成果')
    resources = selected_resources(scheme, published)
    for source in resources:
        target = dest / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for item in published:
        for filename in item['files']:
            source = result_file(item['id'], filename)
            target = dest/'data/final-results'/item['directory']/filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    write_json(dest/'data/final-results/catalog.json', {'currentResultId':current_result_id, 'results':published})
    for name in ('现场使用手册-现行.md','整改执行台账-现行.md','E3同新域聚合顺序受控诊断-20261027.md','数据接入接口-v1.md','实现与来源说明.md','逐格网连续模拟修订与验收-20261026.md'):
        origin = ROOT / 'docs' / name
        if not origin.is_file():
            raise ValueError('缺少交付说明：'+name)
        (dest / 'docs').mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, dest / 'docs' / name)
    if (ROOT/'docs/images').is_dir():shutil.copytree(ROOT/'docs/images',dest/'docs/images',dirs_exist_ok=True)
    (dest / 'config').mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'config/platform.json', dest / 'config/platform.json')
    shutil.copy2(ROOT / 'config/station-locations.json', dest / 'config/station-locations.json')
    shutil.copy2(ROOT / 'config/station-datums.json', dest / 'config/station-datums.json')
    shutil.copy2(ROOT / 'config/observation-analysis.json', dest / 'config/observation-analysis.json')
    shutil.copytree(ROOT / 'config/operational', dest / 'config/operational')
    source_manifest = ROOT / 'docs/source-manifest.json'
    if source_manifest.is_file():
        shutil.copy2(source_manifest, dest / 'data/source-manifest.json')
    shutil.copytree(seed, dest / 'seed/workspace', dirs_exist_ok=True)
    seed_report = read_json(seed / 'seed-report.json', {})
    shutil.copy2(ROOT / 'README.md', dest / 'README.md')
    write_json(dest / 'release-info.json', {'projectId':'selinco','version':VERSION,
                'schemeId':scheme['id'],'schemeRevision':scheme['revision'],'spatialVersion':scheme['spatialVersion'],
                'release':scheme['release'],'defaultResultId':current_result_id,'defaultInputId':scheme['inputId'],
                'resultIdentity':scheme['resultIdentity'],'seedReport':seed_report,'verifiedSeedCounts':counts,
                'manual':'docs/现场使用手册-现行.md','scientificStatus':seed_report.get('scientificStatus'),
                'installationAcceptance':'pending-actual-installed-verification'})
    def included(path):
        relative = path.relative_to(dest)
        return relative.parts[0] not in ('workspace', 'restored-workspaces') and relative.as_posix() != 'config/active-workspace.json'
    write_json(dest/'source-manifest.json', {'schemaVersion':1,'createdAt':now(),'projectId':'selinco','version':VERSION,
                'releaseId':scheme['release']['id'],'defaultResultId':current_result_id,
                'files':[{'path':p.relative_to(ROOT).as_posix(),'sha256':sha_file(p)}
                         for folder in ('backend','scripts','frontend/src') for p in sorted((ROOT/folder).rglob('*'))
                         if p.is_file() and p.suffix in ('.py','.vue','.mjs','.js','.css') and '__pycache__' not in p.parts]})
    manifest = {'createdAt': now(), 'projectId': 'selinco','version':VERSION,'releaseId':scheme['release']['id'],
                'files': [{'path': p.relative_to(dest).as_posix(), 'bytes': p.stat().st_size, 'sha256': sha_file(p)}
                          for p in sorted(dest.rglob('*')) if p.is_file() and included(p)]}
    write_json(dest/'release-manifest.json',manifest)
    write_json(ROOT / 'releases/build-manifest.json',manifest)
    print(dest)

if __name__ == '__main__':
    main()
