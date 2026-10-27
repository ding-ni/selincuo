import unittest
from scripts.audit_lake_changes import changes


class LakeChangesTests(unittest.TestCase):
    def test_pairs_require_exact_dates_same_window_and_exclude_initialization_day(self):
        result={'daily':[
            {'date':'2025-01-01','observedLevel':10,'lakeLevel':10},
            {'date':'2025-01-02','observedLevel':10.1,'lakeLevel':10.1},
            {'date':'2025-01-03','observedLevel':10.2,'lakeLevel':10.2},
            {'date':'2025-01-05','observedLevel':10.4,'lakeLevel':10.4},
            {'date':'2025-01-06','observedLevel':20,'lakeLevel':20}],
            'diagnostics':{'lakeWindows':[{'start':'2025-01-01T12:00','end':'2025-01-05T23:00'},
                                           {'start':'2025-01-06T12:00','end':'2025-01-10T23:00'}]}}
        row=changes(result,1,2025)
        self.assertEqual(row['count'],1)
        self.assertEqual(row['pairs'][0]['start'],'2025-01-02')
        self.assertEqual(row['skillVsConstantLevel'],1)
        self.assertEqual(row['directionAccuracyAbove1cm'],1)

    def test_unchanging_simulation_has_zero_skill_and_no_missing_day_fill(self):
        result={'daily':[{'date':'2025-01-02','observedLevel':10,'lakeLevel':10},
                         {'date':'2025-01-09','observedLevel':10.1,'lakeLevel':10}],
                'diagnostics':{'lakeWindows':[{'start':'2025-01-01','end':'2025-01-31'}]}}
        self.assertEqual(changes(result,1,2025)['count'],0)
        self.assertEqual(changes(result,7,2025)['skillVsConstantLevel'],0)
