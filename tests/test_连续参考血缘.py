"""C1 independent arithmetic and append-only reference lineage checks.

All generated fixtures stay in a unique build/连续参考测试-* batch.  These tests
never clean a directory, move original files, or call the historical test suites.
"""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1]
os.environ.setdefault('SELINCO_APP_ROOT', str(BASE))
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from backend import continuous_reference as reference
from backend.common import canonical, digest, read_json, sha_file
from backend.database import Database
from backend.published_results import catalog, current_id, detail, result_file
from backend.release_registry import PROTOCOL, current, migrate as migrate_releases


def preserved_directory(prefix):
    build = BASE / 'build'
    build.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=build))


class HourlyDerivationTests(unittest.TestCase):
    """Expected values below are hand calculations, not another model run."""

    def vectors(self, start='2023-04-20T01:00:00', hours=72, flux=1000.):
        time = pd.date_range(start, periods=hours, freq='h').values.astype('int64')
        net = np.full(hours, flux, dtype=np.float64)
        flow = np.column_stack([np.full(hours, 2.), np.full(hours, 7.)])
        return time, flow, np.cumsum(net), net

    def derive(self, vectors=None, area=1_000_000.):
        return reference.derive_hourly(*(vectors or self.vectors()), area,
                                       reference.ANCHOR_CONFIG['time'],
                                       reference.ANCHOR_CONFIG['level'])

    def test_independent_signed_volume_anchor_and_no_preanchor_backfill(self):
        time = pd.date_range('2023-04-20T11:00:00', periods=5, freq='h').values.astype('int64')
        net = np.array([4., -2., 6., 8., -4.])
        # Cumulative volumes 4,2,8,16,12; the fixed anchor is the second hour.
        volume = np.array([4., 2., 8., 16., 12.])
        flow = np.column_stack([np.ones(5), np.full(5, 3.)])
        result = self.derive((time, flow, volume, net), area=2.)
        h0 = 4544.03
        expected = np.array([np.nan, h0, h0 + 3., h0 + 7., h0 + 5.])
        np.testing.assert_allclose(result['lakeLevel'], expected, rtol=0, atol=1e-11, equal_nan=True)
        self.assertTrue(np.isnan(result['lakeLevel'][0]))
        self.assertEqual(result['lakeLevel'][1], reference.ANCHOR_CONFIG['level'])
        self.assertLessEqual(result['consistency']['maximumDifferenceM3'], 1e-12)

    def test_end_hour_full_24h_daily_mean_and_partial_day_missing(self):
        vectors = self.vectors(hours=73)
        result = self.derive(vectors)
        days = {row['date'][:10]: row for row in result['daily']}
        # Apr21 is indexed by hours Apr21 01 through Apr22 00 (indices24..47).
        # Their mean index is35.5, versus anchor index11: 24.5*1000/1e6 m.
        self.assertAlmostEqual(days['2023-04-21']['lakeLevel'], 4544.03 + .0245, places=10)
        self.assertEqual(days['2023-04-21']['forcingHours'], 24)
        self.assertEqual(days['2023-04-21']['lakeHours'], 24)
        self.assertEqual(days['2023-04-21']['duja'], 2.)
        self.assertEqual(days['2023-04-21']['total'], 7.)
        self.assertTrue(pd.isna(days['2023-04-20']['lakeLevel']))
        self.assertEqual(days['2023-04-20']['lakeHours'], 13)
        # One end-hour on Apr23 is retained as incomplete, never interpolated.
        self.assertEqual(days['2023-04-23']['forcingHours'], 1)
        self.assertTrue(pd.isna(days['2023-04-23']['lakeLevel']))
        self.assertTrue(pd.isna(days['2023-04-23']['duja']))

    def test_only_fixed_anchor_calendar_day_is_excluded_not_later_window_starts(self):
        # The reference is continuous beyond2023.  It must retain Apr9 of2025,
        # although that date is an initialization day in the old window result.
        hours = int((pd.Timestamp('2025-04-11T00:00:00') -
                     pd.Timestamp('2023-04-20T01:00:00')) / pd.Timedelta(hours=1)) + 1
        result = self.derive(self.vectors(hours=hours, flux=1000.))
        excluded = [row['date'][:10] for row in result['daily'] if row['initializationDayExcluded']]
        self.assertEqual(excluded, ['2023-04-20'])
        later = next(row for row in result['daily'] if row['date'].startswith('2025-04-09'))
        self.assertFalse(later['initializationDayExcluded'])
        mean_hour_offset = float((pd.Timestamp('2025-04-09T12:30:00') -
                                  pd.Timestamp('2023-04-20T12:00:00')) / pd.Timedelta(hours=1))
        self.assertAlmostEqual(later['lakeLevel'], 4544.03 + mean_hour_offset * .001, places=9)
        self.assertEqual(later['lakeHours'], 24)

    def test_window_activity_flux_cannot_replace_complete_continuous_net_flux(self):
        time, flow, volume, net = self.vectors()
        # In a window-only account these first ten hours would be omitted.
        window_flux = net.copy()
        window_flux[:10] = 0
        with self.assertRaises(ValueError):
            self.derive((time, flow, volume, window_flux))
        tampered = volume.copy()
        tampered[40:] += 500.
        with self.assertRaises(ValueError):
            self.derive((time, flow, tampered, net))

    def test_month_uses_complete_hourly_net_and_total_flow_once(self):
        result = self.derive(self.vectors(hours=72))
        self.assertEqual(len(result['monthly']), 1)
        month = result['monthly'][0]
        self.assertEqual(month['month'], '2023-04')
        self.assertEqual(month['hours'], 72)
        self.assertFalse(month['complete'])
        self.assertEqual(month['hourlyNetM3'], 72_000.)
        self.assertEqual(month['inflowM3'], 72 * 7 * 3600.)
        self.assertEqual(month['atmosphericNetM3'], 72_000. - 72 * 7 * 3600.)
        self.assertEqual(month['cumulativeChangeM3'], 72_000.)
        self.assertEqual(month['cumulativeMinusHourlyNetM3'], 0.)

    def test_seven_river_total_includes_duja_only_inside_zajia(self):
        time, _, volume, net = self.vectors()
        flow = np.tile([2., 5., 3., 1., 4., 6., 19.], (len(time), 1))
        self.derive((time, flow, volume, net))
        flow[:, -1] = 21.  # Wrongly adding the Duja2 again must fail.
        with self.assertRaisesRegex(ValueError, '杜加不得重复'):
            self.derive((time, flow, volume, net))

    def test_missing_anchor_gap_duplicate_and_nonfinite_vectors_are_rejected(self):
        time, flow, volume, net = self.vectors()
        cases = [
            (time[12:], flow[12:], volume[12:], net[12:]),
            (np.r_[time[:30], time[31:]], np.r_[flow[:30], flow[31:]],
             np.r_[volume[:30], volume[31:]], np.r_[net[:30], net[31:]]),
        ]
        duplicate = time.copy()
        duplicate[30] = duplicate[29]
        cases.append((duplicate, flow, volume, net))
        invalid = net.copy()
        invalid[30] = np.nan
        cases.append((time, flow, volume, invalid))
        for index, vectors in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                self.derive(vectors)

    def test_nonpositive_or_nonfinite_parent_area_is_rejected(self):
        for area in (0, -1, float('nan'), float('inf')):
            with self.subTest(area=area), self.assertRaises(ValueError):
                self.derive(area=area)


class ReferenceLineageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.batch = preserved_directory('连续参考测试-')

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix=self._testMethodName + '-', dir=self.batch))
        self.db = Database(self.folder / '工作区')
        migrate_releases(self.db)
        reference.migrate(self.db)
        self.release = current(self.db)
        self.request = {'baselineReleaseId': self.release['id'], 'protocolId': PROTOCOL['id'],
                        'anchorConfigId': reference.ANCHOR_CONFIG['id']}
        self.source = self.tiny_verified_source()

    def tiny_verified_source(self):
        """A source-reader boundary fixture for writer/transaction tests only.

        Source authentication itself is tested through the real source reader in
        separate tests below.  No input file is copied for each transaction case.
        """
        time = pd.date_range('2023-04-20T01:00:00', periods=72, freq='h').values.astype('int64')
        net = np.full(72, 1000.)
        volume = np.cumsum(net)
        flow = np.column_stack([np.full(72, 2.), np.full(72, 7.)])
        hourly = self.folder / '合成测试小时源.npz'
        with hourly.open('xb') as stream:
            np.savez_compressed(stream, time=time, flow=flow,
                                continuousLakeVolumeChange=volume, lakeFlux=net)
        parent = copy.deepcopy(detail(self.release['resultId']))
        parent['result'].update(start='2023-04-20T01:00:00', end='2023-04-23T00:00:00', hours=72)
        revision = json.loads(self.db.rows('SELECT payload FROM scheme_revisions WHERE id=?',
                                          (self.release['schemeRevisionId'],))[0]['payload'])
        lineage = {'release': copy.deepcopy(self.release), 'parentResultId': parent['id'],
                   'schemeRevisionSHA256': digest(revision), 'fixture': 'synthetic-test-only',
                   'parentFinalContinuousVolumeM3': float(volume[-1]),
                   'sourceSHA256': sha_file(hourly),
                   'parentSpatial': {'lakeAreaKm2': 1., 'sha256': 'fixture-spatial'}}
        observations = {'profile': reference.QUALITY_CONFIG['qualityProfile'],
                        'level': {'2023-04-20T00:00:00': 4544.03}, 'flow': {}}
        return {'lineage': lineage, 'task': parent, 'hourlyPath': hourly,
                'observations': observations, 'areaM2': 1_000_000.}

    def source_reader(self, db, request, cancelled=None):
        # Keep the public request validation active even across this internal seam.
        reference._request(request)
        return self.source

    def generate(self, **kwargs):
        destination = self.db.workspace / 'tasks' / ('连续参考-' + str(len(list(self.db.workspace.glob('tasks/*')))))
        return reference.generate(self.db, self.request, destination, **kwargs)

    def records(self):
        return self.db.rows('SELECT id,payload FROM continuous_reference_records')

    def test_client_cannot_choose_anchor_scores_path_or_parent_result(self):
        forged = [('anchorTime', '2025-01-01T00:00:00'), ('anchorLevel', 4545.),
                  ('path', 'client.csv'), ('scores', {'rmse': 0}),
                  ('checks', {'waterBalanceValid': True}), ('parentResultId', 'OTHER')]
        for key, value in forged:
            with self.subTest(field=key), self.assertRaises(ValueError):
                reference.generate(self.db, {**self.request, key: value}, self.folder / ('拒绝-' + key))
        self.assertEqual(self.records(), [])
        self.assertEqual(current(self.db), self.release)

    def test_unknown_protocol_anchor_and_stale_release_are_rejected(self):
        for field in ('protocolId', 'anchorConfigId', 'baselineReleaseId'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                reference.generate(self.db, {**self.request, field: 'unknown'}, self.folder / ('拒绝-' + field))
        self.assertEqual(self.records(), [])

    def test_early_cancellation_never_commits_record(self):
        with self.assertRaises(InterruptedError):
            self.generate(cancelled=lambda: True)
        self.assertEqual(self.records(), [])
        self.assertEqual(current(self.db), self.release)

    def test_published_parameter_revision_cannot_be_changed_to_match_new_claim(self):
        rows = self.db.rows('SELECT payload FROM scheme_revisions WHERE id=?',
                            (self.release['schemeRevisionId'],))
        snapshot = json.loads(rows[0]['payload'])
        snapshot['parameters']['KC'] += .01
        with self.db.connect() as connection:
            connection.execute('UPDATE scheme_revisions SET payload=? WHERE id=?',
                               (canonical(snapshot), self.release['schemeRevisionId']))
        with self.assertRaisesRegex(ValueError, '不可变修订.*身份不一致'):
            self.generate()
        self.assertEqual(self.records(), [])

    def test_changed_parent_source_file_is_rejected_by_declared_six_file_sha(self):
        fixture_root = self.folder / '改动源文件的发行副本'
        catalog_source = BASE / 'data/final-results/catalog.json'
        catalog_target = fixture_root / 'data/final-results/catalog.json'
        catalog_target.parent.mkdir(parents=True)
        with catalog_target.open('xb') as stream:
            stream.write(catalog_source.read_bytes())
        item = next(row for row in catalog() if row['id'] == self.release['resultId'])
        target = fixture_root / 'data/final-results' / item['directory']
        target.mkdir(parents=True)
        copied = [{'source': str(catalog_source), 'target': str(catalog_target),
                   'sha256': sha_file(catalog_target)}]
        for name in sorted(item['files']):
            path = target / name
            source = result_file(item['id'], name)
            with path.open('xb') as stream:
                # Only this new fixture source is intentionally corrupted.
                stream.write(b'intentionally-altered-hourly-fixture' if name == 'hourly.npz'
                             else source.read_bytes())
            actual_sha = sha_file(path)
            copied.append({'source': str(source), 'target': str(path),
                           'expectedSHA256': item['files'][name]['sha256'], 'sha256': actual_sha,
                           'intentionalMutation': name == 'hourly.npz'})
            if name != 'hourly.npz':
                self.assertEqual(actual_sha, item['files'][name]['sha256'])
        with (self.folder / '源文件复制核验清单.json').open('x', encoding='utf-8') as stream:
            json.dump(copied, stream, ensure_ascii=False, indent=2)
        with patch.object(reference, 'ROOT', fixture_root):
            with self.assertRaisesRegex(ValueError, '父成果文件SHA或大小变化.*hourly'):
                self.generate()
        self.assertEqual(self.records(), [])

    def test_parent_area_content_change_without_new_spatial_identity_is_rejected(self):
        parent = detail(self.release['resultId'])['result']
        spatial = BASE / 'data/derived/spatial/versions' / parent['spatialVersion'] / 'manifest.json'
        if not spatial.is_file():
            spatial = BASE / 'data/derived/spatial/manifest.json'
        altered = copy.deepcopy(read_json(spatial))
        altered['lakeAreaKm2'] += 1.
        altered_path = self.folder / '湖面积改变的空间源.json'
        with altered_path.open('x', encoding='utf-8') as stream:
            json.dump(altered, stream, ensure_ascii=False, indent=2)
        original_read = reference.read_json

        def read_source(path, default=None):
            if Path(path).resolve() == spatial.resolve():
                return original_read(altered_path, default)
            return original_read(path, default)

        with patch.object(reference, 'read_json', side_effect=read_source):
            with self.assertRaisesRegex(ValueError, '父自身空间版本.*变化'):
                self.generate()
        self.assertEqual(self.records(), [])

    def test_reader_rejects_source_lineage_and_area_drift_after_archive(self):
        with patch.object(reference, 'source_snapshot', side_effect=self.source_reader):
            result = self.generate()
            reference.read_verified_reference(self.db, result['referenceId'])
            original = self.source
            for kind in ('source', 'area'):
                changed = copy.deepcopy(original)
                if kind == 'source':
                    changed['lineage']['sourceSHA256'] = 'a-different-source-hash'
                else:
                    changed['areaM2'] = 2_000_000.
                self.source = changed
                with self.subTest(change=kind), self.assertRaisesRegex(ValueError, '血缘不同'):
                    reference.read_verified_reference(self.db, result['referenceId'])
            self.source = original
            reference.read_verified_reference(self.db, result['referenceId'])
        self.assertEqual(len(self.records()), 1)

    def test_late_cancel_preserves_files_but_never_commits_complete_record(self):
        stop = [False]

        def progress(value, message):
            if value >= 95:
                stop[0] = True

        with patch.object(reference, 'source_snapshot', side_effect=self.source_reader):
            with self.assertRaises(InterruptedError):
                self.generate(progress=progress, cancelled=lambda: stop[0])
        self.assertTrue(stop[0], 'must exercise the final pre-commit cancellation gate')
        self.assertEqual(self.records(), [])
        self.assertTrue(list((self.db.workspace / 'tasks').glob('*/*')),
                        'cancelled task artifacts remain available for inspection')
        self.assertEqual(current(self.db), self.release)

    def test_release_changed_while_deriving_never_commits_reference(self):
        changed = [False]

        def progress(value, message):
            if value >= 95 and not changed[0]:
                changed[0] = True
                with self.db.connect() as connection:
                    connection.execute(
                        'INSERT INTO operational_releases(id,previous_id,scheme_revision_id,result_id,'
                        'evaluation_id,published_at,action) VALUES(?,?,?,?,NULL,?,?)',
                        ('RELEASE-隔离测试变化', self.release['id'], self.release['schemeRevisionId'],
                         self.release['resultId'], 'fixture-time', 'fixture-change'))

        with patch.object(reference, 'source_snapshot', side_effect=self.source_reader):
            with self.assertRaises(ValueError):
                self.generate(progress=progress)
        self.assertTrue(changed[0], 'must exercise a release change after outputs are written')
        self.assertEqual(self.records(), [])

    def test_source_changed_during_derivation_never_commits_reference(self):
        changed = [False]

        def progress(value, message):
            if value >= 95 and not changed[0]:
                changed[0] = True
                updated = copy.deepcopy(self.source)
                updated['lineage']['sourceSHA256'] = 'changed-during-derivation'
                self.source = updated

        with patch.object(reference, 'source_snapshot', side_effect=self.source_reader):
            with self.assertRaisesRegex(ValueError, '冻结来源变化'):
                self.generate(progress=progress)
        self.assertTrue(changed[0])
        self.assertEqual(self.records(), [])

    def test_output_changed_after_hashing_is_rejected_before_commit(self):
        changed = [False]

        def progress(value, message):
            if value >= 95 and not changed[0]:
                changed[0] = True
                path = next((self.db.workspace / 'tasks').glob('*/日过程.csv'))
                with path.open('ab') as stream:
                    stream.write(b'partial-output-mutation')

        with patch.object(reference, 'source_snapshot', side_effect=self.source_reader):
            with self.assertRaises(ValueError):
                self.generate(progress=progress)
        self.assertTrue(changed[0])
        self.assertEqual(self.records(), [])

    def test_idempotent_repeat_verifies_existing_artifacts_and_rejects_tampering(self):
        with patch.object(reference, 'source_snapshot', side_effect=self.source_reader):
            first = self.generate()
            record = reference.read_verified_reference(self.db, first['referenceId'])
            again = self.generate()
        self.assertTrue(again['reused'])
        self.assertEqual(again['referenceId'], first['referenceId'])
        self.assertEqual(len(self.records()), 1)
        artifact = record['artifacts'][0]
        path = self.db.workspace / record['archiveDirectory'] / artifact['path']
        # This is an output created by this test.  No original source is touched.
        with path.open('ab') as stream:
            stream.write(b'\nfixture-tamper\n')
        with patch.object(reference, 'source_snapshot', side_effect=self.source_reader):
            with self.assertRaises(ValueError):
                reference.read_verified_reference(self.db, first['referenceId'])
            with self.assertRaises(ValueError):
                self.generate()
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(current(self.db), self.release)

    def test_real_protected_parent_original_metrics_identity_and_six_files_unchanged(self):
        identifier = current_id()
        before = detail(identifier)
        original_metrics = copy.deepcopy(before['result']['metrics'])
        original_identity = copy.deepcopy(before['result']['identity'])
        declared = next(row for row in catalog() if row['id'] == identifier)['files']
        before_hashes = {name: sha_file(result_file(identifier, name)) for name in declared}
        # Reuse the local immutable116MiB input read-only.  Only file location is
        # redirected; the real _product still verifies registry/disk equality and
        # the full data SHA.  The production HTTP acceptance copies that product
        # into a completely independent workspace as a separate end-to-end check.
        input_id = before['result']['inputId']
        locations = [BASE / 'build/grid-revision-20261026/workspace', BASE / 'workspace']
        source_workspace = next((root for root in locations if
                                 (root / 'inputs/products' / input_id / 'manifest.json').is_file()), None)
        self.assertIsNotNone(source_workspace, 'local frozen parent input is required for the real-source check')
        manifest = read_json(source_workspace / 'inputs/products' / input_id / 'manifest.json')
        with self.db.connect() as connection:
            connection.execute('INSERT INTO input_products VALUES(?,?,?,?)',
                               (input_id, '只读父输入核验', 'fixture-time', canonical(manifest)))
        from backend import release_runtime
        original_product = release_runtime._product
        readonly_product_view = SimpleNamespace(workspace=source_workspace, items=self.db.items)

        def locate_readonly_product(db, requested_id):
            self.assertIs(db, self.db)
            self.assertEqual(requested_id, input_id)
            return original_product(readonly_product_view, requested_id)

        with patch.object(release_runtime, '_product', side_effect=locate_readonly_product):
            outcome = self.generate()
            record = reference.read_verified_reference(self.db, outcome['referenceId'])
        self.assertEqual(record['method'], 'legacy-hourly-volume-fixed-anchor-derivation-v1')
        self.assertTrue(record['archiveComplete'])
        self.assertFalse(outcome['reused'])
        self.assertEqual(current(self.db), self.release)
        after = detail(identifier)
        self.assertEqual(after['result']['metrics'], original_metrics)
        self.assertEqual(after['result']['identity'], original_identity)
        self.assertEqual(digest(after), digest(before))
        self.assertEqual(len(before_hashes), 6)
        self.assertEqual(before_hashes, {name: sha_file(result_file(identifier, name)) for name in declared})
        self.assertEqual(original_metrics['2025']['lake']['count'], 255)
        # This is the original protected file's score, before archive rounding.
        self.assertEqual(original_metrics['2025']['lake']['rmse'], .06523442125833821)


if __name__ == '__main__':
    unittest.main(verbosity=2)
