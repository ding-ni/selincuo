import unittest
from unittest.mock import Mock
from backend.browser_lifetime import BrowserLifetime


class BrowserLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.stamp = 0
        self.jobs = Mock()
        self.jobs.active.return_value = []
        self.shutdown = Mock()
        self.lifetime = BrowserLifetime(self.jobs, self.shutdown, clock=lambda: self.stamp)

    def test_does_not_exit_before_first_page_opens(self):
        self.stamp = 1000
        self.assertFalse(self.lifetime.tick())

    def test_last_tab_closes_after_grace(self):
        for tab in ('first', 'second'):
            self.lifetime.update(tab, 'open')
        self.lifetime.update('first', 'close');self.stamp = 8
        self.assertFalse(self.lifetime.tick())
        self.lifetime.update('second', 'close');self.stamp = 14
        self.assertTrue(self.lifetime.tick())
        self.shutdown.assert_called_once()

    def test_refresh_does_not_close_server(self):
        self.lifetime.update('old', 'open');self.lifetime.update('old', 'close')
        self.stamp = 2;self.lifetime.update('new', 'open');self.stamp = 10
        self.assertFalse(self.lifetime.tick())
        self.shutdown.assert_not_called()

    def test_stops_and_waits_for_tasks_before_shutdown(self):
        self.jobs.active.return_value = [{'id': 'JOB-isolated'}]
        self.lifetime.update('page', 'open');self.lifetime.update('page', 'close')
        self.stamp = 6
        self.assertFalse(self.lifetime.tick())
        self.jobs.stop.assert_called_once_with('JOB-isolated')
        self.shutdown.assert_not_called()
        self.jobs.active.return_value = []
        self.assertTrue(self.lifetime.tick())

    def test_unexpected_browser_disconnect_expires(self):
        self.lifetime.update('page', 'open');self.stamp = 181
        self.assertFalse(self.lifetime.tick())
        self.stamp = 187
        self.assertTrue(self.lifetime.tick())

    def test_reopening_during_stop_allows_later_jobs_to_stop(self):
        self.jobs.active.return_value = [{'id': 'JOB-isolated'}]
        self.lifetime.update('first', 'open');self.lifetime.update('first', 'close')
        self.stamp = 6;self.lifetime.tick()
        self.lifetime.update('new', 'open');self.lifetime.update('new', 'close')
        self.stamp = 12;self.lifetime.tick()
        self.assertEqual(self.jobs.stop.call_count, 2)
