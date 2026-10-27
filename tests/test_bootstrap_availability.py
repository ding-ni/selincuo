"""Monitoring remains readable when simulation resources are unavailable."""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import urlopen

from backend.business_context import builtin_result_available
from backend.common import digest
from backend.grid_storage import FORMAT
from backend.server import Handler


class ObservationOnlyDatabase:
    def items(self, table):
        return []

    def summary(self):
        return {'counts': {'stations': 1}}

    def rows(self, sql, args=()):
        if 'FROM stations' in sql:
            return [{'id': 'SLC', 'name': '主站'}]
        if 'MAX(time)' in sql:
            return [{'time': '2024-01-01T00:00:00'}]
        if 'FROM tasks' in sql:
            return []
        raise AssertionError(sql)


class BootstrapAvailabilityTests(unittest.TestCase):
    def test_missing_model_resources_keep_station_bootstrap_readable(self):
        scheme = {'id': 'builtin', 'name': '内置方案', 'payload': {'resultId': 'JOB-missing'}}
        with patch('backend.server.read_json', return_value=None), \
             patch('backend.grid_spatial.current', side_effect=FileNotFoundError('missing spatial version')), \
             patch('backend.operational_scheme.definition', side_effect=ValueError('missing spatial manifest')), \
             patch('backend.research_schemes.builtin', return_value=scheme), \
             patch('backend.published_results.catalog', return_value=[]), \
             patch('backend.release_registry.current', return_value={'resultId': None}):
            with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
                server.db = ObservationOnlyDatabase()
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    with urlopen(f'http://127.0.0.1:{server.server_port}/api/model/bootstrap', timeout=5) as response:
                        result = json.load(response)
                finally:
                    server.shutdown()
                    worker.join()

        self.assertEqual(result['stations'][0]['id'], 'SLC')
        self.assertEqual(result['latestObservation'], '2024-01-01T00:00:00')
        self.assertEqual(result['inputs'], [])
        self.assertEqual(result['gridSpatial'], {})
        self.assertEqual(result['operationalSchemes'], [])
        self.assertTrue(result['spatialResourceMessage'])
        self.assertTrue(result['operationalSchemesMessage'])
        self.assertFalse(result['ready'])
        self.assertFalse(result['builtinResultAvailable'])
        self.assertIsNone(result['homeResultId'])

    def test_builtin_zarr_requires_bound_manifest_and_every_registered_file(self):
        workspace = (Path.cwd() / '__virtual_builtin_fixture__').resolve()
        folder = workspace / 'tasks' / 'JOB-example'
        zarr = folder / 'hourly.zarr'
        files = {'.zattrs': 'a' * 64, '.zgroup': 'b' * 64, 'time/.zarray': 'c' * 64,
                 'time/0': 'd' * 64, 'lakeLevel/.zarray': 'e' * 64, 'lakeLevel/0': 'f' * 64}
        manifest = {'format': FORMAT, 'complete': True, 'files': files, 'sha256': digest(files)}
        result = {'hourlyArtifact': {'format': FORMAT, 'complete': True, 'sha256': manifest['sha256']}}
        db = SimpleNamespace(workspace=workspace,
                             rows=lambda sql, args: [{'status': 'succeeded', 'trashed': 0,
                                                      'result': json.dumps(result)}])
        present = {folder / name for name in ('result.json', 'checkpoint.json', 'daily.csv')}
        present.update(zarr / name for name in files)

        def read_fixture(path, default=None):
            if path == zarr / '.zattrs':
                return {'format': FORMAT, 'complete': True}
            if path == folder / 'hourly-manifest.json':
                return manifest
            raise AssertionError(path)

        with patch('backend.published_results.catalog', return_value=[]), \
             patch('backend.business_context.read_json', side_effect=read_fixture), \
             patch.object(Path, 'is_dir', lambda path: path == zarr), \
             patch.object(Path, 'is_file', lambda path: path in present):
            self.assertTrue(builtin_result_available(db, 'JOB-example'))
            present.remove(zarr / 'lakeLevel/0')
            self.assertFalse(builtin_result_available(db, 'JOB-example'))
            present.add(zarr / 'lakeLevel/0')
            manifest['sha256'] = '0' * 64
            self.assertFalse(builtin_result_available(db, 'JOB-example'))
            manifest['sha256'] = digest(files)
            result['hourlyArtifact']['sha256'] = '1' * 64
            self.assertFalse(builtin_result_available(db, 'JOB-example'))
            files['../outside'] = '2' * 64
            manifest['sha256'] = result['hourlyArtifact']['sha256'] = digest(files)
            self.assertFalse(builtin_result_available(db, 'JOB-example'))
            files.clear()
            manifest['sha256'] = result['hourlyArtifact']['sha256'] = digest(files)
            self.assertFalse(builtin_result_available(db, 'JOB-example'))


if __name__ == '__main__':
    unittest.main()
