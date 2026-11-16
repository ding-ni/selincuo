from copy import deepcopy
import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from backend.database import Database
from backend.model_core import DEFAULT_PARAMETERS, initial_states
from backend.grid_simulation import PARTITIONS
from backend.forecasts import _hydrology, _reanchor, run, preflight
from backend.forecast_inputs import _rain, _request_json, create_manual, import_hourly, parse_ensemble, seasonal_evaporation, preview_evaporation, fetch_ensemble


def context_fixture():
    p = deepcopy(DEFAULT_PARAMETERS)
    units = [{'id': 'U-' + str(i), 'partition': name, 'basin': 'zajia', 'kind': kind,
              'areaKm2': 10., 'elevation': z, 'gridIndex': i} for i, (name, kind, z) in enumerate([
                  ('duja', 'land', 4500.), ('zajia_rest', 'glacier', 5500.)])]
    spatial = {'version': 'SPATIAL-test', 'units': units, 'grids': [
        {'id': 'g0', 'lon': 89., 'lat': 32., 'referenceElevation': 4500.},
        {'id': 'g1', 'lon': 89.1, 'lat': 32., 'referenceElevation': 5000.}],
        'lakeWeights': [.5, .5], 'lakeAreaKm2': 2400.}
    routing = {part: {'sources': {'interflow': 0., 'baseflow': 0.},
                           'channel': {'storage': 0., 'queue': [0.] * (int(p['lagHours']) + 2)}} for part in PARTITIONS}
    state = {'identity': {'routingMode': 'source-separated'}, 'validAt': '2026-10-21T00:00:00',
             'unitStates': initial_states(units, p).tolist(), 'unitIds': [u['id'] for u in units],
             'parameters': p, 'routing': routing, 'lake': {'level': 4543.}, 'inputId': 'INPUT-test'}
    return {'spatial': spatial, 'parameters': p, 'regions': {}, 'state': state, 'published': True}


class ForecastTests(unittest.TestCase):
    def test_rain_window_conserves_total_in_fractional_hours(self):
        for shape in ('uniform', 'triangle'):
            rain = _rain(25., 3.5, 9.25, shape)
            self.assertAlmostEqual(rain.sum(), 25.)
            self.assertTrue(np.all(rain[:3] == 0))
            self.assertTrue(np.all(rain[10:] == 0))

    def test_hydrology_split_preserves_snow_soil_and_routing_state(self):
        context = context_fixture(); times = pd.date_range('2026-10-21T01:00', periods=72, freq='h')
        rain = np.full((72, 2), .3); temperature = np.full((72, 2), 5.); evap = np.full((72, 2), .03)
        full, end, diagnostic = _hydrology(context, context['state'], times, rain, temperature, evap)
        first, middle, _ = _hydrology(context, context['state'], times[:31], rain[:31], temperature[:31], evap[:31])
        second, split, _ = _hydrology(context, middle, times[31:], rain[31:], temperature[31:], evap[31:])
        np.testing.assert_allclose(np.r_[first['total'], second['total']], full['total'], atol=1e-12)
        np.testing.assert_allclose(split['unitStates'], end['unitStates'], atol=1e-12)
        self.assertEqual(split['routing'], end['routing'])
        self.assertLess(diagnostic['landBalanceMaximumMm'], 1e-6)

    def test_manual_temperature_is_corrected_once_and_model_forecast_uses_state(self):
        context = context_fixture(); db = Database(Path(tempfile.mkdtemp(prefix='selinco-forecast-test-')))
        times = pd.date_range('2026-10-21T01:00', periods=168, freq='h')
        seasonal = (np.full((168, 2), .03), np.full(168, .04), {'method': 'test-original-era'})
        payload = {'sourceResultId': 'JOB-test', 'issueTime': '2026-10-21T00:00:00', 'days': 7,
                   'daily': [{'date': str(d.date()), 'precipitationMm': 12., 'temperatureC': 5.} for d in pd.date_range('2026-10-21', periods=7)]}
        with patch('backend.forecast_inputs._context', return_value=context), patch('backend.forecast_inputs.seasonal_evaporation', return_value=seasonal):
            weather = create_manual(db, payload)
        with np.load(db.workspace / weather['dataPath']) as data:
            np.testing.assert_allclose(data['temperature'][0, 0], [8.25, 1.75])
            np.testing.assert_allclose(data['precipitation'][0].sum(axis=0), [84., 84.])
        original = deepcopy(context['state'])
        with patch('backend.forecasts.source_context', return_value=context), patch('backend.forecasts._history', return_value=None):
            result = run(db, {'sourceResultId': 'JOB-test', 'inputId': weather['id']})
        self.assertEqual(context['state'], original)
        self.assertEqual(result['memberCount'], 1)
        self.assertEqual(len(result['times']), 168)
        self.assertTrue(np.isfinite(result['control']['lakeLevel']).all())
        self.assertGreater(max(result['control']['inflow']), 0)
        self.assertEqual(result['control']['lakeLevel'], result['quantiles']['p50']['lakeLevel'])
        checkpoint = Path(db.workspace / result['memberCheckpointIndex'][0]['path'])
        self.assertTrue(checkpoint.exists())

    def test_daily_evaporation_region_and_lake_override_seasonal_reference(self):
        context = context_fixture(); db = Database(Path(tempfile.mkdtemp(prefix='selinco-evaporation-input-')))
        seasonal = (np.full((168, 2), .03), np.full(168, .04), {'method': 'original-era'})
        row = {'precipitationMm': 0., 'temperatureC': 5., 'evaporationMm': 2.4,
               'overrides': {'duja': {'evaporationMm': 4.8}}, 'lake': {'evaporationMm': 7.2}}
        payload = {'sourceResultId': 'JOB-test', 'issueTime': '2026-10-21T00:00:00', 'days': 7, 'daily': [deepcopy(row) for _ in range(7)]}
        with patch('backend.forecast_inputs._context', return_value=context), patch('backend.forecast_inputs.seasonal_evaporation', return_value=seasonal):
            weather = create_manual(db, payload)
        with np.load(db.workspace / weather['dataPath']) as data:
            np.testing.assert_allclose(data['evaporation'][0, 0], [.2, .1])
            np.testing.assert_allclose(data['lakeEvaporation'], .3)

    def test_hourly_excel_import_and_missing_hour_are_not_filled_with_zero(self):
        from openpyxl import Workbook
        context = context_fixture(); db = Database(Path(tempfile.mkdtemp(prefix='selinco-hourly-weather-')))
        path = db.workspace / '未来逐时气象.xlsx'
        book = Workbook(); sheet = book.active
        sheet.append(['时间', '区域', '降水(mm)', '气温(℃)', '蒸发(mm)'])
        times = pd.date_range('2026-10-21T01:00', periods=168, freq='h')
        for time in times: sheet.append([time.to_pydatetime(), 'uniform', .4, 5., .02])
        book.save(path); book.close()
        seasonal = (np.full((168, 2), .03), np.full(168, .04), {'method': 'original-era'})
        payload = {'sourceResultId': 'JOB-test', 'issueTime': '2026-10-21T00:00:00', 'days': 7, 'path': path.name}
        with patch('backend.forecast_inputs._context', return_value=context), patch('backend.forecast_inputs.seasonal_evaporation', return_value=seasonal):
            weather = import_hourly(db, payload)
            with np.load(db.workspace / weather['dataPath']) as data:
                np.testing.assert_allclose(data['evaporation'], .02)
                np.testing.assert_allclose(data['lakeEvaporation'], .02)
                np.testing.assert_allclose(data['precipitation'], .4)
            rows = [{'time': time.isoformat(), 'region': 'uniform', 'precipitationMm': .4, 'temperatureC': 5.} for time in times if time != times[24]]
            with self.assertRaisesRegex(ValueError, '未完整覆盖'):
                import_hourly(db, {**payload, 'rows': rows})

    def test_preflight_reports_real_state_gap(self):
        context = context_fixture(); weather = {'issueTime': '2026-11-15T00:00:00', 'sourceResultId': 'JOB-test',
           'spatialVersion': context['spatial']['version'], 'unitIds': context['state']['unitIds']}
        with patch('backend.forecasts.source_context', return_value=context), patch('backend.forecasts._input', return_value=weather), \
             patch('backend.forecasts._history', side_effect=ValueError('实际气象未覆盖状态推进')):
            result = preflight(None, {'sourceResultId': 'JOB-test', 'inputId': 'FWEATHER-test'})
        self.assertFalse(result['ready']); self.assertIn('状态推进', result['deficiencies'][0])

    def test_real_frozen_warm_rain_state_is_read_without_changing_continuation_contract(self):
        from backend.grid_simulation import model_identity
        from backend.forecasts import source_context, FROZEN_WARM_RAIN_HANDOFF
        from backend.published_results import ROOT, result_file
        from backend.common import read_json
        db = SimpleNamespace(workspace=Path(tempfile.mkdtemp(prefix='selinco-frozen-state-')))
        result = read_json(result_file('JOB-05c3f62282ce4239aed1', 'result.json'))
        result = result.get('result') or result
        context = source_context(db, 'JOB-05c3f62282ce4239aed1')
        self.assertEqual(context['state']['validAt'], '2026-10-21T00:00:00')
        self.assertEqual(context['stateHandoff']['method'], 'verified-frozen-warm-rain-state')
        self.assertEqual((result['identity']['modelImplementation'], model_identity()), FROZEN_WARM_RAIN_HANDOFF)
        self.assertEqual(len(context['state']['unitIds']), 31)

    def test_recent_main_level_is_advanced_from_actual_observation_time(self):
        from backend.lake_curves import validate
        curve = validate({'name': '定面积表', 'type': 'quadratic', 'minLevel': 4537., 'maxLevel': 4545., 'offsetM': 0.,
                          'coefficients': {'a': 0., 'b': 2., 'c': 20., 'referenceLevel': 4540.}})
        advancement = {'recentForcing': [
            {'time': '2026-10-21T11:00:00', 'inflow': 100., 'rainMm': 0., 'evaporationMm': 0.},
            {'time': '2026-10-21T12:00:00', 'inflow': 100., 'rainMm': 0., 'evaporationMm': 0.}]}
        level = _reanchor(None, context_fixture(), {'time': '2026-10-21T10:30:00', 'level': 4543.},
                          pd.Timestamp('2026-10-21T12:00'), curve, advancement)
        self.assertAlmostEqual(level, 4543. + 100 * 5400 / 2e9, places=9)

    def test_ensemble_parser_uses_all_members_and_rejects_missing_tail(self):
        times = pd.date_range('2026-11-15', periods=24, freq='h').strftime('%Y-%m-%dT%H:%M').tolist()
        response = {'hourly': {'time': times}, 'hourly_units': {}}
        for i in range(51):
            suffix = '' if i == 0 else '_member' + str(i).zfill(2)
            for name, unit, value in [('precipitation', 'mm', 1.), ('temperature_2m', '°C', 5.)]:
                key = name + suffix; response['hourly'][key] = [value] * 24; response['hourly_units'][key] = unit
        actual, members, values = parse_ensemble(response)
        self.assertEqual(actual[0], pd.Timestamp('2026-11-15T08:00'))
        self.assertEqual(len(members), 51); self.assertEqual(values['precipitation'].shape, (51, 24))
        response['hourly']['precipitation_member50'][-1] = None
        with self.assertRaisesRegex(ValueError, '缺测'): parse_ensemble(response)
        short_times, _, short_values = parse_ensemble(response, actual[:-1])
        self.assertEqual(len(short_times), 23)
        self.assertEqual(short_values['precipitation'].shape, (51, 23))

    def test_multi_coordinate_ensemble_uses_one_elevation_placeholder_per_point(self):
        from urllib.parse import parse_qs, urlparse
        context = context_fixture()
        context['spatial']['grids'][1]['lon'] = 89.4
        context['sourceResultId'] = 'JOB-test'
        db = Database(Path(tempfile.mkdtemp(prefix='selinco-ensemble-contract-')))
        utc_times = pd.date_range('2026-10-20T16:00', periods=169, freq='h')
        responses = []
        for point_index, lon in enumerate((89., 89.5)):
            response = {'latitude': 32., 'longitude': lon,
                        'hourly': {'time': utc_times.strftime('%Y-%m-%dT%H:%M').tolist()}, 'hourly_units': {}}
            for m in range(51):
                suffix = '' if m == 0 else '_member' + str(m).zfill(2)
                for name, unit, value in [('precipitation', 'mm', m / 100.), ('temperature_2m', '°C', 5. + 2. * point_index)]:
                    key = name + suffix; response['hourly'][key] = [value] * len(utc_times); response['hourly_units'][key] = unit
            responses.append(response)
        seasonal = (np.full((168, 2), .03), np.full(168, .04), {'method': 'original-era'})
        with patch('backend.forecast_inputs._context', return_value=context), patch('backend.forecast_inputs.seasonal_evaporation', return_value=seasonal), \
             patch('backend.forecast_inputs._request_json', side_effect=[responses]) as fetch:
            weather = fetch_ensemble(db, {'sourceResultId': 'JOB-test', 'issueTime': '2026-10-21T00:00:00', 'days': 7})
        self.assertEqual(fetch.call_count, 1)
        query = parse_qs(urlparse(fetch.call_args.args[0]).query)
        self.assertEqual(query['latitude'], ['32.0,32.0'])
        self.assertEqual(query['longitude'], ['89.0,89.5'])
        self.assertEqual(query['elevation'], ['nan,nan'])
        self.assertEqual(query['cell_selection'], ['nearest'])
        self.assertEqual(query['models'], ['ecmwf_ifs025_ensemble'])
        self.assertEqual(weather['metadata']['gridToDownloadedCell'], [0, 1])
        self.assertEqual(len(weather['metadata']['downloadedCells']), 2)
        self.assertEqual([x['returnedLongitude'] for x in weather['metadata']['downloadedCells']], [89., 89.5])
        with np.load(db.workspace / weather['dataPath']) as values:
            self.assertEqual(values['precipitation'].shape, (51, 168, 2))
            np.testing.assert_allclose(values['precipitation'][50], .5)
            np.testing.assert_allclose(values['temperature'][0, 0], [5., 3.75])
        with patch('backend.forecasts.source_context', return_value=context), patch('backend.forecasts._history', return_value=None):
            result = run(db, {'sourceResultId': 'JOB-test', 'inputId': weather['id'], 'thresholds': [{'id': 'T', 'level': 4543.}]})
        self.assertEqual(result['memberCount'], 51)
        self.assertEqual(result['memberArtifact']['shape'], [51, 168])
        with np.load(db.workspace / result['memberArtifact']['path']) as values:
            self.assertEqual(values['flow'].shape, (51, 168, 7))
            np.testing.assert_allclose(result['quantiles']['p90']['lakeLevel'], np.percentile(values['lakeLevel'], 90, axis=0))
            np.testing.assert_allclose(result['exceedance'][0]['hourlyFraction'], np.mean(values['lakeLevel'] >= 4543., axis=0))
        self.assertEqual(len(result['memberCheckpointIndex']), 51)

    def test_fifteen_day_ensemble_keeps_common_finite_prefix_without_zero_tail(self):
        wanted = pd.date_range('2026-11-15T01:00', periods=360, freq='h')
        utc = wanted.tz_localize('Asia/Shanghai').tz_convert('UTC').tz_localize(None)
        response = {'hourly': {'time': utc.strftime('%Y-%m-%dT%H:%M').tolist()}, 'hourly_units': {}}
        for m in range(51):
            suffix = '' if m == 0 else '_member' + str(m).zfill(2)
            for name, unit in [('precipitation', 'mm'), ('temperature_2m', '°C')]:
                key = name + suffix
                count = 330 if m == 50 and name == 'precipitation' else 336
                response['hourly'][key] = [1.] * count + [None] * (360 - count)
                response['hourly_units'][key] = unit
        times, members, values = parse_ensemble(response, wanted, allow_tail=True)
        self.assertEqual(len(times), 330); self.assertEqual(values['precipitation'].shape, (51, 330))
        self.assertEqual(times[-1], wanted[329]); self.assertTrue(np.all(values['precipitation'] == 1.))
        response['hourly']['precipitation'][24] = -.1
        _, _, bounded = parse_ensemble(response, wanted, allow_tail=True)
        self.assertEqual(bounded['precipitation'][0, 24], 0.)
        response['hourly']['temperature_2m_member01'][72] = None
        with self.assertRaisesRegex(ValueError, '中间缺测'): parse_ensemble(response, wanted, allow_tail=True)

    def test_open_meteo_response_uses_gzip_transport_and_preserves_json(self):
        import gzip
        from http.client import IncompleteRead
        from io import BytesIO
        body = b'{"hourly":{"time":["2026-11-15T01:00"]}}'
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = gzip.compress(body)
        response.headers = {'Content-Encoding': 'gzip'}
        with patch('backend.forecast_inputs.urlopen', return_value=response) as open_url:
            value = _request_json('https://example.invalid/data')
        self.assertEqual(value['hourly']['time'], ['2026-11-15T01:00'])
        self.assertEqual(open_url.call_args.args[0].get_header('Accept-encoding'), 'gzip')

    def test_open_meteo_retries_a_truncated_response_and_stops_after_three_attempts(self):
        from http.client import IncompleteRead
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = b'{"complete":true}'
        response.headers = {}
        with patch('backend.forecast_inputs.urlopen', side_effect=[IncompleteRead(b'{"complete"'), response]) as open_url, \
             patch('backend.forecast_inputs.time.sleep') as pause:
            self.assertEqual(_request_json('https://example.invalid/data'), {'complete': True})
        self.assertEqual(open_url.call_count, 2)
        pause.assert_called_once_with(.25)
        with patch('backend.forecast_inputs.urlopen', side_effect=IncompleteRead(b'partial')) as open_url, \
             patch('backend.forecast_inputs.time.sleep') as pause:
            with self.assertRaisesRegex(ValueError, '截断或不可解析，已重试3次'):
                _request_json('https://example.invalid/data')
        self.assertEqual(open_url.call_count, 3)
        self.assertEqual(pause.call_count, 2)

    def test_multi_point_ensemble_requests_are_bounded_and_have_matching_elevation_values(self):
        from urllib.parse import parse_qs, urlparse
        context = context_fixture()
        context['spatial']['grids'].extend([
            {'id': 'g2', 'lon': 89.5, 'lat': 32., 'referenceElevation': 5000.},
            {'id': 'g3', 'lon': 89.75, 'lat': 32., 'referenceElevation': 5000.},
            {'id': 'g4', 'lon': 90., 'lat': 32., 'referenceElevation': 5000.},
            {'id': 'g5', 'lon': 90.25, 'lat': 32., 'referenceElevation': 5000.},
            {'id': 'g6', 'lon': 90.5, 'lat': 32., 'referenceElevation': 5000.},
            {'id': 'g7', 'lon': 90.75, 'lat': 32., 'referenceElevation': 5000.},
            {'id': 'g8', 'lon': 91., 'lat': 32., 'referenceElevation': 5000.},
            {'id': 'g9', 'lon': 91.25, 'lat': 32., 'referenceElevation': 5000.}])
        context['spatial']['lakeWeights'] = [.1] * 10
        db = Database(Path(tempfile.mkdtemp(prefix='selinco-ensemble-batches-')))
        utc_times = pd.date_range('2026-10-20T16:00', periods=169, freq='h')
        def response(lon):
            row = {'latitude': 32., 'longitude': lon,
                   'hourly': {'time': utc_times.strftime('%Y-%m-%dT%H:%M').tolist()}, 'hourly_units': {}}
            for member in range(51):
                suffix = '' if member == 0 else '_member' + str(member).zfill(2)
                for name, unit, value in [('precipitation', 'mm', 1.), ('temperature_2m', '°C', 5.)]:
                    key = name + suffix
                    row['hourly'][key] = [value] * len(utc_times)
                    row['hourly_units'][key] = unit
            return row
        seasonal = (np.full((168, 2), .03), np.full(168, .04), {'method': 'original-era'})
        with patch('backend.forecast_inputs._context', return_value=context), \
             patch('backend.forecast_inputs.seasonal_evaporation', return_value=seasonal), \
             patch('backend.forecast_inputs._request_json', side_effect=lambda url: [response(float(lon)) for lon in parse_qs(urlparse(url).query)['longitude'][0].split(',')]) as fetch:
            weather = fetch_ensemble(db, {'sourceResultId': 'JOB-test', 'issueTime': '2026-10-21T00:00:00', 'days': 7})
        self.assertEqual(fetch.call_count, 3)
        requests = [parse_qs(urlparse(call.args[0]).query) for call in fetch.call_args_list]
        self.assertEqual([len(value['longitude'][0].split(',')) for value in requests], [4, 4, 1])
        self.assertEqual([len(value['elevation'][0].split(',')) for value in requests], [4, 4, 1])
        self.assertTrue(all(set(value['elevation'][0].split(',')) == {'nan'} for value in requests))
        self.assertEqual(weather['metadata']['gridToDownloadedCell'], [0, 0, 1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(weather['metadata']['locationsPerRequest'], 4)
        response_folder = db.workspace / weather['metadata']['sourceFolder']
        self.assertEqual(sorted(path.name for path in response_folder.glob('集合气象原响应-*.json')),
                         ['集合气象原响应-001.json', '集合气象原响应-002.json', '集合气象原响应-003.json'])
        with np.load(db.workspace / weather['dataPath']) as values:
            self.assertEqual(values['precipitation'].shape, (51, 168, 2))

    def test_seasonal_evaporation_uses_actual_month_hour_before_issue(self):
        context = context_fixture(); times = pd.date_range('2026-11-15T01:00', periods=24, freq='h')
        prior = pd.date_range('2025-11-14T01:00', periods=24, freq='h')
        future = pd.date_range('2027-11-14T01:00', periods=24, freq='h')
        source = {'times': prior.append(future), 'arrays': {'evaporation': np.r_[np.full((24, 2), .2), np.full((24, 2), 99.)]}}
        with patch('backend.grid_simulation.prepare', return_value=source):
            unit, lake, metadata = seasonal_evaporation(None, context, times)
        np.testing.assert_allclose(unit, .2); np.testing.assert_allclose(lake, .2)
        self.assertEqual(metadata['referenceEnd'], prior[-1].isoformat())

    def test_evaporation_preview_uses_area_weighted_daily_reference(self):
        context = context_fixture(); context['spatial']['units'][0]['areaKm2'] = 10.; context['spatial']['units'][1]['areaKm2'] = 30.
        db = Database(Path(tempfile.mkdtemp(prefix='selinco-evaporation-preview-')))
        expected_times = pd.date_range('2026-10-22T01:00', periods=168, freq='h')
        unit = np.tile([.1, .3], (168, 1)); lake = np.full(168, .4)
        with patch('backend.forecast_inputs._context', return_value=context), \
             patch('backend.forecast_inputs.seasonal_evaporation', return_value=(unit, lake, {'method': 'seasonal-hourly-original-era'})) as seasonal:
            result = preview_evaporation(db, {'sourceResultId': 'JOB-test', 'issueTime': '2026-10-22T00:00:00', 'days': 7})
        args = seasonal.call_args.args
        np.testing.assert_array_equal(args[2].asi8, expected_times.asi8)
        self.assertEqual(result['sourceResultId'], 'JOB-test')
        self.assertEqual(result['daily'][0]['date'], '2026-10-22')
        self.assertAlmostEqual(result['daily'][0]['evaporationMm'], 6.)
        self.assertAlmostEqual(result['daily'][0]['regions']['duja'], 2.4)
        self.assertAlmostEqual(result['daily'][0]['lakeEvaporationMm'], 9.6)
        with self.assertRaisesRegex(ValueError, '00时'):
            preview_evaporation(db, {'sourceResultId': 'JOB-test', 'issueTime': '2026-10-22T01:00:00', 'days': 7})


if __name__ == '__main__': unittest.main()
