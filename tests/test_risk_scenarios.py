"""Spill-volume conservation, connected wetting and retained scenario results."""
from pathlib import Path
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import unittest
import uuid
import zipfile
from unittest.mock import patch

import numpy as np

from backend.database import Database
from backend.common import write_json
from backend.lake_curves import default_curve, storage
from backend import operational_store as store
from backend import risk_scenarios as risk


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]/'build'/('漫溢定向检查-'+uuid.uuid4().hex[:10])
        self.db = Database(self.root)

    def test_default_and_catalog_register_each_bundled_dataset_once(self):
        bundle = self.root / '随包承灾体'
        adopted = [{'file': f'{kind}.geojson', 'kind': kind, 'name': name, 'downloadedAtUTC': '20261117T1229Z'}
                   for kind, name in [('road', 'OSM道路'), ('settlement', 'OSM居民点')]]
        write_json(bundle / '承灾体资料.json', {'source': 'OpenStreetMap contributors', 'license': 'ODbL 1.0', 'adopted': adopted})
        for item, geometry in zip(adopted, [{'type': 'LineString', 'coordinates': [[89, 31], [89.01, 31]]},
                                            {'type': 'Point', 'coordinates': [89, 31]}]):
            write_json(bundle / item['file'], {'type': 'FeatureCollection', 'features': [
                {'type': 'Feature', 'geometry': geometry, 'properties': {}}]})
        with patch.object(risk, 'EXPOSURE_BUNDLE', bundle), ThreadPoolExecutor(max_workers=2) as pool:
            default = pool.submit(risk.default_scenario, self.db)
            catalog = pool.submit(risk.catalog, self.db)
            choices = catalog.result()['exposures']
            self.assertEqual({row['id'] for row in choices}, set(default.result()['exposureIds']))
            self.assertEqual(len(choices), 2)
            self.assertEqual(len(store.list_records(self.db, 'exposures')), 2)

    def test_existing_bundle_alias_is_hidden_but_historical_result_still_resolves(self):
        data = {'type': 'FeatureCollection', 'features': [{'type': 'Feature',
                'geometry': {'type': 'Point', 'coordinates': [89, 31]}, 'properties': {}}]}
        payload = {'name': 'OSM居民点', 'kind': 'settlement', 'geojson': data}
        duplicates = [risk.import_exposure(self.db, payload, bundle_file='settlements.geojson') for _ in range(2)]
        custom = risk.import_exposure(self.db, {**payload, 'geojson': {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [89.5, 31]}, 'properties': {}}]}})
        with patch.object(risk, 'EXPOSURE_BUNDLE', self.root / '无随包资料'):
            choices = {row['id'] for row in risk.catalog(self.db)['exposures']}
            self.assertIn(custom['id'], choices)
            shown, = choices - {custom['id']}
            hidden, = {row['id'] for row in duplicates} - {shown}
            outlet = {'lon': 89, 'lat': 31, 'crestM': 4544, 'widthM': 60}
            scenario = risk.save_scenario(self.db, {'outlet': outlet, 'exposureIds': [hidden, shown, custom['id']]})
            self.assertEqual(scenario['exposureIds'], [shown, custom['id']])
            existing = store.save(self.db, risk.RESULTS, {'scenario': {'exposureIds': [hidden]}})
            self.assertEqual(risk.result_exposures(self.db, existing['id'])['items'][0]['id'], hidden)
            self.assertEqual(len(store.list_records(self.db, 'exposures')), 3)

    def test_osm_reread_reuses_same_data_but_preserves_a_changed_snapshot(self):
        request = {'bbox': [88, 30, 90, 32], 'features': ['settlements']}
        with patch('requests.post') as post:
            def mock_osm(lon):
                post.return_value.json.return_value = {'elements': [
                    {'id': 1, 'lon': lon, 'lat': 31, 'tags': {'place': 'village'}}]}

            mock_osm(89)
            first, = risk.fetch_osm(self.db, request)['exposures']
            again, = risk.fetch_osm(self.db, request)['exposures']
            mock_osm(89.1)
            changed, = risk.fetch_osm(self.db, request)['exposures']
        self.assertEqual(first['id'], again['id'])
        self.assertNotEqual(first['id'], changed['id'])
        self.assertEqual(len(store.list_records(self.db, 'exposures')), 2)

    def test_weir_is_dry_below_crest_and_increases_with_head(self):
        outlet = {'crestM': 4543.6, 'widthM': 30., 'coefficient': .6}
        self.assertEqual(risk.weir_discharge(4543.59, outlet), 0)
        self.assertGreater(risk.weir_discharge(4543.8, outlet), risk.weir_discharge(4543.7, outlet))
        self.assertAlmostEqual(risk.weir_discharge(4543.8, outlet)/risk.weir_discharge(4543.7, outlet), 2**1.5, places=7)

    def test_spill_cannot_remove_storage_below_crest(self):
        curve = default_curve()
        initial = 4543.66
        outlet = {'crestM': 4543.65, 'widthM': 1e10, 'coefficient': .6}
        rows = risk.simulate_spill(['2026-10-22T01:00:00'], [0], [0], [0], initial, curve, outlet)
        self.assertAlmostEqual(rows[0]['level'], outlet['crestM'], places=9)
        self.assertAlmostEqual(rows[0]['spillM3'], storage(initial, curve)-storage(outlet['crestM'], curve), places=4)
        self.assertLess(abs(rows[0]['balanceErrorM3']), 1e-4)

    def test_crest_above_curve_range_has_no_spill_or_release(self):
        for offset in (0., 2.5):
            with self.subTest(offset=offset):
                curve = default_curve(); curve['offsetM'] = offset
                initial = 4543.66-offset
                outlet = {'crestM': curve['maxLevel']-offset+1., 'widthM': 30.,
                          'coefficient': .6, 'releaseCapacityM3s': 1e7}
                row = risk.simulate_spill(['2026-10-22T01:00:00'], [10.], [0.], [0.],
                                          initial, curve, outlet)[0]
                self.assertEqual(row['spillM3'], 0.)
                self.assertEqual(row['releasedM3'], 0.)
                self.assertAlmostEqual(row['storageChangeM3'], 36000., places=4)
                self.assertGreater(row['level'], initial)
                self.assertLess(abs(row['balanceErrorM3']), 1e-4)

    def test_crest_below_curve_range_stops_release_at_curve_minimum(self):
        for offset in (0., 2.5):
            with self.subTest(offset=offset):
                curve = default_curve(); curve['offsetM'] = offset
                minimum = curve['minLevel']-offset
                initial = minimum+.01
                outlet = {'crestM': minimum-5., 'widthM': .01,
                          'coefficient': .6, 'releaseCapacityM3s': 1e7}
                rows = risk.simulate_spill(['2026-10-22T01:00:00','2026-10-22T02:00:00'],
                    [0.,0.], [0.,0.], [0.,0.], initial, curve, outlet)
                self.assertGreater(rows[0]['spillM3'], 0.)
                self.assertGreater(rows[0]['releasedM3'], 0.)
                self.assertAlmostEqual(rows[0]['totalOutflowM3'],
                    storage(initial, curve)-storage(minimum, curve), places=4)
                self.assertAlmostEqual(rows[0]['level'], minimum, places=9)
                self.assertEqual(rows[1]['totalOutflowM3'], 0.)
                self.assertTrue(all(abs(row['balanceErrorM3']) < 1e-4 for row in rows))

    def test_outside_crest_does_not_relax_lake_curve_range(self):
        curve = default_curve()
        outlet = {'crestM': curve['maxLevel']+1., 'widthM': 30., 'coefficient': .6}
        with self.assertRaisesRegex(ValueError, '湖位超出库容曲线适用范围'):
            risk.simulate_spill(['2026-10-22T01:00:00'], [0.], [0.], [0.],
                               curve['maxLevel']+.1, curve, outlet)
        with self.assertRaisesRegex(ValueError, '蓄水量超出库容曲线适用范围'):
            risk.simulate_spill(['2026-10-22T01:00:00'], [1e7], [0.], [0.],
                               curve['maxLevel'], curve, outlet)
        outlet['crestM'] = curve['minLevel']-1.
        with self.assertRaisesRegex(ValueError, '蓄水量超出库容曲线适用范围'):
            risk.simulate_spill(['2026-10-22T01:00:00'], [0.], [0.], [1.],
                               curve['minLevel'], curve, outlet)

    def test_lake_ledger_includes_precipitation_evaporation_and_spill(self):
        curve = default_curve()
        rows = risk.simulate_spill(['2026-10-22T01:00:00','2026-10-22T02:00:00'],
            [100, 200], [1, 0], [.2, .1], 4543.66, curve,
            {'crestM': 4543.65, 'widthM': 30., 'coefficient': .6})
        self.assertGreater(rows[0]['precipitationM3'], rows[0]['evaporationM3'])
        self.assertTrue(all(r['spillM3'] > 0 for r in rows))
        self.assertLess(max(abs(r['balanceErrorM3']) for r in rows), 1e-4)

    def test_manual_release_shares_above_crest_storage_limit_and_closes_ledger(self):
        from backend.lake_curves import area
        curve = default_curve()
        initial, crest = 4543.66, 4543.65
        outlet = {'crestM': crest, 'widthM': 30., 'coefficient': .6,
                  'releaseCapacityM3s': 1e7}
        rows = risk.simulate_spill(['2026-10-22T01:00:00'], [10.], [1.], [.2],
                                   initial, curve, outlet)
        row = rows[0]
        available = (storage(initial, curve)-storage(crest, curve)+10.*3600.
                     +(1.-.2)/1000.*area(initial, curve))
        self.assertGreater(row['spillM3'], 0.)
        self.assertGreater(row['releasedM3'], 0.)
        self.assertAlmostEqual(row['totalOutflowM3'], row['spillM3']+row['releasedM3'], places=7)
        self.assertLessEqual(row['totalOutflowM3'], available+1e-5)
        self.assertAlmostEqual(row['totalOutflowM3'], available, places=4)
        self.assertLessEqual(row['releasedM3s'], outlet['releaseCapacityM3s'])
        self.assertLess(abs(row['balanceErrorM3']), 1e-4)

    def test_disconnected_depression_and_existing_lake_have_no_false_depth(self):
        z = np.full((7, 7), 10.)
        z[3, 1:4] = [6., 4., 3.]
        z[1, 5] = 0.
        lake = np.zeros_like(z, dtype=bool);lake[3, 1] = True
        depth, wet = risk.connected_depth(z, np.ones_like(lake), lake, 7.)
        self.assertTrue(wet[3, 3]);self.assertEqual(depth[3, 3], 4.)
        self.assertFalse(wet[1, 5]);self.assertEqual(depth[1, 5], 0.)
        self.assertEqual(depth[3, 1], 0.)

    def test_saved_scenario_requires_outlet_and_preserves_manual_datum(self):
        with self.assertRaisesRegex(ValueError, '溢口经度'):
            risk.save_scenario(self.db, {'name': '缺失位置'})
        scenario = risk.save_scenario(self.db, {'name': '独立测试情景', 'demOffsetM': 1.2,
            'outlet': {'lon': 89.1, 'lat': 31.8, 'crestM': 4543.8, 'widthM': 30,
                       'releaseCapacityM3s': 2.5}})
        loaded = risk.detail(self.db, scenario['id'])
        self.assertEqual(loaded['mode'], 'scenario');self.assertEqual(loaded['demOffsetM'], 1.2)
        self.assertEqual(loaded['outlet']['releaseCapacityM3s'], 2.5)

    def test_geojson_exposure_is_retained_with_source(self):
        record = risk.import_exposure(self.db, {'name': '隔离点', 'kind': 'settlement', 'source': '检查资料',
            'geojson': {'type': 'FeatureCollection', 'features': [{'type': 'Feature',
                'properties': {'name': '点一'}, 'geometry': {'type': 'Point', 'coordinates': [89.1,31.8]}}]}})
        self.assertEqual(record['featureCount'], 1)
        self.assertTrue((self.db.workspace/record['path']).is_file())
        self.assertEqual(store.get(self.db, 'exposures', record['id'])['source'], '检查资料')

    def test_risk_task_keeps_daily_peak_frames_and_comparison(self):
        from rasterio.transform import from_origin
        from types import SimpleNamespace
        from backend.operations_api import get
        selected = risk.import_exposure(self.db, {'name': '现场道路', 'kind': 'road', 'source': '现场测绘',
            'geojson': {'type': 'FeatureCollection', 'features': [{'type': 'Feature',
                'properties': {'name': '道路甲'}, 'geometry': {'type': 'MultiLineString',
                    'coordinates': [[[89.0001,31.9995],[89.0019,31.9995]]]}}]}})
        scenario = risk.save_scenario(self.db, {'name': '隔离演算', 'demOffsetM': 0,
            'outlet': {'lon':89.1,'lat':31.8,'crestM':4543.65,'widthM':30,
                       'releaseCapacityM3s': 2.},
            'alternatives':[{'name':'增宽方案','widthM':60}], 'exposureIds':[selected['id']]})
        forecast = {'id':'F-CHECK','name':'隔离预报','times':['2026-11-15T01:00:00','2026-11-15T02:00:00'],
            'control':{'inflow':[100,200],'lakeRainMm':[0,0],'lakeEvaporationMm':[0,0]},
            'initialLakeLevel':4543.66,'lakeCurve':default_curve()}
        z = np.array([[4543.6,4543.65],[4544.,4543.64]])
        terrain = {'elevation':z,'valid':np.ones((2,2),dtype=bool),
            'lakeMask':np.array([[True,False],[False,False]]),'transform':from_origin(89.,32.,.001,.001),
            'bounds':[89.,31.998,89.002,32.], 'cellAreas':np.full((2,2),10000.),
            'source':'隔离地形','verticalReference':'隔离检查基准'}
        with patch('backend.forecasts.detail', return_value=forecast), patch.object(risk, 'load_terrain', return_value=terrain):
            result = risk.run(self.db, {'forecastId':'F-CHECK','scenarioId':scenario['id']}, self.root/'tasks'/'RISK-CHECK')
        self.assertEqual(len(result['comparisons']),2)
        self.assertGreater(result['comparisons'][1]['spillVolumeM3'],result['comparisons'][0]['spillVolumeM3'])
        self.assertGreater(result['comparisons'][0]['releaseVolumeM3'],0.)
        self.assertAlmostEqual(result['comparisons'][0]['totalOutflowM3'],
            result['comparisons'][0]['spillVolumeM3']+result['comparisons'][0]['releaseVolumeM3'],places=7)
        from backend.reports import _data
        report = _data(self.db, {'reportType':'comparison','riskResultId':result['id']})
        report_lines = ' '.join(report['sections'][0]['lines'])
        self.assertIn('人工泄流体积',report_lines)
        self.assertIn('总出流',report_lines)
        # Archived frames are daily plus the peak hour; flooding is counted against today's shoreline.
        self.assertEqual(result['frameRows'], [1])
        frame = risk.terrain_frame(self.db,result['id'],0)
        self.assertEqual((frame['frames'], frame['time']), (1, forecast['times'][1]))
        self.assertEqual(frame['terrain']['width'],2)
        self.assertEqual(frame['depths'][1][1],0.)
        self.assertEqual(result['comparisons'][0]['inundatedAreaKm2'],0.)
        # Editing the scenario used for a later run does not change this run's inputs.
        risk.save_scenario(self.db, {'exposureIds':[]}, scenario['id'])
        server = SimpleNamespace(db=self.db)
        path = 'risk-results/'+result['id']
        found, exposures = get(server, path+'/exposures', {})
        self.assertTrue(found)
        self.assertEqual([x['id'] for x in exposures['items']], [selected['id']])
        self.assertEqual(exposures['items'][0]['features'][0]['geometry']['type'], 'MultiLineString')
        _, compact = get(server, path+'/frame', {'index':'0', 'depthOnly':'1'})
        self.assertEqual(compact['time'], frame['time'])
        self.assertEqual(compact['depths'], frame['depths'])
        self.assertNotIn('elevations', compact['terrain'])
        self.assertIn('elevations', frame['terrain'])
        with self.assertRaises(ValueError):
            risk.terrain_frame(self.db,result['id'],1)

    def test_complete_shapefile_zip_import(self):
        import geopandas as gpd
        from shapely.geometry import Point
        shape_folder = self.root/'shape-input'
        shape_folder.mkdir(parents=True)
        gpd.GeoDataFrame({'name':['site']},geometry=[Point(89.1,31.8)],crs='EPSG:4326').to_file(shape_folder/'site.shp')
        archive = io.BytesIO()
        with zipfile.ZipFile(archive,'w') as output:
            for path in shape_folder.iterdir():
                output.write(path,path.name)
        record = risk.import_exposure(self.db, {'name':'ZIP站点','filename':'site.zip',
            'dataBase64':base64.b64encode(archive.getvalue()).decode('ascii')})
        self.assertEqual(record['featureCount'],1)

    def test_affected_road_length_and_point_depth_from_actual_geometry(self):
        from rasterio.transform import from_origin
        collection = {'type':'FeatureCollection','features':[
            {'type':'Feature','properties':{'name':'road'},'geometry':{'type':'LineString',
                'coordinates':[[89.0001,31.9995],[89.0029,31.9995]]}},
            {'type':'Feature','properties':{'name':'point'},'geometry':{'type':'Point','coordinates':[89.0015,31.9995]}}]}
        record = risk.import_exposure(self.db, {'name':'几何检查','geojson':collection,'kind':'road'})
        terrain = {'transform':from_origin(89.,32.,.001,.001)}
        affected = risk.assess_exposures(self.db,[record['id']],np.array([[0.,2.,0.]]),terrain)
        self.assertEqual(affected['count'],2)
        self.assertGreater(affected['roadLengthM'],50)
        self.assertLess(affected['roadLengthM'],120)
        self.assertEqual([r['maxDepthM'] for r in affected['objects']],[2.,2.])

    def test_road_judged_on_surface_not_ground(self):
        from rasterio.transform import from_origin
        road = {'type':'FeatureCollection','features':[{'type':'Feature','properties':{'name':'road'},
            'geometry':{'type':'LineString','coordinates':[[89.0001,31.9995],[89.0029,31.9995]]}}]}
        record = risk.import_exposure(self.db, {'name':'路面检查','geojson':road,'kind':'road'})
        terrain = {'transform':from_origin(89.,32.,.001,.001)}
        below = risk.assess_exposures(self.db,[record['id']],np.array([[0.,.8,0.]]),terrain)
        self.assertEqual(below['count'],0)           # water under the embankment: road dry
        over = risk.assess_exposures(self.db,[record['id']],np.array([[0.,1.2,0.]]),terrain)
        self.assertEqual(over['count'],1)
        self.assertEqual(over['roadLengthM'],0.)     # 0.2 m over surface: passable
        self.assertGreater(over['overtoppedRoadLengthM'],50)
        self.assertEqual(over['objects'][0]['roadState'],'overtopped')
        closed = risk.assess_exposures(self.db,[record['id']],np.array([[0.,1.5,0.]]),terrain)
        self.assertGreater(closed['roadLengthM'],50)
        self.assertEqual(closed['objects'][0]['roadState'],'closed')


if __name__ == '__main__':
    unittest.main()
