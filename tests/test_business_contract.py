import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pandas as pd

from backend.database import Database
from backend.observation_query import series
from backend.published_results import current_id


class StandardSelectionTests(unittest.TestCase):
    def test_explicit_standard_does_not_follow_catalog_order(self):
        for rows in ([{'id': 'A'}, {'id': 'B'}], [{'id': 'B'}, {'id': 'A'}]):
            with patch('backend.published_results.read_json', return_value={'currentResultId': 'A', 'results': rows}):
                self.assertEqual(current_id(), 'A')

    def test_multiple_products_need_an_explicit_selection(self):
        with patch('backend.published_results.read_json', return_value={'results': [{'id': 'A'}, {'id': 'B'}]}):
            with self.assertRaises(ValueError):
                current_id()

    def test_empty_catalog_remains_usable_for_data_management(self):
        with patch('backend.published_results.read_json', return_value={'results': []}):
            self.assertIsNone(current_id())


class ConcurrentMonitoringTests(unittest.TestCase):
    def test_simultaneous_cold_queries_share_one_aggregation(self):
        barrier = threading.Barrier(3)

        def aggregate(*args):
            time.sleep(.05)
            return pd.Series([3.5], index=pd.to_datetime(['2024-01-01']))

        with tempfile.TemporaryDirectory() as temp:
            db = Database(temp)

            def query(_):
                barrier.wait(timeout=5)
                return series(db, 'A', 'precipitation')

            with patch('backend.observation_query.daily_series', side_effect=aggregate) as compute:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    results = list(pool.map(query, range(3)))
                self.assertEqual(compute.call_count, 1)
                self.assertTrue(all(r['rows'][0]['value'] == 3.5 for r in results))


if __name__ == '__main__':
    unittest.main()
