"""Calibration settings can read the adopted immutable packaged result."""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.request import urlopen
from unittest.mock import patch

import pandas as pd

from backend.server import Handler


class CalibrationDataPublishedTests(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(rows=lambda *_: [{'time': '2026-10-20T00:00:00'}])
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.db = self.db
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/api/model/calibration-data'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def test_adopted_published_result_supplies_warmup_and_initialization(self):
        series = pd.Series([1.0], index=pd.DatetimeIndex(['2024-01-01']))
        adopted = {
            'request': {'start': '2017-01-01T00:00:00'},
            'result': {
                'initialization': {'time': '2021-04-23T12:00:00', 'level': 4543.66},
                'warmupDiagnostics': {'passed': True},
            },
        }
        with (patch('backend.observation_profiles.quality_series', return_value=series),
              patch('backend.model_forcing.current_summary', return_value={'inputId': 'INPUT-default'}),
              patch('backend.two_stage_calibration.defaults', return_value={}),
              patch('backend.business_selection.current', return_value={'resultId': 'JOB-published'}),
              patch('backend.business_context.result_task', return_value=adopted)):
            with urlopen(self.url, timeout=10) as response:
                result = json.load(response)

        self.assertEqual(result['warmup']['start'], '2017-01-01T00:00:00')
        self.assertEqual(result['warmup']['initialization'], adopted['result']['initialization'])
        self.assertEqual(result['warmup']['diagnostics'], {'passed': True})


if __name__ == '__main__':
    unittest.main()
