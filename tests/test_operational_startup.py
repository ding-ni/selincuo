"""Saved desktop/background startup and graceful task cancellation."""
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

from backend.database import Database
from backend import server as platform_server


class OperationalStartupTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[1] / 'build' / ('后台启动检查-' + uuid.uuid4().hex[:10])
        self.db = Database(folder)

    def start(self, saved_mode=None, arguments=(), frozen=True):
        if saved_mode:
            self.db.set_setting('operational-runtime', {'mode': saved_mode})
        server = Mock(db=self.db, server_port=8899)
        with patch.object(platform_server, 'Platform', return_value=server) as platform, \
                patch.object(platform_server.sys, 'argv', ['platform', '--no-browser', *arguments]), \
                patch.object(platform_server.sys, 'frozen', frozen, create=True), \
                patch.object(platform_server, 'read_json', return_value={'port': 8899}), \
                patch('backend.operations_runtime.OperationalScheduler') as scheduler, \
                patch('backend.browser_lifetime.BrowserLifetime') as lifetime, \
                patch.object(platform_server.threading, 'Thread'), patch('builtins.print'):
            platform_server.main()
        platform.assert_called_once_with(('127.0.0.1', 8899), None)
        scheduler.return_value.start.assert_called_once()
        scheduler.return_value.stop.assert_called_once()
        server.server_close.assert_called_once()
        return server, lifetime

    def test_saved_background_survives_installed_restart(self):
        server, lifetime = self.start('background')
        self.assertTrue(server.background_mode)
        lifetime.assert_not_called()

    def test_saved_desktop_closes_with_browser(self):
        server, lifetime = self.start('desktop')
        self.assertFalse(server.background_mode)
        lifetime.assert_called_once_with(server.jobs, server.shutdown)

    def test_login_background_argument_overrides_desktop_setting(self):
        server, lifetime = self.start('desktop', ['--background'])
        self.assertTrue(server.background_mode)
        lifetime.assert_not_called()

    def test_explicit_desktop_argument_overrides_background_setting(self):
        server, lifetime = self.start('background', ['--close-with-browser'])
        self.assertFalse(server.background_mode)
        lifetime.assert_called_once()

    def test_fresh_install_keeps_desktop_lifetime(self):
        server, lifetime = self.start()
        self.assertFalse(server.background_mode)
        lifetime.assert_called_once()

    def test_source_without_saved_mode_keeps_development_server(self):
        server, lifetime = self.start(frozen=False)
        self.assertFalse(server.background_mode)
        lifetime.assert_not_called()

    def test_executable_probes_configured_port_before_workspace_changes(self):
        import app_entry
        with patch.object(app_entry.sys, 'argv', ['platform', '--no-browser']), \
                patch.object(app_entry, 'running_instance', return_value=True) as existing, \
                patch('backend.common.read_json', return_value={'port': 8899}), \
                patch('backend.server.main') as serve, patch('builtins.print'):
            app_entry.main()
        self.assertEqual(existing.call_args.args[0], 8899)
        serve.assert_not_called()

    def test_shutdown_cancels_active_task_after_checkpoint_is_saved(self):
        from backend.jobs import Jobs
        jobs = Jobs(self.db)
        started, stopped = threading.Event(), threading.Event()
        checkpoint = self.db.workspace / '已停止任务进度.txt'

        def execute(db, kind, request, folder, progress, cancelled):
            started.set()
            while not cancelled():
                threading.Event().wait(.01)
            checkpoint.write_text('saved progress', encoding='utf-8')
            raise InterruptedError('任务已停止，进度已保存')

        def shutdown():
            self.assertEqual(checkpoint.read_text(encoding='utf-8'), 'saved progress')
            stopped.set()

        runtime = SimpleNamespace(db=self.db, jobs=jobs, operations_scheduler=Mock(), shutdown=shutdown)
        handler = object.__new__(platform_server.Handler)
        handler.server, handler.path = runtime, '/api/model/shutdown'
        handler.document, handler.send_json = lambda: {}, Mock()
        with patch('backend.operations_api.execute', side_effect=execute):
            task = jobs.submit('data-sync', {'connectionId': 'isolated-startup-test'})
            self.assertTrue(started.wait(5), 'isolated task did not start')
            platform_server.Handler.post(handler)
            handler.send_json.assert_called_once_with({'status': 'stopping'})
            self.assertTrue(stopped.wait(5), 'server did not finish stopping its task')
        self.assertEqual(self.db.task(task['id'])['status'], 'cancelled')
        runtime.operations_scheduler.stop.assert_called_once()

    def test_failed_ensemble_fetch_retries_twice_inside_the_same_slot(self):
        from datetime import datetime, timedelta, timezone
        from backend.operations_runtime import BEIJING, OperationalScheduler
        instant = datetime(2026, 11, 15, 20, 0, tzinfo=BEIJING)
        slot = instant.strftime('%Y-%m-%d') + '-pm'
        attempted_at = (instant.astimezone(timezone.utc) - timedelta(minutes=6)).isoformat()
        self.db.set_setting('operational-runtime', {'forecast': {'enabled': True, 'sourceResultId': 'JOB-test'}})
        self.db.set_setting('forecast-schedule', {
            'slot': slot, 'taskId': 'FETCH-FAILED', 'at': attempted_at, 'attempt': 1})
        jobs = SimpleNamespace(restore_pending=False, restart_required=False, active=lambda: [],
                               submit=Mock(return_value={'id': 'FETCH-RETRY-2'}))
        runtime = SimpleNamespace(db=self.db, jobs=jobs)
        failed = {'id': 'FETCH-FAILED', 'kind': 'forecast-input-ensemble', 'status': 'failed'}
        scheduler = OperationalScheduler(runtime)
        with patch('backend.data_connections.due_connections', return_value=[]), \
                patch('backend.warnings.expire'), \
                patch('backend.reports.due_reports', return_value=[]), \
                patch.object(self.db, 'task', return_value=failed):
            scheduler.tick(instant)
        jobs.submit.assert_called_once_with('forecast-input-ensemble', {
            'enabled': True, 'sourceResultId': 'JOB-test', 'days': 15, 'name': '定时重试集合气象'})
        schedule = self.db.setting('forecast-schedule')
        self.assertEqual((schedule['taskId'], schedule['attempt']), ('FETCH-RETRY-2', 2))
        jobs.submit.reset_mock()
        self.db.set_setting('forecast-schedule', {
            'slot': slot, 'taskId': 'FETCH-FAILED-3', 'at': attempted_at, 'attempt': 3})
        with patch('backend.data_connections.due_connections', return_value=[]), \
                patch('backend.warnings.expire'), \
                patch('backend.reports.due_reports', return_value=[]), \
                patch.object(self.db, 'task', return_value=failed):
            scheduler.tick(instant)
        jobs.submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
