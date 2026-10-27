"""Verify software workflows in a disposable workspace, not new scientific admission."""
import argparse
import json
import time
import sys
import hashlib
import sqlite3
import urllib.error
import urllib.request
from urllib.parse import urlencode, urljoin, urlsplit
from pathlib import Path
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import VERSION, now, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8882')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-version', default=VERSION)
    parser.add_argument('--workspace', type=Path, required=True,
                        help='Exact disposable workspace; refuse unexpected or user workspaces')
    args = parser.parse_args()

    def api(path, data=None, method=None):
        request = urllib.request.Request(args.url + '/api/model/' + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type': 'application/json'}, method=method)
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response)

    def poll(task_id):
        deadline = time.time() + 900
        while time.time() < deadline:
            task = api('tasks/' + task_id)
            if task['status'] in ('succeeded', 'failed', 'cancelled', 'interrupted'):
                if task['status'] != 'succeeded':
                    raise RuntimeError(task_id+' '+task['status']+'：'+task['message'])
                return task
            time.sleep(.5)
        raise TimeoutError('内置方案安装验证超过900秒：'+task_id)

    def download(artifact):
        assert artifact.get('complete') is True, artifact
        url = urljoin(args.url, artifact['downloadUrl'])
        assert urlsplit(url).netloc == urlsplit(args.url).netloc
        with urllib.request.urlopen(url, timeout=180) as response:
            content = response.read()
        assert len(content) == artifact['bytes']
        assert hashlib.sha256(content).hexdigest() == artifact['sha256']
        return content

    health = json.load(urllib.request.urlopen(args.url + '/api/health', timeout=10))
    expected_workspace = args.workspace.resolve()
    if expected_workspace in ((ROOT/'workspace').resolve(), (ROOT/'运行程序/workspace').resolve()):
        raise ValueError('安装验收不能在源码或已安装业务工作区执行')
    if Path(health['workspace']).resolve() != expected_workspace:
        raise ValueError('验收端口对应的工作区与指定临时目录不一致')
    boot = api('bootstrap')
    assert health['projectId'] == boot['projectId'] == 'selinco'
    assert boot['version'] == args.expected_version, boot['version']
    assert boot['database']['counts']['tasks'] == 0
    assert boot['database']['counts']['parameter_versions'] == 0
    assert boot['database']['counts']['states'] == 0
    context = api('business-context')
    default = context['defaultResult']
    assert context['release']['resultId'] == boot['standardResultId'] == default['id']
    assert default['role'] == 'current-default'
    assert default['evaluation']['mode'] == 'window'
    assert default['evaluation']['formalScoresFollowChartZoom'] is False
    assert context['defaultResultInputId'] == default['inputId']
    assert len(boot['inputs']) == 1 and boot['inputs'][0]['id'] == default['inputId']
    assert context['forcing']['selectedResult']['inputId'] == default['inputId']
    builtin = api('published-result?'+urlencode({'id':default['id']}))
    assert builtin['status'] == 'succeeded' and builtin['result']['daily']
    assert builtin['result']['inputId'] == default['inputId']
    summary = api('result-summary?'+urlencode({'resultId':default['id']}))
    assert summary['identity'] == builtin['result']['identity']
    assert summary['evaluation']['annual'] == builtin['result']['metrics']
    scheme = next(s for s in boot['operationalSchemes'] if s['id'] == 'selinco-historical-standard-v1')
    assert scheme['available'], scheme['message']
    plan = api('simulation-plan?'+urlencode({'schemeId':scheme['id'],'schemeRevision':scheme['revision'],
               'inputId':default['inputId'],'start':'2025-01-01T00:00','end':'2026-01-01T00:00'}))
    assert plan['action'] == 'reuse' and plan['resultId'] == default['id'] and plan['createsTask'] is False
    assert api('bootstrap')['database']['counts']['tasks'] == 0, '只读运行计划不能产生任务'
    request = {'kind':'simulation','schemeId':scheme['id'],'schemeRevision':scheme['revision'],
               'inputId':default['inputId'],
               'start':'2025-01-01T00:00','end':'2026-01-01T00:00','name':'v'+args.expected_version+'软件验收·2025历史方案复算',
               'clientRequestId':'packaged-'+args.expected_version+'-standard-history-verification'}
    submitted = api('tasks', request)
    duplicate = api('tasks', request)
    assert submitted['id'] == duplicate['id']
    task = poll(submitted['id'])
    assert task['favorite'] and task['collection'] == '历史模拟'
    assert task['request']['start'] == '2023-01-01T00:00:00'
    assert task['request']['evaluationStart'] == '2025-01-01T00:00:00'
    result = task['result']
    assert result['scheme']['id'] == scheme['id']
    assert result['parameters'] == scheme['parameters']
    assert result['partitionParameters'] == scheme['partitionParameters']
    assert len(result['daily']) == 365
    assert set(result['metrics']) == {'2025'}
    expected = builtin['result']['metrics']['2025']
    for group, keys in (('flow',('nse','pbias')),('lake',('rmse','mae','bias'))):
        for key in keys:
            assert abs(result['metrics']['2025'][group][key]-expected[group][key]) < 1e-9, (group,key)
    assert result['diagnostics']['landGlacierMaxBalanceErrorMm'] < 1e-9
    assert result['modelDiagnostics']['catchmentBalanceMaxErrorM3'] < .001
    reference_days={row['date'][:10]:row for row in builtin['result']['daily'] if row['date'].startswith('2025')}
    assert len(reference_days) == 365
    for row in result['daily']:
        reference=reference_days[row['date'][:10]]
        for key in ('lakeLevel','duja','total'):
            a,b=row.get(key),reference.get(key)
            assert (a is None and b is None) or a is not None and b is not None and abs(a-b)<1e-9,(row['date'],key,a,b)
    exported = poll(api('tasks',{'kind':'export-result','taskId':task['id'],'format':'daily.csv',
                               'name':'软件验收·后台日成果导出','clientRequestId':'packaged-daily-export'})['id'])
    export_again = api('tasks',{'kind':'export-result','taskId':task['id'],'format':'daily.csv',
                              'name':'软件验收·复用已完成导出','clientRequestId':'packaged-daily-export-reuse'})
    assert export_again['id'] == exported['id']
    csv_lines = download(exported['result']['artifacts'][0]).decode('utf-8-sig').strip().splitlines()
    assert len(csv_lines) == 366
    continued = poll(api('tasks', {'kind':'continuation','inputId':result['inputId'],
        'start':'2026-01-01T01:00:00','end':'2026-01-01T02:00:00','stateId':result['stateId'],
        'parameters':result['parameters'],'partitionParameters':result['partitionParameters'],
        'routingMode':result['routingMode'],'name':'软件验收·状态续算'})['id'])
    state = api('states/' + result['stateId'])['state']
    assert state['identity'] == result['identity']
    assert continued['result']['identity'] == result['identity']
    assert continued['result']['hours'] == 2
    after = api('business-context')
    assert after['release']['id'] == context['release']['id']
    assert after['defaultResult']['identity'] == default['identity']
    backup_task = poll(api('tasks',{'kind':'backup','name':'软件验收·后台完整备份',
                                  'clientRequestId':'packaged-background-backup'})['id'])
    backup = backup_task['result']['artifacts'][0]
    archive = download(backup)
    upload = urllib.request.Request(args.url + '/api/model/upload?filename=verification-backup.zip',
                                    data=archive, method='PUT')
    uploaded = json.load(urllib.request.urlopen(upload, timeout=180))
    preview = api('restore-preview', {'filename':uploaded['filename']})
    assert preview['files'] > 0 and preview['bytes'] > 0 and preview['token']
    restore_task = poll(api('tasks', {'kind':'restore-workspace','filename':uploaded['filename'],'token':preview['token'],'name':'软件验收·后台恢复','clientRequestId':'packaged-background-restore'})['id'])
    restored = restore_task['result']
    assert restored['restartRequired'] is True
    restored_path=Path(restored['workspace']).resolve()
    assert restored_path != expected_workspace and (restored_path/'database/hydrology.sqlite3').is_file()
    with closing(sqlite3.connect((restored_path/'database/hydrology.sqlite3').as_uri()+'?mode=ro',uri=True)) as connection:
        restored_release=connection.execute('SELECT id,result_id FROM operational_releases ORDER BY sequence DESC LIMIT 1').fetchone()
        assert restored_release == (context['release']['id'],default['id'])
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert connection.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == boot['database']['counts']['observations']
    report = {'at':now(),'projectId':'selinco','version':boot['version'],'workspace':health['workspace'],
        'freshCounts':boot['database']['counts'],'scheme':task['result']['scheme'],
        'taskId':task['id'],'continuationTaskId':continued['id'],'dailyRows':len(result['daily']),
        'metrics':result['metrics'],'evaluationMode':summary['evaluation']['mode'],'defaultRelease':context['release'],
        'defaultInputId':default['inputId'],'readOnlySimulationPlan':plan,
        'exportRows':len(csv_lines)-1,'exportTaskId':exported['id'],'exportReusedTaskId':export_again['id'],
        'backupTaskId':backup_task['id'],'backup':backup,'restore':restored,'restoreTaskId':restore_task['id'],
        'modelIdentity':result['identity'],'catchmentBalanceMaxErrorM3':result['modelDiagnostics']['catchmentBalanceMaxErrorM3'],
        'checks':['clean-seed','published-default-context-and-frozen-input','window-evaluation-explicit','builtin-result-viewable',
                  'read-only-covered-window-reuse','builtin-scheme-available','fixed-parameters-server-owned',
                  '2023-land-warmup-with-original-window-lake-initialization','duplicate-request-idempotency','2025-full-year-result',
                  'background-CSV-export-and-artifact-reuse','saved-state-continuation','background-backup-artifact-download',
                  'background-independent-directory-restore','default-release-unchanged','frozen-reference-metrics-match',
                  'frozen-reference-daily-values-match','water-balance'],
        'acceptanceScope':'packaged-software-workflow','scientificAdmission':'not-assessed-by-this-script',
        'candidateUpgradePassed':False,'passed':True}
    write_json(args.output, report)
    print(json.dumps(report,ensure_ascii=False))


if __name__ == '__main__':
    main()
