"""Frozen-release request adapter, not a publication or task-dispatch entry point.

Only server-owned archived revisions are read. This module never looks up the
mutable research scheme table, invents scientific checks or spoofs model hashes.
Plan and submit use the same adapter; callers must keep Jobs' internal request
path separate from its user-controlled research-template preparation.
"""
from copy import deepcopy
from contextlib import ExitStack
from pathlib import Path
import json
import zipfile

import numpy as np
import pandas as pd

from .common import ROOT, CONTEXT_KEY, canonical, digest, model_identity, read_json, sha_file, within


def freeze_revision(db, task, runtime_evidence=None):
    """Build a complete immutable snapshot from an actual successful task.

    The returned snapshot is a proposal to persist, never a new release. Missing
    observations or runtime evidence are recorded and block new computation.
    """
    if task.get('status')!='succeeded' or not task.get('result'):
        raise ValueError('只能冻结成功完整成果')
    result=task['result'];request=task.get('request') or {}
    scheme=result.get('scheme') or request.get('schemeSnapshot')
    if not scheme or not result.get('identity') or not result.get('parameters'):
        raise ValueError('成果缺少方案、身份或参数')
    identifier=result.get('inputId') or request.get('inputId')
    product=next((row for row in db.items('input_products') if row['id']==identifier),None)
    if not product:raise ValueError('冻结成果输入不存在')
    retained=('projectId','contextKey','modelFamily','spatialVersion','inputId','parameters','partitionParameters',
              'routingMode','start','end','evaluationStart','evaluationEnd','schemeSnapshot','researchConfiguration',
              'periods','target','search','initialLakeLevel','initialLakeTime','initialLakeSource','_observations','_modelIdentity')
    frozen={key:deepcopy(request[key]) for key in retained if key in request}
    frozen.update(inputId=identifier,parameters=deepcopy(result['parameters']),
                  partitionParameters=deepcopy(result.get('partitionParameters',{})),
                  routingMode=result.get('routingMode') or request.get('routingMode'),
                  spatialVersion=result.get('spatialVersion') or scheme.get('spatialVersion'),
                  modelFamily=result.get('modelFamily') or request.get('modelFamily'),schemeSnapshot=deepcopy(scheme))
    initial=result.get('initialization') or scheme.get('initialization')
    snapshot={'schemaVersion':2,'scheme':deepcopy(scheme),'identity':deepcopy(result['identity']),
              'parameters':deepcopy(result['parameters']),'partitionParameters':deepcopy(result.get('partitionParameters',{})),
              'initialization':deepcopy(initial),'modelFamily':frozen.get('modelFamily') or 'legacy-v0.8',
              'routingMode':frozen['routingMode'],'frozenRequest':frozen,'inputManifest':deepcopy(product['manifest']),
              'inputRequirement':{'spatialVersion':product['manifest'].get('spatialVersion'),
                                  'transformationSHA256':product['manifest'].get('transformationSHA256')},
              'sourceResultId':task['id'],'sourceResultPayloadSHA256':digest(result),'sourceRequestSHA256':digest(request),
              'runtimeEvidence':deepcopy(runtime_evidence or {}),'status':'frozen-runtime-candidate','admissionPassed':False}
    snapshot['snapshotSHA256']=digest(snapshot)
    return snapshot


def _load(db, expected):
    from .release_registry import current
    from .business_context import result_task
    release=current(db)
    if not expected or release['id']!=expected:
        raise ValueError('默认发布已变化或未指定expectedReleaseId，请刷新运行计划')
    rows=db.rows('SELECT payload FROM scheme_revisions WHERE id=?',(release.get('schemeRevisionId'),))
    if not rows:raise ValueError('发布缺少不可变方案修订')
    snapshot=json.loads(rows[0]['payload'])
    task=result_task(db,release['resultId'])
    result=task.get('result') or {}
    if task.get('status')!='succeeded' or snapshot.get('identity')!=result.get('identity') or snapshot.get('scheme')!=result.get('scheme'):
        raise ValueError('发布方案与完整成果身份不一致')
    if snapshot.get('parameters')!=result.get('parameters') or snapshot.get('partitionParameters',{})!=result.get('partitionParameters',{}):
        raise ValueError('发布参数与实际成果不一致')
    if snapshot.get('sourceResultPayloadSHA256') and digest(result)!=snapshot['sourceResultPayloadSHA256']:
        raise ValueError('发布成果内容与冻结摘要不同')
    return release,snapshot,task


def _product(db, identifier):
    product=next((row for row in db.items('input_products') if row['id']==identifier),None)
    if not product:raise ValueError('发布指定输入不存在：'+str(identifier))
    directory=within(db.workspace/'inputs/products',identifier)
    manifest=read_json(directory/'manifest.json')
    if manifest!=product['manifest']:
        raise ValueError('输入数据库与磁盘清单不一致')
    path=within(directory,manifest['data'])
    if not path.is_file() or path.is_symlink() or sha_file(path)!=manifest.get('dataSHA256'):
        raise ValueError('输入文件缺失或摘要变化')
    return manifest,path


def verify_input_extension(base_manifest, base_path, manifest, path):
    """Compare exact historical NPZ prefixes with bounded 1MiB byte reads.

    The baseline contains already converted, frozen floating values. Matching
    parent names or transformation labels alone never certify a pure extension.
    """
    for key in ('spatialVersion','transformationSHA256','start','lakePrecipitationCorrection'):
        if manifest.get(key)!=base_manifest.get(key):raise ValueError('延伸输入身份不同：'+key)
    if pd.Timestamp(manifest['end'])<pd.Timestamp(base_manifest['end']):
        raise ValueError('延伸输入未覆盖原完整历史前缀')
    maximum=0
    with zipfile.ZipFile(base_path) as old,zipfile.ZipFile(path) as new:
        for key in ('time','precipitation','temperature','evaporation'):
            with old.open(key+'.npy') as left,new.open(key+'.npy') as right:
                def header(stream):
                    version=np.lib.format.read_magic(stream)
                    return np.lib.format._read_array_header(stream,version)
                shape,order,dtype=header(left);new_shape,new_order,new_dtype=header(right)
                if order or new_order or dtype.hasobject or dtype!=new_dtype or shape[1:]!=new_shape[1:] or shape[0]>new_shape[0]:
                    raise ValueError('延伸输入数组结构不同：'+key)
                remaining=int(np.prod(shape))*dtype.itemsize
                while remaining:
                    size=min(1024*1024,remaining);a,b=left.read(size),right.read(size);maximum=max(maximum,size)
                    if len(a)!=size or len(b)!=size or a!=b:raise ValueError('历史输入前缀数值变化：'+key)
                    remaining-=size
    return {'verified':True,'historicalPrefixEnd':base_manifest['end'],'maximumReadBytes':maximum,
            'baseInputSHA256':base_manifest['dataSHA256'],'inputSHA256':manifest['dataSHA256']}


def _period(query, result):
    start,end=pd.Timestamp(query['start']),pd.Timestamp(query['end'])
    if start.tzinfo or end.tzinfo or start!=start.normalize() or end!=end.normalize() or start>=end:
        raise ValueError('业务查看窗口须为北京时间完整日，结束使用次日排他边界')
    daily=result.get('daily') or []
    if not daily:raise ValueError('发布成果缺少展示覆盖')
    lo=pd.Timestamp(daily[0]['date']).normalize();hi=pd.Timestamp(daily[-1]['date']).normalize()+pd.Timedelta(days=1)
    return start,end,lo,hi


def _routing_mode(snapshot,result,frozen):
    modes=[value for value in (snapshot.get('routingMode'),result.get('routingMode'),
                               result.get('identity',{}).get('routingMode'),frozen.get('routingMode')) if value is not None]
    if not modes or len(set(modes))!=1:
        raise ValueError('冻结修订、成果身份与请求的汇流结构缺失或不一致')
    return modes[0]


def _packaged_observations(result_id, expected_sha256):
    """Load only the protected result's separately archived actual snapshot.

    An absent index or result entry preserves strict-current reconstruction.
    An indexed file that is missing, malformed or mismatched is an installation error,
    never an invitation to fall back to a different current observation ledger.
    The result identity remains the authority; this index cannot redefine it.
    """
    directory=ROOT/'config/operational'
    index=read_json(directory/'published-observations.json')
    if index is None:return None
    if (not isinstance(index,dict) or index.get('schemaVersion')!=1 or
            index.get('projectId')!='selinco' or not isinstance(index.get('snapshots'),dict)):
        raise ValueError('发行冻结观测索引结构或项目身份不正确')
    record=index['snapshots'].get(result_id)
    if record is None:return None
    if (not isinstance(record,dict) or record.get('observationSHA256')!=expected_sha256 or
            not isinstance(record.get('file'),str) or not record['file'] or
            not isinstance(record.get('fileSHA256'),str)):
        raise ValueError('发行冻结观测索引与原成果身份不一致')
    path=within(directory,record['file'])
    if not path.is_file():raise FileNotFoundError('已登记的发行冻结观测文件缺失')
    if sha_file(path)!=record['fileSHA256']:
        raise ValueError('发行冻结观测文件摘要不一致')
    observation=read_json(path)
    if (not isinstance(observation,dict) or
            any(not isinstance(observation.get(key),kind) for key,kind in
                (('sources',list),('flow',dict),('level',dict),('windows',list))) or
            digest(observation)!=expected_sha256):
        raise ValueError('发行冻结观测快照与原成果身份不一致')
    return observation


def adapt(db, query, require_compute=False):
    """One authoritative plan/submit preparation; no mutable scheme lookup."""
    release,snapshot,task=_load(db,query.get('expectedReleaseId'))
    result=task['result'];start,end,lo,hi=_period(query,result)
    default_input=result.get('inputId') or task.get('request',{}).get('inputId')
    identifier=query.get('inputId') or default_input
    covered=identifier==default_input and lo<=start and end<=hi
    plan={'releaseId':release['id'],'resultId':task['id'],'inputId':identifier,'action':'reuse' if covered else 'rerun',
          'requestedCoverage':{'start':start.isoformat(),'endExclusive':end.isoformat()},'createsTask':not covered,
          'reasons':['同一冻结发布成果已覆盖所选日期'] if covered else [],'preparedRequest':None}
    if covered and not require_compute:return plan
    frozen=deepcopy(snapshot.get('frozenRequest') or {})
    family=result.get('modelFamily') or frozen.get('modelFamily')
    legacy=family!='grid-continuous-v1'
    if not frozen and legacy:
        from .operational_scheme import prepare_request as legacy_prepare
        legacy_query={'schemeId':snapshot['scheme']['id'],'schemeRevision':snapshot['scheme']['revision'],
                      'inputId':identifier,'start':start.isoformat(),'end':end.isoformat()}
        frozen=legacy_prepare(db,legacy_query)
        if (frozen['schemeSnapshot']!=snapshot['scheme'] or frozen['parameters']!=snapshot['parameters'] or
            frozen.get('partitionParameters',{})!=snapshot.get('partitionParameters',{})):
            raise ValueError('内置计算定义与冻结发布修订不同，不能静默套用最新定义')
        archived_observation=None
        if snapshot.get('status')=='protected-legacy':
            archived_observation=_packaged_observations(task['id'],result['identity'].get('observationSHA256'))
        if archived_observation is not None:frozen['_observations']=archived_observation
        else:
            from .simulation import observation_snapshot
            frozen['_observations']=observation_snapshot(db)
    if not frozen:raise ValueError('发布修订缺少完整冻结请求，允许查看已有成果，禁止重新搭配运行')
    if snapshot.get('snapshotSHA256') and digest({k:v for k,v in snapshot.items() if k!='snapshotSHA256'})!=snapshot['snapshotSHA256']:
        raise ValueError('不可变修订摘要不同')
    evidence=snapshot.get('runtimeEvidence') or {}
    protected_legacy=legacy and snapshot.get('status')=='protected-legacy'
    if not protected_legacy and (evidence.get('fullArtifactsVerified') is not True or evidence.get('strictContinuationVerified') is not True):
        raise ValueError('发布缺少完整产物或严格续算的后端证据')
    observations=frozen.get('_observations')
    expected_observations=result['identity'].get('observationSnapshotSHA256') or result['identity'].get('observationSHA256')
    if not observations or digest(observations)!=expected_observations:
        raise ValueError('缺少与发布身份一致的冻结观测快照')
    if family=='grid-continuous-v1':
        from .grid_simulation import model_identity as implementation
    else:implementation=model_identity
    current_implementation=implementation()
    matching_model=current_implementation==result['identity'].get('modelImplementation')
    if not legacy and not matching_model:
        raise ValueError('数值实现已变化，不能伪造旧模型身份复算发布成果')
    baseline,baseline_path=_product(db,default_input)
    if snapshot.get('inputManifest') and snapshot['inputManifest']!=baseline:
        raise ValueError('发布冻结输入清单变化')
    if result['identity'].get('inputSHA256') and baseline['dataSHA256']!=result['identity']['inputSHA256']:
        raise ValueError('发布成果输入文件摘要不同')
    manifest,path=(baseline,baseline_path) if identifier==default_input else _product(db,identifier)
    prefix=None
    if identifier!=default_input:prefix=verify_input_extension(baseline,baseline_path,manifest,path)
    origin=pd.Timestamp(frozen['start'])
    if start<origin or end>pd.Timestamp(manifest['end']):raise ValueError('所选日期超出冻结起点或真实驱动覆盖')
    forbidden=('researchSchemeId','candidateParameterId','clientRequestId','_parentState')
    for key in forbidden:frozen.pop(key,None)
    frozen.update(projectId='selinco',contextKey=CONTEXT_KEY,kind='simulation',inputId=identifier,
                  parameters=deepcopy(snapshot['parameters']),partitionParameters=deepcopy(snapshot.get('partitionParameters',{})),
                  start=origin.isoformat(),end=end.isoformat(),evaluationStart=start.isoformat(),evaluationEnd=end.isoformat(),
                  schemeSnapshot=deepcopy(snapshot['scheme']),routingMode=_routing_mode(snapshot,result,frozen),
                  expectedReleaseId=release['id'],releaseRevisionId=release['schemeRevisionId'],origin='operational-release',
                  name=str(query.get('name') or '已发布方案历史模拟')[:120])
    frozen['_modelIdentity']=current_implementation
    if family=='grid-continuous-v1':
        if frozen.get('spatialVersion')!=result['spatialVersion'] or not frozen.get('researchConfiguration') or not frozen.get('periods'):
            raise ValueError('逐格网发布缺少冻结空间、研究配置或时段')
        if digest(snapshot.get('initialization'))!=result['identity'].get('initializationSHA256'):
            raise ValueError('逐格网湖位初态与发布身份不同')
        # The original observation snapshot reproduces the original automatic
        # anchor, preserving its method/source identity instead of relabelling
        # it as a new manual anchor.
        from .grid_simulation import anchor
        if anchor(frozen,observations,origin,end)!=snapshot['initialization']:
            raise ValueError('冻结请求不能精确复现湖位锚点')
        frozen['modelFamily']='grid-continuous-v1'
    plan.update(preparedRequest=frozen,inputCompatibility=prefix or {'verified':True,'sameFrozenInput':True},
                calculationStart=origin.isoformat(),matchingFrozenObservation=True,matchingFrozenModelImplementation=matching_model,
                resultIdentityExpectedToMatch=matching_model and identifier==default_input,
                runtimeEvidenceStatus='protected-legacy-existing-workflow' if protected_legacy else 'archived-runtime-evidence',
                reasons=['从不可变发布修订与核对一致的观测快照重新计算；不读取可变研究模板']+
                        ([] if matching_model else ['当前实现身份已变化，结果保留真实新身份；不声称与旧冻结模型身份相同']))
    return plan


def simulation_plan(db, query):
    return {key:value for key,value in adapt(db,query).items() if key!='preparedRequest'}


def prepare_request(db, query):
    return adapt(db,query,require_compute=True)['preparedRequest']
