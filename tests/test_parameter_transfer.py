"""Contract checks for optional parameter transfer; no files are created."""
from copy import deepcopy
import json
import math
import unittest

from backend.model_core import PARAMETER_NAMES, parameters
from backend.parameter_regions import resolve_regions
from backend.parameter_transfer import normalize_transfer_policy, resolve_transfer


class ParameterTransferTests(unittest.TestCase):
    def setUp(self):
        self.units = [
            {'id': 'duja-land', 'partition': 'duja', 'kind': 'land', 'areaKm2': 64.},
            {'id': 'duja-ice', 'partition': 'duja', 'kind': 'glacier', 'areaKm2': 36.},
            {'id': 'zagen-land', 'partition': 'zagen', 'kind': 'land', 'areaKm2': 300.},
            {'id': 'zagen-ice', 'partition': 'zagen', 'kind': 'glacier', 'areaKm2': 100.},
            {'id': 'ali-land', 'partition': 'ali', 'kind': 'land', 'areaKm2': .0001},
            {'partition': 'fringe', 'kind': 'land', 'areaKm2': 2500.},
            {'id': 'unknown', 'partition': 'unresolved', 'kind': 'land', 'areaKm2': 2500.},
            {'id': 'outside', 'partition': 'external', 'kind': 'land', 'areaKm2': 2500.},
        ]
        self.base = {'WM': 210., 'tauHours': 10., 'lagHours': 50.,
                     'lakeCoefficient': 1.4, 'interflowTauHours': 10., 'baseflowTauHours': 200.}

    def test_legacy_matches_existing_parameter_resolution(self):
        overrides = {'duja': {'WM': 300., 'KG': .1}, 'zagen': {'tauHours': 80.}}
        expected_base = parameters(self.base)
        expected_patches, expected_regions = resolve_regions(self.units, expected_base, overrides)
        base, patches, audit = resolve_transfer(self.units, self.base, overrides)
        self.assertEqual(base, expected_base)
        self.assertEqual(patches, expected_patches)
        self.assertEqual(resolve_regions(self.units, base, patches)[1], expected_regions)
        self.assertEqual(audit['effective']['regions']['duja']['parameters'], expected_regions['duja'])
        self.assertEqual(audit['effective']['regions']['ali']['parameters'], expected_base)
        self.assertEqual(audit['effective']['regions']['duja']['sources']['WM'], 'regional-override')
        self.assertEqual(audit['effective']['regions']['duja']['sources']['lakeCoefficient'], 'global-base')
        self.assertEqual(audit['effective']['regions']['duja']['sources']['B'], 'model-default')
        # Legacy parameter resolution historically did not require area metadata.
        missing_area = [{'partition': 'duja'}]
        legacy, legacy_regions, legacy_audit = resolve_transfer(missing_area, {}, {})
        self.assertEqual((legacy, legacy_regions), (parameters({}), {}))
        self.assertIsNone(legacy_audit['effective']['regions']['duja']['areaKm2'])

    def test_shared_uses_one_base_without_lake_override_or_hidden_patch(self):
        original = deepcopy((self.units, self.base))
        policy = {'version': 1, 'mode': 'duja-shared', 'donor': 'duja',
                  'areaExponent': .5, 'sourceTaskId': 'user-declared-task'}
        base, patches, audit = resolve_transfer(self.units, self.base, {}, policy)
        self.assertEqual((self.units, self.base), original)
        self.assertEqual(audit['raw']['parameters'], self.base)
        self.assertEqual(audit['raw']['partitionParameters'], {})
        self.assertEqual(audit['raw']['parameterTransfer'], policy)
        self.assertEqual(audit['policy']['sourceTaskId'], 'user-declared-task')
        self.assertEqual(set(patches), {unit['partition'] for unit in self.units})
        self.assertEqual(len(PARAMETER_NAMES), 25)
        for partition, patch in patches.items():
            self.assertEqual(set(patch), set(PARAMETER_NAMES) - {'lakeCoefficient'})
            self.assertEqual(patch['WM'], 210.)
            self.assertNotIn('lakeCoefficient', patch)
            row = audit['effective']['regions'][partition]
            self.assertEqual(row['parameters'], base)
            self.assertEqual(len(row['sources']), 25)
            self.assertFalse(row['routingScaleApplied'])
        self.assertEqual(base['lakeCoefficient'], 1.4)
        self.assertEqual(audit['units'][5]['id'], 'unit-index:5')
        self.assertEqual(audit['units'][5]['idSource'], 'index-fallback')
        self.assertEqual(audit['units'][5]['parameterGroup'],
                         audit['effective']['regions']['fringe']['parameterGroup'])
        json.dumps(audit, allow_nan=False)

    def test_area_scaling_uses_total_real_hru_area_and_records_clamps(self):
        policy = {'mode': 'duja-area-routing', 'sourceTaskId': 'declared-only'}
        base, patches, audit = resolve_transfer(self.units, self.base, {}, policy)
        regions = audit['effective']['regions']
        self.assertEqual(regions['duja']['areaKm2'], 100.)
        self.assertEqual(regions['duja']['landAreaKm2'], 64.)
        self.assertEqual(regions['duja']['glacierAreaKm2'], 36.)
        self.assertEqual(regions['zagen']['areaKm2'], 400.)
        self.assertEqual(regions['zagen']['routingScale'], 2.)
        self.assertEqual(patches['zagen']['tauHours'], 20.)
        self.assertEqual(patches['zagen']['lagHours'], 72.)
        self.assertEqual(regions['zagen']['routing']['lagHours'],
                         {'donorValue': 50., 'unclampedValue': 100., 'value': 72.,
                          'min': 0, 'max': 72, 'clamped': True})
        self.assertEqual(regions['ali']['routing']['tauHours']['unclampedValue'], .01)
        self.assertEqual(patches['ali']['tauHours'], 1.)
        self.assertTrue(regions['ali']['routing']['tauHours']['clamped'])
        self.assertEqual(patches['duja']['tauHours'], self.base['tauHours'])
        for partition in regions:
            self.assertEqual(patches[partition]['interflowTauHours'], 10.)
            self.assertEqual(patches[partition]['baseflowTauHours'], 200.)
            self.assertEqual(regions[partition]['parameters']['lakeCoefficient'], 1.4)
        for partition, reason in (('fringe', 'distributed-direct-lake-inflow'),
                                  ('unresolved', 'not-a-verified-catchment'),
                                  ('external', 'not-a-verified-catchment')):
            self.assertFalse(regions[partition]['routingScaleApplied'])
            self.assertEqual(regions[partition]['routingScaleBasis'], reason)
            self.assertEqual(patches[partition]['tauHours'], 10.)
            self.assertEqual(patches[partition]['lagHours'], 50.)
        self.assertEqual(regions['zagen']['sources']['tauHours'], 'area-scaled:global-base')
        self.assertEqual(resolve_regions(self.units, base, patches)[1]['zagen'], regions['zagen']['parameters'])

    def test_transfer_rejects_all_explicit_overrides(self):
        for mode in ('duja-shared', 'duja-area-routing'):
            for patch in ({'duja': {'WM': 300.}}, {'duja': {}}, {'zagen': {'WM': 250.}}):
                with self.subTest(mode=mode, patch=patch), self.assertRaisesRegex(
                        ValueError, '先迁移到共享基准并清除覆盖'):
                    resolve_transfer(self.units, self.base, patch, {'mode': mode})

    def test_channel_upper_bound_does_not_change_donor_or_other_timescales(self):
        base = {**self.base, 'tauHours': 100.}
        _, patches, audit = resolve_transfer(self.units, base, {}, {'mode': 'duja-area-routing'})
        channel = audit['effective']['regions']['zagen']['routing']['tauHours']
        self.assertEqual(channel['unclampedValue'], 200.)
        self.assertEqual(channel['value'], 168.)
        self.assertTrue(channel['clamped'])
        self.assertEqual(patches['duja']['tauHours'], 100.)
        self.assertEqual(patches['zagen']['interflowTauHours'], base['interflowTauHours'])
        self.assertEqual(patches['zagen']['baseflowTauHours'], base['baseflowTauHours'])

    def test_missing_donor_and_invalid_hru_areas_fail_closed_for_transfer(self):
        for mode in ('duja-shared', 'duja-area-routing'):
            with self.subTest(mode=mode, issue='missing-donor'), self.assertRaisesRegex(ValueError, '杜加'):
                resolve_transfer(self.units[2:], self.base, {}, {'mode': mode})
            for invalid in (None, 0, -1, math.nan, math.inf, True, '100'):
                bad = deepcopy(self.units)
                bad[0]['areaKm2'] = invalid
                with self.subTest(mode=mode, invalid=invalid), self.assertRaisesRegex(ValueError, '有效正面积'):
                    resolve_transfer(bad, self.base, {}, {'mode': mode})
            bad = deepcopy(self.units)
            bad[0]['kind'] = 'water'
            with self.subTest(mode=mode, invalid='water-kind'), self.assertRaisesRegex(ValueError, '陆地或冰川'):
                resolve_transfer(bad, self.base, {}, {'mode': mode})

    def test_invalid_policies_and_legacy_validation_are_not_silently_accepted(self):
        for invalid in ({'version': True}, {'version': 2}, {'mode': 'other'},
                        {'donor': 'zagen'}, {'areaExponent': 1.}, {'areaExponent': math.nan},
                        {'sourceTaskId': ''}, {'typo': True}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_transfer_policy(invalid)
        with self.assertRaisesRegex(ValueError, '湖面蒸发系数'):
            resolve_transfer(self.units, self.base, {'duja': {'lakeCoefficient': .8}}, None)
        with self.assertRaisesRegex(ValueError, '未知参数分区'):
            resolve_transfer(self.units, self.base, {'not-a-partition': {'WM': 200.}}, None)

    def test_structural_notes_are_conditional_and_source_is_not_verified(self):
        _, _, audit = resolve_transfer(self.units, self.base, None,
                                        {'mode': 'duja-shared', 'sourceTaskId': 'anything'})
        lumped = next(note for note in audit['structuralNotes'] if note['condition'] == 'routingMode=lumped')
        self.assertEqual(lumped['inactiveKeys'], ['interflowTauHours', 'baseflowTauHours'])
        self.assertTrue(any('仅为调用方声明' in note for note in audit['assumptions']))
        self.assertEqual(audit['policy']['sourceTaskId'], 'anything')


if __name__ == '__main__':
    unittest.main()
