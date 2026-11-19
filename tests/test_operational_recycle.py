import tempfile
import unittest
from pathlib import Path

from backend.database import Database
from backend import operational_store as store


class OperationalRecycleTests(unittest.TestCase):
    def test_recycle_hides_record_and_restore_brings_it_back(self):
        db = Database(Path(tempfile.mkdtemp(prefix='selinco-recycle-')))
        kept = store.save(db, 'forecasts', {'name': '正式预报'}, identifier='FORECAST-a')
        store.save(db, 'forecasts', {'name': '测试预报'}, identifier='FORECAST-b')
        store.set_trashed(db, 'forecasts', 'FORECAST-b', True, '测试记录')
        self.assertEqual([r['id'] for r in store.list_records(db, 'forecasts')], [kept['id']])
        trashed = store.list_records(db, 'forecasts', trashed=True)
        self.assertEqual([r['id'] for r in trashed], ['FORECAST-b'])
        self.assertEqual(trashed[0]['trashReason'], '测试记录')
        # The record itself stays readable for results that reference it.
        self.assertEqual(store.get(db, 'forecasts', 'FORECAST-b')['name'], '测试预报')
        store.set_trashed(db, 'forecasts', 'FORECAST-b', False)
        self.assertEqual(sorted(r['id'] for r in store.list_records(db, 'forecasts')), ['FORECAST-a', 'FORECAST-b'])
        self.assertNotIn('trashed', store.get(db, 'forecasts', 'FORECAST-b'))


if __name__ == '__main__':
    unittest.main()
