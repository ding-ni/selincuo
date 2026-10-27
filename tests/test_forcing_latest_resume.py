"""Verify acquisition reuse and recovery without touching real source files."""
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch
import tempfile
import threading
import time

import numpy as np
import pandas as pd
import xarray as xr

from backend import forcing_latest as latest
from backend.common import read_json


class ForcingLatestResumeTests(TestCase):
    @classmethod
    def setUpClass(cls):
        # Keep test artifacts: the project expressly forbids automatic cache cleanup.
        cls.workspace = Path(tempfile.mkdtemp(prefix='selinco-forcing-check-'))
        cls.folder = cls.workspace / 'downloads' / 'era5-land'
        cls.folder.mkdir(parents=True)
        cls.source = cls.folder / 'era5-land-202212.nc'
        times = pd.date_range('2022-12-01', periods=48, freq='h')
        lat = np.round(np.arange(33.5, 29.89, -0.1), 1)
        lon = np.round(np.arange(87.2, 92.41, 0.1), 1)
        shape = (len(times), len(lat), len(lon))
        dims = ('time', 'latitude', 'longitude')
        ds = xr.Dataset({
            't2m': (dims, np.full(shape, 273.15, dtype='float32'), {'units': 'K'}),
            'tp': (dims, np.zeros(shape, dtype='float32'), {'units': 'm', 'GRIB_stepType': 'accum'}),
            'pev': (dims, np.zeros(shape, dtype='float32'), {'units': 'm', 'GRIB_stepType': 'accum'}),
        }, coords={'time': times, 'latitude': lat, 'longitude': lon})
        ds.to_netcdf(cls.source, engine='h5netcdf')

    def test_complete_local_file_is_verified_for_domain_units_and_hours(self):
        lo, hi = pd.Timestamp('2022-12-01'), pd.Timestamp('2022-12-02T23:00:00')
        target = self.folder / 'era5-land-202212-01-02-new.nc'
        found, refresh = latest._local_candidate(target, [self.source], pd.Period('2022-12'),
                                                  lo, hi, True, {})
        self.assertEqual(found[0], self.source)
        self.assertFalse(refresh)
        self.assertEqual(found[1]['variables']['precipitation']['hours'], 48)
        with self.assertRaisesRegex(ValueError, '未完整覆盖'):
            latest.verified_coverage(self.source, lo, hi + pd.Timedelta(hours=1))

    def test_provisional_prior_file_is_not_mistaken_for_a_final_revision(self):
        checked = {'sha256': 'same', 'expver': ['0005'], 'variables': {
            key: {'startUTC': '2022-12-01T00:00:00', 'endUTC': '2022-12-02T23:00:00'}
            for key in latest.VARIABLES}}
        with patch.object(latest, 'inspect_file', return_value={'sha256': 'same'}), \
             patch.object(latest, 'inspect', return_value=checked):
            found, refresh = latest._local_candidate(self.folder / 'new.nc', [self.source],
                pd.Period('2022-12'), pd.Timestamp('2022-12-01'),
                pd.Timestamp('2022-12-02T23:00:00'), True, {})
        self.assertIsNone(found)
        self.assertTrue(refresh)

    def test_default_refresh_detects_old_provisional_before_reusing_requests(self):
        checked = {'sha256': 'same', 'expver': ['0005'], 'variables': {
            key: {'startUTC': '2022-12-01T00:00:00', 'endUTC': '2022-12-02T23:00:00'}
            for key in latest.VARIABLES}}
        proof = self.source.with_suffix('.provenance.json')
        latest._write_new_json(proof, {'catalog': {'catalogUpdated': 'old'}, 'verification': checked})
        with patch.object(latest, 'inspect_file', return_value={'sha256': 'same'}), \
             patch.object(latest, 'inspect', return_value=checked):
            found, refresh = latest._local_candidate(self.folder / 'new.nc', [self.source],
                pd.Period('2022-12'), pd.Timestamp('2022-12-01'),
                pd.Timestamp('2022-12-02T23:00:00'), False, {}, {'catalogUpdated': 'new'})
        self.assertIsNone(found)
        self.assertTrue(refresh)
        with patch.object(latest, 'inspect_file', return_value={'sha256': 'same'}), \
             patch.object(latest, 'inspect', return_value=checked):
            found, refresh = latest._local_candidate(self.folder / 'new.nc', [self.source],
                pd.Period('2022-12'), pd.Timestamp('2022-12-01'),
                pd.Timestamp('2022-12-02T23:00:00'), False, {}, {'catalogUpdated': 'old'})
        self.assertEqual(found[0], self.source)
        self.assertFalse(refresh)

    def test_corrupt_chunks_and_existing_targets_are_preserved(self):
        folder = self.folder / 'test-chunks'
        folder.mkdir(exist_ok=True)
        old = folder / '0'
        old.write_bytes(b'bad!')
        old.with_suffix('.sha256').write_text('invalid', encoding='ascii')
        self.assertIsNone(latest._usable_chunk(folder, 0, 4))
        good = latest._save_chunk(folder, 0, b'good')
        self.assertEqual(latest._usable_chunk(folder, 0, 4), good)
        self.assertEqual(old.read_bytes(), b'bad!')
        target, partial = self.folder / 'existing.nc', self.folder / 'assembled.partial'
        target.write_bytes(b'old')
        partial.write_bytes(b'new')
        published = latest._publish_partial(partial, target)
        self.assertNotEqual(published, target)
        self.assertEqual(target.read_bytes(), b'old')
        self.assertEqual(partial.read_bytes(), b'new')
        self.assertEqual(published.read_bytes(), b'new')

    def test_remote_request_recovered_across_catalog_changes_and_receipts_preserved(self):
        request = {'year': '2021', 'month': '01'}
        old = SimpleNamespace(request_id='old-id', request=request, status='successful')
        new = SimpleNamespace(request_id='new-id', request=request, status='successful')
        target = self.folder / 'request-test.nc'
        old_meta, new_meta = {'catalogUpdated': 'old'}, {'catalogUpdated': 'new'}
        latest._remember_remote(target, old, request, old_meta)
        receipt = target.with_suffix('.request.json')
        original = receipt.read_bytes()
        api = SimpleNamespace(get_remote=lambda ident: {'old-id': old, 'new-id': new}[ident],
                              get_jobs=Mock(return_value=SimpleNamespace(json={'jobs': []})))
        client = SimpleNamespace(client=api)
        self.assertIs(latest._saved_remote(client, self.folder, target, request, {}, new_meta), old)
        self.assertIsNone(latest._saved_remote(client, self.folder, target, request, {}, new_meta, True))
        latest._remember_remote(target, new, request, new_meta)
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(read_json(receipt)['requestId'], 'old-id')
        self.assertIs(latest._saved_remote(client, self.folder, target, request, {}, new_meta, True), new)

    def test_remote_running_request_beats_older_successful_without_local_receipt(self):
        folder = self.folder / 'remote-no-receipt'
        folder.mkdir()
        request = {'year': '2021', 'month': '02'}
        old = SimpleNamespace(request_id='old', request=request, status='successful')
        running = SimpleNamespace(request_id='running', request=request, status='running')
        api = SimpleNamespace(get_remote=lambda ident: {'old': old, 'running': running}[ident],
            get_jobs=Mock(return_value=SimpleNamespace(json={'jobs': [
                {'processID': 'reanalysis-era5-land', 'jobID': 'old'},
                {'processID': 'reanalysis-era5-land', 'jobID': 'running'}]})))
        chosen = latest._saved_remote(SimpleNamespace(client=api), folder, folder / 'target.nc', request,
                                      {}, {'catalogUpdated': 'new'}, refresh_provisional=True)
        self.assertIs(chosen, running)

    def test_active_remote_beats_successful_local_receipt(self):
        folder = self.folder / 'remote-active-over-receipt'
        folder.mkdir()
        request = {'year': '2021', 'month': '03'}
        old = SimpleNamespace(request_id='old', request=request, status='successful')
        running = SimpleNamespace(request_id='running', request=request, status='running')
        target = folder / 'target.nc'
        latest._remember_remote(target, old, request, {'catalogUpdated': 'test'})
        api = SimpleNamespace(get_remote=lambda ident: {'old': old, 'running': running}[ident],
            get_jobs=Mock(return_value=SimpleNamespace(json={'jobs': [
                {'processID': 'reanalysis-era5-land', 'jobID': 'running'}]})))
        self.assertIs(latest._saved_remote(SimpleNamespace(client=api), folder, target, request,
                                           {}, {'catalogUpdated': 'test'}), running)
        api.get_jobs.reset_mock()
        cache = {'requests': {latest.digest(request): [old, running]}}
        self.assertIs(latest._saved_remote(SimpleNamespace(client=api), folder, target, request,
                                           cache, {'catalogUpdated': 'test'}), running)
        api.get_jobs.assert_not_called()

    def test_provisional_provenance_is_found_by_hash_in_nested_incoming(self):
        workspace = Path(tempfile.mkdtemp(prefix='selinco-incoming-proof-check-'))
        folder = workspace / 'incoming' / 'source'
        folder.mkdir(parents=True)
        latest._write_new_json(folder / 'source.provenance.json', {
            'catalog': {'catalogUpdated': 'old'},
            'verification': {'sha256': 'a1', 'expver': ['0005'], 'variables': {
                'temperature': {'startUTC': '2022-12-01T00:00:00', 'endUTC': '2022-12-31T23:00:00'}}}})
        manifest = {'sources': [{'name': 'source.nc', 'sha256': 'a1'}]}
        summary = latest.provenance_summary(SimpleNamespace(workspace=workspace), manifest)
        self.assertTrue(summary['hasProvisional'])
        self.assertEqual(summary['sources'][0]['catalogUpdated'], 'old')
        self.assertEqual(summary['unclassifiedSourceCount'], 0)

    def test_preflight_failure_replaces_stale_update_status(self):
        workspace = Path(tempfile.mkdtemp(prefix='selinco-preflight-check-'))
        db = SimpleNamespace(workspace=workspace)
        with patch.object(latest, 'availability', side_effect=ValueError('官方目录暂不可用')):
            with self.assertRaisesRegex(ValueError, '官方目录暂不可用'):
                latest.acquire_latest(db, start_date='2020-01-01', end_date='2022-12-31', task_id='JOB-test')
        status = read_json(workspace / 'downloads/era5-land/latest-status.json')
        self.assertEqual(status['status'], 'incomplete')
        self.assertEqual(status['stage'], 'preflight')
        self.assertEqual(status['message'], '官方目录暂不可用')
        self.assertEqual(status['taskId'], 'JOB-test')

    def test_cancellation_during_registration_or_merge_preserves_input_identity(self):
        base = {'id': 'base', 'start': '2022-12-01T09:00:00', 'end': '2026-10-21T07:00:00',
                'spatialVersion': 'source', 'lakePrecipitationCorrection': {'factor': 0.5}}
        merged = {**base, 'id': 'merged'}
        client = SimpleNamespace(client=SimpleNamespace(get_jobs=Mock()), retrieve=Mock())
        meta = {'catalogUpdated': 'test', 'variables': ['2m_temperature', 'total_precipitation',
                'potential_evaporation'], 'availableStartUTC': '1981-01-01T00:00:00Z',
                'availableEndUTC': '2026-10-31T23:00:00Z'}
        definition = {'spatialVersion': 'source', 'initialization': {'origin': '2023-01-01'},
                      'inputRequirement': {}}
        lo, hi = pd.Timestamp('2022-12-01'), pd.Timestamp('2022-12-02T23:00:00')
        for stop_during in ('registration', 'merge'):
            with self.subTest(stop_during=stop_during):
                workspace = Path(tempfile.mkdtemp(prefix='selinco-cancel-check-'))
                db = SimpleNamespace(workspace=workspace, items=lambda _: [{'manifest': base}])
                stopped = {'value': False}

                def register(*args, progress, **kwargs):
                    if stop_during == 'registration':
                        stopped['value'] = True
                    progress(100, 'registered')
                    return {'id': 'fresh'}

                def merge(*args):
                    stopped['value'] = True
                    return merged

                with patch.object(latest, 'availability', return_value=meta), \
                     patch.object(latest, 'acquisition_window', return_value=(lo, hi)), \
                     patch.object(latest, '_candidate_files', return_value=[self.source]), \
                     patch('cdsapi.Client', return_value=client), \
                     patch('backend.operational_scheme.definition', return_value=definition), \
                     patch('backend.forcing.register_era5', side_effect=register), \
                     patch('backend.forcing_revisions.merge_products', side_effect=merge):
                    with self.assertRaises(InterruptedError):
                        latest.acquire_latest(db, cancelled=lambda: stopped['value'],
                                              start_date='2022-12-01', end_date='2022-12-02')
                status = read_json(workspace / 'downloads/era5-land/latest-status.json')
                self.assertEqual(status['status'], 'cancelled')
                self.assertEqual(status['inputId'], 'fresh' if stop_during == 'registration' else 'merged')


class ForcingCommitTests(TestCase):
    def test_restart_reconciles_download_and_registration_after_interruption(self):
        from backend.common import now
        from backend.database import Database
        from backend.jobs import Jobs

        for stage in ('transfer', 'source-registered', 'merged', 'registered'):
            with self.subTest(stage=stage):
                workspace = Path(tempfile.mkdtemp(prefix='selinco-interrupted-update-check-'))
                db = Database(workspace)
                stamp = now()
                with db.connect() as connection:
                    connection.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request) VALUES(?,?,?,?,?,?)',
                                       ('JOB-commit-crash', 'update-forcing', 'running', stamp, stamp, '{}'))
                path = workspace / 'downloads/era5-land/latest-status.json'
                value = {'status': 'succeeded' if stage == 'registered' else 'running',
                         'taskId': 'JOB-commit-crash', 'stage': stage}
                if stage != 'transfer':value['inputId'] = 'input-retained'
                latest.write_json(path, value)
                jobs = Jobs(db)
                try:
                    self.assertEqual(db.task('JOB-commit-crash')['status'], 'interrupted')
                    status = read_json(path)
                    self.assertEqual(status['status'], 'interrupted')
                    self.assertEqual(status.get('inputId'), value.get('inputId'))
                finally:
                    jobs.close()

    def test_old_active_provisional_finishes_before_requesting_current_revision(self):
        from backend.common import digest
        workspace = Path(tempfile.mkdtemp(prefix='selinco-provisional-refresh-check-'))
        folder = workspace / 'downloads/era5-land'
        folder.mkdir(parents=True)
        base = {'id': 'base', 'start': '2022-12-01T09:00:00', 'end': '2026-10-21T07:00:00',
                'spatialVersion': 'source', 'lakePrecipitationCorrection': {'factor': 0.5}}
        merged = {**base, 'id': 'merged'}
        db = SimpleNamespace(workspace=workspace, items=lambda _: [{'manifest': base}])
        meta = {'catalogUpdated': 'new', 'variables': ['2m_temperature', 'total_precipitation',
                'potential_evaporation'], 'availableStartUTC': '1981-01-01T00:00:00Z',
                'availableEndUTC': '2026-10-31T23:00:00Z'}
        definition = {'spatialVersion': 'source', 'initialization': {'origin': '2023-01-01'},
                      'inputRequirement': {}}
        lo, hi = pd.Timestamp('2022-12-01'), pd.Timestamp('2022-12-02T23:00:00')
        request = {'variable': meta['variables'], 'year': '2022', 'month': '12', 'day': ['01', '02'],
                   'time': [f'{h:02}:00' for h in range(24)], 'area': latest.AREA,
                   'data_format': 'netcdf', 'download_format': 'unarchived'}
        identity = digest({'request': request, 'catalogUpdated': meta['catalogUpdated']})[:16]
        target = folder / f'era5-land-202212-01-02-{identity}.nc'

        class Remote:
            def __init__(self, identifier, running=False):
                self.request_id, self.request, self.calls = identifier, request, 0
                self.running = running

            @property
            def status(self):
                self.calls += 1
                return 'running' if self.running and self.calls <= 3 else 'successful'

            def get_results(self):
                return SimpleNamespace(content_length=3, location=self.request_id)

        old, new = Remote('old-active', True), Remote('new-current')
        latest._remember_remote(target, old, request, {'catalogUpdated': 'old'})
        api = SimpleNamespace(get_remote=lambda _: old,
                              get_jobs=Mock(return_value=SimpleNamespace(json={'jobs': []})))
        client = SimpleNamespace(client=api, retrieve=Mock(return_value=new))
        response = SimpleNamespace(status_code=206, headers={'Content-Range': 'bytes 0-2/3'},
                                   content=b'nc!', raise_for_status=lambda: None)
        checked = {'expver': ['0005'], 'sha256': 'test', 'variables': {
            key: {'startUTC': lo.isoformat(), 'endUTC': hi.isoformat()} for key in latest.VARIABLES}}
        with patch.object(latest, 'availability', return_value=meta), \
             patch.object(latest, 'acquisition_window', return_value=(lo, hi)), \
             patch.object(latest, '_local_candidate', return_value=(None, True)), \
             patch.object(latest, 'verified_coverage', return_value=checked), \
             patch('cdsapi.Client', return_value=client), \
             patch('requests.get', return_value=response), \
             patch('backend.operational_scheme.definition', return_value=definition), \
             patch('backend.forcing.register_era5', return_value={'id': 'fresh'}) as register, \
             patch('backend.forcing_revisions.merge_products', return_value=merged):
            latest.acquire_latest(db, start_date='2022-12-01', end_date='2022-12-02')
        client.retrieve.assert_called_once()
        files = list(folder.glob('*.nc'))
        self.assertEqual(len(files), 2)
        catalogs = {read_json(p.with_suffix('.provenance.json'))['catalog']['catalogUpdated']: p for p in files}
        self.assertEqual(set(catalogs), {'old', 'new'})
        self.assertEqual(register.call_args.args[0], [catalogs['new']])

    def test_stop_before_and_after_commit_have_consistent_results(self):
        from backend.database import Database
        from backend.jobs import Jobs

        for stop_before_commit in (True, False):
            with self.subTest(stop_before_commit=stop_before_commit):
                workspace = Path(tempfile.mkdtemp(prefix='selinco-commit-check-'))
                db = Database(workspace)
                ready, proceed = threading.Event(), threading.Event()

                def acquire(db, progress, cancelled, *, start_date, end_date, commit, task_id):
                    result = {'status': 'succeeded', 'stage': 'registered', 'inputId': 'input-test',
                              'updateId': 'test', 'verifiedEnd': '2023-01-01T00:00:00', 'taskId': task_id}
                    path = workspace / 'downloads/era5-land/latest-status.json'
                    latest.write_json(path, {**result, 'status': 'running'})
                    if stop_before_commit:
                        ready.set()
                        if not proceed.wait(10):
                            raise RuntimeError('test gate timed out')
                    try:
                        commit(result)
                    except InterruptedError:
                        latest.write_json(path, {**result, 'status': 'cancelled'})
                        raise
                    if not stop_before_commit:
                        ready.set()
                        if not proceed.wait(10):
                            raise RuntimeError('test gate timed out')
                    return result

                with patch.object(latest, 'acquire_latest', side_effect=acquire):
                    jobs = Jobs(db)
                    task = jobs.submit('update-forcing', {})
                    self.assertTrue(ready.wait(10))
                    stopped = jobs.stop(task['id'])
                    self.assertEqual(stopped['status'], 'stopping' if stop_before_commit else 'succeeded')
                    proceed.set()
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        actual = db.task(task['id'])
                        if actual['status'] in ('cancelled', 'succeeded', 'failed'):
                            break
                        time.sleep(.02)
                    self.assertEqual(actual['status'], 'cancelled' if stop_before_commit else 'succeeded')
                    status = read_json(workspace / 'downloads/era5-land/latest-status.json')
                    self.assertEqual(status['status'], actual['status'])
                    self.assertEqual(status['inputId'], 'input-test')
                    jobs.close()

    @patch.object(latest, '_local_candidate', return_value=((Path('verified-local.nc'), {}), False))
    def test_acquisition_reuses_source_and_passes_corrected_base_to_merge(self, find_local):
        base = {'id': 'base', 'start': '2022-12-01T09:00:00', 'end': '2026-10-21T07:00:00',
                'spatialVersion': 'source', 'lakePrecipitationCorrection': {'factor': 0.5}}
        merged = {**base, 'id': 'merged'}
        workspace = Path(tempfile.mkdtemp(prefix='selinco-reuse-check-'))
        db = SimpleNamespace(workspace=workspace, items=lambda _: [{'manifest': base}])
        source = Path('verified-local.nc')
        client = SimpleNamespace(client=SimpleNamespace(get_jobs=Mock()), retrieve=Mock())
        meta = {'catalogUpdated': 'test', 'variables': ['2m_temperature', 'total_precipitation',
                'potential_evaporation'], 'availableStartUTC': '1981-01-01T00:00:00Z',
                'availableEndUTC': '2026-10-31T23:00:00Z'}
        definition = {'spatialVersion': 'source', 'initialization': {'origin': '2023-01-01'},
                      'inputRequirement': {}}
        lo, hi = pd.Timestamp('2022-12-01'), pd.Timestamp('2022-12-02T23:00:00')
        with patch.object(latest, 'availability', return_value=meta), \
             patch.object(latest, 'acquisition_window', return_value=(lo, hi)), \
             patch.object(latest, '_candidate_files', return_value=[source]), \
             patch('cdsapi.Client', return_value=client), \
             patch('backend.operational_scheme.definition', return_value=definition), \
             patch('backend.forcing.register_era5', return_value={'id': 'fresh'}) as register, \
             patch('backend.forcing_revisions.merge_products', return_value=merged) as merge:
            result = latest.acquire_latest(db, start_date='2022-12-01', end_date='2022-12-02')
        client.retrieve.assert_not_called()
        self.assertEqual(register.call_args.args[0], [source])
        merge.assert_called_once_with(db, 'base', 'fresh')
        self.assertEqual(result['manifest']['lakePrecipitationCorrection'], base['lakePrecipitationCorrection'])


class ContinuousInputMergeTests(TestCase):
    def test_backfill_keeps_correction_and_parent_unchanged_and_rejects_gap(self):
        from backend.common import ROOT, canonical, digest, now, read_json, sha_file, write_json
        from backend.database import Database
        from backend.forcing import load_input
        from backend.forcing_revisions import merge_products

        workspace = Path(tempfile.mkdtemp(prefix='selinco-forcing-merge-check-'))
        db = Database(workspace)
        correction = {'method': 'station-point-annual-ratio-v1', 'scope': 'lake-only', 'factor': 0.5}
        spatial = read_json(ROOT / 'data/derived/spatial/manifest.json')['version']

        def product(identifier, start, values, corrected=False):
            folder = workspace / 'inputs' / 'products' / identifier
            folder.mkdir(parents=True)
            times = pd.date_range(start, periods=len(values), freq='h')
            array = np.asarray(values, dtype=float)[:, None]
            data = folder / 'forcing.npz'
            np.savez_compressed(data, time=times.asi8, precipitation=array,
                                temperature=array + 270, evaporation=array * .01)
            manifest = {'id': identifier, 'projectId': 'selinco', 'spatialVersion': spatial,
                        'conversionVersion': 'test-hours-v1', 'data': 'forcing.npz',
                        'dataSHA256': sha_file(data), 'start': times[0].isoformat(),
                        'end': times[-1].isoformat(), 'hours': len(times), 'sources': []}
            if corrected:
                manifest.update(lakePrecipitationCorrection=correction,
                                transformationSHA256=digest(correction))
            write_json(folder / 'manifest.json', manifest)
            with db.connect() as connection:
                connection.execute('INSERT INTO input_products VALUES(?,?,?,?)',
                                   (identifier, identifier, now(), canonical(manifest)))
            return manifest

        original = product('base', '2023-01-01T00:00:00', [3, 4, 5, 6, 7], True)
        product('early', '2022-12-31T21:00:00', [0, 1, 2, 3, 4])
        merged = merge_products(db, 'base', 'early')
        _, arrays = load_input(db, merged['id'])
        self.assertEqual(arrays['precipitation'][:, 0].tolist(), list(range(8)))
        self.assertEqual(merged['revisionSelection']['changedHistoricalHours'], 0)
        self.assertEqual(merged['lakePrecipitationCorrection'], correction)
        self.assertEqual(merged['transformationSHA256'], digest(correction))
        self.assertEqual(sha_file(workspace / 'inputs/products/base/forcing.npz'), original['dataSHA256'])
        self.assertEqual(merge_products(db, 'base', 'early')['id'], merged['id'])
        product('gap', '2022-12-30T00:00:00', [0, 1])
        with self.assertRaisesRegex(ValueError, '缺少小时'):
            merge_products(db, 'base', 'gap')


if __name__ == '__main__':
    main()
