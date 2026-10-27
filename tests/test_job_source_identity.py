"""A worker must not label already-imported code with a newer disk identity."""

import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from backend.jobs import Jobs


class QueueFixture:
    workspace = Path('unused-workspace')

    def __init__(self):
        self.operations = []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, sql, args=()):
        self.operations.append((sql, args))

    def task(self, identifier):
        return {'id': identifier, 'kind': 'simulation', 'request': {}}


class JobSourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.db = QueueFixture()
        with patch('backend.common.model_identity', return_value='startup-sha'):
            self.jobs = Jobs(self.db)

    def tearDown(self):
        self.jobs.executor.shutdown(wait=True)

    def test_changed_source_rejects_new_calculation_before_queueing(self):
        with patch('backend.common.model_identity', return_value='changed-sha'), \
             patch.object(self.jobs, '_submit') as submit:
            with self.assertRaisesRegex(ValueError, '源码已变化.*重启'):
                self.jobs.submit('simulation', {})
            submit.assert_not_called()

    def test_changed_source_fails_queued_task_before_running(self):
        with patch('backend.common.model_identity', return_value='changed-sha'), \
             patch.object(Path, 'write_text') as write_error, \
             patch('backend.jobs.run_simulation') as runner:
            self.jobs._execute('JOB-queued')
        runner.assert_not_called()
        write_error.assert_called_once()
        failed = [(sql, args) for sql, args in self.db.operations if "status='failed'" in sql]
        self.assertEqual(len(failed), 1)
        self.assertIn('源码已变化', failed[0][1][0])
        self.assertIn('重启', failed[0][1][0])

    def test_same_source_allows_submission(self):
        with patch('backend.common.model_identity', return_value='startup-sha'), \
             patch.object(self.jobs, '_submit', return_value={'id': 'ok'}) as submit:
            self.assertEqual(self.jobs.submit('simulation', {}), {'id': 'ok'})
            submit.assert_called_once()

    def test_changed_source_does_not_block_backup(self):
        with patch('backend.common.model_identity', return_value='changed-sha'), \
             patch.object(self.jobs, '_submit', return_value={'id': 'backup'}):
            self.assertEqual(self.jobs.submit('backup', {}), {'id': 'backup'})


if __name__ == '__main__':
    unittest.main()
