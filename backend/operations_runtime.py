"""Local background acquisition and scheduled forecast/report tasks."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import threading
import time

from .common import ROOT, now

BEIJING = timezone(timedelta(hours=8))


def runtime_status(server):
    config = server.db.setting('operational-runtime', {})
    return {'mode': 'background' if getattr(server, 'background_mode', False) else 'desktop',
            'settings': {'mode': config.get('mode', 'desktop'), 'monitorRefreshSeconds': 60,
                         'forecast': config.get('forecast', {}), 'autostart': config.get('autostart', False)},
            'lastSchedule': server.db.setting('operational-schedule-status', {}),
            'forecastSchedule': server.db.setting('forecast-schedule', {}),
            'workspace': str(server.db.workspace), 'port': server.server_port}


def save_runtime(server, payload):
    config = server.db.setting('operational-runtime', {})
    if 'mode' in payload:
        if payload['mode'] not in ('desktop', 'background'):
            raise ValueError('运行方式须为desktop或background')
        config['mode'] = payload['mode']
    if 'forecast' in payload:
        forecast = dict(payload['forecast'])
        if forecast.get('enabled') and not forecast.get('sourceResultId'):
            raise ValueError('开启滚动预测前请选择起报状态来源')
        config['forecast'] = forecast
    server.db.set_setting('operational-runtime', config)
    result = runtime_status(server)
    result['restartRequired'] = config.get('mode') != result['mode']
    return result


def configure_autostart(server, payload):
    enabled = payload.get('enabled') is True
    task_name = '色林错平台后台采集'
    command = [sys.executable, '--background', '--no-browser', '--workspace', str(server.db.workspace), '--port', str(server.server_port)]
    if not getattr(sys, 'frozen', False):
        command[1:1] = ['-m', 'backend.server']
    if enabled:
        action = subprocess.list2cmdline(command)
        # The task runs in this project directory so source-mode module loading
        # and the installed executable share the same workspace.
        escaped_action = action.replace("'", "''")
        escaped_root = str(ROOT).replace("'", "''")
        script = "$a=New-ScheduledTaskAction -Execute '" + command[0].replace("'", "''") + "' -Argument '" + subprocess.list2cmdline(command[1:]).replace("'", "''") + "' -WorkingDirectory '" + escaped_root + "';$t=New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name);Register-ScheduledTask -TaskName '" + task_name + "' -Action $a -Trigger $t -Description '色林错监测采集与滚动预测' -Force | Out-Null"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-Command', script], capture_output=True, text=True, timeout=30)
    else:
        result = subprocess.run(['schtasks.exe', '/Change', '/TN', task_name, '/DISABLE'], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('登录自启设置失败：' + (result.stderr or result.stdout).strip())
    config = server.db.setting('operational-runtime', {})
    config.update(autostart=enabled)
    server.db.set_setting('operational-runtime', config)
    return runtime_status(server)


class OperationalScheduler:
    def __init__(self, server):
        self.server = server
        self.closed = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.watch, daemon=True, name='selinco-operational-scheduler')
        self.thread.start()

    def stop(self):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=3)

    def watch(self):
        while not self.closed.wait(15):
            try:
                self.tick()
            except Exception as error:
                self.server.db.set_setting('operational-schedule-status', {'at': now(), 'status': 'failed', 'message': str(error)[:800]})

    def tick(self, instant=None):
        db, jobs = self.server.db, self.server.jobs
        if jobs.restore_pending or jobs.restart_required or jobs.active():
            return
        instant = instant or datetime.now(BEIJING)
        from .data_connections import due_connections
        for connection in due_connections(db):
            identifier = connection['id'] if isinstance(connection, dict) else connection
            jobs.submit('data-sync', {'connectionId': identifier, 'name': '定时采集监测资料'})
            return
        from .warnings import expire
        expire(db)
        from .reports import due_reports
        for report in due_reports(db, instant):
            jobs.submit('report', report)
            return
        config = db.setting('operational-runtime', {}).get('forecast', {})
        if not config.get('enabled'):
            return
        # Two local acquisition windows; each archive records its real provider
        # coverage instead of presenting the window as a provider issue time.
        slot = instant.strftime('%Y-%m-%d') + ('-am' if instant.hour < 12 else '-pm')
        schedule = db.setting('forecast-schedule', {})
        if schedule.get('slot') == slot:
            task = db.task(schedule['taskId'])
            if task['status'] == 'failed' and task['kind'] == 'forecast-input-ensemble':
                attempt = int(schedule.get('attempt', 1))
                attempted_at = datetime.fromisoformat(schedule['at'])
                current = instant if instant.tzinfo else instant.replace(tzinfo=BEIJING)
                if (attempt < 3 and
                        current.astimezone(timezone.utc) >= attempted_at.astimezone(timezone.utc) + timedelta(minutes=5)):
                    retry = jobs.submit('forecast-input-ensemble', {
                        **config, 'days': 15, 'name': '定时重试集合气象'})
                    db.set_setting('forecast-schedule', {
                        'slot': slot, 'taskId': retry['id'], 'at': now(), 'attempt': attempt + 1})
                return
            if task['status'] == 'succeeded' and task['kind'] == 'forecast-input-ensemble':
                identifier = (task['result'] or {}).get('id') or (task['result'] or {}).get('inputId')
                if identifier:
                    from .forecasts import preflight
                    request = {**config, 'inputId': identifier, 'name': '定时集合预测'}
                    ready = preflight(db, request)
                    if ready.get('ready'):
                        forecast = jobs.submit('forecast', request)
                        db.set_setting('forecast-schedule', {
                            'slot': slot, 'taskId': forecast['id'], 'at': now(), 'attempt': 1})
                    else:
                        db.set_setting('operational-schedule-status', {'at': now(), 'status': 'waiting-data', 'message': '；'.join(ready.get('deficiencies', []))})
            return
        task = jobs.submit('forecast-input-ensemble', {**config, 'days': 15, 'name': '定时获取集合气象'})
        db.set_setting('forecast-schedule', {
            'slot': slot, 'taskId': task['id'], 'at': now(), 'attempt': 1})
