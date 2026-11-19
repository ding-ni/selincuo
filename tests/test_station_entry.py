import tempfile
import unittest

import pandas as pd

from backend import station_entry, state_update
from backend.data_exchange import import_records
from backend.database import Database


class StationEntryTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(tempfile.mkdtemp(prefix='selinco-entry-test-'))
        with self.db.connect() as c:
            c.execute('INSERT INTO stations VALUES(?,?,?,?,?,?)', ('SLC', '色林错主站', 'lake', 89., 32., '{}'))
            c.execute('INSERT INTO stations VALUES(?,?,?,?,?,?)', ('DJ', '杜加', 'rain', 89.1, 31.9, '{}'))
        # One existing record each so the stations register lists the variables.
        import_records(self.db, {'schemaVersion': 1, 'projectId': 'selinco', 'sourceKey': 'seed', 'records': [
            {'station': 'SLC', 'variable': 'level', 'time': '2026-10-01T08:00:00', 'step': 'irregular', 'value': 4544.1, 'unit': 'm'},
            {'station': 'DJ', 'variable': 'precipitation', 'time': '2026-10-01', 'step': 'day', 'value': 1.0, 'unit': 'mm'}]})

    def test_entries_are_stored_and_read_back_as_station_observations(self):
        listed = {s['id']: s['variables'] for s in station_entry.stations(self.db)}
        self.assertIn('precipitation', listed['DJ'])
        self.assertIn('level', listed['SLC'])
        result = station_entry.save(self.db, {'records': [
            {'station': 'DJ', 'variable': 'precipitation', 'date': '2026-10-22', 'value': 6.4},
            {'station': 'SLC', 'variable': 'level', 'date': '2026-10-22', 'value': 4544.21}]})
        self.assertEqual(result['saved'], 2)
        day = pd.Timestamp('2026-10-22')
        rain = state_update._daily_values(self.db, 'DJ', 'precipitation', day, day)
        self.assertAlmostEqual(rain[day], 6.4)
        levels = state_update._level_observations(self.db, day, day)
        self.assertAlmostEqual(levels[day]['level'], 4544.21)
        self.assertEqual(levels[day]['hour'], 8)

    def test_new_entry_replaces_same_day_and_keeps_others(self):
        station_entry.save(self.db, {'records': [{'station': 'DJ', 'variable': 'precipitation', 'date': '2026-10-22', 'value': 6.4}]})
        station_entry.save(self.db, {'records': [{'station': 'DJ', 'variable': 'precipitation', 'date': '2026-10-22', 'value': 3.0},
                                                 {'station': 'DJ', 'variable': 'precipitation', 'date': '2026-10-23', 'value': 0.0}]})
        a, b = pd.Timestamp('2026-10-22'), pd.Timestamp('2026-10-23')
        rain = state_update._daily_values(self.db, 'DJ', 'precipitation', a, b)
        self.assertAlmostEqual(rain[a], 3.0)
        self.assertAlmostEqual(rain[b], 0.0)
        # The seed record from another source stays.
        seed = pd.Timestamp('2026-10-01')
        self.assertAlmostEqual(state_update._daily_values(self.db, 'DJ', 'precipitation', seed, seed)[seed], 1.0)

    def test_rejects_out_of_range_and_unknown_stations(self):
        for bad in ({'station': 'DJ', 'variable': 'precipitation', 'date': '2026-10-22', 'value': -1},
                    {'station': 'SLC', 'variable': 'level', 'date': '2026-10-22', 'value': 4600},
                    {'station': 'XX', 'variable': 'precipitation', 'date': '2026-10-22', 'value': 1},
                    {'station': 'DJ', 'variable': 'level', 'date': '2026-10-22', 'value': 4544}):
            with self.assertRaises(ValueError):
                station_entry.save(self.db, {'records': [bad]})


if __name__ == '__main__':
    unittest.main()
