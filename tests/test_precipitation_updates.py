import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend.common import canonical, digest, now, sha_file, write_json
from backend.database import Database
from backend.precipitation_updates import estimate_profile, refresh, _station_daily
from backend.research_schemes import _bind_current_precipitation_input


class ForcingSampleDb:
    def __init__(self, workspace, manifest, locations, coverages, observations):
        self.workspace = workspace
        self.manifest = manifest
        self.locations = locations
        self.coverages = coverages
        self.records = observations

    def items(self, table):
        return [{'id': self.manifest['id'], 'manifest': self.manifest}]

    def rows(self, sql, args=()):
        if 'FROM stations' in sql:
            return self.locations
        if 'FROM coverage' in sql:
            return self.coverages
        return []

    def observations(self, station, variable, **kwargs):
        return [row for row in self.records if row['station_id'] == station and row['variable'] == variable]


def sample_input(root, confirmed=True):
    folder = root / 'inputs/products/INPUT-test'
    folder.mkdir(parents=True)
    times = pd.date_range('2023-12-01T01:00:00', '2025-01-01T00:00:00', freq='h')
    spatial = {'version': 'GRID-test', 'grids': [
        {'id': f'g{i}', 'lon': 89. + i * .1, 'lat': 32.} for i in range(4)]}
    locations = [{'id': sid, 'lon': spatial['grids'][i]['lon'], 'lat': 32.}
                 for i, sid in enumerate(('SLC', 'DUJA', 'BLQ', '115DB'))]
    rain = np.full((len(times), 4), 1. / 24.)
    temp = np.full((len(times), 4), 10.)
    data_path = folder / 'forcing.npz'
    np.savez_compressed(data_path, time=times.asi8, precipitation=rain,
                        temperature=temp, evaporation=np.zeros_like(rain))
    manifest = {'id': 'INPUT-test', 'projectId': 'selinco', 'data': 'forcing.npz',
                'dataSHA256': sha_file(data_path), 'spatialVersion': 'GRID-test',
                'start': times[0].isoformat(), 'end': times[-1].isoformat(), 'hours': len(times)}
    write_json(folder / 'manifest.json', manifest)
    coverages, observations = [], []
    for i, sid in enumerate(('SLC', 'DUJA', 'BLQ', '115DB')):
        meta = {'coverageConfirmed': confirmed, 'zeroForAbsent': True, 'stoppedIntervals': []}
        coverages.append({'source_id': f'src-{sid}', 'station_id': sid, 'step': 'day',
                          'start': '2024-01-01T00:00:00', 'end': '2024-12-31T00:00:00',
                          'metadata': canonical(meta)})
        for day in pd.date_range('2024-01-01', '2024-12-31', freq='D'):
            observations.append({'source_id': f'src-{sid}', 'station_id': sid,
                'variable': 'precipitation', 'step': 'day', 'time': day.isoformat(),
                'value': 1., 'quality': 'valid', 'unit': 'mm'})
    return ForcingSampleDb(root, manifest, locations, coverages, observations), spatial


class PrecipitationUpdateTests(unittest.TestCase):
    def test_event_midnight_is_current_day_and_stopped_days_are_missing(self):
        class EventDb:
            def observations(self, station, variable, **kwargs):
                return []
            def rows(self, sql, args=()):
                if 'ROW_NUMBER()' in sql:
                    return []
                return [{'time': '2025-06-02T00:00:00', 'value': 3.0,
                         'quality': 'valid', 'source_id': 'src'}]

        coverage = [{'step': 'irregular', 'source_id': 'src',
                     'start': '2025-06-01T00:00:00', 'end': '2025-06-05T00:00:00',
                     'metadata': {'stoppedIntervals': [['2025-06-03T12:00:00',
                                                        '2025-06-03T13:00:00']]}}]
        times = pd.date_range('2025-06-01T01:00:00', '2025-06-05T00:00:00', freq='h')
        days = pd.date_range('2025-06-01', '2025-06-04', freq='D')
        era = {day: {'warmHours': 24, 'warm': 1.0, 'total': 1.0,
                     'minimumTemperature': 10.0} for day in days}
        daily, _ = _station_daily(EventDb(), 'SLC', coverage, days[0], days[-1],
                                  era, np.full(len(times), 10.0), times)
        self.assertEqual(daily[days[0]]['observed'], 0.0)
        self.assertEqual(daily[days[1]]['observed'], 3.0)
        self.assertNotIn(days[2], daily)
        self.assertEqual(daily[days[3]]['observed'], 0.0)

    def test_only_confirmed_coverage_contributes_to_estimated_factors(self):
        with TemporaryDirectory(prefix='selinco-precip-estimate-') as temporary:
            db, spatial = sample_input(Path(temporary), confirmed=False)
            with patch('backend.grid_spatial.load', return_value=spatial):
                profile, diagnostics = estimate_profile(db, 'INPUT-test')
            self.assertEqual(diagnostics['usableStationSeasons'], 0)
            self.assertTrue(all(not value['seasonFactors'] for value in profile['stations'].values()))

    def test_confirmed_warm_samples_produce_factors_from_existing_rule(self):
        with TemporaryDirectory(prefix='selinco-precip-estimate-') as temporary:
            db, spatial = sample_input(Path(temporary), confirmed=True)
            with patch('backend.grid_spatial.load', return_value=spatial):
                profile, diagnostics = estimate_profile(db, 'INPUT-test')
            self.assertGreater(diagnostics['usableStationSeasons'], 0)
            for station in profile['stations'].values():
                self.assertAlmostEqual(station['seasonFactors']['MAM'], 1., places=6)
                self.assertAlmostEqual(station['seasonFactors']['JJA'], 1., places=6)
                self.assertAlmostEqual(station['seasonFactors']['SON'], 1., places=6)
            self.assertEqual(profile['temperatureThresholdC'], 5.)
            self.assertEqual(profile['radiusKm'], 100.)

    def test_insufficient_samples_keep_current_input_and_stage_new_forcing(self):
        with TemporaryDirectory(prefix='selinco-precip-refresh-') as temporary:
            db = Database(temporary)
            active = {'id': 'INPUT-old', 'manifest': {'id': 'INPUT-old', 'landPrecipitationCorrection': None}}
            with patch('backend.model_forcing.current_product', return_value=active), \
                 patch('backend.precipitation_updates.estimate_profile', return_value=(None, {'reason': 'no-confirmed'})):
                result = refresh(db, 'INPUT-raw')
            self.assertEqual(result['status'], 'insufficient')
            self.assertEqual(result['inputId'], 'INPUT-old')
            self.assertEqual(db.setting('pending-precipitation-input')['inputId'], 'INPUT-raw')
            self.assertIsNone(db.setting('current-model-forcing'))

    def test_success_activates_immutable_input_and_invalidates_configuration_cache(self):
        with TemporaryDirectory(prefix='selinco-precip-refresh-') as temporary:
            db = Database(temporary)
            active_profile = {'method': 'station-warm-rain-spatial-v1',
                              'stations': {'SLC': {'lon': 89., 'lat': 32., 'seasonFactors': {'DJF': .8}}}}
            active = {'id': 'INPUT-old', 'manifest': {'id': 'INPUT-old',
                      'landPrecipitationCorrection': active_profile}}
            profile = {'method': 'station-warm-rain-spatial-v1', 'stations': {
                'SLC': {'lon': 89., 'lat': 32., 'seasonFactors': {'JJA': .9}}}}
            diagnostics = {'usableStationSeasons': 1, 'stationSeasons': {'SLC': []},
                           'observationSnapshotSHA256': 'snapshot'}
            new_manifest = {'id': 'INPUT-new', 'name': 'new warm corrected input'}
            with db.connect() as connection:
                connection.execute('INSERT INTO settings VALUES(?,?)',
                                   ('current-model-forcing', canonical({'inputId': 'INPUT-old'})))
                connection.execute('INSERT INTO settings VALUES(?,?)',
                                   ('calibration-configuration:SCHEME-test', canonical('old-cache')))
            with patch('backend.model_forcing.current_product', return_value=active), \
                 patch('backend.precipitation_updates.estimate_profile', return_value=(profile, diagnostics)), \
                 patch('backend.land_precipitation.register_product', return_value=new_manifest), \
                 patch('backend.model_forcing._product', return_value={'id': 'INPUT-new', 'manifest': new_manifest}), \
                 patch('backend.model_forcing.verify_product'):
                result = refresh(db, 'INPUT-raw')
            self.assertEqual(result['status'], 'updated')
            self.assertEqual(db.setting('current-model-forcing')['inputId'], 'INPUT-new')
            self.assertIsNone(db.setting('calibration-configuration:SCHEME-test'))
            self.assertEqual(db.items('input_products'), [])

    def test_new_scheme_run_uses_current_input_without_changing_parameter_settings(self):
        scheme = {'inputBinding': 'frozen', 'inputId': 'INPUT-old',
                  'inputRequirement': {'transformationSHA256': 'old-transform'},
                  'parameters': {'WM': 120.}, 'search': {'keys': ['WM'], 'bounds': {'WM': [80., 200.]}}}
        current = {'id': 'INPUT-new', 'manifest': {'precipitationCorrectionUpdate': {
            'method': 'automatic-warm-rain-reestimate-v1'}}}
        with patch('backend.model_forcing.current_product', return_value=current):
            configured, refreshed = _bind_current_precipitation_input(None, scheme)
        self.assertEqual(configured, 'INPUT-old')
        self.assertTrue(refreshed)
        self.assertEqual(scheme['inputId'], 'INPUT-new')
        self.assertEqual(scheme['parameters'], {'WM': 120.})
        self.assertEqual(scheme['search'], {'keys': ['WM'], 'bounds': {'WM': [80., 200.]}})
        self.assertNotIn('transformationSHA256', scheme['inputRequirement'])


if __name__ == '__main__':
    unittest.main()
