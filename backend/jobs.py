from __future__ import annotations

import json
import threading
import uuid
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
        with self.db.connect() as db:
            db.execute("UPDATE tasks SET status='interrupted',message='服务重启，任务已中断；可恢复原条件重新计算',updated_at=? WHERE status IN ('queued','running','stopping')", (now(),))

    def active(self):
        return self.db.rows("SELECT id,kind,status,progress,message FROM tasks WHERE status IN ('queued','running','stopping')")

    def submit(self, kind, payload):
        if kind not in ('simulation', 'trial', 'continuation', 'calibration', 'import-observations', 'build-spatial', 'register-inputs', 'download-forcing'):
            raise ValueError('未知任务类型')
        payload = {k: v for k, v in dict(payload).items() if not k.startswith('_')}
        if payload.get('projectId', 'selinco') != 'selinco' or payload.get('contextKey', CONTEXT_KEY) != CONTEXT_KEY:
            raise ValueError('拒绝其他项目的计算上下文')
        payload['projectId'], payload['contextKey'] = 'selinco', CONTEXT_KEY
        identifier = 'JOB-' + uuid.uuid4().hex[:20]
        folder = self.db.workspace / 'tasks' / identifier
        if kind in ('simulation', 'trial', 'continuation', 'calibration'):
            payload['parameters'] = parameters(payload.get('parameters'))
            products = self.db.rows('SELECT id FROM input_products WHERE id=?', (payload.get('inputId'),))
            if not products:
                raise ValueError('请先登记真实ERA5-Land输入，当前不能开展历史计算')
            payload['_observations'] = observation_snapshot(self.db)
            payload['_modelIdentity'] = model_identity()
        if kind == 'calibration' and payload.get('stage') == 'lake':
            if not payload.get('baseTaskId'):
                raise ValueError('湖泊自动率定须选择已完成的产汇流成果，以冻结其流域参数')
            base = self.db.task(payload['baseTaskId'])
            if base['status'] != 'succeeded' or not base['result'] or base['request'].get('inputId') != payload.get('inputId'):
                raise ValueError('湖泊率定基准须为同一输入版本的成功成果')
            current_spatial = read_json(ROOT / 'data/derived/spatial/manifest.json')['version']
            if base['result']['identity']['modelImplementation'] != model_identity() or base['result']['spatialVersion'] != current_spatial:
                raise ValueError('基准成果的模型或空间版本已变化，请重新计算基准')
            payload['parameters'] = dict(base['result']['parameters'])
            payload['start'], payload['end'] = base['request']['start'], base['request']['end']
            payload['_observations'] = base['request']['_observations']
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
            payload['_observations'] = parent_task['request']['_observations']
        folder.mkdir(parents=True, exist_ok=False)
        write_json(folder / 'request.json', payload)
        with self.db.connect() as db:
            db.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,name) VALUES(?,?,?,?,?,?,?)',
                       (identifier, kind, 'queued', now(), now(), canonical(payload), str(payload.get('name', ''))[:120]))
        self.db.log('task.submit', {'id': identifier, 'kind': kind})
        self.executor.submit(self._execute, identifier)
        return self.public(self.db.task(identifier))

    @staticmethod
    def public(task):
        task = dict(task)
        if isinstance(task.get('request'), dict):
            task['request'] = {k: v for k, v in task['request'].items() if not k.startswith('_')}
        if isinstance(task.get('result'), dict):
            task['result'] = {k: v for k, v in task['result'].items() if k != 'checkpoint'}
        return task

    def stop(self, identifier):
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

        def progress(value, message):
            if is_cancelled():
                raise InterruptedError('用户停止任务')
            with self.db.connect() as db:
                db.execute('UPDATE tasks SET progress=?,message=?,updated_at=? WHERE id=?',
                           (min(99.9, float(value)), message, now(), identifier))

        try:
            if is_cancelled():
                raise InterruptedError('用户停止任务')
            with self.db.connect() as db:
                db.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?", (now(), identifier))
            kind = task['kind']
            if kind == 'download-forcing':
                from .acquisition import acquire
                result = acquire(self.db.workspace, direct=bool(request.get('direct')), progress=progress, cancelled=is_cancelled)
            elif kind == 'import-observations':
                from .observations import import_observations
                result = import_observations(self.db, progress=progress)
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
                result = register_era5(paths, self.db, name=request.get('name', 'ERA5-Land历史输入'), progress=progress)
            elif kind == 'calibration':
                from .calibration import calibrate
                result = calibrate(self.db, request, folder, progress, is_cancelled)
            else:
                result = run_simulation(self.db, request, folder, progress, is_cancelled)
            if is_cancelled():
                raise InterruptedError('用户停止任务')
            result['taskId'] = identifier
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
            with self.db.connect() as db:
                db.execute("UPDATE tasks SET status='cancelled',message=?,updated_at=? WHERE id=?", (str(exc), now(), identifier))
        except Exception as exc:
            import traceback
            (folder / 'error.log').write_text(traceback.format_exc(), encoding='utf-8')
            with self.db.connect() as db:
                db.execute("UPDATE tasks SET status='failed',message=?,updated_at=? WHERE id=?", (str(exc)[:1500], now(), identifier))
        finally:
            self.cancelled.discard(identifier)

    def close(self):
        if self.active():
            raise ValueError('仍有排队、运行或停止中的任务，请先停止任务')
        self.executor.shutdown(wait=False)
