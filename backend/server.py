from __future__ import annotations

import argparse
import functools
import json
import mimetypes
import os
import re
import shutil
import threading
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .common import ROOT, VERSION, WORKSPACE, CONTEXT_KEY, canonical, digest, now, read_json, within, write_json
from .database import Database
from .jobs import Jobs
from .model_core import DEFAULT_PARAMETERS, PARAMETER_DEFINITIONS


def upload_size_limit(extension):
    return (50 if extension=='.zip' else 4)*1024**3


def guard_restore_writes(jobs, path, uploading=False):
    if not (getattr(jobs,'restore_pending',False) or getattr(jobs,'restart_required',False)):
        return
    allowed=path in ('tasks','restore-preview','shutdown') or bool(re.fullmatch(r'tasks/[^/]+/stop',path))
    if uploading or not allowed:
        raise ValueError('恢复已排队或完成，旧工作区暂停写入；请等待恢复结束并重启后继续')


def receive_upload(source, destination, length, extension):
    """Bounded streaming; incomplete files never become restorable uploads."""
    if not 0 < length <= upload_size_limit(extension):
        raise ValueError('ZIP大小须为1字节至50GiB；其他文件须为1字节至4GiB')
    destination.parent.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(destination.parent).free < length+16*1024*1024:
        raise ValueError('上传磁盘可用空间不足：需要至少 '+str(length+16*1024*1024)+' 字节')
    partial=destination.with_name(destination.name+'.partial')
    with partial.open('xb') as stream:
        remaining=length
        while remaining:
            chunk=source.read(min(1024*1024,remaining))
            if not chunk:raise ValueError('文件上传未完成，半成品保留但不可恢复')
            stream.write(chunk);remaining-=len(chunk)
    partial.replace(destination)


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
        from .release_registry import migrate
        migrate(self.db)
        from .continuous_reference import migrate as migrate_references
        migrate_references(self.db)
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
                                   'backendSourceSHA256': self.server.jobs.backend_source_identity,
                                   'workspace': str(self.db.workspace)})
        if not url.path.startswith('/api/'):
            if url.path.startswith('/spatial/'):
                relative = unquote(url.path.removeprefix('/spatial/'), errors='strict')
                # Decode exactly once. Do not accept a second encoded path
                # separator or traversal sequence as an apparent filename.
                if re.search(r'%(?:2e|2f|5c|3a)', relative, re.IGNORECASE):
                    raise ValueError('空间资源路径编码不合法')
                return self.file(within(ROOT / 'data/derived/spatial', relative))
            if not (ROOT / 'frontend/dist/index.html').is_file():
                return self.send_json({'error': '网页尚未构建，请执行安装平台脚本'}, 503)
            return super().do_GET()
        if path == 'business-context':
            from .business_context import business_context
            return self.send_json(business_context(self.db, query.get('resultId')))
        if path == 'result-summary':
            from .business_context import result_summary
            return self.send_json(result_summary(self.db, query['resultId']))
        if path == 'simulation-plan':
            from .business_context import simulation_plan
            return self.send_json(simulation_plan(self.db, query))
        if path == 'bootstrap':
            spatial_message = ''
            try:
                spatial = read_json(ROOT / 'data/derived/spatial/manifest.json')
                if not isinstance(spatial, dict):
                    spatial = None
                    spatial_message = '空间基础资料缺失，请接入完整空间资源。'
            except (OSError, ValueError, TypeError):
                spatial = None
                spatial_message = '空间基础资料无法读取，请检查空间资源。'
            summary = {k: v for k, v in (spatial or {}).items() if k not in ('weights', 'lakeWeights', 'grids', 'sourceFiles')}
            inputs = self.db.items('input_products')
            for item in inputs:
                item['compatible'] = bool(spatial and item['manifest'].get('spatialVersion') == spatial.get('version'))
                from .forcing_latest import provenance_summary
                item['provenanceSummary']=provenance_summary(self.db,item['manifest'])
            from .calibration_config import public_config
            from .operational_scheme import catalog
            from .published_results import catalog as published_catalog
            from .release_registry import current as current_release
            annual_ready = any(i['compatible'] and i['manifest']['start'] <= '2023-01-01T00:00:00' and i['manifest']['end'] >= '2026-01-01T00:00:00' for i in inputs)
            from .grid_spatial import current as grid_current
            from .research_schemes import builtin, list_schemes, list_parameters
            from .business_context import builtin_result_available
            try:
                grid=grid_current()
                if not grid and not spatial_message:
                    spatial_message = '当前空间版本缺失，请接入完整空间资源。'
            except (OSError, ValueError, KeyError, TypeError):
                grid = None
                spatial_message = '当前空间版本缺失或校验失败，请检查空间资源。'
            grid_summary={k:v for k,v in (grid or {}).items() if k not in ('grids','units','lakeWeights')}
            try:
                operational_schemes = catalog(self.db)
                operational_message = ''
            except (OSError, ValueError, KeyError, TypeError):
                operational_schemes = []
                operational_message = '原历史方案或其空间资料无法读取，请检查基础资源。'
            builtin_scheme = builtin(self.db)
            from .business_selection import current as business_current
            selected_business=business_current(self.db)
            scheme_rows = list_schemes(self.db)
            active_scheme = next((row for row in scheme_rows if row['id'] == selected_business.get('schemeId')), builtin_scheme)
            active_payload = (active_scheme or {}).get('payload', {})
            active_periods = active_payload.get('periods') or {}
            calibration_config = public_config()
            if active_periods:
                calibration_config.update(warmupYear=int(active_periods['warmupStart'][:4]),
                                          calibrationYear=int(active_periods['trainingStart'][:4]),
                                          warmupStart=active_periods['warmupStart'])
                if active_payload.get('twoStage', {}).get('finalRefit'):
                    calibration_config['excludedYears'] = []
            builtin_result = selected_business.get('resultId') or (builtin_scheme or {}).get('payload', {}).get('resultId')
            builtin_available = builtin_result_available(self.db, builtin_result)
            return self.send_json({'projectId': 'selinco', 'name': '色林错预警预报平台', 'version': VERSION, 'gridSpatial':grid_summary,
                                   'spatialResourceMessage': spatial_message, 'operationalSchemesMessage': operational_message,
                                   'contextKey': CONTEXT_KEY, 'spatial': summary, 'database': self.db.summary(),
                                   'stations': self.db.rows('SELECT * FROM stations ORDER BY id'),
                                   'inputs': inputs, 'parameters': list_parameters(self.db),
                                   'schemes': scheme_rows,
                                   'operationalSchemes': operational_schemes,
                                   'publishedResults': published_catalog(), 'standardResultId': current_release(self.db)['resultId'],
                                   'homeResultId': builtin_result if builtin_available else None,
                                   'homeSchemeId':selected_business.get('schemeId') or (builtin_scheme or {}).get('id'),
                                   'currentBusiness':selected_business,
                                   'builtinResultAvailable': builtin_available,
                                   'builtinResultMessage': '' if builtin_available else '当前工作区缺少内置连续成果或其完整逐时资料，请接入对应成果资源。',
                                   'calibrationConfig': calibration_config, 'routingModes': [{'id':'source-separated','label':'分水源调蓄 + 河道汇流'},{'id':'lumped','label':'原方案：合并径流单库汇流'}],
                                   'parameterDefinitions': PARAMETER_DEFINITIONS, 'defaultParameters': DEFAULT_PARAMETERS,
                                   'forecast': {'enabled': False, 'reason': '本版完成历史计算，7—15天滚动预报在下一阶段接入'},
                                   'ready': bool(spatial and grid and inputs), 'missingInputs': [] if inputs else ['ERA5-Land真实逐小时降水、气温与蒸发能力输入'],
                                   'historicalPlan': {'warmupYear': calibration_config['warmupYear'], 'calibrationYear': calibration_config['calibrationYear'], 'validationYear': int(active_periods.get('evaluationStart', '2025')[:4]), 'start': active_periods.get('warmupStart', '2023-01-01T00:00:00'), 'end': active_periods.get('evaluationEnd', '2026-01-01T00:00:00'), 'dailyConvention': '(day 00:00, next day 00:00] Asia/Shanghai', 'futureObservationsUsedInSearch': bool(active_payload.get('twoStage', {}).get('finalRefit'))},
                                   'annualValidation': {'ready': annual_ready, 'message': '年度历史驱动完整' if annual_ready else '完整2023—2025年真实驱动尚未齐备，年度率定验证待开展'},
                                   'periodDefaults': {'start': active_payload.get('historyPeriod', {}).get('start', active_periods.get('warmupStart', '2023-01-01T00:00'))[:16], 'end': active_payload.get('historyPeriod', {}).get('end', active_periods.get('evaluationEnd', '2026-01-01T00:00'))[:16]},
                                   'latestObservation': self.db.rows('SELECT MAX(time) time FROM observations WHERE value IS NOT NULL')[0]['time']})
        if path == 'cds-help':
            return self.file(ROOT/'docs/CDS账户恢复与继续下载.md',download=True)
        if path == 'dashboard':
            from .dashboard import monitoring
            return self.send_json(monitoring(self.db))
        if path == 'map-frame':
            from .dashboard import map_frame
            return self.send_json(map_frame(self.db,query['inputId'],query['day'],query.get('variable','precipitation'),query.get('spatialVersion')))
        if path == 'map-range':
            from .map_ranges import range_for
            return self.send_json(range_for(self.db,query))
        if path == 'snow-frame':
            from .dashboard import snow_frame
            return self.send_json(snow_frame(self.db,query['taskId'],query['day']))
        if path == 'observation-catalog':
            from .observation_query import catalog
            return self.send_json(catalog(self.db))
        if path == 'observation-page':
            from .observation_query import page
            return self.send_json(page(self.db, query))
        if path == 'datum-candidates':
            from .observation_profiles import datum_candidates
            return self.send_json(datum_candidates(self.db))
        if path == 'station-datum':
            from .stations import datum_at
            return self.send_json(datum_at(query['stationId'], query['time']))
        if path == 'research-template':
            from .research_schemes import template
            return self.send_json(template(self.db))
        if path == 'schemes':
            from .research_schemes import list_schemes
            return self.send_json({'schemes': list_schemes(self.db, query.get('includeTrashed') == 'true', query.get('includeExperiments')=='true')})
        if path == 'calibration-data':
            from .observation_profiles import quality_series
            from .model_forcing import current_summary
            from .two_stage_calibration import defaults
            rows={}
            for key,station,variable in [('flow','DUJA','flow'),('lake','SLC','level')]:
                values=quality_series(self.db,station,variable).dropna()
                rows[key]={'stationId':station,'start':values.index.min().isoformat() if len(values) else None,
                    'end':values.index.max().isoformat() if len(values) else None,'days':len(values),
                    'years':[{'year':int(y),'days':len(g),'start':g.index.min().isoformat(),'end':g.index.max().isoformat()}
                             for y,g in values.groupby(values.index.year)]}
            from .business_selection import current
            choice = current(self.db)
            adopted = self.db.task(choice['resultId']) if choice.get('resultId') else {}
            adopted_request = adopted.get('request') or {}
            return self.send_json({**rows,'forcing':current_summary(self.db),'defaults':defaults(),
                'warmup': {'start': adopted_request.get('start'),
                           'initialization': (adopted.get('result') or {}).get('initialization'),
                           'diagnostics': (adopted.get('result') or {}).get('warmupDiagnostics')}})
        if path == 'parameters':
            from .research_schemes import list_parameters
            return self.send_json({'parameters': list_parameters(self.db, query.get('includeTrashed') == 'true')})
        if path == 'spatial-version':
            from .grid_spatial import load
            return self.send_json(load(query['version']))
        if path == 'forcing-coverage':
            from .forcing_latest import availability
            from .model_forcing import current_summary
            folder=self.db.workspace/'downloads/era5-land'
            meta=availability() if query.get('check')=='true' else read_json(folder/'latest-catalog.json')
            if query.get('check')=='true':write_json(folder/'latest-catalog.json',meta)
            return self.send_json({'catalog':meta,'update':read_json(folder/'latest-status.json'),
                'current':current_summary(self.db),
                'precipitationCorrection':self.db.setting('rainfall-correction-comparison')})
        if path == 'search-progress':
            task=self.db.task(query['taskId'])
            if task['kind'] in ('two-stage-calibration','calibration-replay'):
                from .two_stage_calibration import search_progress
                return self.send_json(search_progress(within(self.db.workspace/'tasks',task['id']),
                    max(0,int(query.get('after',0))),min(1000,max(1,int(query.get('limit',200))))))
            value=read_json(within(self.db.workspace/'tasks',query['taskId'])/'search-progress.json',{'trace':[]})
            trace=value.get('trace',[]);after=max(0,int(query.get('after',0)))
            return self.send_json({**value,'trace':trace[after:],'count':len(trace)})
        if path == 'result-range':
            from .result_query import range_for
            return self.send_json(range_for(self.db, query))
        if path in ('result-capabilities', 'result-series', 'result-frame', 'result-checkpoint'):
            from .result_query import metadata, read, final
            if path == 'result-capabilities': return self.send_json(metadata(self.db, query['resultId']))
            if path == 'result-checkpoint': return self.send_json(final(self.db, query['resultId']))
            return self.send_json(read(self.db, query, frame=path == 'result-frame'))
        if path == 'observations':
            return self.send_json({'rows': self.db.observations(query.get('station', 'SLC'), query.get('variable', 'level'),
                                                               query.get('step'), query.get('start'), query.get('end'), query.get('limit', 2000))})
        if path == 'data-contract':
            from .data_exchange import contract
            return self.send_json(contract())
        if path == 'published-result':
            from .published_results import detail, result_file
            if query.get('file'):
                return self.file(result_file(query['id'], query['file']), download=True)
            return self.send_json(detail(query['id']))
        if path == 'data-template':
            from io import BytesIO
            from openpyxl import Workbook
            from .data_exchange import contract
            book = Workbook()
            book.active.title = 'data'
            book.active.append(contract()['requiredColumns'] + contract()['optionalColumns'])
            stream = BytesIO()
            book.save(stream)
            content = stream.getvalue()
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            self.send_header('Content-Disposition', 'attachment; filename="selinco-observations.xlsx"')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            return self.wfile.write(content)
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
            if query.get('profile') in ('quality-v2','raw','candidate'):
                from .observation_profiles import series
                return self.send_json(series(self.db,query.get('station','SLC'),query.get('variable','level'),query.get('start'),query.get('end'),query['profile']))
            from .observation_query import series
            return self.send_json(series(self.db, query.get('station', 'SLC'), query.get('variable', 'level'), query.get('start'), query.get('end')))
        if path == 'continuous-reference-config':
            from .continuous_reference import ANCHOR_CONFIG
            from .release_registry import PROTOCOL,current
            return self.send_json({'anchor':ANCHOR_CONFIG,'protocolId':PROTOCOL['id'],
                                   'baselineReleaseId':current(self.db)['id']})
        if path == 'continuous-references':
            rows=self.db.rows('SELECT payload FROM continuous_reference_records ORDER BY rowid DESC LIMIT 200')
            return self.send_json({'references':[{k:value.get(k) for k in
                ('id','createdAt','baselineReleaseId','parentResultId','method','archiveComplete')}
                for value in (json.loads(row['payload']) for row in rows)]})
        if path == 'continuous-reference':
            from .continuous_reference import read_verified_reference
            return self.send_json(read_verified_reference(self.db,query.get('id')))
        if path == 'evaluations':
            rows=self.db.rows('SELECT id,payload FROM evaluation_records ORDER BY rowid DESC LIMIT 200')
            values=[]
            for row in rows:
                record=json.loads(row['payload'])
                values.append({k:record.get(k) for k in ('id','createdAt','baselineReleaseId','baselineResultId',
                    'candidateResultId','comparisonStatus','comparisonReasons','admission','archiveComplete')})
            return self.send_json({'evaluations':values})
        if path == 'evaluation':
            rows=self.db.rows('SELECT payload FROM evaluation_records WHERE id=?',(query.get('id'),))
            if not rows:raise FileNotFoundError('评价归档不存在')
            return self.send_json(json.loads(rows[0]['payload']))
        if path in ('tasks', 'trashed-results'):
            # Normal task polling stays bounded. The recycle bin needs its
            # full set so "clear" does not silently omit older results.
            selection = ("WHERE trashed=1 AND status NOT IN ('queued','running','stopping') AND kind IN "
                         "('simulation','continuation','trial','calibration','calibration-workflow','two-stage-calibration','calibration-replay','sensitivity') "
                         "ORDER BY created_at DESC" if path == 'trashed-results'
                         else 'ORDER BY created_at DESC LIMIT 500')
            rows = self.db.rows('''SELECT id,kind,status,created_at,updated_at,progress,message,trashed,favorite,user_saved AS userSaved,name,notes,collection,
                json_object('researchSchemeId',json_extract(request,'$.researchSchemeId'),
                            'researchSchemeRevision',json_extract(request,'$.researchSchemeRevision'),
                            'purpose',json_extract(request,'$.purpose'),
                            'replaySourceTaskId',json_extract(request,'$.replay.sourceTaskId'),
                            'schemeName',json_extract(request,'$.schemeSnapshot.name'),
                            'start',json_extract(request,'$.start'),
                            'end',json_extract(request,'$.end'),
                            'evaluationStart',json_extract(request,'$.evaluationStart'),
                            'evaluationEnd',json_extract(request,'$.evaluationEnd'),
                            'modelFamily',json_extract(request,'$.modelFamily'),
                            'inputId',json_extract(request,'$.inputId'),
                            'schemeId',json_extract(request,'$.schemeId'),
                            'baselineReleaseId',json_extract(request,'$.baselineReleaseId'),
                            'protocolId',json_extract(request,'$.protocolId'),
                            'anchorConfigId',json_extract(request,'$.anchorConfigId'),
                            'schemeRevision',json_extract(request,'$.schemeRevision')) AS compact_request,
                CASE WHEN kind='restore-workspace' AND status='succeeded' THEN
                     json_object('workspace',json_extract(result,'$.workspace'),
                                 'restartRequired',json_extract(result,'$.restartRequired'),
                                 'sourceWorkspace',json_extract(result,'$.sourceWorkspace'),
                                 'message',json_extract(result,'$.message'),
                                 'files',json_extract(result,'$.files'),
                                 'bytes',json_extract(result,'$.bytes'),
                                 'databaseIntegrity',json_extract(result,'$.databaseIntegrity'))
                WHEN kind='evaluation' AND status='succeeded' THEN
                     json_object('evaluationId',json_extract(result,'$.evaluationId'),
                                 'comparisonStatus',json_extract(result,'$.comparisonStatus'),
                                 'admission',json_extract(result,'$.admission'),
                                 'artifacts',json_extract(result,'$.artifacts'))
                WHEN kind='continuous-reference' AND status='succeeded' THEN
                     json_object('referenceId',json_extract(result,'$.referenceId'),
                                 'method',json_extract(result,'$.method'),
                                 'artifacts',json_extract(result,'$.artifacts'))
                WHEN kind IN ('export-result','backup') AND status='succeeded' THEN
                     json_object('artifacts',json_extract(result,'$.artifacts')) ELSE NULL END AS compact_result
                FROM tasks ''' + selection)
            for row in rows:
                row['request']={k:v for k,v in json.loads(row.pop('compact_request')).items() if v is not None}
                compact=row.pop('compact_result')
                row['result']=json.loads(compact) if compact else None
                row['heartbeatAt']=row['updated_at']
                row['phase']=row['message'].split(' · ')[0]
                row['termination']=({'cancelled':'user-stopped','interrupted':'service-restarted',
                                     'failed':'failed','succeeded':'completed'}).get(row['status'])
            from .business_selection import visible_tasks
            rows=visible_tasks(self.db,rows,query.get('includeExperiments')=='true')
            return self.send_json({'tasks': rows})
        if re.fullmatch(r'tasks/[^/]+/replay-plan', path):
            from .calibration_replay import replay_plan
            return self.send_json(replay_plan(self.db, path.split('/')[1]))
        if path.startswith('tasks/'):
            return self.send_json(self.server.jobs.public(self.db.task(path.split('/')[1])))
        if path == 'states':
            states = self.db.items('states')
            from .business_selection import visible_tasks
            task_rows=[{'id':r['id'],'request':json.loads(r['request']),'user_saved':r['user_saved']}
                       for r in self.db.rows('SELECT id,request,user_saved FROM tasks')]
            visible_ids={r['id'] for r in visible_tasks(self.db,task_rows)}
            states=[r for r in states if r['task_id'] in visible_ids or r.get('favorite') or r.get('name')]
            from .result_query import compatible
            for row in states:
                payload = row['payload']
                row['identity'] = payload['identity']
                row['canContinue'] = compatible(payload)
                row['summary'] = {'sweMean': sum(s[0] for s in payload['unitStates']) / len(payload['unitStates']), 'lake': payload['lake']}
                row.pop('payload')
            return self.send_json({'states': states})
        if path.startswith('states/'):
            rows = self.db.rows('SELECT * FROM states WHERE id=?', (path.split('/')[1],))
            if not rows:
                raise FileNotFoundError('状态不存在')
            from .result_query import checkpoint_detail
            tasks = self.db.rows('SELECT result FROM tasks WHERE id=?', (rows[0]['task_id'],))
            result = json.loads(tasks[0]['result'] or '{}') if tasks else {}
            return self.send_json(checkpoint_detail(json.loads(rows[0]['payload']), result))
        if path == 'process':
            return self.process(query)
        if path == 'export':
            from .permanent_deletion import guard_pending
            with self.server.jobs.lock:
                task = self.db.task(query['taskId'])
                guard_pending(self.db,'task',task['id'])
                filename = query.get('format', 'daily.csv')
                if filename not in ('daily.csv', 'checkpoint.json', 'result.json', 'hourly.npz', 'request.json'):
                    raise ValueError('不支持的导出类型')
                if filename=='hourly.npz' and task.get('result',{}).get('storageFormat')=='zarr-v2':
                    from .grid_storage import export_npz
                    return self.file(export_npz(within(self.db.workspace/'tasks',task['id'])),download=True)
                return self.file(within(self.db.workspace / 'tasks', task['id']) / filename, download=True)
        if path=='task-artifact':
            from .maintenance import artifact_file
            from .permanent_deletion import guard_pending
            with self.server.jobs.lock:
                task=self.db.task(query['taskId'])
                guard_pending(self.db,'task',task['id'])
                artifact=next((item for item in (task.get('result') or {}).get('artifacts',[]) if item.get('name')==query['name']),None)
                if artifact and artifact.get('storage')=='task':
                    guard_pending(self.db,'task',artifact.get('ownerTaskId'))
                return self.file(artifact_file(self.db,task,query['name']),download=True)
        if path == 'download':
            if query['filename'].endswith(('.partial','.sqlite3','.complete.json')):
                raise ValueError('中间文件不可下载')
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
        guard_restore_writes(self.server.jobs,path)
        value = self.document()
        if path in ('permanent-delete/preview','permanent-delete/execute'):
            from .permanent_deletion import preview, execute
            with self.server.jobs.lock:
                return self.send_json(preview(self.db,value.get('objects')) if path.endswith('preview')
                                      else execute(self.db,value.get('objects'),value.get('token')))
        if path == 'operational-release':
            from .release_registry import publish
            from .permanent_deletion import guard_no_pending
            with self.server.jobs.lock:
                guard_no_pending(self.db)
                return self.send_json(publish(self.db, value))
        if path == 'data-import-preview':
            from .data_exchange import preview_file
            return self.send_json(preview_file(self.db, value))
        if path in ('data-import', 'data-import-file'):
            if self.server.jobs.active():
                raise ValueError('请等待计算结束后更新资料')
            from .data_exchange import import_records, import_file
            return self.send_json((import_records if path == 'data-import' else import_file)(self.db, value))
        if path == 'coverage':
            from .observations import confirm_coverage
            return self.send_json(confirm_coverage(self.db, value))
        if path == 'tasks':
            from .permanent_deletion import guard_no_pending, guard_parameter_source, guard_pending, guard_state
            with self.server.jobs.lock:
                if value.get('kind')=='backup':guard_no_pending(self.db)
                for field,kind in (('researchSchemeId','scheme'),('taskId','task'),('parentTaskId','task'),('baseTaskId','task'),('lakeOnlyBaseTaskId','task'),('baselineResultId','task'),('candidateResultId','task')):
                    if value.get(field):guard_pending(self.db,kind,value[field])
                if value.get('candidateParameterId'):guard_parameter_source(self.db,value['candidateParameterId'])
                if value.get('stateId'):guard_state(self.db,value['stateId'])
                return self.send_json(self.server.jobs.submit(value.pop('kind'), value), 202)
        if path == 'parameters/from-task':
            from .research_schemes import save_parameters_from_task
            from .permanent_deletion import guard_pending
            with self.server.jobs.lock:
                guard_pending(self.db,'task',value.get('taskId'))
                return self.send_json(save_parameters_from_task(self.db, value.get('taskId'), value.get('name')))
        if path == 'business-adopt':
            from .business_selection import adopt_result
            from .permanent_deletion import guard_pending
            with self.server.jobs.lock:
                guard_pending(self.db,'task',value.get('taskId'))
                return self.send_json(adopt_result(self.db,value.get('taskId'),value.get('name')))
        if path.startswith('parameters/'):
            parts = path.split('/')
            if len(parts) != 3 or parts[2] != 'metadata':
                raise FileNotFoundError('接口不存在')
            if not isinstance(value.get('trashed'), bool):
                raise ValueError('请指定是否移入回收站')
            from .research_schemes import parameter_metadata
            from .permanent_deletion import guard_pending
            with self.server.jobs.lock:
                guard_pending(self.db,'parameter',parts[1])
                return self.send_json(parameter_metadata(self.db, parts[1], value['trashed']))
        if path.startswith('schemes/'):
            parts = path.split('/')
            if len(parts) != 3:
                raise FileNotFoundError('接口不存在')
            _, identifier, action = parts
            if action == 'adopt-parameters':
                from .research_schemes import adopt_parameters
                from .permanent_deletion import guard_parameter_source, guard_pending
                with self.server.jobs.lock:
                    guard_pending(self.db,'scheme',identifier)
                    guard_parameter_source(self.db,value.get('parameterId'))
                    return self.send_json(adopt_parameters(self.db, identifier, value.get('parameterId')))
            if action == 'metadata':
                if not isinstance(value.get('trashed'), bool):
                    raise ValueError('请指定是否移入回收站')
                from .research_schemes import scheme_metadata
                from .permanent_deletion import guard_pending
                with self.server.jobs.lock:
                    guard_pending(self.db,'scheme',identifier)
                    return self.send_json(scheme_metadata(self.db, identifier, value['trashed']))
            raise FileNotFoundError('接口不存在')
        if path.startswith('tasks/'):
            _, identifier, action = path.split('/')
            task = self.db.task(identifier)
            if action == 'replay':
                from .calibration_replay import BASELINE_ID
                from .permanent_deletion import guard_pending
                with self.server.jobs.lock:
                    guard_pending(self.db, 'task', identifier)
                    guard_pending(self.db, 'task', BASELINE_ID)
                    return self.send_json(self.server.jobs.submit('calibration-replay',
                        {**value, 'sourceTaskId': identifier}), 202)
            if action == 'stop':
                return self.send_json(self.server.jobs.stop(identifier))
            if action == 'resume':
                return self.send_json(self.server.jobs.resume_calibration(identifier))
            if action == 'metadata':
                from .permanent_deletion import guard_pending
                with self.server.jobs.lock:
                    guard_pending(self.db,'task',identifier)
                    task=self.db.task(identifier)
                    allowed = {k: v for k, v in value.items() if k in ('name', 'notes', 'collection', 'favorite', 'trashed')}
                    if task['status'] in ('queued', 'running', 'stopping') and allowed.get('trashed'):
                        raise ValueError('活动任务不能移入回收站')
                    if allowed.get('trashed'):
                        from .business_selection import current as business_current
                        if business_current(self.db).get('resultId')==identifier:
                            raise ValueError('当前采用成果不能移入回收站')
                        from .research_schemes import builtin
                        standard = builtin(self.db)
                        if standard and standard['payload'].get('resultId') == identifier:
                            raise ValueError('内置方案成果受到保护，不能移入回收站')
                    if any(len(str(v)) > 4000 for v in allowed.values()):
                        raise ValueError('备注过长')
                    with self.db.connect() as conn:
                        for key, item in allowed.items():
                            conn.execute(f'UPDATE tasks SET {key}=? WHERE id=?', (int(bool(item)) if key in ('favorite', 'trashed') else str(item), identifier))
                        if 'favorite' in allowed:
                            conn.execute('UPDATE tasks SET user_saved=? WHERE id=?',(int(bool(allowed['favorite'])),identifier))
                    return self.send_json({'status': 'saved'})
            if action == 'cleanup-preview':
                from .maintenance import cleanup_preview
                from .permanent_deletion import guard_pending
                with self.server.jobs.lock:
                    guard_pending(self.db,'task',identifier)
                    return self.send_json(cleanup_preview(self.db, identifier))
            if action == 'cleanup-execute':
                from .maintenance import cleanup_execute
                from .permanent_deletion import guard_pending
                with self.server.jobs.lock:
                    guard_pending(self.db,'task',identifier)
                    if self.server.jobs.active():
                        raise ValueError('清理前请等待所有计算任务结束')
                    return self.send_json(cleanup_execute(self.db, identifier, value['token']))
        if path in ('parameters', 'schemes'):
            if path=='schemes' and value.get('payload',{}).get('schemaVersion')==2:
                from .research_schemes import save
                from .permanent_deletion import guard_parameter_source, guard_pending
                with self.server.jobs.lock:
                    if value.get('parentId'):guard_pending(self.db,'scheme',value['parentId'])
                    guard_parameter_source(self.db,value.get('payload',{}).get('adoptedParameterId'))
                    return self.send_json(save(self.db,value['payload'],value.get('parentId')))
            if path == 'parameters':
                from .research_schemes import save_parameters_from_task, _result_parameters
                requested = value.get('payload') or {}
                task_id = requested.get('sourceTaskId') or value.get('sourceTaskId')
                if not task_id:
                    raise ValueError('参数组必须从成功成果保存')
                from .permanent_deletion import guard_pending
                with self.server.jobs.lock:
                    guard_pending(self.db,'task',task_id)
                    actual = _result_parameters(self.db, task_id)
                    for field in ('parameters', 'partitionParameters'):
                        if field in requested and requested[field] != actual[field]:
                            raise ValueError('参数必须与来源成果实际计算值一致')
                    if 'parameterTransfer' in requested:
                        from .parameter_transfer import normalize_transfer_policy
                        if normalize_transfer_policy(requested['parameterTransfer']) != actual['parameterTransfer']:
                            raise ValueError('参数传递策略必须与来源成果实际策略一致')
                    return self.send_json(save_parameters_from_task(self.db, task_id, value.get('name')))
            table = 'parameter_versions' if path == 'parameters' else 'schemes'
            item = dict(value['payload'])
            from .permanent_deletion import guard_parameter_source, guard_pending
            with self.server.jobs.lock:
                guard_parameter_source(self.db,item.get('adoptedParameterId'))
                guard_pending(self.db,'task',item.get('sourceTaskId'))
                identifier = ('PARAM-' if path == 'parameters' else 'SCHEME-') + uuid.uuid4().hex[:16]
                self.db.save_item(table, identifier, value['name'], item)
                return self.send_json({'id': identifier})
        if path.startswith('states/'):
            identifier = path.split('/')[1]
            from .permanent_deletion import guard_state
            with self.server.jobs.lock:
                guard_state(self.db,identifier)
                with self.db.connect() as conn:
                    if not conn.execute('SELECT id FROM states WHERE id=?', (identifier,)).fetchone():
                        raise FileNotFoundError('状态不存在')
                    conn.execute('UPDATE states SET name=?,favorite=? WHERE id=?', (str(value.get('name', ''))[:120], int(bool(value.get('favorite'))), identifier))
            return self.send_json({'status': 'saved'})
        if path == 'backup':
            from .maintenance import backup_workspace
            from .permanent_deletion import guard_no_pending
            with self.server.jobs.lock:
                guard_no_pending(self.db)
                if self.server.jobs.active():
                    raise ValueError('请在任务结束后备份，确保成果与状态一致')
                return self.send_json(backup_workspace(self.db))
        if path in ('restore-preview', 'restore'):
            from .maintenance import restore_request_preview
            if path == 'restore-preview':
                return self.send_json(restore_request_preview(self.db,value))
            return self.send_json(self.server.jobs.submit('restore-workspace',value),202)
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
        guard_restore_writes(self.server.jobs,'upload',uploading=True)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        name = query.get('filename', '')
        if not name or '/' in name or '\\' in name or ':' in name or name.startswith('.'):
            raise ValueError('文件名不合法')
        extension = Path(name).suffix.lower()
        exchange = query.get('format') == 'exchange'
        if extension not in ('.xls', '.xlsx', '.csv', '.nc', '.zip') or (extension == '.csv' and not exchange):
            raise ValueError('支持Excel、NetCDF和工作区备份ZIP')
        length = int(self.headers.get('Content-Length', 0))
        if not 0 < length <= upload_size_limit(extension):
            raise ValueError('ZIP大小须为1字节至50GiB；其他文件须为1字节至4GiB')
        if extension in ('.xls', '.xlsx') and not exchange:
            station = query.get('station')
            mapping = {'BLQ': 'blq', '115DB': '115daoban', 'DUJA': 'dj', 'SLC': 'slc'}
            if station not in mapping:
                raise ValueError('上传Excel前请选择对应站点')
            destination = self.db.workspace / 'observation-incoming' / mapping[station] / 'imports' / uuid.uuid4().hex[:12] / name
        else:
            destination = self.db.workspace / 'incoming' / (uuid.uuid4().hex[:12] + '-' + name)
        receive_upload(self.rfile,destination,length,extension)
        return self.send_json({'filename': destination.name, 'bytes': length})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8882)
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
