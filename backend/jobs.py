from __future__ import annotations

import json
import threading
import uuid
import time
from concurrent.futures import ThreadPoolExecutor

from .common import CONTEXT_KEY, ROOT, canonical, digest, model_identity, now, read_json, write_json
from .model_core import parameters
from .simulation import observation_snapshot, run_simulation


class Jobs:
    def __init__(self, database):
        self.db = database
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='selinco-model')
        self.cancelled = set()
        self.lock = threading.RLock()
        self.restore_pending = False
        self.restart_required = False
        with self.db.connect() as db:
            db.execute("UPDATE tasks SET status='interrupted',message='服务重启，任务已中断；可恢复原条件重新计算',updated_at=? WHERE status IN ('queued','running','stopping')", (now(),))

    def active(self):
        return self.db.rows("SELECT id,kind,status,progress,message FROM tasks WHERE status IN ('queued','running','stopping')")

    def submit(self, kind, payload):
        with self.lock:
            request_id = payload.get('clientRequestId')
            submission = None
            if request_id is not None:
                if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
                    raise ValueError('计算请求编号无效')
                fingerprint = digest({'kind': kind, 'payload': {k: v for k, v in payload.items() if not k.startswith('_')}})
                key = 'task-request:' + request_id
                previous = self.db.setting(key)
                if previous:
                    if previous['fingerprint'] != fingerprint:
                        raise ValueError('此请求编号已用于不同计算条件，请重新提交')
                    return self.public(self.db.task(previous['taskId']))
                submission = (key, fingerprint)
            return self._submit(kind, payload, submission)

    def _submit(self, kind, payload, submission=None):
        if self.restore_pending or self.restart_required:
            raise ValueError('恢复已排队或完成，暂停接收新任务；请等待恢复结束并重启后继续')
        if kind not in ('simulation', 'trial', 'continuation', 'calibration', 'calibration-workflow', 'sensitivity', 'import-observations', 'build-spatial', 'build-grid-spatial', 'register-inputs', 'download-forcing', 'update-forcing', 'export-result', 'backup', 'restore-workspace', 'evaluation'):
            raise ValueError('未知任务类型')
        payload = {k: v for k, v in dict(payload).items() if not k.startswith('_')}
        if payload.get('projectId', 'selinco') != 'selinco' or payload.get('contextKey', CONTEXT_KEY) != CONTEXT_KEY:
            raise ValueError('拒绝其他项目的计算上下文')
        payload['projectId'], payload['contextKey'] = 'selinco', CONTEXT_KEY
        release_bound=kind=='simulation' and bool(payload.get('expectedReleaseId'))
        if release_bound:
            # This is an internal server-owned request. Do not send it through
            # mutable research template preparation or recapture observations.
            from .release_runtime import prepare_request
            client_id=payload.get('clientRequestId')
            payload=prepare_request(self.db,payload)
            if client_id is not None:payload['clientRequestId']=client_id
        if kind=='evaluation':
            allowed={'projectId','contextKey','name','clientRequestId','protocolId','baselineReleaseId','baselineResultId','candidateResultId'}
            if set(payload)-allowed:
                raise ValueError('评价任务只接受协议、成果身份和任务名称；禁止提交分数、checks或诊断结论')
        if kind in ('export-result','backup','restore-workspace','evaluation'):
            # These tasks never enter numerical request preparation.
            payload={k:v for k,v in payload.items() if k in ('projectId','contextKey','name','clientRequestId','taskId','format','filename','backupTaskId','token','protocolId','baselineReleaseId','baselineResultId','candidateResultId')}
        if kind=='restore-workspace':
            from .maintenance import restore_request_preview
            preview=restore_request_preview(self.db,payload)
            if payload.get('token')!=preview['token']:
                raise ValueError('备份预览已变化，请重新读取后恢复')
        if kind=='export-result':
            from .business_context import result_task
            from .maintenance import artifact_file
            source=result_task(self.db,payload.get('taskId'))
            if source.get('status')!='succeeded' or not source.get('result') or source.get('trashed'):
                raise ValueError('只能导出完整成功成果')
            payload['format']=payload.get('format','hourly.npz')
            if payload['format'] not in ('daily.csv','checkpoint.json','result.json','hourly.npz','request.json'):
                raise ValueError('不支持的导出类型')
            payload['_artifactKey']=digest({'taskId':source['id'],'format':payload['format'],
                                           'identity':source['result'].get('identity'),
                                           'hourlyArtifact':source['result'].get('hourlyArtifact')})
            previous=self.db.rows("SELECT id,status FROM tasks WHERE kind='export-result' AND json_extract(request,'$._artifactKey')=? ORDER BY created_at DESC",(payload['_artifactKey'],))
            for row in previous:
                prior=self.db.task(row['id'])
                if row['status'] in ('queued','running'):return self.public(prior)
                if row['status']=='succeeded':
                    try:
                        for artifact in prior['result']['artifacts']:artifact_file(self.db,prior,artifact['name'])
                    except (OSError,ValueError,KeyError):continue
                    return self.public(prior)
        research = bool(payload.get('researchSchemeId'))
        if research and not release_bound:
            if kind not in ('simulation', 'calibration', 'trial') or payload.get('schemeId'):
                raise ValueError('我的方案仅用于独立模拟、人工试调或率定')
            if kind=='calibration' and payload.get('candidateParameterId'):
                raise ValueError('请将候选参数保存为方案新修订后再率定')
            from .research_schemes import prepare_request
            payload = prepare_request(self.db, payload)
        elif not release_bound and payload.get('modelFamily') == 'grid-continuous-v1' and kind != 'continuation':
            raise ValueError('请先保存研究方案修订，再提交计算')
        standard = kind == 'simulation' and (release_bound or bool(payload.get('schemeId')))
        if standard and not release_bound:
            from .operational_scheme import prepare_request
            payload = prepare_request(self.db, payload)
        if kind == 'calibration-workflow':
            from .calibration_workflow import prepare
            payload = prepare(self.db, payload)
        identifier = 'JOB-' + uuid.uuid4().hex[:20]
        folder = self.db.workspace / 'tasks' / identifier
        if kind in ('simulation', 'trial', 'continuation', 'calibration', 'calibration-workflow', 'sensitivity'):
            normalized_parameters=parameters(payload.get('parameters'))
            if release_bound and normalized_parameters!=payload.get('parameters'):
                raise ValueError('发布参数不能被当前默认参数补全或改写')
            payload['parameters'] = normalized_parameters
            products = self.db.rows('SELECT id FROM input_products WHERE id=?', (payload.get('inputId'),))
            if not products:
                raise ValueError('请先登记真实ERA5-Land输入，当前不能开展历史计算')
            from .forcing_latest import provenance_summary
            product=next(r for r in self.db.items('input_products') if r['id']==payload['inputId'])
            payload['inputProvenance']=provenance_summary(self.db,product['manifest'])
            if release_bound:
                if not payload.get('_observations') or not payload.get('_modelIdentity'):
                    raise ValueError('发布请求缺少服务端冻结观测或真实模型身份')
            elif research:
                from .grid_simulation import observations, model_identity as grid_identity
                payload['_observations'] = observations(self.db)
                payload['_observations']['sourceDigests']=self.db.rows("SELECT DISTINCT s.id,s.sha256 FROM sources s JOIN observations o ON o.source_id=s.id WHERE s.rowid IN (SELECT MAX(rowid) FROM sources GROUP BY path) AND ((o.station_id='SLC' AND o.variable='level') OR (o.station_id='DUJA' AND o.variable='flow')) ORDER BY s.id")
                payload['_modelIdentity'] = grid_identity()
            else:
                payload['_observations'] = observation_snapshot(self.db)
                payload['_modelIdentity'] = model_identity()
        if kind == 'calibration' and not research and payload.get('stage') in ('lake', 'routing'):
            if not payload.get('baseTaskId'):
                raise ValueError('汇流或湖泊自动率定须选择成功基准成果，以冻结其余参数')
            base = self.db.task(payload['baseTaskId'])
            if base['status'] != 'succeeded' or not base['result'] or base['request'].get('inputId') != payload.get('inputId'):
                raise ValueError('率定基准须为同一输入版本的成功成果')
            current_spatial = read_json(ROOT / 'data/derived/spatial/manifest.json')['version']
            if base['result']['identity']['modelImplementation'] != model_identity() or base['result']['spatialVersion'] != current_spatial:
                raise ValueError('基准成果的模型或空间版本已变化，请重新计算基准')
            payload['parameters'] = dict(base['result']['parameters'])
            payload.pop('partitionParameters', None)
            if base['result'].get('partitionParameters'):
                payload['partitionParameters'] = dict(base['result']['partitionParameters'])
                if payload.get('stage') == 'routing':
                    payload['calibrationPartition'] = 'duja'
            payload['routingMode'] = base['result'].get('routingMode', base['request'].get('routingMode', 'source-separated'))
            payload['start'], payload['end'] = base['request']['start'], base['request']['end']
            payload['_observations'] = base['request']['_observations']
        if kind == 'sensitivity':
            import pandas as pd
            if pd.Timestamp(payload.get('start') or '2100') > pd.Timestamp('2023-01-01') or pd.Timestamp(payload.get('end') or '1900') < pd.Timestamp('2025-01-01'):
                raise ValueError('敏感性实验需要完整2023预热和2024评价时段')
        if kind == 'continuation':
            states = self.db.rows('SELECT * FROM states WHERE id=?', (payload.get('stateId'),))
            if not states:
                raise ValueError('父状态不存在')
            parent_record = states[0]
            parent_task = self.db.task(parent_record['task_id'])
            if parent_task['status'] != 'succeeded':
                raise ValueError('父状态的生产任务不是成功任务')
            checkpoint = json.loads(parent_record['payload'])
            if digest(checkpoint) != parent_task['result'].get('checkpointSHA256'):
                raise ValueError('父状态与成功成果摘要不一致')
            payload['_parentState'] = checkpoint
            if checkpoint.get('schemaVersion') == 3:
                from .grid_simulation import model_identity as grid_identity
                payload['modelFamily'] = 'grid-continuous-v1'
                payload['spatialVersion'] = checkpoint['identity']['spatialVersion']
                payload['_modelIdentity'] = grid_identity()
                payload['origin'] = 'user-research'
            if 'partitionParameters' not in payload and checkpoint.get('partitionParameters'):
                payload['partitionParameters'] = checkpoint['partitionParameters']
            payload['_observations'] = parent_task['request']['_observations']
            for field in ('schemeSnapshot','researchSchemeId','researchConfiguration','periods'):
                if field in parent_task['request']:payload[field]=parent_task['request'][field]
        folder.mkdir(parents=True, exist_ok=False)
        write_json(folder / 'request.json', payload)
        with self.db.connect() as db:
            db.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,name,favorite,collection) VALUES(?,?,?,?,?,?,?,?,?)',
                       (identifier, kind, 'queued', now(), now(), canonical(payload), str(payload.get('name', ''))[:120],
                        int(standard), '历史模拟' if standard else ''))
            if submission:
                db.execute('INSERT INTO settings(key,value) VALUES(?,?)',
                           (submission[0], canonical({'fingerprint': submission[1], 'taskId': identifier})))
        self.db.log('task.submit', {'id': identifier, 'kind': kind})
        if kind=='restore-workspace':self.restore_pending=True
        self.executor.submit(self._execute, identifier)
        return self.public(self.db.task(identifier))

    @staticmethod
    def public(task):
        task = dict(task)
        task['heartbeatAt']=task.get('updated_at')
        task['phase']=task.get('message','').split(' · ')[0]
        task['termination']=({'cancelled':'user-stopped','interrupted':'service-restarted','failed':'failed','succeeded':'completed'}).get(task.get('status'))
        if isinstance(task.get('request'), dict):
            task['request'] = {k: v for k, v in task['request'].items() if not k.startswith('_')}
        if isinstance(task.get('result'), dict):
            task['result'] = {k: v for k, v in task['result'].items() if k != 'checkpoint'}
            from .result_evaluation import report
            evaluation=report(task)
            if evaluation:task['result']['periodEvaluation']=evaluation
        return task

    def stop(self, identifier):
        with self.lock:
            return self._stop(identifier)

    def _stop(self, identifier):
        task = self.db.task(identifier)
        if task['status'] not in ('queued', 'running', 'stopping'):
            return self.public(task)
        self.cancelled.add(identifier)
        with self.db.connect() as db:
            db.execute("UPDATE tasks SET status='stopping',message='正在停止，等待当前计算批次结束',updated_at=? WHERE id=?", (now(), identifier))
        return self.public(self.db.task(identifier))

    def _execute(self, identifier):
        folder = self.db.workspace / 'tasks' / identifier
        task = self.db.task(identifier)
        request = task['request']

        def is_cancelled():
            return identifier in self.cancelled

        last_progress=[0.0]
        heartbeat_stop=threading.Event()
        heartbeat_thread=None
        def heartbeat():
            while not heartbeat_stop.wait(2):
                try:
                    with self.db.connect() as db:
                        db.execute("UPDATE tasks SET updated_at=? WHERE id=? AND status IN ('running','stopping')",(now(),identifier))
                except Exception:
                    # The task itself will report durable errors. A transient
                    # heartbeat failure must not silently end the worker.
                    pass
        def progress(value, message):
            if is_cancelled():
                raise InterruptedError('用户停止任务')
            stamp=time.monotonic()
            if stamp-last_progress[0]<.25:return
            last_progress[0]=stamp
            with self.db.connect() as db:
                db.execute('UPDATE tasks SET progress=?,message=?,updated_at=? WHERE id=?',
                           (min(99.9, float(value)), message, now(), identifier))

        try:
            if is_cancelled():
                raise InterruptedError('用户停止任务')
            with self.db.connect() as db:
                db.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?", (now(), identifier))
            heartbeat_thread=threading.Thread(target=heartbeat,daemon=True,name='selinco-task-heartbeat')
            heartbeat_thread.start()
            kind = task['kind']
            if kind=='simulation' and request.get('expectedReleaseId'):
                from .release_registry import current
                if current(self.db)['id']!=request['expectedReleaseId']:
                    raise ValueError('排队期间默认发布已变化，请重新生成运行计划')
                if request.get('modelFamily')=='grid-continuous-v1':
                    from .grid_simulation import model_identity as implementation
                else:implementation=model_identity
                if implementation()!=request.get('_modelIdentity'):
                    raise ValueError('排队期间模型实现变化，拒绝使用旧冻结实现身份')
            if kind=='export-result':
                from .maintenance import export_result
                result=export_result(self.db,request,folder,progress,is_cancelled)
            elif kind=='evaluation':
                from .evaluation_archive import archive,REQUEST_KEYS
                from .common import within,sha_file
                from urllib.parse import urlencode
                result=archive(self.db,{k:request[k] for k in REQUEST_KEYS if k in request},folder,progress,is_cancelled)
                record=json.loads(self.db.rows('SELECT payload FROM evaluation_records WHERE id=?',(result['evaluationId'],))[0]['payload'])
                directory=within(self.db.workspace,record['archiveDirectory'])
                relative=directory.relative_to(self.db.workspace)
                if len(relative.parts)!=2 or relative.parts[0]!='tasks':raise ValueError('评价产物目录不是任务归档')
                result['artifacts']=[]
                for item in record['artifacts']:
                    path=within(directory,item['path'])
                    result['artifacts'].append({'name':item['path'],'filename':item['path'],'storage':'task',
                        'ownerTaskId':relative.parts[1],'bytes':path.stat().st_size,'sha256':sha_file(path),
                        'mtimeNs':path.stat().st_mtime_ns,'complete':True,
                        'downloadUrl':'/api/model/task-artifact?'+urlencode({'taskId':identifier,'name':item['path']})})
            elif kind=='restore-workspace':
                from .maintenance import restore_archive, restore_preview, restore_workspace
                archive=restore_archive(self.db,request)
                if digest(restore_preview(archive))!=request['token']:
                    raise ValueError('排队期间备份预览变化，未执行恢复')
                destination=ROOT/'restored-workspaces'/('restored-'+uuid.uuid4().hex[:12])
                result=restore_workspace(archive,destination,progress,is_cancelled)
                result.update(sourceWorkspace=str(self.db.workspace),message='恢复与全部校验完成；退出后重新启动使用恢复工作区，原工作区保留。')
            elif kind=='backup':
                from .maintenance import backup_workspace
                from urllib.parse import urlencode
                artifact=backup_workspace(self.db,progress,is_cancelled)
                artifact.update(name=artifact['filename'],storage='exports',ownerTaskId=identifier,
                                downloadUrl='/api/model/task-artifact?'+urlencode({'taskId':identifier,'name':artifact['filename']}))
                result={'artifacts':[artifact],'termination':'completed','files':artifact['files']}
            elif kind == 'update-forcing':
                from .forcing_latest import acquire_latest
                result = acquire_latest(self.db, progress, is_cancelled)
            elif kind == 'build-grid-spatial':
                from .grid_spatial import build
                result = build(progress)
            elif request.get('modelFamily') == 'grid-continuous-v1':
                if kind == 'calibration':
                    from .grid_calibration import run
                else:
                    from .grid_simulation import run
                result = run(self.db, request, folder, progress, is_cancelled)
            elif kind == 'download-forcing':
                from .acquisition import acquire
                result = acquire(self.db.workspace, direct=bool(request.get('direct')), progress=progress, cancelled=is_cancelled)
            elif kind == 'import-observations':
                from .observations import import_observations
                incoming = self.db.workspace / 'observation-incoming'
                result = import_observations(self.db, incoming if incoming.is_dir() else ROOT / 'data/raw/observations/slc', progress=progress)
            elif kind == 'build-spatial':
                from .spatial import build_spatial
                result = build_spatial(progress)
            elif kind == 'register-inputs':
                from .forcing import register_era5
                from .common import within
                roots = [self.db.workspace / 'downloads', self.db.workspace / 'incoming']
                paths = []
                for root in roots:
                    paths += list(root.rglob('*.nc')) if root.is_dir() else []
                if request.get('files'):
                    allowed = {p.resolve() for p in paths}
                    paths = [within(self.db.workspace, name) for name in request['files']]
                    if any(p not in allowed for p in paths):
                        raise ValueError('只能登记工作区下载或上传目录中的NetCDF')
                result = register_era5(paths, self.db, name=request.get('name', 'ERA5-Land气象驱动'), progress=progress)
                if request.get('baseInputId'):
                    from .forcing_revisions import merge_products
                    result = merge_products(self.db,request['baseInputId'],result['id'])
                else:
                    from .forcing_extension import extend
                    result = extend(self.db, result)
                from .forcing import register_lake_correction
                from .operational_scheme import definition
                correction = definition().get('inputRequirement', {}).get('lakePrecipitationCorrection')
                if correction and not result.get('lakePrecipitationCorrection'):
                    result = register_lake_correction(self.db, result['id'], correction)
            elif kind == 'sensitivity':
                from .sensitivity import analyze
                result = analyze(self.db, request, folder, progress, is_cancelled)
            elif kind == 'calibration-workflow':
                from .calibration_workflow import run
                result = run(self.db, request, folder, progress, is_cancelled)
            elif kind == 'calibration':
                from .calibration import calibrate
                result = calibrate(self.db, request, folder, progress, is_cancelled)
            else:
                result = run_simulation(self.db, request, folder, progress, is_cancelled)
            if is_cancelled():
                raise InterruptedError('用户停止任务')
            result['taskId'] = identifier
            if kind=='restore-workspace':
                # No later task may write the old workspace after the pointer
                # is committed. Stop requests serialize with this final step.
                with self.lock:
                    if is_cancelled():raise InterruptedError('用户停止恢复；原工作区未切换')
                    active=ROOT/'config/active-workspace.json'
                    previous=read_json(active)
                    switched=False
                    write_json(folder/'result.json',result)
                    try:
                        with self.db.connect() as connection:
                            connection.execute("UPDATE tasks SET status='succeeded',result=?,progress=100,message='恢复完成，请重启平台',updated_at=? WHERE id=?",(canonical(result),now(),identifier))
                            connection.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',(now(),'task.restore-succeeded',canonical({'id':identifier,'workspace':result['workspace']})))
                            write_json(active,{'workspace':__import__('pathlib').Path(result['workspace']).relative_to(ROOT).as_posix()})
                            switched=True
                    except Exception:
                        if switched:
                            if previous is None:active.unlink(missing_ok=True)
                            else:write_json(active,previous)
                        raise
                    self.restart_required=True
                return
            if result.get('checkpoint'):
                result['checkpointSHA256'] = digest(result['checkpoint'])
            write_json(folder / 'result.json', result)
            with self.db.connect() as db:
                if result.get('checkpoint'):
                    state_id = 'STATE-' + identifier[4:]
                    db.execute('INSERT INTO states VALUES(?,?,?,?,?,?,?)',
                               (state_id, identifier, now(), result['checkpoint']['validAt'], canonical(result['checkpoint']), '', 0))
                    result['stateId'] = state_id
                db.execute("UPDATE tasks SET status='succeeded',result=?,progress=100,message='完成',updated_at=? WHERE id=?", (canonical(result), now(), identifier))
            write_json(folder / 'result.json', result)
            self.db.log('task.succeeded', {'id': identifier})
        except InterruptedError as exc:
            trace=read_json(folder/'search-progress.json')
            if trace is not None:
                write_json(folder/'search-progress.json',{**trace,'termination':'user-stopped','stoppedAt':now()})
            with self.db.connect() as db:
                db.execute("UPDATE tasks SET status='cancelled',message=?,updated_at=? WHERE id=?", (str(exc), now(), identifier))
        except Exception as exc:
            import traceback
            (folder / 'error.log').write_text(traceback.format_exc(), encoding='utf-8')
            with self.db.connect() as db:
                db.execute("UPDATE tasks SET status='failed',message=?,updated_at=? WHERE id=?", (str(exc)[:1500], now(), identifier))
        finally:
            heartbeat_stop.set()
            if heartbeat_thread:heartbeat_thread.join(timeout=3)
            self.cancelled.discard(identifier)
            if task['kind']=='restore-workspace':self.restore_pending=False

    def close(self):
        if self.active():
            raise ValueError('仍有排队、运行或停止中的任务，请先停止任务')
        self.executor.shutdown(wait=False)
