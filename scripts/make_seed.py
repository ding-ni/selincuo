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


def copy_observations(source, target, required_sources=None, include_all_sources=False):
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
        source_query = 'SELECT * FROM sources WHERE id IN (SELECT DISTINCT source_id FROM observations)'
        if include_all_sources:
            source_query = 'SELECT * FROM sources'
            extra = ''
            required_ids = []
        sources = [dict(row) for row in origin.execute(source_query+extra,required_ids)]
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


def business_default(source, scheme_id):
    """Resolve one already-adopted business result without changing its identity."""
    if scheme_id == 'current':
        selected=source.rows('SELECT value FROM settings WHERE key=?',('business-adopted-scheme-v1',))
        if not selected:raise ValueError('工作区没有当前采用方案，不能指定业务默认')
        scheme_id=json.loads(selected[0]['value']).get('schemeId')
    if not scheme_id:
        return None
    rows = source.rows('SELECT value FROM settings WHERE key=?', ('business-adopted-scheme-v1',))
    if not rows:
        raise ValueError('工作区没有当前采用方案，不能指定业务默认')
    selection = json.loads(rows[0]['value'])
    if selection.get('schemeId') != scheme_id:
        raise ValueError('指定业务方案不是当前采用方案')
    schemes = [row for row in source.items('schemes') if row['id'] == scheme_id]
    parameters = [row for row in source.items('parameter_versions') if row['id'] == selection.get('parameterId')]
    tasks = source.rows('SELECT id,status,request,result,trashed FROM tasks WHERE id=?', (selection.get('resultId'),))
    if len(schemes) != 1 or len(parameters) != 1 or len(tasks) != 1:
        raise ValueError('采用方案、参数或结果记录缺失')
    scheme, parameter, task = schemes[0], parameters[0], tasks[0]
    if task['status'] != 'succeeded' or task['trashed']:
        raise ValueError('采用结果未成功完成或已回收')
    result = json.loads(task['result'] or '{}')
    payload, parameter_payload = scheme['payload'], parameters[0]['payload']
    result_identity = result.get('identity') or {}
    input_id = result.get('inputId') or result_identity.get('inputId')
    if (not input_id or payload.get('inputId') != input_id or
            payload.get('spatialVersion') != result.get('spatialVersion') or
            payload.get('parameters') != result.get('parameters') or
            payload.get('partitionParameters', {}) != result.get('partitionParameters', {}) or
            parameter_payload.get('parameters') != result.get('parameters') or
            parameter_payload.get('partitionParameters', {}) != result.get('partitionParameters', {}) or
            parameter_payload.get('resultIdentitySHA256') not in (None, digest(result_identity)) or
            selection.get('inputId') not in (None, input_id) or
            selection.get('spatialVersion') not in (None, result.get('spatialVersion'))):
        raise ValueError('采用方案、参数与结果数值身份不一致')
    product = next((row for row in source.items('input_products') if row['id'] == input_id), None)
    if not product:
        raise ValueError('业务方案绑定的气象输入缺失或摘要不符')
    verify_business_forcing_binding(product['manifest'], result['spatialVersion'], result_identity)
    return {'selection': selection, 'scheme': scheme, 'parameter': parameter,
            'task': {**task, 'result': result, 'request': json.loads(task['request'])},
            'product': product, 'resultIdentitySHA256': digest(result_identity),
            'schemePayloadSHA256': digest(payload), 'parameterPayloadSHA256': digest(parameter_payload)}


def verify_business_forcing_binding(manifest, calculation_version, identity):
    """Verify the recorded forcing identity and aggregated spatial lineage."""
    if identity.get('inputSHA256') is not None:
        if identity['inputSHA256'] != manifest.get('dataSHA256'):
            raise ValueError('业务方案的气象输入摘要不同')
        if manifest.get('spatialVersion') != calculation_version:
            raise ValueError('业务方案的气象空间版本不同')
        return
    # Continuous results identify source grid and transformations; input file
    # bytes are independently checked against the manifest when seed is copied.
    from backend import grid_spatial
    source = grid_spatial.load(manifest['spatialVersion'])
    calculation = grid_spatial.load(calculation_version)
    grid_hash = digest(source['grids'])
    if (not identity.get('forcingGridSHA256') or
            identity['forcingGridSHA256'] != grid_hash or
            calculation.get('forcingGridVersion', digest(calculation['grids'])) != grid_hash or
            not identity.get('forcingTransformationSHA256') or
            identity['forcingTransformationSHA256'] != manifest.get('transformationSHA256') or
            digest(manifest.get('lakePrecipitationCorrection')) != manifest.get('transformationSHA256') or
            identity.get('landForcingTransformationSHA256') != manifest.get('landTransformationSHA256') or
            (manifest.get('landTransformationSHA256') is not None and
             digest(manifest.get('landPrecipitationCorrection')) != manifest.get('landTransformationSHA256'))):
        raise ValueError('业务方案气象格网或降水处理身份不一致')


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


def build_seed(source_workspace, output, standard_only=True, business_scheme_id=None):
    source = ReadOnlySource(source_workspace)
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('种子目标非空，请指定新的空目录，不覆盖既有资料')
    release, item, task, product = release_selection(source)
    selected_business = business_default(source, business_scheme_id)
    from backend.release_runtime import _packaged_observations
    expected_observation_sha=task['result'].get('identity',{}).get('observationSHA256')
    frozen_observation=_packaged_observations(task['id'],expected_observation_sha)
    required_sources=frozen_observation['sources'] if frozen_observation is not None else []
    target = Database(output)
    sources = copy_observations(source, target, required_sources, include_all_sources=bool(selected_business))
    products = [product] if standard_only else source.items('input_products')
    if selected_business:
        business_product = selected_business['product']
        by_id = {row['id']: row for row in products}
        by_id[business_product['id']] = business_product
        products = list(by_id.values())
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
    if selected_business:
        selection = selected_business['selection']
        with target.connect() as connection:
            connection.execute('INSERT INTO schemes VALUES(?,?,?,?)',
                               (selected_business['scheme']['id'], selected_business['scheme']['name'],
                                selected_business['scheme']['created_at'], canonical(selected_business['scheme']['payload'])))
            connection.execute('INSERT INTO parameter_versions VALUES(?,?,?,?)',
                               (selected_business['parameter']['id'], selected_business['parameter']['name'],
                                selected_business['parameter']['created_at'], canonical(selected_business['parameter']['payload'])))
            connection.execute('INSERT INTO settings VALUES(?,?)',
                               ('business-adopted-scheme-v1', canonical(selection)))
            connection.execute('INSERT INTO settings VALUES(?,?)',
                               ('current-model-forcing', canonical({'inputId':selected_business['product']['id'],
                                'source':'packaged-business-default'})))
            connection.execute('INSERT INTO settings VALUES(?,?)',
                               ('scheme-purpose:'+selection['familyId'], canonical('business')))
            connection.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                               (now(), 'business.package-seed', canonical({'schemeId': selection['schemeId'],
                                'parameterId': selection['parameterId'], 'resultId': selection['resultId']})))
    revision_id = seed_registry(source,target,release,task)
    from backend.release_registry import current
    if current(source)['id'] != release['id']:
        raise ValueError('生成期间默认发布变化，禁止激活本次种子')
    counts = target.summary()['counts']
    expected_business_count = 1 if selected_business else 0
    if (counts['tasks'] or counts['states'] or counts['parameter_versions'] != expected_business_count or
            len(target.items('schemes')) != expected_business_count):
        raise ValueError('种子含开发任务或研究方案')
    with target.connect() as connection:
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('种子数据库完整性检查失败')
    business_report = None
    if selected_business:
        selection = selected_business['selection']
        business_report = {'schemeId': selection['schemeId'], 'familyId': selection['familyId'],
                           'parameterId': selection['parameterId'], 'resultId': selection['resultId'],
                           'name': selection.get('name') or selected_business['scheme']['payload'].get('name'),
                           'inputId': selected_business['product']['id'],
                           'spatialVersion': selected_business['task']['result']['spatialVersion'],
                           'resultIdentitySHA256': selected_business['resultIdentitySHA256'],
                           'schemePayloadSHA256': selected_business['schemePayloadSHA256'],
                           'parameterPayloadSHA256': selected_business['parameterPayloadSHA256'],
                           'inputManifestSHA256': digest(selected_business['product']['manifest'])}
    write_json(target.workspace/'seed-report.json',{'schemaVersion':2,'createdAt':now(),'projectId':'selinco','counts':counts,
               'databaseSHA256':sha_file(target.path),'standardOnly':standard_only,'release':{**release,'schemeRevisionId':revision_id},
               'resultId':task['id'],'resultIdentity':task['result']['identity'],'resultFiles':item['files'],
               'defaultInputId':product['id'],'inputManifest':product['manifest'],'spatialVersion':task['result']['scheme']['spatialVersion'],
               'observationSources':sources,'observationSnapshotPurpose':'current-query-ledger; frozen-result-evaluation-unchanged',
               'frozenObservationSHA256':digest(frozen_observation) if frozen_observation is not None else None,
               'frozenObservationRequiredSourceCount':len(required_sources),'businessDefault':business_report,
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
    parser.add_argument('--business-scheme-id', help='将当前已采用的这一条业务方案、参数和驱动加入发行种子')
    args = parser.parse_args()
    if args.activate and (ROOT/'seed').resolve() not in args.output.resolve().parents:
        raise ValueError('激活种子必须位于本工程seed目录')
    output = build_seed(args.source_workspace,args.output,standard_only=args.standard_only,
                        business_scheme_id=args.business_scheme_id)
    if args.activate:
        write_json(ROOT / 'seed/current.json', {'directory': output.relative_to(ROOT / 'seed').as_posix()})
    print(output)


if __name__ == '__main__':
    main()
