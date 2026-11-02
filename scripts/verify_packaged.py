"""Verify software workflows in a disposable workspace, not new scientific admission."""
import argparse
import csv
import io
from datetime import datetime, timedelta
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
    assert boot['database']['counts']['parameter_versions'] == 1
    assert boot['database']['counts']['states'] == 0
    context = api('business-context')
    default = context['defaultResult']
    assert context['release']['resultId'] == boot['standardResultId']
    assert context['release']['status'] == 'protected-legacy'
    assert context['release']['admissionPassed'] is False
    assert default['id'] == boot['homeResultId'] == context['adopted']['resultId']
    assert default['role'] == 'current-adopted'
    assert context['defaultResultInputId'] == default['inputId']
    assert len(boot['schemes']) == 1 and boot['schemes'][0]['id'] == context['adopted']['schemeId']
    assert len(boot['parameters']) == 1 and boot['parameters'][0]['id'] == context['adopted']['parameterId']
    protected = api('published-result?'+urlencode({'id':boot['standardResultId']}))
    protected_input = protected['result']['inputId']
    assert protected['status'] == 'succeeded' and protected['result']['identity']
    assert len(boot['inputs']) == 2
    assert {row['id'] for row in boot['inputs']} == {default['inputId'],protected_input}
    assert context['forcing']['selectedResult']['inputId'] == default['inputId']
    builtin = api('published-result?'+urlencode({'id':default['id']}))
    assert builtin['status'] == 'succeeded' and builtin['result']['daily']
    assert builtin['result']['inputId'] == default['inputId']
    summary = api('result-summary?'+urlencode({'resultId':default['id']}))
    assert summary['identity'] == builtin['result']['identity']
    assert summary['evaluation']['annual'] == builtin['result']['metrics']
    scheme = boot['schemes'][0]
    draft = api('research-template')
    assert draft['name'] == '我的色林错方案'
    configuration = api('schemes/'+scheme['id']+'/calibration-configuration')
    assert configuration['initialParameters'] == scheme['payload']['calibrationInitialParameters']
    assert configuration['matchesCurrentImplementation'] is True
    assert configuration['form']['search'] == scheme['payload']['search']
    plan_request={'researchSchemeId':scheme['id'],'inputId':default['inputId'],
                  'start':'2026-10-16','end':'2026-10-18'}
    plan = api('simulation-plan?'+urlencode(plan_request))
    assert plan['inputId'] and plan['calculationStart'] <= '2026-10-18'
    assert api('bootstrap')['database']['counts']['tasks'] == 0, '只读运行计划不能产生任务'
    request = {'kind':'simulation','researchSchemeId':scheme['id'],'inputId':default['inputId'],
               'start':'2026-10-16','end':'2026-10-18','name':'v'+args.expected_version+'软件验收·业务方案试算',
               'clientRequestId':'packaged-'+args.expected_version+'-business-scheme-verification'}
    submitted = api('tasks', request)
    duplicate = api('tasks', request)
    assert submitted['id'] == duplicate['id']
    task = poll(submitted['id'])
    assert task['request']['researchSchemeId'] == scheme['id']
    assert task['request']['evaluationStart'] == '2026-10-16T00:00:00'
    result = task['result']
    assert result['scheme']['id'] == scheme['id']
    assert result['parameters'] == scheme['payload']['parameters']
    assert result['partitionParameters'] == scheme['payload']['partitionParameters']
    assert len(result['daily']) > 0
    assert result['diagnostics']['landGlacierMaxBalanceErrorMm'] < 1e-9
    assert result['diagnostics']['routingMaxBalanceErrorM3'] < .001
    assert result['inputId'] in {item['id'] for item in api('bootstrap')['inputs']}
    exported = poll(api('tasks',{'kind':'export-result','taskId':task['id'],'format':'daily.csv',
                               'name':'软件验收·后台日成果导出','clientRequestId':'packaged-daily-export'})['id'])
    export_again = api('tasks',{'kind':'export-result','taskId':task['id'],'format':'daily.csv',
                              'name':'软件验收·复用已完成导出','clientRequestId':'packaged-daily-export-reuse'})
    assert export_again['id'] == exported['id']
    csv_rows = list(csv.DictReader(io.StringIO(download(exported['result']['artifacts'][0]).decode('utf-8-sig'))))
    assert len(csv_rows) == len(result['daily']) and csv_rows
    assert [row['date'][:10] for row in csv_rows] == [row['date'][:10] for row in result['daily']]
    state = api('states/' + result['stateId'])['state']
    valid_at = datetime.fromisoformat(state['validAt'])
    continuation_start = valid_at + timedelta(hours=1)
    continuation_end = valid_at + timedelta(hours=2)
    continued = poll(api('tasks', {'kind':'continuation','inputId':result['inputId'],
        'start':continuation_start.isoformat(timespec='seconds'), 'end':continuation_end.isoformat(timespec='seconds'),
        'stateId':result['stateId'], 'parameters':result['parameters'],
        'partitionParameters':result['partitionParameters'],
        'routingMode':result['routingMode'],'name':'软件验收·状态续算'})['id'])
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
        assert restored_release == (context['release']['id'],context['release']['resultId'])
        restored_business=json.loads(connection.execute("SELECT value FROM settings WHERE key='business-adopted-scheme-v1'").fetchone()[0])
        assert restored_business['resultId'] == default['id']
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert connection.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == boot['database']['counts']['observations']
    report = {'at':now(),'projectId':'selinco','version':boot['version'],'workspace':health['workspace'],
        'freshCounts':boot['database']['counts'],'scheme':task['result']['scheme'],
        'taskId':task['id'],'continuationTaskId':continued['id'],'dailyRows':len(result['daily']),
        'metrics':result.get('metrics',{}),'evaluationMode':summary['evaluation']['mode'],'defaultRelease':context['release'],
        'defaultInputId':default['inputId'],'readOnlySimulationPlan':plan,
        'exportRows':len(csv_rows),'exportTaskId':exported['id'],'exportReusedTaskId':export_again['id'],
        'backupTaskId':backup_task['id'],'backup':backup,'restore':restored,'restoreTaskId':restore_task['id'],
        'modelIdentity':result['identity'],'routingBalanceMaxErrorM3':result['diagnostics']['routingMaxBalanceErrorM3'],
        'checks':['clean-seed','business-home-and-protected-science-release-separated','single-business-scheme-and-parameter',
                  'best-result-viewable-with-zarr','read-only-business-simulation-plan','business-scheme-short-simulation',
                  'duplicate-request-idempotency','water-balance',
                  'background-CSV-export-and-artifact-reuse','saved-state-continuation','background-backup-artifact-download',
                  'background-independent-directory-restore','business-adoption-and-protected-release-restored'],
        'acceptanceScope':'packaged-software-workflow','scientificAdmission':'not-assessed-by-this-script',
        'candidateUpgradePassed':False,'passed':True}
    write_json(args.output, report)
    print(json.dumps(report,ensure_ascii=False))


if __name__ == '__main__':
    main()
