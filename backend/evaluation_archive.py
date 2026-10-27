"""Server-computed, immutable comparison archives. Archiving never publishes."""
from datetime import datetime
import csv
import math
from pathlib import Path
import sys
from .common import ROOT,canonical,digest,now,read_json,sha_file,within,write_json
from .release_registry import PROTOCOL,current,admission
from .business_context import result_task

REQUEST_KEYS={'protocolId','baselineReleaseId','baselineResultId','candidateResultId'}
SCOPES={'lake':('lakeLevel','observedLevel'),'flow':('duja','observedFlow')}


def implementation_identity():
    source=ROOT/'backend/evaluation_archive.py' if getattr(sys,'frozen',False) else Path(__file__)
    if not source.is_file():raise FileNotFoundError('评价实现源码未随程序安装，不能冻结真实实现摘要')
    return sha_file(source)


def verify_input_binding(db,task,snapshot):
    """Verify input provenance without certifying numerical continuation."""
    result=task['result'];request=task.get('request') or {};manifest=snapshot['inputManifest']
    identifier=result.get('inputId');frozen=snapshot['frozenRequest'];identity=result['identity']
    if manifest.get('id')!=identifier or request.get('inputId')!=identifier or frozen.get('inputId')!=identifier:
        raise ValueError('实际请求、成果与冻结输入清单ID不一致')
    if result.get('modelFamily')!='grid-continuous-v1':
        if identity.get('inputId')!=identifier or identity.get('inputSHA256')!=manifest.get('dataSHA256'):
            raise ValueError('实际成果与冻结输入清单身份不一致')
        return {'method':'legacy-input-sha','inputSHA256':manifest['dataSHA256']}
    directory=within(db.workspace/'inputs/products',identifier)
    if read_json(directory/'manifest.json')!=manifest:raise ValueError('逐格网输入数据库与磁盘清单不一致')
    path=within(directory,manifest['data'])
    if not path.is_file() or sha_file(path)!=manifest.get('dataSHA256'):
        raise ValueError('逐格网实际输入文件摘要变化')
    checkpoint=result.get('checkpoint') or read_json(within(db.workspace/'tasks',task['id'])/'checkpoint.json')
    if (not checkpoint or digest(checkpoint)!=result.get('checkpointSHA256') or
            checkpoint.get('identity')!=identity or checkpoint.get('validAt')!=result.get('end') or
            checkpoint.get('inputId')!=identifier):
        raise ValueError('逐格网完整检查点摘要、时刻或身份不一致')
    if digest(request.get('_observations'))!=identity.get('observationSnapshotSHA256'):
        raise ValueError('逐格网冻结观测快照摘要不一致')
    from . import grid_simulation
    key=(str(db.workspace),identifier,result['spatialVersion'])
    # Input manifests are immutable in normal operation. Evict this one entry
    # before verification to avoid accepting stale cached arrays after edits.
    grid_simulation._cache.pop(key,None)
    try:
        source=grid_simulation.prepare(db,identifier,result['spatialVersion'])
        if (source['manifest']!=manifest or
            identity.get('forcingTransformationSHA256')!=manifest.get('transformationSHA256') or
            identity.get('forcingGridSHA256')!=source['spatial'].get('forcingGridVersion')):
            raise ValueError('逐格网气象转换或格网身份不一致')
        prefix=grid_simulation.prefix_identity(source,checkpoint['validAt'])
        if prefix!=checkpoint.get('forcingPrefixSHA256'):
            raise ValueError('逐格网历史气象前缀与冻结检查点不同')
        return {'method':'grid-checkpoint-input-prefix','inputSHA256':manifest['dataSHA256'],
                'checkpointSHA256':result['checkpointSHA256'],'forcingPrefixSHA256':prefix,
                'verifiedThrough':checkpoint['validAt'],
                'doesNotCertifyStrictContinuation':True}
    finally:grid_simulation._cache.pop(key,None)


def finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def metrics(pairs):
    n=len(pairs)
    if not n:return {'count':0,'rmse':None,'mae':None,'bias':None,'nse':None,'pbias':None}
    observations=[r['observed'] for r in pairs];simulations=[r['simulated'] for r in pairs]
    differences=[s-o for s,o in zip(simulations,observations)]
    mean=sum(observations)/n;denominator=sum((o-mean)**2 for o in observations)
    return {'count':n,'rmse':math.sqrt(sum(d*d for d in differences)/n),
            'mae':sum(abs(d) for d in differences)/n,'bias':sum(differences)/n,
            'nse':1-sum(d*d for d in differences)/denominator if n>=2 and denominator>0 else None,
            'pbias':100*sum(differences)/sum(observations) if sum(observations)!=0 else None}


def mode(result):
    initialization=result.get('initialization') or (result.get('scheme') or {}).get('initialization') or {}
    if initialization.get('lakeLevel')=='first-reliable-value-per-observation-window':return 'window'
    if (result.get('modelFamily')=='grid-continuous-v1' and initialization.get('time') and
        finite(initialization.get('level'))):return 'continuous'
    return 'unverified'


def quality(task):
    result=task.get('result') or {};request=task.get('request') or {}
    return result.get('observationProfile') or request.get('_observations',{}).get('profile') or 'frozen-legacy-unverified'


def identity(task):
    result=task['result'];request=task.get('request') or {}
    return {'resultId':task['id'],'resultIdentity':result.get('identity') or {},
            'resultIdentitySHA256':digest(result.get('identity') or {}),
            'resultPayloadSHA256':digest(result),'requestSHA256':digest(request),
            'scheme':result.get('scheme') or request.get('schemeSnapshot'),
            'spatialVersion':result.get('spatialVersion'), 'inputId':result.get('inputId'),
            'initialization':result.get('initialization') or (result.get('scheme') or {}).get('initialization'),
            'lakeMode':mode(result),'qualityProfile':quality(task),
            'observationSources':request.get('_observations',{}).get('sourceDigests',[])}


def frozen_days(task):
    output={}
    for row in task['result'].get('daily') or []:
        text=row.get('date')
        if not isinstance(text,str):raise ValueError('冻结成果daily缺少日期')
        day=datetime.fromisoformat(text.replace('Z','+00:00'))
        if day.tzinfo or any((day.hour,day.minute,day.second,day.microsecond)):
            raise ValueError('冻结日过程须为无时区北京时间日标签')
        key=day.date().isoformat()
        if key in output:raise ValueError('冻结日过程包含重复日期：'+key)
        output[key]=row
    return output


def initialization_exclusions(task):
    """Exclude fitted lake anchor days using frozen metadata, never daily fit."""
    result=task['result'];request=task.get('request') or {};lake_mode=mode(result)
    sources=[];values=[];reasons=[];window_dates={}
    if lake_mode=='continuous':
        initialization=result.get('initialization') or (result.get('scheme') or {}).get('initialization') or {}
        values=[initialization.get('time')];sources=['continuous-initialization.time']
    elif lake_mode=='window':
        for name,windows in [('result.diagnostics.lakeWindows',(result.get('diagnostics') or {}).get('lakeWindows')),
                             ('request._observations.windows',(request.get('_observations') or {}).get('windows'))]:
            if windows is not None:
                if not isinstance(windows,list):reasons.append('窗口初始化元数据不是列表：'+name);continue
                sources.append(name)
                values.extend(row.get('start') if isinstance(row,dict) else None for row in windows)
                declared=[]
                for row in windows:
                    try:
                        timestamp=datetime.fromisoformat(row['start'].replace('Z','+00:00'))
                        if timestamp.tzinfo:raise ValueError('timezone not frozen Beijing label')
                        declared.append(timestamp.date().isoformat())
                    except (ValueError,KeyError,TypeError,AttributeError):continue
                if declared:window_dates[name]=set(declared)
        # Requests may retain all historical windows while result diagnostics
        # describe only this calculation. Compare their relevant subsets.
        coverage_start=str(result.get('start') or '')[:10]
        coverage_end=str(result.get('end') or '')[:10]
        window_dates={name:{day for day in dates if (not coverage_start or day>=coverage_start) and
                           (not coverage_end or day<=coverage_end)} for name,dates in window_dates.items()}
        if len(window_dates)>1 and len({tuple(sorted(value)) for value in window_dates.values()})>1:
            reasons.append('冻结结果与请求的窗口初始化日期冲突，湖位样本不可核验')
        if not sources:
            from .release_runtime import _packaged_observations
            archived=_packaged_observations(task['id'],result.get('identity',{}).get('observationSHA256'))
            if archived is not None:
                sources.append('packaged-verified-observation.windows')
                values.extend(row.get('start') if isinstance(row,dict) else None for row in archived['windows'])
        if not values:reasons.append('窗口初始化缺少冻结窗口起点，湖位样本不可核验')
    else:reasons.append('湖位初始化口径未核验，湖位样本不可核验')
    dates=set()
    for value in values:
        try:
            if not isinstance(value,str):raise ValueError('missing timestamp')
            timestamp=datetime.fromisoformat(value.replace('Z','+00:00'))
            if timestamp.tzinfo:raise ValueError('timezone not frozen Beijing label')
            dates.add(timestamp.date().isoformat())
        except ValueError:reasons.append('冻结初始化起点无效：'+str(value))
    if lake_mode=='continuous' and not dates:reasons.append('连续初始化缺少有效锚点日')
    ordered=sorted(dates)
    return {'resultId':task['id'],'lakeMode':lake_mode,'sources':sources,'dates':ordered,
            'datesSHA256':digest(ordered),'metadataVerified':not reasons,'reasons':reasons}


def archive(db,request,folder,progress=None,cancelled=None):
    """Use from a heavy queue worker. Request contains IDs only, never scores.

    Returns task result with evaluationId. The registry receives backend-derived
    evidence and explicit unknown checks, so current admission always fails until
    separate, validated diagnostic artifact readers are implemented.
    """
    if set(request)-REQUEST_KEYS:raise ValueError('评价请求只接受协议、发布基准和两套成果ID；禁止提交分数或checks')
    if any(not isinstance(request.get(key),str) or not request[key] for key in REQUEST_KEYS):
        raise ValueError('评价请求必须完整指定四个身份字段')
    if request['protocolId']!=PROTOCOL['id']:raise ValueError('未知冻结评价协议')
    release=current(db)
    if request['baselineReleaseId']!=release['id']:raise ValueError('默认发布已变化，请按当前基准重新提交评价')
    protocol_rows=db.rows('SELECT payload FROM evaluation_protocols WHERE id=?',(request['protocolId'],))
    if not protocol_rows or __import__('json').loads(protocol_rows[0]['payload'])!=PROTOCOL:
        raise ValueError('数据库协议与当前评价实现不一致')
    tasks=[]
    for key in ('baselineResultId','candidateResultId'):
        task=result_task(db,request[key])
        if task.get('status')!='succeeded' or not task.get('result') or task.get('trashed'):
            raise ValueError('评价只能采用完整成功且未移入回收站的成果')
        tasks.append(task)
    baseline,candidate=tasks
    if baseline['id']==candidate['id']:raise ValueError('基准和候选不能为同一成果')
    identities=[identity(t) for t in tasks]
    revision_snapshot=None;revision_reasons=[];input_binding=None
    try:
        from .release_runtime import freeze_revision
        proposed=freeze_revision(db,candidate)
        frozen=proposed['frozenRequest'];manifest=proposed['inputManifest']
        missing=[key for key in ('start','end','spatialVersion','routingMode','parameters','inputId','schemeSnapshot',
                                 '_modelIdentity','_observations') if not frozen.get(key)]
        if missing:raise ValueError('实际冻结请求缺少必要字段：'+','.join(missing))
        input_binding=verify_input_binding(db,candidate,proposed)
        revision_snapshot=proposed
    except (ValueError,KeyError,FileNotFoundError) as exc:
        revision_reasons.append(str(exc))
    implementation_sha=implementation_identity()
    fingerprint=digest({'request':request,'identities':identities,'protocol':PROTOCOL,
                        'schemeRevisionSnapshotSHA256':digest(revision_snapshot) if revision_snapshot else None,
                        'implementation':'evaluation-archive-v1','implementationSHA256':implementation_sha})
    identifier='EVAL-'+fingerprint[:24]
    existing=db.rows('SELECT payload FROM evaluation_records WHERE id=?',(identifier,))
    if existing:
        value=__import__('json').loads(existing[0]['payload'])
        root=within(db.workspace,value['archiveDirectory'])
        for item in value['artifacts']:
            path=within(root,item['path'])
            if not path.is_file() or sha_file(path)!=item['sha256']:raise ValueError('既有评价归档文件不完整，拒绝静默覆盖')
        return {'evaluationId':identifier,'reused':True,'comparisonStatus':value['comparisonStatus'],
                'admission':value['admission'],'archiveDirectory':value['archiveDirectory']}
    if cancelled and cancelled():raise InterruptedError('用户停止评价')
    if progress:progress(5,'核验冻结成果与评价身份')
    reasons=[]
    if any(i['lakeMode']!='continuous' for i in identities):reasons.append('缺少两套已核验固定锚点连续成果；窗口初始化与连续评价不可直接排序')
    if all(i['lakeMode']=='continuous' for i in identities) and identities[0]['initialization']!=identities[1]['initialization']:
        reasons.append('两套连续成果的冻结湖位锚点或初始化条件不同')
    if any(i['qualityProfile']=='frozen-legacy-unverified' for i in identities) or identities[0]['qualityProfile']!=identities[1]['qualityProfile']:
        reasons.append('观测质量规则缺失或不一致')
    # Continuous reference must explicitly refer back to the protected release.
    # Its lineage is generated by a future backend baseline-reproduction task;
    # a free client flag or matching parameter name is not sufficient evidence.
    if baseline['id']!=release['resultId']:
        reasons.append('连续基准与当前默认发布的复现血缘尚无已核验后端产物，不能作为发布比较基准')
    days=[frozen_days(t) for t in tasks];pairs={};conflicts=[];years={};monthly=[]
    exclusions=[initialization_exclusions(task) for task in tasks]
    for exclusion in exclusions:
        reasons.extend(exclusion['resultId']+'：'+reason for reason in exclusion['reasons'])
    lake_metadata_verified=all(row['metadataVerified'] for row in exclusions)
    excluded_anchor_days={day for exclusion in exclusions for day in exclusion['dates']}
    common_days=set(days[0])&set(days[1])
    excluded_common_days=sorted(day for day in common_days&excluded_anchor_days if day[:4] in PROTOCOL['years'])
    for scope,(simulation,observation) in SCOPES.items():
        pairs[scope]=[]
        for day in sorted(set(days[0])&set(days[1])):
            if day[:4] not in PROTOCOL['years']:continue
            if scope=='lake' and (not lake_metadata_verified or day in excluded_anchor_days):continue
            b,c=days[0][day],days[1][day];bo,co=b.get(observation),c.get(observation)
            if finite(bo) and finite(co) and not math.isclose(bo,co,rel_tol=0,abs_tol=1e-9):
                conflicts.append({'scope':scope,'date':day,'baselineObserved':bo,'candidateObserved':co});continue
            if not all(finite(v) for v in (bo,co,b.get(simulation),c.get(simulation))):continue
            pairs[scope].append({'date':day,'observed':float(bo),'baseline':float(b[simulation]),'candidate':float(c[simulation])})
        for year in PROTOCOL['years']:
            selected=[r for r in pairs[scope] if r['date'].startswith(year)]
            years.setdefault(year,{})[scope]={'commonCount':len(selected),
                'baseline':metrics([{'observed':r['observed'],'simulated':r['baseline']} for r in selected]),
                'candidate':metrics([{'observed':r['observed'],'simulated':r['candidate']} for r in selected])}
        for month in sorted({r['date'][:7] for r in pairs[scope]}):
            rows=[r for r in pairs[scope] if r['date'].startswith(month)]
            monthly.append({'scope':scope,'month':month,'count':len(rows),
                            'baselineBias':sum(r['baseline']-r['observed'] for r in rows)/len(rows),
                            'candidateBias':sum(r['candidate']-r['observed'] for r in rows)/len(rows)})
    if conflicts:reasons.append('冻结观测值在共同日期存在冲突，不具备公平比较条件')
    if any(years[y][scope]['commonCount']<2 for y in PROTOCOL['years'] for scope in SCOPES):
        reasons.append('至少一主要年份或要素缺少两个合格共同日期')
    checks={key:False for key in ('initialStateStable','waterBalanceValid','parameterBoundsValid','inputProvenanceValid',
                                 'fullArtifactsVerified','strictContinuationVerified','limitationsArchived','commonDatesVerified')}
    checks['commonDatesVerified']=not reasons
    checks['limitationsArchived']=True
    evidence={'id':identifier,**request,'baselineIdentity':identities[0],'candidateIdentity':identities[1],
              'protocol':PROTOCOL,'protocolSHA256':digest(PROTOCOL),'implementation':'evaluation-archive-v1',
              'implementationSHA256':implementation_sha,
              'createdAt':now(),'comparisonStatus':'not-comparable' if reasons else 'comparable',
              'comparisonReasons':reasons,'lakeMode':'continuous' if all(i['lakeMode']=='continuous' for i in identities) else 'mixed-or-unverified',
              'years':years,'monthlyResiduals':monthly,'observationConflicts':conflicts,
              'initializationExclusions':{'rule':'exclude-union-of-frozen-lake-initialization-calendar-days-v1',
                  'perResult':exclusions,'excludedCommonDates':excluded_common_days,
                  'excludedCommonDatesSHA256':digest(excluded_common_days),
                  'lakeSamplesMetadataVerified':lake_metadata_verified},
              'commonDateSHA256':{scope:digest([r['date'] for r in rows]) for scope,rows in pairs.items()},
              'pairedDataSHA256':{scope:digest(rows) for scope,rows in pairs.items()},
              'checks':checks,'diagnostics':{key:('passed' if checks[key] else 'unknown') for key in checks},
              'stability':{},'resultId':candidate['id'],'resultIdentitySHA256':identities[1]['resultIdentitySHA256'],
              'schemeRevisionId':'REV-'+digest(revision_snapshot)[:24] if revision_snapshot else None,
              'schemeRevisionFreeze':{'status':'frozen' if revision_snapshot else 'incomplete',
                                      'reasons':revision_reasons,'inputBinding':input_binding},
              'limitations':['评价仅重算冻结daily共同样本，不代表完整逐时文件已校验。',
                             '初态、严格续算、水量和输入来源尚无正式诊断产物读入，均保留unknown。',
                             '2025及2026资料已经用于开发，不能称未触碰的外部验证。',
                             '归档操作不更新默认发布；任意修订业务派发仍未接通。'],
              'nextRequired':['核验当前发布的连续基准复现血缘','引入后端诊断产物读取与校验','冻结发布方案修订并接通业务派发'],
              'archiveComplete':False,'artifacts':[]}
    evidence['admission']=admission(evidence)
    folder=Path(folder).resolve()
    archive_directory=folder.relative_to(db.workspace.resolve()).as_posix()
    folder.mkdir(parents=True,exist_ok=True)
    for scope,rows in pairs.items():
        if cancelled and cancelled():raise InterruptedError('用户停止评价')
        with (folder/(scope+'-common-pairs.csv')).open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=['date','observed','baseline','candidate']);writer.writeheader();writer.writerows(rows)
    write_json(folder/'comparison-report.json',{k:v for k,v in evidence.items() if k not in ('artifacts','archiveComplete')})
    for filename in ('comparison-report.json','flow-common-pairs.csv','lake-common-pairs.csv'):
        path=folder/filename
        evidence['artifacts'].append({'path':path.name,'bytes':path.stat().st_size,'sha256':sha_file(path)})
    evidence['archiveDirectory']=archive_directory
    evidence['archiveComplete']=True
    if progress:progress(95,'归档共同日期、年度指标与不利证据')
    if cancelled and cancelled():raise InterruptedError('用户停止评价')
    # Workspace records are normally frozen; fail closed if an external edit or
    # future maintenance action changed the inputs while this task was working.
    if [identity(result_task(db,t['id'])) for t in tasks]!=identities:
        raise ValueError('评价期间冻结成果或请求发生变化，归档未提交')
    if revision_snapshot and freeze_revision(db,result_task(db,candidate['id']))!=revision_snapshot:
        raise ValueError('评价期间方案或输入清单发生变化，归档未提交')
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        latest=connection.execute('SELECT id FROM operational_releases ORDER BY sequence DESC LIMIT 1').fetchone()
        if not latest or latest[0]!=release['id']:raise ValueError('评价期间默认发布变化，归档未提交；请重新评价')
        if revision_snapshot:
            row=connection.execute('SELECT payload FROM scheme_revisions WHERE id=?',(evidence['schemeRevisionId'],)).fetchone()
            if row and row[0]!=canonical(revision_snapshot):raise ValueError('不可变方案修订摘要冲突，拒绝覆盖')
            connection.execute('INSERT OR IGNORE INTO scheme_revisions(id,payload) VALUES(?,?)',
                               (evidence['schemeRevisionId'],canonical(revision_snapshot)))
        connection.execute('INSERT INTO evaluation_records(id,payload) VALUES(?,?)',(identifier,canonical(evidence)))
        connection.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',
                           (now(),'evaluation.archive',canonical({'evaluationId':identifier,'comparisonStatus':evidence['comparisonStatus'],'admissionPassed':False})))
    return {'evaluationId':identifier,'reused':False,'comparisonStatus':evidence['comparisonStatus'],
            'admission':evidence['admission'],'archiveDirectory':evidence['archiveDirectory']}
