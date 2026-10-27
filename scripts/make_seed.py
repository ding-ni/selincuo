"""Create a clean seed bound to the published result's frozen input."""
import sys
import shutil
import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import WORKSPACE, canonical, digest, now, read_json, write_json, sha_file, within
from backend.database import Database


class ReadOnlySource:
    """Opening a source must never migrate its DB or interrupt its jobs."""
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.path = self.workspace / 'database/hydrology.sqlite3'
        if not self.path.is_file():
            raise ValueError('源工作区数据库不存在')

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


def release_selection(source):
    from backend.release_registry import current
    from backend.published_results import catalog, detail
    release = current(source)
    item = next((row for row in catalog() if row['id'] == release['resultId']), None)
    if not item:
        raise ValueError('当前发布成果尚未归档到不可变发行目录，不能用研究任务代替')
    task = detail(item['id'])
    result = task['result']
    identifier = result.get('inputId') or result.get('identity', {}).get('inputId')
    product = next((row for row in source.items('input_products') if row['id'] == identifier), None)
    if not product:
        raise ValueError('缺少当前发布成果实际使用的冻结输入：'+str(identifier))
    if product['manifest'].get('spatialVersion') != result['scheme']['spatialVersion']:
        raise ValueError('发布成果与冻结输入的空间身份不同')
    expected = result.get('identity', {}).get('inputSHA256')
    if expected and product['manifest'].get('dataSHA256') != expected:
        raise ValueError('发布成果与输入数据摘要不同')
    return release, item, task, product


def copy_observations(source, target, required_sources=None):
    """Keep current query observations and published-snapshot source files.

    Frozen observations remain a separately verified resource. A source without
    query observations may still be required to reproduce that snapshot.
    """
    required={}
    for row in required_sources or []:
        identifier, expected=row.get('id'),row.get('sha256')
        if (not isinstance(identifier,str) or not identifier or not isinstance(expected,str) or
                not expected or identifier in required):
            raise ValueError('冻结观测来源清单缺少身份、摘要或含重复来源')
        required[identifier]=expected
    copied = []
    with source.connect() as origin, target.connect() as destination:
        origin.execute('BEGIN')
        required_ids=list(required)
        extra=' OR id IN ('+','.join('?' for _ in required_ids)+')' if required_ids else ''
        sources = [dict(row) for row in origin.execute('SELECT * FROM sources WHERE id IN (SELECT DISTINCT source_id FROM observations)'+extra,required_ids)]
        found={row['id']:row for row in sources}
        for identifier,expected in required.items():
            if identifier not in found or found[identifier]['sha256']!=expected:
                raise ValueError('冻结观测来源登记缺失或摘要不同：'+identifier)
        for row in sources:
            relative = json.loads(row['metadata']).get('archivedPath')
            if not relative:
                raise ValueError('观测来源缺少归档路径：'+row['id'])
            path = within(source.workspace, relative)
            if not path.is_file() or path.is_symlink() or sha_file(path) != row['sha256']:
                raise ValueError('观测来源缺失或摘要不同：'+row['id'])
            output = within(target.workspace, relative)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
            copied.append({'path':relative,'sha256':row['sha256'],'bytes':path.stat().st_size,'sourceId':row['id']})
        source_ids=[row['id'] for row in sources]
        slots=','.join('?' for _ in source_ids)
        for table, field in [('stations',None),('sources','id'),('observations',None),('coverage','source_id')]:
            condition=(' WHERE '+field+' IN ('+slots+')') if field and source_ids else ' WHERE 0' if field else ''
            cursor = origin.execute('SELECT * FROM '+table+condition,source_ids if field and source_ids else [])
            columns = [column[0] for column in cursor.description]
            sql = 'INSERT OR REPLACE INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')'
            while True:
                rows = cursor.fetchmany(5000)
                if not rows:
                    break
                destination.executemany(sql, [tuple(row) for row in rows])
    return copied


def seed_registry(source, target, release, task):
    from backend.release_registry import SCHEMA, PROTOCOL
    result = task['result']
    snapshot = {'scheme':result['scheme'],'identity':result['identity'],'parameters':result['parameters'],
                'partitionParameters':result.get('partitionParameters',{}),'initialization':result['scheme']['initialization'],
                'status':'protected-legacy','admissionPassed':False}
    revision_id = release.get('schemeRevisionId')
    if revision_id:
        rows = source.rows('SELECT payload FROM scheme_revisions WHERE id=?', (revision_id,))
        if not rows:
            raise ValueError('发布方案快照缺失')
        snapshot = json.loads(rows[0]['payload'])
        if snapshot.get('scheme') != result.get('scheme') or snapshot.get('identity') != result.get('identity'):
            raise ValueError('发布修订与冻结成果身份不同')
    else:
        revision_id = 'REV-'+digest(snapshot)[:24]
    evaluation_id = release.get('evaluationId')
    evidence = None
    if evaluation_id:
        rows = source.rows('SELECT payload FROM evaluation_records WHERE id=?', (evaluation_id,))
        if not rows:
            raise ValueError('发布评价证据缺失')
        evidence = json.loads(rows[0]['payload'])
    with target.connect() as connection:
        connection.executescript(SCHEMA)
        connection.execute('INSERT OR IGNORE INTO schema_version VALUES(2,?)', (now(),))
        connection.execute('INSERT INTO scheme_revisions VALUES(?,?)', (revision_id,canonical(snapshot)))
        connection.execute('INSERT INTO evaluation_protocols VALUES(?,?)', (PROTOCOL['id'],canonical(PROTOCOL)))
        if evidence:
            connection.execute('INSERT INTO evaluation_records VALUES(?,?)', (evaluation_id,canonical(evidence)))
        connection.execute('INSERT INTO operational_releases(id,previous_id,scheme_revision_id,result_id,evaluation_id,published_at,action) VALUES(?,NULL,?,?,?,?,?)',
                           (release['id'],revision_id,task['id'],evaluation_id,release.get('publishedAt') or now(),'protected-import' if snapshot.get('status')=='protected-legacy' else 'release-seed'))
        connection.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                           (now(),'release.seed',canonical({'releaseId':release['id'],'sourcePreviousReleaseId':release.get('previousReleaseId'),'historyIncluded':False})))
    return revision_id


def build_seed(source_workspace, output, standard_only=True):
    source = ReadOnlySource(source_workspace)
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('种子目标非空，请指定新的空目录，不覆盖既有资料')
    release, item, task, product = release_selection(source)
    from backend.release_runtime import _packaged_observations
    expected_observation_sha=task['result'].get('identity',{}).get('observationSHA256')
    frozen_observation=_packaged_observations(task['id'],expected_observation_sha)
    required_sources=frozen_observation['sources'] if frozen_observation is not None else []
    target = Database(output)
    sources = copy_observations(source, target,required_sources)
    products = [product] if standard_only else source.items('input_products')
    for selected in products:
        origin = within(source.workspace/'inputs/products',selected['id'])
        data = within(origin,selected['manifest']['data'])
        if not data.is_file() or data.is_symlink() or sha_file(data) != selected['manifest']['dataSHA256']:
            raise ValueError('输入数据缺失或摘要不同：'+selected['id'])
        if any(path.is_symlink() for path in origin.rglob('*')):
            raise ValueError('输入目录含链接')
        shutil.copytree(origin,within(target.workspace/'inputs/products',selected['id']))
        with target.connect() as connection:
            connection.execute('INSERT INTO input_products VALUES(?,?,?,?)',
                               (selected['id'],selected['name'],selected['created_at'],canonical(selected['manifest'])))
    revision_id = seed_registry(source,target,release,task)
    from backend.release_registry import current
    if current(source)['id'] != release['id']:
        raise ValueError('生成期间默认发布变化，禁止激活本次种子')
    counts = target.summary()['counts']
    if any(counts[name] for name in ('tasks','states','parameter_versions')) or target.items('schemes'):
        raise ValueError('种子含开发任务或研究方案')
    with target.connect() as connection:
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('种子数据库完整性检查失败')
    write_json(target.workspace/'seed-report.json',{'schemaVersion':2,'createdAt':now(),'projectId':'selinco','counts':counts,
               'databaseSHA256':sha_file(target.path),'standardOnly':standard_only,'release':{**release,'schemeRevisionId':revision_id},
               'resultId':task['id'],'resultIdentity':task['result']['identity'],'resultFiles':item['files'],
               'defaultInputId':product['id'],'inputManifest':product['manifest'],'spatialVersion':task['result']['scheme']['spatialVersion'],
               'observationSources':sources,'observationSnapshotPurpose':'current-query-ledger; frozen-result-evaluation-unchanged',
               'frozenObservationSHA256':digest(frozen_observation) if frozen_observation is not None else None,
               'frozenObservationRequiredSourceCount':len(required_sources),
               'publicNetcdfOriginalsIncluded':False,'researchTasksIncluded':False,
               'scientificStatus':'protected-legacy' if not release.get('admissionPassed') else 'archived-release-evidence'})
    return target.workspace


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'seed/workspace')
    parser.add_argument('--activate', action='store_true')
    parser.add_argument('--source-workspace', type=Path, default=WORKSPACE)
    parser.add_argument('--standard-only', action='store_true', default=True, help='仅复制当前发布成果冻结输入（默认行为）')
    args = parser.parse_args()
    if args.activate and (ROOT/'seed').resolve() not in args.output.resolve().parents:
        raise ValueError('激活种子必须位于本工程seed目录')
    output = build_seed(args.source_workspace,args.output,standard_only=args.standard_only)
    if args.activate:
        write_json(ROOT / 'seed/current.json', {'directory': output.relative_to(ROOT / 'seed').as_posix()})
    print(output)


if __name__ == '__main__':
    main()
