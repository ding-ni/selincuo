from copy import deepcopy
from types import SimpleNamespace
import unittest

from backend.business_selection import automatic_adoption_decision


class LakeAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(setting=lambda *_: {'resultId': 'baseline'})
        self.result = {'calibrationWorkflow': {'selectionMode': 'flow-constrained-lake',
            'selection': {'flowConstraints': {'minNse': .70, 'maxAbsPbias': 5.},
                'adoptionComparison': {'baselineTaskId': 'baseline', 'eligible': True,
                    'nse': .72, 'pbias': 3., 'lakeRmseImproved': True, 'lakeScoreImproved': True}}}}

    def test_improved_lake_with_acceptable_flow_can_be_adopted(self):
        self.assertTrue(automatic_adoption_decision(self.db, self.result)['eligible'])

    def test_better_lake_does_not_waive_flow_or_other_lake_requirement(self):
        for change in ({'nse': .69}, {'pbias': -5.1}, {'nse': float('nan')},
                       {'lakeRmseImproved': False}, {'lakeScoreImproved': False}):
            result = deepcopy(self.result)
            result['calibrationWorkflow']['selection']['adoptionComparison'].update(change)
            self.assertFalse(automatic_adoption_decision(self.db, result)['eligible'])

    def test_async_completion_preserves_newer_user_choice(self):
        db = SimpleNamespace(setting=lambda *_: {'resultId': 'newer-user-choice'})
        self.assertFalse(automatic_adoption_decision(db, self.result)['eligible'])

    def test_legacy_workflow_keeps_its_existing_adoption_behavior(self):
        self.assertTrue(automatic_adoption_decision(self.db, {})['eligible'])


if __name__ == '__main__':
    unittest.main()
