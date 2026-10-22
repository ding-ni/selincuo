from __future__ import annotations

import argparse
import functools
import json
import mimetypes
import os
import re
import threading
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .common import ROOT, VERSION, WORKSPACE, CONTEXT_KEY, canonical, digest, now, read_json, within, write_json
from .database import Database
from .jobs import Jobs
from .model_core import DEFAULT_PARAMETERS, PARAMETER_DEFINITIONS


class Platform(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, workspace=None):
        self.db = Database(workspace)
        self.lock_stream = (self.db.workspace / 'instance.lock').open('a+b')
        self.lock_stream.seek(0)
        if not self.lock_stream.read(1):
            self.lock_stream.write(b'0')
            self.lock_stream.flush()
        self.lock_stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(self.lock_stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.lock_stream.close()
                raise ValueError('此色林错工作区已被另一个服务使用，请打开已有实例')
        self.jobs = Jobs(self.db)
        self.instance_id = uuid.uuid4().hex
        super().__init__(address, Handler)

    def server_close(self):
        super().server_close()
        self.lock_stream.close()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'frontend/dist'), **kwargs)

    def log_message(self, format, *args):
        if args and str(args[1] if len(args) > 1 else '').startswith('5'):
            super().log_message(format, *args)

    @property
    def db(self):
        return self.server.db

    def send_json(self, value, status=200):
        body = canonical(value).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def document(self):
        length = int(self.headers.get('Content-Length', 0))
        if not 0 <= length <= 4 * 1024 * 1024:
            raise ValueError('请求超过4MB，请使用文件上传入口')
        if length and 'application/json' not in self.headers.get('Content-Type', ''):
            raise ValueError('接口请求须为JSON')
        value = json.loads(self.rfile.read(length)) if length else {}
        if not isinstance(value, dict):
            raise ValueError('请求须为对象')
        return value

    def guard(self):
        host = self.headers.get('Host', '').split(':')[0].lower()
        if host not in ('127.0.0.1', 'localhost'):
            raise ValueError('仅允许本机访问')
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            raise ValueError('拒绝跨站请求')

    def dispatch(self, fn):
        try:
            self.guard()
            fn()
        except FileNotFoundError as exc:
            self.send_json({'code': 'RESULT_NOT_FOUND', 'error': str(exc)}, 404)
        except (ValueError, KeyError, TypeError) as exc:
            self.send_json({'error': str(exc)}, 400)
        except Exception as exc:
            self.send_json({'error': str(exc), 'code': 'INTERNAL_ERROR'}, 500)

    def do_GET(self):
        self.dispatch(self.get)

    def get(self):
        url = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        path = url.path.removeprefix('/api/model/')
        if url.path == '/api/health':
            return self.send_json({'projectId': 'selinco', 'version': VERSION, 'instanceId': self.server.instance_id,
                                   'workspace': str(self.db.workspace)})
        if not url.path.startswith('/api/'):
            if url.path.startswith('/spatial/'):
                return self.file(within(ROOT / 'data/derived/spatial', url.path.removeprefix('/spatial/')))
            if not (ROOT / 'frontend/dist/index.html').is_file():
                return self.send_json({'error': '网页尚未构建，请执行安装平台脚本'}, 503)
            return super().do_GET()
        if path == 'bootstrap':
            spatial = read_json(ROOT / 'data/derived/spatial/manifest.json')
            summary = {k: v for k, v in (spatial or {}).items() if k not in ('weights', 'lakeWeights', 'grids', 'sourceFiles')}
            inputs = self.db.items('input_products')
            for item in inputs:
                item['compatible'] = bool(spatial and item['manifest']['spatialVersion'] == spatial['version'])
            annual_ready = any(i['compatible'] and i['manifest']['start'] <= '2023-01-01T00:00:00' and i['manifest']['end'] >= '2026-01-01T00:00:00' for i in inputs)
            return self.send_json({'projectId': 'selinco', 'name': '色林错预警预报平台', 'version': VERSION,
                                   'contextKey': CONTEXT_KEY, 'spatial': summary, 'database': self.db.summary(),
                                   'stations': self.db.rows('SELECT * FROM stations ORDER BY id'),
                                   'inputs': inputs, 'parameters': self.db.items('parameter_versions'),
                                   'schemes': self.db.items('schemes'),
                                   'parameterDefinitions': PARAMETER_DEFINITIONS, 'defaultParameters': DEFAULT_PARAMETERS,
                                   'forecast': {'enabled': False, 'reason': '本版完成历史计算，7—15天滚动预报在下一阶段接入'},
                                   'ready': bool(spatial and inputs), 'missingInputs': [] if inputs else ['ERA5-Land真实逐小时降水、气温与蒸发能力输入'],
                                   'historicalPlan': {'warmupYear': 2023, 'calibrationYear': 2024, 'validationYear': 2025, 'start': '2023-01-01T00:00:00', 'end': '2026-01-01T00:00:00', 'dailyConvention': '(day 00:00, next day 00:00] Asia/Shanghai', 'futureObservationsUsedInSearch': False},
                                   'annualValidation': {'ready': annual_ready, 'message': '年度历史驱动完整' if annual_ready else '完整2023—2025年真实驱动尚未齐备，年度率定验证待开展'},
                                   'periodDefaults': {'start': '2023-01-01T00:00', 'end': '2026-01-01T00:00'},
                                   'latestObservation': self.db.rows('SELECT MAX(time) time FROM observations WHERE value IS NOT NULL')[0]['time']})
        if path == 'cds-help':
            return self.file(ROOT/'docs/CDS账户恢复与继续下载.md',download=True)
        if path == 'dashboard':
            from .dashboard import monitoring
            return self.send_json(monitoring(self.db))
        if path == 'map-frame':
            from .dashboard import map_frame
            return self.send_json(map_frame(self.db,query['inputId'],query['day'],query.get('variable','precipitation')))
        if path == 'snow-frame':
            from .dashboard import snow_frame
            return self.send_json(snow_frame(self.db,query['taskId'],query['day']))
        if path == 'observations':
            return self.send_json({'rows': self.db.observations(query.get('station', 'SLC'), query.get('variable', 'level'),
                                                               query.get('step'), query.get('start'), query.get('end'), query.get('limit', 2000))})
        if path == 'import-preview':
            from .observations import scan
            return self.send_json({'files': scan(ROOT / 'data/raw/observations/slc')})
        if path == 'acquisition':
            from .acquisition import inventory
            return self.send_json(inventory(self.db.workspace))
        if path == 'input-files':
            files = [p for name in ('downloads', 'incoming') for p in (self.db.workspace / name).rglob('*.nc')]
            return self.send_json({'files': [{'path': p.relative_to(self.db.workspace).as_posix(), 'bytes': p.stat().st_size} for p in sorted(files)]})
        if path == 'input-qc':
            report = read_json(within(self.db.workspace / 'reports', query['inputId'] + '-station-qc.json'))
            if report is None:
                raise FileNotFoundError('该输入尚无站点交叉报告，请重新登记真实输入生成报告')
            return self.send_json(report)
        if path == 'input-series':
            from .simulation import prepare
            import pandas as pd
            source = prepare(self.db, query['inputId'])
            frame = pd.DataFrame({'precipitation': source['lakeRain'], 'evaporation': source['lakeEvaporation']}, index=source['times']).resample('D').sum()
            frame['temperature'] = pd.Series(source['temperature'].mean(axis=1), index=source['times']).resample('D').mean()
            return self.send_json({'rows': json.loads(frame.reset_index(names='date').to_json(orient='records', date_format='iso')),
                                   'note': '降水与蒸发为湖面面积加权基准；气温为计算单元均值，尚未高程修正'})
        if path == 'observation-series':
            from .observations import daily_series
            series = daily_series(self.db, query.get('station', 'SLC'), query.get('variable', 'level'), query.get('start'), query.get('end'))
            return self.send_json({'rows': [{'time': time.isoformat(), 'value': float(value)} for time, value in series.dropna().items()]})
        if path == 'tasks':
            rows = self.db.rows('SELECT id,kind,status,created_at,updated_at,progress,message,trashed,favorite,name,notes,collection FROM tasks ORDER BY created_at DESC LIMIT 500')
            return self.send_json({'tasks': rows})
        if path.startswith('tasks/'):
            return self.send_json(self.server.jobs.public(self.db.task(path.split('/')[1])))
        if path == 'states':
            states = self.db.items('states')
            for row in states:
                payload = row['payload']
                row['identity'] = payload['identity']
                row['summary'] = {'sweMean': sum(s[0] for s in payload['unitStates']) / len(payload['unitStates']), 'lake': payload['lake']}
                row.pop('payload')
            return self.send_json({'states': states})
        if path.startswith('states/'):
            rows = self.db.rows('SELECT * FROM states WHERE id=?', (path.split('/')[1],))
            if not rows:
                raise FileNotFoundError('状态不存在')
            return self.send_json({'state': json.loads(rows[0]['payload'])})
        if path == 'process':
            return self.process(query)
        if path == 'export':
            task = self.db.task(query['taskId'])
            filename = query.get('format', 'daily.csv')
            if filename not in ('daily.csv', 'checkpoint.json', 'result.json', 'hourly.npz', 'request.json'):
                raise ValueError('不支持的导出类型')
            return self.file(within(self.db.workspace / 'tasks', task['id']) / filename, download=True)
        if path == 'download':
            return self.file(within(self.db.workspace / 'exports', query['filename']), download=True)
        if path == 'system':
            import h5py
            return self.send_json({'version': VERSION, 'projectId': 'selinco', 'workspace': str(self.db.workspace),
                                   'database': self.db.summary()['counts'], 'activeTasks': self.server.jobs.active(),
                                   'hdf5Version': h5py.version.hdf5_version,
                                   'downloadStatus': read_json(self.db.workspace / 'downloads/era5-land/source-manifest.json', {'files': []})})
        raise FileNotFoundError('接口不存在')

    def file(self, path, download=False):
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError('文件不存在')
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(path.stat().st_size))
        if download:
            from urllib.parse import quote
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(path.name))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        with path.open('rb') as source:
            while block := source.read(1024 * 1024):
                self.wfile.write(block)

    def process(self, query):
        import numpy as np
        import pandas as pd
        task = self.db.task(query['taskId'])
        if task['status'] != 'succeeded' or not task['result'] or 'units' not in task['result']:
            raise ValueError('请选择已完成的模型成果')
        unit = int(query.get('unit', 0))
        if not 0 <= unit < len(task['result']['units']):
            raise ValueError('计算单元不存在')
        with np.load(self.db.workspace / 'tasks' / task['id'] / 'hourly.npz', allow_pickle=False) as data:
            times = pd.to_datetime(data['time'])
            frame = pd.DataFrame(data['unitStates'][:, unit, :], index=times,
                                 columns=['swe', 'snowLiquid', 'WU', 'WL', 'WD', 'freeWater', 'FR', 'glacierStorage'])
            frame = frame.resample('D').mean()
            rows = json.loads(frame.reset_index(names='date').to_json(orient='records', date_format='iso'))
        return self.send_json({'unit': task['result']['units'][unit], 'rows': rows})

    def do_POST(self):
        self.dispatch(self.post)

    def post(self):
        path = urlsplit(self.path).path.removeprefix('/api/model/')
        value = self.document()
        if path == 'coverage':
            from .observations import confirm_coverage
            return self.send_json(confirm_coverage(self.db, value))
        if path == 'tasks':
            return self.send_json(self.server.jobs.submit(value.pop('kind'), value), 202)
        if path.startswith('tasks/'):
            _, identifier, action = path.split('/')
            task = self.db.task(identifier)
            if action == 'stop':
                return self.send_json(self.server.jobs.stop(identifier))
            if action == 'metadata':
                allowed = {k: v for k, v in value.items() if k in ('name', 'notes', 'collection', 'favorite', 'trashed')}
                if task['status'] in ('queued', 'running', 'stopping') and allowed.get('trashed'):
                    raise ValueError('活动任务不能移入回收站')
                if any(len(str(v)) > 4000 for v in allowed.values()):
                    raise ValueError('备注过长')
                with self.db.connect() as conn:
                    for key, item in allowed.items():
                        conn.execute(f'UPDATE tasks SET {key}=? WHERE id=?', (int(bool(item)) if key in ('favorite', 'trashed') else str(item), identifier))
                return self.send_json({'status': 'saved'})
            if action == 'cleanup-preview':
                from .maintenance import cleanup_preview
                return self.send_json(cleanup_preview(self.db, identifier))
            if action == 'cleanup-execute':
                if self.server.jobs.active():
                    raise ValueError('清理前请等待所有计算任务结束')
                from .maintenance import cleanup_execute
                return self.send_json(cleanup_execute(self.db, identifier, value['token']))
        if path in ('parameters', 'schemes'):
            table = 'parameter_versions' if path == 'parameters' else 'schemes'
            item = dict(value['payload'])
            if path == 'parameters':
                from .model_core import parameters
                item['parameters'] = parameters(item.get('parameters'))
                if item.get('sourceTaskId'):
                    task = self.db.task(item['sourceTaskId'])
                    if task['status'] != 'succeeded' or task['result']['parameters'] != item['parameters']:
                        raise ValueError('命名参数必须与已完成成果参数一致')
            identifier = ('PARAM-' if path == 'parameters' else 'SCHEME-') + uuid.uuid4().hex[:16]
            self.db.save_item(table, identifier, value['name'], item)
            return self.send_json({'id': identifier})
        if path.startswith('states/'):
            identifier = path.split('/')[1]
            with self.db.connect() as conn:
                if not conn.execute('SELECT id FROM states WHERE id=?', (identifier,)).fetchone():
                    raise FileNotFoundError('状态不存在')
                conn.execute('UPDATE states SET name=?,favorite=? WHERE id=?', (str(value.get('name', ''))[:120], int(bool(value.get('favorite'))), identifier))
            return self.send_json({'status': 'saved'})
        if path == 'backup':
            if self.server.jobs.active():
                raise ValueError('请在任务结束后备份，确保成果与状态一致')
            from .maintenance import backup_workspace
            return self.send_json(backup_workspace(self.db))
        if path in ('restore-preview', 'restore'):
            from .maintenance import restore_preview, restore_workspace
            archive = within(self.db.workspace / 'incoming', value['filename'])
            manifest = restore_preview(archive)
            if path == 'restore-preview':
                return self.send_json({'files': len(manifest['files']), 'bytes': sum(r['bytes'] for r in manifest['files']), 'token': digest(manifest)})
            if self.server.jobs.active() or value.get('token') != digest(manifest):
                raise ValueError('请先结束任务并重新核对备份预览')
            destination = ROOT / 'restored-workspaces' / ('restored-' + uuid.uuid4().hex[:12])
            result = restore_workspace(archive, destination)
            write_json(ROOT / 'config/active-workspace.json', {'workspace': destination.relative_to(ROOT).as_posix()})
            return self.send_json({**result, 'message': '恢复到独立目录完成；退出后重新启动将使用恢复工作区'})
        if path == 'shutdown':
            self.server.jobs.close()
            self.send_json({'status': 'stopping'})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        raise FileNotFoundError('接口不存在')

    def do_PUT(self):
        self.dispatch(self.upload)

    def upload(self):
        from urllib.parse import unquote
        url = urlsplit(self.path)
        if url.path != '/api/model/upload':
            raise FileNotFoundError('上传入口不存在')
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        name = query.get('filename', '')
        if not name or '/' in name or '\\' in name or ':' in name or name.startswith('.'):
            raise ValueError('文件名不合法')
        extension = Path(name).suffix.lower()
        if extension not in ('.xls', '.xlsx', '.nc', '.zip'):
            raise ValueError('支持Excel、NetCDF和工作区备份ZIP')
        length = int(self.headers.get('Content-Length', 0))
        if not 0 < length <= 4 * 1024 ** 3:
            raise ValueError('文件大小须为1字节至4GB')
        if extension in ('.xls', '.xlsx'):
            station = query.get('station')
            mapping = {'BLQ': 'blq', '115DB': '115daoban', 'DUJA': 'dj', 'SLC': 'slc'}
            if station not in mapping:
                raise ValueError('上传Excel前请选择对应站点')
            destination = ROOT / 'data/raw/observations/slc' / mapping[station] / 'imports' / uuid.uuid4().hex[:12] / name
        else:
            destination = self.db.workspace / 'incoming' / (uuid.uuid4().hex[:12] + '-' + name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('xb') as stream:
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError('文件上传未完成')
                stream.write(chunk)
                remaining -= len(chunk)
        return self.send_json({'filename': destination.name, 'bytes': length})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8879)
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    server = Platform(('127.0.0.1', args.port), args.workspace)
    print(f'色林错预警预报平台 {VERSION}\n工作区：{server.db.workspace}\nhttp://127.0.0.1:{server.server_port}/', flush=True)
    if not args.no_browser:
        threading.Timer(.7, lambda: webbrowser.open(f'http://127.0.0.1:{server.server_port}/')).start()
    try:
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
